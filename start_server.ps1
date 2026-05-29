# start_server.ps1
Set-Location $PSScriptRoot
.\.venv\Scripts\Activate.ps1
$env:AUTO_TOKEN="change-this-token"
$env:DATA_DIR="./data"
python server\main.py
