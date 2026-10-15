# One-time setup on Windows (PowerShell). Creates .venv and installs dependencies.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
py -3 -m venv .venv 2>$null; if (-not $?) { python -m venv .venv }
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m systemfactory init-demo
Write-Host "Setup complete. Start with: .\scripts\start.ps1"
