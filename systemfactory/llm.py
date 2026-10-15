"""LLM provider layer: Ollama (local, default for real runs) and per-call evidence records.

Every inference attempt, successful or not, produces one `llm_calls` row with the exact model, its
digest, whether execution was real or simulated, timings and the token counts reported by the runtime.
Token counts are copied from the provider's response (Ollama: prompt_eval_count / eval_count); they are
never estimated.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field

import httpx

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"


class ProviderError(Exception):
    status = "error"


class ProviderUnavailable(ProviderError):
    status = "unavailable"


class ModelNotFound(ProviderError):
    status = "model_missing"


class InferenceTimeout(ProviderError):
    status = "timeout"


class InvalidModelOutput(ProviderError):
    status = "invalid_output"


# ------------------------------------------------------------------ sanitisation (shared with evidence export)
_SECRET_RES = [
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"), "<redacted:anthropic-key>"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "<redacted:api-key>"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "<redacted:github-token>"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "<redacted:aws-key>"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "<redacted:private-key>"),
    (re.compile(r"(?i)\b(authorization|x-api-key)\s*[:=]\s*\S+"), r"\1: <redacted>"),
]


def redact(text: str) -> str:
    for rx, rep in _SECRET_RES:
        text = rx.sub(rep, text)
    return text


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


# ------------------------------------------------------------------ Ollama
@dataclass
class ChatResult:
    content: str
    model: str
    input_tokens: int
    output_tokens: int
    total_seconds: float
    load_seconds: float
    eval_seconds: float
    raw_meta: dict = field(default_factory=dict)


class OllamaClient:
    """Minimal client for the Ollama HTTP API (https://github.com/ollama/ollama/blob/main/docs/api.md)."""

    provider = "ollama"
    execution = "real-local"

    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        self.base_url = (base_url or os.environ.get("SF_OLLAMA_URL") or DEFAULT_OLLAMA_URL).rstrip("/")
        self.timeout = float(timeout or os.environ.get("SF_OLLAMA_TIMEOUT", "300"))
        self._digests: dict[str, dict] = {}

    def _get(self, path: str, timeout: float = 5.0) -> dict:
        try:
            r = httpx.get(self.base_url + path, timeout=timeout)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise ProviderUnavailable(f"Ollama not reachable at {self.base_url}: {e}") from e
        except httpx.TimeoutException as e:
            raise InferenceTimeout(f"Ollama did not answer {path} within {timeout}s") from e
        r.raise_for_status()
        return r.json()

    def version(self) -> str:
        return self._get("/api/version").get("version", "unknown")

    def models(self) -> list[dict]:
        return self._get("/api/tags").get("models", [])

    def model_info(self, model: str) -> dict:
        """Digest and details of an installed model, from /api/tags (cached per client)."""
        if model not in self._digests:
            for m in self.models():
                self._digests[m["name"]] = {"digest": m.get("digest"), "size": m.get("size"),
                                            "details": m.get("details", {}), "modified_at": m.get("modified_at")}
        if model not in self._digests:
            raise ModelNotFound(f"model '{model}' is not installed in Ollama at {self.base_url} "
                                f"(run: ollama pull {model})")
        return self._digests[model]

    def status(self, models: list[str]) -> dict:
        """Availability report used by the dashboard, health endpoint and check script. Never raises."""
        out = {"provider": "ollama", "url": self.base_url, "reachable": False, "version": None, "models": {}}
        try:
            out["version"] = self.version()
            out["reachable"] = True
            installed = {m["name"]: m for m in self.models()}
            out["installed"] = sorted(installed)
            for name in models:
                m = installed.get(name)
                out["models"][name] = ({"installed": True, "digest": m.get("digest"),
                                        "parameter_size": m.get("details", {}).get("parameter_size"),
                                        "quantization": m.get("details", {}).get("quantization_level"),
                                        "family": m.get("details", {}).get("family")}
                                       if m else {"installed": False})
        except ProviderError as e:
            out["error"] = str(e)
        return out

    def chat(self, model: str, messages: list[dict], *, schema: dict | None = None, options: dict | None = None,
             timeout: float | None = None) -> ChatResult:
        body = {"model": model, "messages": messages, "stream": False,
                "options": {"temperature": 0, "seed": 7, "num_ctx": int(os.environ.get("SF_OLLAMA_NUM_CTX", "12288")),
                            **(options or {})},
                "keep_alive": os.environ.get("SF_OLLAMA_KEEP_ALIVE", "10m")}
        if schema is not None:
            body["format"] = schema  # Ollama structured outputs: constrain the reply to this JSON schema
        t = timeout or self.timeout
        try:
            r = httpx.post(self.base_url + "/api/chat", json=body, timeout=httpx.Timeout(t, connect=5.0))
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise ProviderUnavailable(f"Ollama not reachable at {self.base_url}: {e}") from e
        except httpx.TimeoutException as e:
            raise InferenceTimeout(f"model '{model}' did not finish within {t}s") from e
        if r.status_code == 404:
            raise ModelNotFound(f"model '{model}' not found in Ollama (run: ollama pull {model})")
        if r.status_code >= 400:
            raise ProviderError(f"Ollama returned HTTP {r.status_code}: {r.text[:300]}")
        d = r.json()
        ns = 1e9
        return ChatResult(content=d.get("message", {}).get("content", ""), model=d.get("model", model),
                          input_tokens=int(d.get("prompt_eval_count") or 0), output_tokens=int(d.get("eval_count") or 0),
                          total_seconds=(d.get("total_duration") or 0) / ns, load_seconds=(d.get("load_duration") or 0) / ns,
                          eval_seconds=(d.get("eval_duration") or 0) / ns,
                          raw_meta={k: d.get(k) for k in ("done_reason", "created_at")})


def parse_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    raise InvalidModelOutput(f"model reply is not valid JSON: {text[:200]!r}")


class Recorder:
    """Wraps inference calls so that every attempt is persisted as an `llm_calls` row."""

    def __init__(self, store, wf_id: int, iteration: int | None, step: str, role: str):
        self.store, self.wf_id, self.iteration, self.step, self.role = store, wf_id, iteration, step, role

    def chat(self, client: OllamaClient, model: str, messages: list[dict], *, schema: dict | None = None,
             purpose: str = "", timeout: float | None = None,
             options: dict | None = None) -> tuple[ChatResult, dict]:
        prompt_text = json.dumps(messages, ensure_ascii=False)
        rec = {"workflow_id": self.wf_id, "iteration": self.iteration, "step": self.step, "role": self.role,
               "provider": client.provider, "execution": client.execution, "model": model, "purpose": purpose,
               "base_url": client.base_url, "started_at": time.time(), "prompt_sha256": sha256(prompt_text),
               "prompt": redact(prompt_text), "model_digest": None, "model_details": {}}
        try:
            info = client.model_info(model)
            rec["model_digest"], rec["model_details"] = info.get("digest"), info.get("details", {})
            res = client.chat(model, messages, schema=schema, timeout=timeout, options=options)
        except ProviderError as e:
            rec.update(status=e.status, error=str(e), finished_at=time.time())
            self.store.add_llm_call(rec)
            raise
        rec.update(status="ok", finished_at=time.time(), input_tokens=res.input_tokens, output_tokens=res.output_tokens,
                   runtime_seconds=res.total_seconds, load_seconds=res.load_seconds, eval_seconds=res.eval_seconds,
                   response=redact(res.content), response_sha256=sha256(res.content), served_model=res.model,
                   done_reason=res.raw_meta.get("done_reason"))
        call_id = self.store.add_llm_call(rec)
        rec["id"] = call_id
        return res, rec
