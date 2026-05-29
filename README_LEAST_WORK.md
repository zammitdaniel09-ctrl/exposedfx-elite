# Imperium Final Railway — Least Work

## 1. Local prepare

```powershell
cd $env:USERPROFILE\Downloads
Expand-Archive .\imperium-final-railway.zip -DestinationPath $env:USERPROFILE -Force
cd $env:USERPROFILE\imperium-final-railway
.\tools\setup_local_final.ps1
.\tools\make_session_b64.ps1
```

This copies your Telegram session from `imperium-layer-router`, then creates `SESSION_B64.txt` and copies it to clipboard.

## 2. Push private GitHub repo

```powershell
git init
git add .
git commit -m "Initial Imperium final Railway system"
git branch -M main
git remote add origin YOUR_PRIVATE_GITHUB_REPO_URL
git push -u origin main
```

## 3. Railway service 1: server

Start command:

```txt
python server/main.py
```

Variables:

```txt
AUTO_TOKEN=MAKE_A_LONG_RANDOM_SECRET
DATA_DIR=/data
```

Add volume mount path `/data`. Generate public domain.

## 4. Railway service 2: worker

Start command:

```txt
python telegram_worker/worker.py
```

Variables:

```txt
API_ID=33905884
API_HASH=your_api_hash
AUTO_TOKEN=same_secret_as_server
SERVER_URL=https://your-server-domain.up.railway.app
DATA_DIR=/data
SESSION_B64=paste_from_SESSION_B64.txt
DRY_RUN=0
```

Add volume mount path `/data`.

## 5. MT5

Use the EA in `mt5/ImperiumAutoCopierClient_v1.mq5`.

EA ServerURL must be your Railway server URL. Add that URL to MT5 WebRequest.
