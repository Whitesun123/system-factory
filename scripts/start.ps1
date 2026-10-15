# Start the API, dashboard and background worker on http://127.0.0.1:8765
# Optional: $env:SF_RUNTIME = "ollama" to make real local inference the default for new workflows.
Set-Location (Split-Path $PSScriptRoot -Parent)
$py = if (Test-Path .\.venv\Scripts\python.exe) { ".\.venv\Scripts\python.exe" } else { "python" }
& $py -m systemfactory serve --port 8765
