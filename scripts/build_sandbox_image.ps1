# Build the execution sandbox image (the only networked, trusted phase). See sandbox/README.md.
Set-Location (Split-Path $PSScriptRoot -Parent)
$image = if ($env:SF_SANDBOX_IMAGE) { $env:SF_SANDBOX_IMAGE } else { "systemfactory-sandbox:1" }
docker build -t $image sandbox
