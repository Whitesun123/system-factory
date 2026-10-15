# Download the default local models (about 25 GB). Set OLLAMA_MODELS first to store them on a large drive,
# e.g.  $env:OLLAMA_MODELS = "D:\OllamaModels"  (must match the value the Ollama server uses).
$models = @($env:SF_CODER_MODEL, $env:SF_REVIEWER_A_MODEL, $env:SF_REVIEWER_B_MODEL) | Where-Object { $_ }
if (-not $models) { $models = @("qwen2.5-coder:14b", "phi4:14b", "mistral-nemo:12b") }
foreach ($m in $models) { ollama pull $m }
