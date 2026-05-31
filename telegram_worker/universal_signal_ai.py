import json
import os
import re
from typing import Any, Dict, Optional

import requests

from telegram_worker.signal_refiner import build_message, source_line

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-3-haiku-20240307").strip()
USE_CLAUDE = os.environ.get("USE_CLAUDE_SIGNAL_AI", "1").strip() == "1"


def clean(text: str) -> str:
    return (text or "").replace("\u200b", " ").replace("\xa0", " ").strip()


def looks_like_signal(text: str) -> bool:
    t = clean(text).upper()
    has_price = bool(re.search(r"\b\d{2,7}(?:\.\d+)?\b", t))
    has_action = bool(re.search(r"\b(BUY|BUYS|SELL|SELLS|LONG|SHORT|ENTERING|ENTRY)\b", t))
    has_trade_word = bool(re.search(r"\b(SL|STOP|STOPLOSS|STOP\s*LOSS|TP|TARGET|TAKE\s*PROFIT|ENTRY|ENTRIES|LIMIT|ZONE|RISK)\b", t))
    return has_price and (has_action or has_trade_word)


def normalize_symbol(symbol: str, text: str = "") -> str:
    s = (symbol or "").upper().replace("/", "").replace(" ", "")
    t = (text or "").upper()
    if s in ("BTC", "BTCUSD", "BTCUSDT", "BITCOIN") or "BTC" in t or "BITCOIN" in t:
        return "BTCUSD"
    if s in ("ETH", "ETHUSD", "ETHUSDT", "ETHEREUM") or "ETH" in t:
        return "ETHUSD"
    if s in ("SOL", "SOLUSD", "SOLUSDT", "SOLANA") or "SOL" in t:
        return "SOLUSD"
    if s in ("GOLD", "XAU", "XAUUSD") or "GOLD" in t or "XAU" in t:
        return "XAUUSD"
    if s in ("NAS", "NASDAQ", "NAS100", "US100") or "NASDAQ" in t:
        return "NAS100"
    if s:
        return s
    return "XAUUSD"


def symbol_family(symbol: str) -> str:
    s = (symbol or "").upper().replace("/", "")
    if s.startswith("XAU") or "GOLD" in s:
        return "GOLD"
    if any(x in s for x in ("BTC", "ETH", "SOL", "XRP", "USDT")):
        return "CRYPTO"
    if any(x in s for x in ("NAS", "US100", "US30", "SPX", "SP500", "GER", "DAX", "UK100")):
        return "INDEX"
    if len(s) == 6 and s.isalpha():
        return "FOREX"
    return "OTHER"


def risk_from_text(text: str, symbol: str, entry: float, sl: float) -> str:
    t = (text or "").upper()
    if "HIGHER RISK" in t or "HIGH RISK" in t or "VERY HIGH" in t:
        return "HIGH"
    if "MEDIUM RISK" in t:
        return "MEDIUM"
    if "LOW RISK" in t:
        return "LOW"

    distance = abs(float(entry) - float(sl))
    pct = distance / float(entry) if entry else 999
    fam = symbol_family(symbol)

    if fam == "GOLD":
        if distance <= 8:
            return "LOW"
        if distance <= 18:
            return "MEDIUM"
        return "HIGH"
    if fam == "CRYPTO":
        if pct <= 0.003:
            return "LOW"
        if pct <= 0.008:
            return "MEDIUM"
        return "HIGH"
    if fam == "INDEX":
        if pct <= 0.0025:
            return "LOW"
        if pct <= 0.006:
            return "MEDIUM"
        return "HIGH"
    if fam == "FOREX":
        if pct <= 0.001:
            return "LOW"
        if pct <= 0.0025:
            return "MEDIUM"
        return "HIGH"

    if pct <= 0.003:
        return "LOW"
    if pct <= 0.007:
        return "MEDIUM"
    return "HIGH"


def estimate_layer(direction: str, entry_low: float, entry_high: float, sl: float) -> float:
    lo = float(entry_low)
    hi = float(entry_high)
    stop = float(sl)
    if abs(hi - lo) > 0.00001:
        return lo if direction == "BUY" else hi
    entry = hi
    return (entry + stop) / 2


def tp_step(symbol: str, direction: str, entry: float, sl: float, first_tp: Optional[float] = None) -> float:
    if first_tp is not None and abs(float(first_tp) - float(entry)) > 0:
        return abs(float(first_tp) - float(entry))

    risk_distance = abs(float(entry) - float(sl))
    fam = symbol_family(symbol)
    if fam == "CRYPTO":
        return max(risk_distance * 0.5, entry * 0.002)
    if fam == "GOLD":
        return max(risk_distance * 0.5, 5)
    if fam == "INDEX":
        return max(risk_distance * 0.5, entry * 0.0015)
    if fam == "FOREX":
        return max(risk_distance * 0.5, entry * 0.0008)
    return max(risk_distance * 0.5, entry * 0.002)


def estimate_tps(symbol: str, direction: str, entry: float, sl: float, tps: list[float], tp_open: bool = False) -> list[float]:
    cleaned = [float(x) for x in tps if x is not None]
    if len(cleaned) >= 8:
        return cleaned[:8]

    first_tp = cleaned[0] if cleaned else None
    step = tp_step(symbol, direction, entry, sl, first_tp)
    start = first_tp if first_tp is not None else float(entry)

    out = cleaned[:]
    while len(out) < 8:
        if not out:
            next_tp = start + step if direction == "BUY" else start - step
        else:
            next_tp = out[-1] + step if direction == "BUY" else out[-1] - step
        out.append(next_tp)

    return out[:8]


def parse_jsonish(value: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(value)
    except Exception:
        pass
    m = re.search(r"\{.*\}", value, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def claude_extract(text: str) -> Optional[Dict[str, Any]]:
    if not (USE_CLAUDE and ANTHROPIC_API_KEY):
        return None

    prompt = (
        "Extract a trading signal from messy Telegram text. Return JSON only. "
        "Accept any instrument/pair/index/crypto, not only gold. "
        "A signal can be casual, for example 'I am personally entering', 'BTC get ready', 'sell higher risk'. "
        "Need direction, symbol, entry, stop loss, and either numeric take profits or TP open. "
        "If multiple stop losses are present, use the latest/current one, especially phrases like 'set your stop loss to'. "
        "If TP is Open with no number, set tps to [] and tp_open to true. "
        "If only one numeric TP is given, keep that TP; the system will estimate the rest. "
        "Return keys: is_signal, symbol, direction, entry_low, entry_high, sl, tps, tp_open, risk. "
        "direction must be BUY or SELL. risk may be LOW, MEDIUM, HIGH, or empty."
    )

    try:
        res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 600,
                "temperature": 0,
                "system": prompt,
                "messages": [{"role": "user", "content": clean(text)[:4500]}],
            },
            timeout=20,
        )
        if res.status_code >= 400:
            return None
        data = res.json()
        content = "".join(part.get("text", "") for part in data.get("content", []) if part.get("type") == "text")
        obj = parse_jsonish(content)
        if not obj or not obj.get("is_signal"):
            return None

        direction = str(obj.get("direction", "")).upper()
        if direction not in ("BUY", "SELL"):
            return None
        entry_low = float(obj.get("entry_low"))
        entry_high = float(obj.get("entry_high", entry_low))
        sl = float(obj.get("sl"))
        symbol = normalize_symbol(str(obj.get("symbol", "")), text)

        tps = []
        for item in obj.get("tps", []) or []:
            try:
                tps.append(float(item))
            except Exception:
                pass
        tp_open = bool(obj.get("tp_open", False)) or "OPEN" in clean(text).upper()
        if not tps and not tp_open:
            return None

        mid = (entry_low + entry_high) / 2
        risk = str(obj.get("risk", "")).upper()
        if risk not in ("LOW", "MEDIUM", "HIGH"):
            risk = risk_from_text(text, symbol, mid, sl)

        return {
            "is_signal": True,
            "symbol": symbol,
            "direction": direction,
            "entry_low": min(entry_low, entry_high),
            "entry_high": max(entry_low, entry_high),
            "sl": sl,
            "tps": estimate_tps(symbol, direction, mid, sl, tps, tp_open),
            "tp_open": True,
            "risk": risk,
            "layer_point": estimate_layer(direction, min(entry_low, entry_high), max(entry_low, entry_high), sl),
        }
    except Exception:
        return None


def regex_extract(text: str) -> Optional[Dict[str, Any]]:
    raw = clean(text).replace("–", "-").replace("—", "-")
    up = raw.upper()
    if not looks_like_signal(up):
        return None

    if re.search(r"\b(BUY|BUYS|LONG)\b", up):
        direction = "BUY"
    elif re.search(r"\b(SELL|SELLS|SHORT)\b", up):
        direction = "SELL"
    else:
        return None

    symbol = normalize_symbol("", up)

    entry = None
    for pat in [
        r"\bENTRY\b\s*[:\-]?\s*(\d{2,7}(?:\.\d+)?)\s*(?:-|TO)?\s*(\d{2,7}(?:\.\d+)?)?",
        r"\b(?:BUY|BUYS|SELL|SELLS|LONG|SHORT)\b\D{0,40}(\d{2,7}(?:\.\d+)?)\s*(?:-|TO)\s*(\d{2,7}(?:\.\d+)?)",
        r"\b(?:BUY|BUYS|SELL|SELLS|LONG|SHORT)\b\D{0,40}(\d{2,7}(?:\.\d+)?)",
    ]:
        m = re.search(pat, up)
        if m:
            a = float(m.group(1))
            b = float(m.group(2)) if len(m.groups()) > 1 and m.group(2) else a
            entry = (min(a, b), max(a, b))
            break
    if entry is None:
        return None

    sl_matches = re.findall(r"\b(?:SL|STOP\s*LOSS|STOPLOSS|STOP)\b(?:\s+TO)?\s*[:\-]?\s*(\d{2,7}(?:\.\d+)?)", up)
    if not sl_matches:
        return None
    sl = float(sl_matches[-1])

    tps = [float(x) for x in re.findall(r"\b(?:TP\s*#?\s*\d*|TARGET\s*#?\s*\d*|TAKE\s*PROFIT)\b\s*[:\-]?\s*(\d{2,7}(?:\.\d+)?)", up)]
    tp_open = bool(re.search(r"\b(TP|TAKE\s*PROFIT|TARGET)\b\s*[:\-]?\s*OPEN\b", up))
    if not tps and not tp_open:
        return None

    mid = (entry[0] + entry[1]) / 2
    return {
        "is_signal": True,
        "symbol": symbol,
        "direction": direction,
        "entry_low": entry[0],
        "entry_high": entry[1],
        "sl": sl,
        "tps": estimate_tps(symbol, direction, mid, sl, tps, tp_open),
        "tp_open": True,
        "risk": risk_from_text(up, symbol, mid, sl),
        "layer_point": estimate_layer(direction, entry[0], entry[1], sl),
    }


def extract_and_format(text: str, source_name: str, message_id=None) -> Optional[Dict[str, Any]]:
    sig = claude_extract(text) or regex_extract(text)
    if not sig:
        return None
    return {
        "is_signal": True,
        "message": build_message(sig),
        "source": source_line(source_name, message_id),
        "parsed": sig,
    }
