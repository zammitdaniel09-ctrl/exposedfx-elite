# setup_local.ps1
# Run from project root.

Set-Location $PSScriptRoot

py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r server\requirements.txt

Write-Host ""
Write-Host "Setup complete."
Write-Host "Start server with:"
Write-Host '$env:AUTO_TOKEN="change-this-token"; $env:DATA_DIR="./data"; python server\main.py'
