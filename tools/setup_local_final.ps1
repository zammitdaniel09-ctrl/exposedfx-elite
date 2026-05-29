Set-Location $PSScriptRoot\..
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
mkdir data -Force
if (Test-Path "$env:USERPROFILE\imperium-layer-router\data\session.session") { Copy-Item "$env:USERPROFILE\imperium-layer-router\data\session.session" ".\data\session.session" -Force }
Write-Host "DONE local setup" -ForegroundColor Green
