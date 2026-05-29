Set-Location $PSScriptRoot\..
$sessionPath = ".\data\session.session"
if (!(Test-Path $sessionPath)) { Write-Host "Missing .\data\session.session" -ForegroundColor Red; exit }
$b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($sessionPath))
Set-Content ".\SESSION_B64.txt" $b64 -NoNewline
Set-Clipboard $b64
Write-Host "DONE. SESSION_B64 copied to clipboard and saved to SESSION_B64.txt" -ForegroundColor Green
