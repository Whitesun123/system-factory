# Offline test suite: deterministic, no Ollama, no network, no LLM calls.
Set-Location (Split-Path $PSScriptRoot -Parent)
$py = if (Test-Path .\.venv\Scripts\python.exe) { ".\.venv\Scripts\python.exe" } else { "python" }
& $py -m pytest -q -p no:cacheprovider @args
