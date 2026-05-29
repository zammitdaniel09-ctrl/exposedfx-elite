# server/main.py
# Imperium AutoCopier MVP server
# Local FastAPI + SQLite signal store.

import os
import json
import sqlite3
import time
from pathlib import Path
from typing import Optional, List, Dict, Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field


DATA_DIR = Path(os.environ.get("DATA_DIR") or ("/data" if Path("/data").exists() else "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "autocopier.sqlite"
AUTO_TOKEN = os.environ.get("AUTO_TOKEN", "change-this-token")

app = FastAPI(title="Imperium AutoCopier MVP", version="1.0.0")


# ---------------- DB ----------------

def db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            source_chat_id INTEGER,
            source_message_id INTEGER,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_low REAL NOT NULL,
            entry_high REAL NOT NULL,
            sl REAL NOT NULL,
            tp1 REAL NOT NULL,
            tp2 REAL,
            tp3 REAL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            reason TEXT,
            raw_text TEXT,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS copy_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER NOT NULL,
            client_token TEXT NOT NULL,
            mt5_account TEXT,
            status TEXT NOT NULL,
            detail TEXT,
            created_at REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_copy_signal_client ON copy_results(signal_id, client_token)")
    conn.commit()
    conn.close()


init_db()


# ---------------- AUTH ----------------

def require_token(x_auto_token: Optional[str]):
    if x_auto_token != AUTO_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing X-AUTO-TOKEN")


# ---------------- MODELS ----------------

class SignalIn(BaseModel):
    source: str
    source_chat_id: Optional[int] = None
    source_message_id: Optional[int] = None
    symbol: str
    direction: str = Field(pattern="^(BUY|SELL)$")
    entry_low: float
    entry_high: float
    sl: float
    tp1: float
    tp2: Optional[float] = None
    tp3: Optional[float] = None
    raw_text: Optional[str] = None
    expires_minutes: int = 30


class CopyResultIn(BaseModel):
    client_token: str
    mt5_account: Optional[str] = None
    status: str
    detail: Optional[str] = None


# ---------------- VALIDATION ----------------

def validate_signal(sig: SignalIn):
    direction = sig.direction.upper()
    lo = min(sig.entry_low, sig.entry_high)
    hi = max(sig.entry_low, sig.entry_high)
    mid = (lo + hi) / 2.0

    if lo <= 0 or hi <= 0 or sig.sl <= 0 or sig.tp1 <= 0:
        return False, "Prices must be positive"

    if hi < lo:
        return False, "Invalid entry zone"

    if direction == "BUY":
        if sig.sl >= mid:
            return False, "BUY SL must be below entry zone"
        if sig.tp1 <= mid:
            return False, "BUY TP1 must be above entry zone"
        for label, tp in [("TP2", sig.tp2), ("TP3", sig.tp3)]:
            if tp is not None and tp <= mid:
                return False, f"BUY {label} must be above entry zone"

    if direction == "SELL":
        if sig.sl <= mid:
            return False, "SELL SL must be above entry zone"
        if sig.tp1 >= mid:
            return False, "SELL TP1 must be below entry zone"
        for label, tp in [("TP2", sig.tp2), ("TP3", sig.tp3)]:
            if tp is not None and tp >= mid:
                return False, f"SELL {label} must be below entry zone"

    if abs(hi - lo) > 1000:
        return False, "Entry zone too wide"

    return True, "OK"


def row_to_dict(row):
    d = dict(row)
    return d


# ---------------- ROUTES ----------------

@app.get("/")
def root():
    return {
        "ok": True,
        "name": "Imperium AutoCopier MVP",
        "db": str(DB_PATH),
        "time": time.time(),
    }


@app.get("/signals")
def list_signals(limit: int = 50):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM signals ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]


@app.post("/api/v1/signals")
def ingest_signal(sig: SignalIn, x_auto_token: Optional[str] = Header(None)):
    require_token(x_auto_token)

    ok, reason = validate_signal(sig)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)

    now = time.time()
    expires_at = now + sig.expires_minutes * 60

    lo = min(sig.entry_low, sig.entry_high)
    hi = max(sig.entry_low, sig.entry_high)

    conn = db()

    # Deduplicate same source message if provided.
    if sig.source_chat_id and sig.source_message_id:
        existing = conn.execute(
            """
            SELECT id FROM signals
            WHERE source_chat_id=? AND source_message_id=?
            LIMIT 1
            """,
            (sig.source_chat_id, sig.source_message_id),
        ).fetchone()

        if existing:
            conn.close()
            return {"ok": True, "duplicate": True, "id": existing["id"]}

    cur = conn.execute("""
        INSERT INTO signals
        (source, source_chat_id, source_message_id, symbol, direction,
         entry_low, entry_high, sl, tp1, tp2, tp3, status, reason,
         raw_text, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', NULL, ?, ?, ?)
    """, (
        sig.source,
        sig.source_chat_id,
        sig.source_message_id,
        sig.symbol.upper(),
        sig.direction.upper(),
        lo,
        hi,
        sig.sl,
        sig.tp1,
        sig.tp2,
        sig.tp3,
        sig.raw_text,
        now,
        expires_at,
    ))

    conn.commit()
    signal_id = cur.lastrowid
    conn.close()

    return {"ok": True, "id": signal_id}


@app.get("/api/v1/signals/pending")
def pending_signals(
    x_auto_token: Optional[str] = Header(None),
    sources: Optional[str] = Query(None),
    max_age_minutes: int = 30,
    client_token: Optional[str] = Query(None),
):
    require_token(x_auto_token)

    now = time.time()
    cutoff = now - max_age_minutes * 60

    allowed_sources = None
    if sources:
        allowed_sources = {s.strip().lower() for s in sources.split(",") if s.strip()}

    conn = db()
    rows = conn.execute("""
        SELECT * FROM signals
        WHERE status='PENDING'
        AND created_at >= ?
        AND expires_at >= ?
        ORDER BY id ASC
    """, (cutoff, now)).fetchall()

    out = []
    for row in rows:
        d = row_to_dict(row)

        if allowed_sources and d["source"].lower() not in allowed_sources:
            continue

        if client_token:
            copied = conn.execute(
                """
                SELECT id FROM copy_results
                WHERE signal_id=? AND client_token=?
                LIMIT 1
                """,
                (d["id"], client_token),
            ).fetchone()

            if copied:
                continue

        out.append(d)

    conn.close()
    return out


@app.post("/api/v1/signals/{signal_id}/copy_result")
def copy_result(
    signal_id: int,
    result: CopyResultIn,
    x_auto_token: Optional[str] = Header(None),
):
    require_token(x_auto_token)

    conn = db()
    exists = conn.execute("SELECT id FROM signals WHERE id=?", (signal_id,)).fetchone()
    if not exists:
        conn.close()
        raise HTTPException(status_code=404, detail="Signal not found")

    conn.execute("""
        INSERT INTO copy_results
        (signal_id, client_token, mt5_account, status, detail, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        signal_id,
        result.client_token,
        result.mt5_account,
        result.status,
        result.detail,
        time.time(),
    ))

    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/v1/signals/{signal_id}/cancel")
def cancel_signal(signal_id: int, x_auto_token: Optional[str] = Header(None)):
    require_token(x_auto_token)
    conn = db()
    conn.execute(
        "UPDATE signals SET status='CANCELLED', reason='manual cancel' WHERE id=?",
        (signal_id,),
    )
    conn.commit()
    conn.close()
    return {"ok": True}




@app.get("/api/v1/signals/pending_ea")
def pending_signals_ea(
    token: str,
    sources: Optional[str] = Query(None),
    max_age_minutes: int = 30,
    client_token: Optional[str] = Query(None),
):
    if token != AUTO_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    return pending_signals(
        x_auto_token=AUTO_TOKEN,
        sources=sources,
        max_age_minutes=max_age_minutes,
        client_token=client_token,
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)
