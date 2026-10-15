"""Check Ollama availability and whether the configured models are installed.

Reads only /api/version and /api/tags: it does NOT load any model or run inference.
Exit code 0 when Ollama is reachable and every configured model is installed, 1 otherwise.

    python scripts/check_models.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from systemfactory.config import Settings  # noqa: E402
from systemfactory.llm import OllamaClient  # noqa: E402


def main() -> int:
    s = Settings.from_env()
    roles = {"coder": s.coder_model, "reviewer A": s.reviewer_a_model, "reviewer B": s.reviewer_b_model}
    st = OllamaClient(base_url=s.ollama_url).status(sorted(set(roles.values())))
    print(f"Ollama at {st['url']}: {'reachable, version ' + st['version'] if st['reachable'] else 'NOT reachable'}")
    if not st["reachable"]:
        print(f"  {st.get('error')}\n  Start it with: ollama serve   (see README > Ollama setup)")
        return 1
    ok = True
    for role, model in roles.items():
        m = st["models"].get(model, {})
        if m.get("installed"):
            print(f"  [ok]      {role:<11} {model:<22} {m.get('parameter_size')} {m.get('quantization')} "
                  f"digest {m.get('digest', '')[:12]}")
        else:
            ok = False
            print(f"  [missing] {role:<11} {model:<22} -> ollama pull {model}")
    others = sorted(set(st.get("installed", [])) - set(roles.values()))
    if others:
        print(f"  other installed models: {', '.join(others)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
