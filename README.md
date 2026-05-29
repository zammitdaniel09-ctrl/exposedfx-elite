# Imperium AutoCopier MVP

This is a full starter system for:

```txt
Telegram signal groups -> validated signal server -> MT5 client EA
```

It is intentionally separated from your `imperium-layer-router` so your working forwarding system stays untouched.

## What this MVP does

- Reads Telegram signals from your sources.
- Parses actionable signals only.
- Rejects dangerous/incomplete signals:
  - no SL
  - no entry
  - no TP1
  - SL on wrong side
  - TP on wrong side
  - no clear BUY/SELL
- Stores valid signals in a local server.
- MT5 EA polls the server.
- Client chooses which sources to copy.
- EA only enters if current price is inside the entry zone.
- EA calculates lots from risk %.
- EA splits TP targets into separate positions.
- EA sends copied-trade result back to server.

## Important

This is for demo testing first.

Do not run this on client live accounts until you have tested:

- parsing accuracy
- symbol mapping
- lot sizing
- broker execution
- max daily loss
- duplicate prevention
- source selection

Default risk in the EA is set to 1%. You can increase it, but 3-5% is aggressive.

---

# Fastest local setup

## 1. Extract folder

Put this folder at:

```powershell
C:\Users\zammi\imperium-autocopier-mvp
```

## 2. Open PowerShell

```powershell
cd C:\Users\zammi\imperium-autocopier-mvp
```

## 3. Create venv and install

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r server\requirements.txt
```

## 4. Start the local signal server

```powershell
$env:AUTO_TOKEN="change-this-token"
$env:DATA_DIR="./data"
python server\main.py
```

Keep this window open.

Server will run at:

```txt
http://127.0.0.1:8000
```

Open this in browser:

```txt
http://127.0.0.1:8000/docs
```

---

# Test without Telegram first

Open a second PowerShell window:

```powershell
cd C:\Users\zammi\imperium-autocopier-mvp
.\.venv\Scripts\Activate.ps1
$env:AUTO_TOKEN="change-this-token"
python tools\post_test_signal.py
```

Then check:

```txt
http://127.0.0.1:8000/signals
```

---

# MT5 setup

## 1. Copy EA file

Copy:

```txt
mt5\ImperiumAutoCopierClient_v1.mq5
```

To:

```txt
File -> Open Data Folder -> MQL5 -> Experts
```

Then restart MetaEditor/MT5 and compile it.

## 2. Allow WebRequest in MT5

In MT5:

```txt
Tools -> Options -> Expert Advisors
```

Tick:

```txt
Allow WebRequest for listed URL
```

Add:

```txt
http://127.0.0.1:8000
```

## 3. Attach EA to XAUUSD chart

Recommended first:

```txt
DEMO ACCOUNT ONLY
XAUUSD or XAUUSD.s chart
M1 timeframe is fine
Algo Trading ON
```

Recommended EA inputs:

```txt
ServerURL = http://127.0.0.1:8000
ClientToken = change-this-token
AllowedSources = TEST,Market Slayers VIP,Triad FX,Gold Trader Sunny
SymbolMap = XAUUSD:XAUUSD.s,GOLD:XAUUSD.s,XAU:XAUUSD.s
RiskPercent = 1.0
MaxRiskPercent = 3.0
OnlyEnterInsideZone = true
MaxSignalAgeMinutes = 30
MaxDailyLossPercent = 10
MaxOpenTrades = 3
```

---

# Telegram bridge setup

Only do this after the test signal works.

## 1. Use your existing Telegram session

The bridge uses:

```txt
data/session.session
```

You can copy your working session from `imperium-layer-router`:

```powershell
mkdir data -Force
Copy-Item "C:\Users\zammi\imperium-layer-router\data\session.session" ".\data\session.session" -Force
```

## 2. Start bridge in a second PowerShell window

```powershell
cd C:\Users\zammi\imperium-autocopier-mvp
.\.venv\Scripts\Activate.ps1

$env:API_ID="33905884"
$env:API_HASH="YOUR_API_HASH"
$env:AUTO_TOKEN="change-this-token"
$env:SERVER_URL="http://127.0.0.1:8000"
$env:DATA_DIR="./data"

python telegram_bridge\signal_bridge.py
```

---

# Safe build order

1. Server running.
2. Post test signal.
3. MT5 EA copies test signal on demo.
4. Telegram bridge running.
5. Verify parser stores signals correctly.
6. Only then consider real accounts.

---

# File map

```txt
imperium-autocopier-mvp/
├── server/
│   ├── main.py
│   └── requirements.txt
├── telegram_bridge/
│   ├── signal_bridge.py
│   ├── parser.py
│   ├── sources.py
│   └── qr_login.py
├── mt5/
│   └── ImperiumAutoCopierClient_v1.mq5
├── tools/
│   ├── post_test_signal.py
│   └── sample_signal.json
├── docs/
│   └── EXECUTION_RULES.md
├── .gitignore
└── README.md
```
