# start_bridge.ps1
Set-Location $PSScriptRoot
.\.venv\Scripts\Activate.ps1

$env:API_ID="33905884"
$env:API_HASH="PASTE_YOUR_API_HASH_HERE"
$env:AUTO_TOKEN="change-this-token"
$env:SERVER_URL="http://127.0.0.1:8000"
$env:DATA_DIR="./data"

python telegram_bridge\signal_bridge.py
