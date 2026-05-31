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


def fallback_step(symbol: str, entry: float, sl: float) -> float:
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
    """
    TP rules:
    - If TP is Open with no numeric TPs, estimate TP1-TP8 by R multiples:
      TP1 = 0.5R, TP2 = 1R, then TP3-TP8 = 2R-7R.
    - If the provider sends numeric TPs, keep them exactly.
      If fewer than 8 are sent, extend the rest using the original TP spacing.
    """
    cleaned = [float(x) for x in tps if x is not None]
    entry = float(entry)
    sl = float(sl)
    sign = 1 if direction == "BUY" else -1
    risk_distance = abs(entry - sl)

    if len(cleaned) >= 8:
        return cleaned[:8]

    if not cleaned:
        if not tp_open:
            return []
        multipliers = [0.5, 1, 2, 3, 4, 5, 6, 7]
        return [entry + sign * risk_distance * r for r in multipliers]

    out = cleaned[:]

    if len(cleaned) >= 2:
        step = abs(cleaned[-1] - cleaned[-2])
    else:
        step = abs(cleaned[0] - entry)

    if step <= 0:
        step = fallback_step(symbol, entry, sl)

    while len(out) < 8:
        out.append(out[-1] + sign * step)

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
        "If numeric TPs are given, extract only the numeric TPs exactly as written; the system will extend missing TPs. "
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

    tps = []
    for pat in [
        r"\bTP\s*#?\s*\d*\s*[\(\[\{:\-]?\s*(\d{2,7}(?:\.\d+)?)\s*[\)\]\}]?",
        r"\bTARGET\s*#?\s*\d*\s*[\(\[\{:\-]?\s*(\d{2,7}(?:\.\d+)?)\s*[\)\]\}]?",
        r"\bTAKE\s*PROFIT\s*#?\s*\d*\s*[\(\[\{:\-]?\s*(\d{2,7}(?:\.\d+)?)\s*[\)\]\}]?",
    ]:
        for x in re.findall(pat, up):
            v = float(x)
            if v >= 10 and v not in tps:
                tps.append(v)
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
    sig = regex_extract(text) or claude_extract(text)
    if not sig:
        return None
    return {
        "is_signal": True,
        "message": build_message(sig),
        "source": source_line(source_name, message_id),
        "parsed": sig,
    }


# =========================
# OVERRIDES: wider pair + forex + edited-message support
# =========================

_PRICE_ANY = r"\d{1,7}(?:\.\d+)?"

_FOREX_BASES = {
    "EUR","USD","GBP","JPY","AUD","NZD","CAD","CHF"
}

_IGNORE_SYMBOL_WORDS = {
    "ENTRY","TARGET","AROUND","SIGNAL","SHORTS","LONGS","SELLER","BUYING",
    "SELLING","UPDATE","PROFIT","LOSSES","RISKYY","RISKED","MANAGE"
}

def normalize_symbol(symbol: str, text: str = "") -> str:
    s = (symbol or "").upper().replace("/", "").replace(" ", "").replace("-", "")
    t = (text or "").upper().replace("/", "").replace("-", "")

    if "NASDAQ100" in t or "NASDAQ 100" in (text or "").upper() or "NAS100" in t or "US100" in t or re.search(r"\bNAS\b", t):
        return "NAS100"
    if "US30" in t or "DOW" in t:
        return "US30"
    if "SPX500" in t or "SP500" in t or "US500" in t or "S&P" in (text or "").upper():
        return "US500"
    if "GER40" in t or "DAX" in t:
        return "GER40"
    if "UK100" in t:
        return "UK100"

    if "BTC" in t or "BITCOIN" in t or s in ("BTC","BTCUSD","BTCUSDT"):
        return "BTCUSD"
    if "ETH" in t or "ETHEREUM" in t or s in ("ETH","ETHUSD","ETHUSDT"):
        return "ETHUSD"
    if "SOL" in t or "SOLANA" in t or s in ("SOL","SOLUSD","SOLUSDT"):
        return "SOLUSD"

    if "XAU" in t or "GOLD" in t or s in ("XAU","XAUUSD","GOLD"):
        return "XAUUSD"
    if "XAG" in t or "SILVER" in t or s in ("XAG","XAGUSD","SILVER"):
        return "XAGUSD"

    # Any forex pair: EURUSD, GBPJPY, USDJPY, AUDCAD, etc.
    for m in re.finditer(r"\b([A-Z]{6})\b", t):
        pair = m.group(1)
        if pair in _IGNORE_SYMBOL_WORDS:
            continue
        if pair[:3] in _FOREX_BASES and pair[3:] in _FOREX_BASES:
            return pair

    if s and s not in _IGNORE_SYMBOL_WORDS:
        return s

    return "XAUUSD"


def looks_like_signal(text: str) -> bool:
    t = clean(text).upper()
    has_price = bool(re.search(rf"\b{_PRICE_ANY}\b", t))
    has_action = bool(re.search(r"\b(BUY|BUYS|BUYING|SELL|SELLS|SELLING|LONG|LONGS|SHORT|SHORTS|ENTERING|ENTRY|LIMIT)\b", t))
    has_trade_word = bool(re.search(r"\b(SL|S/L|STOP|STOPLOSS|STOP\s*LOSS|TP\s*\d*|TARGET|TAKE\s*PROFIT|ENTRY|ENTRIES|LIMIT|ZONE|RISK|RISKY)\b", t))
    return has_price and (has_action or has_trade_word)


def _direction_any(up: str):
    if re.search(r"\b(BUY|BUYS|BUYING|LONG|LONGS)\b", up):
        return "BUY"
    if re.search(r"\b(SELL|SELLS|SELLING|SHORT|SHORTS)\b", up):
        return "SELL"
    return None


def _entry_any(up: str):
    patterns = [
        rf"\b(?:ENTRY|ENTRIES|ENTER|ENTERING)\b(?:\s+(?:AROUND|AT|NOW))?\s*[:\-]?\s*({_PRICE_ANY})\s*(?:-|TO|/)?\s*({_PRICE_ANY})?",
        rf"\b(?:BUY|BUYS|BUYING|SELL|SELLS|SELLING|LONG|LONGS|SHORT|SHORTS)\b(?:\s+(?:NOW|LIMIT|STOP|ZONE|AT|AROUND))*\D{{0,80}}({_PRICE_ANY})\s*(?:-|TO|/)\s*({_PRICE_ANY})",
        rf"\b(?:BUY|BUYS|BUYING|SELL|SELLS|SELLING|LONG|LONGS|SHORT|SHORTS)\b(?:\s+(?:NOW|LIMIT|STOP|ZONE|AT|AROUND))*\D{{0,80}}({_PRICE_ANY})",
    ]
    for pat in patterns:
        m = re.search(pat, up)
        if m:
            a = float(m.group(1))
            b = float(m.group(2)) if len(m.groups()) > 1 and m.group(2) else a
            return min(a, b), max(a, b)
    return None


def _sl_any(up: str):
    vals = []
    for pat in [
        rf"\b(?:SL|S/L|STOP\s*LOSS|STOPLOSS|STOP)\b(?:\s+TO)?\s*[:\-]?\s*({_PRICE_ANY})",
        rf"\bSET\s+YOUR\s+STOP\s+LOSS\s+TO\s*({_PRICE_ANY})",
    ]:
        vals += [float(x) for x in re.findall(pat, up)]
    return vals[-1] if vals else None


def _tps_any(up: str):
    vals = []
    for pat in [
        rf"\bTP\s*#?\s*\d*\s*[\(\[\{{:\-]?\s*({_PRICE_ANY})\s*[\)\]\}}]?",
        rf"\bTARGET\s*#?\s*\d*\s*[\(\[\{{:\-]?\s*({_PRICE_ANY})\s*[\)\]\}}]?",
        rf"\bTAKE\s*PROFIT\s*#?\s*\d*\s*[\(\[\{{:\-]?\s*({_PRICE_ANY})\s*[\)\]\}}]?",
    ]:
        for x in re.findall(pat, up):
            v = float(x)
            if v not in vals:
                vals.append(v)

    tp_open = bool(re.search(r"\b(?:TP|TAKE\s*PROFIT|TARGET)\b\s*#?\s*\d*\s*[:\-]?\s*OPEN\b", up))
    return vals, tp_open


def regex_extract(text: str) -> Optional[Dict[str, Any]]:
    raw = clean(text).replace("–", "-").replace("—", "-")
    up = raw.upper()

    if not looks_like_signal(up):
        return None

    direction = _direction_any(up)
    if not direction:
        return None

    symbol = normalize_symbol("", up)
    entry = _entry_any(up)
    sl = _sl_any(up)
    tps, tp_open = _tps_any(up)

    if entry is None or sl is None:
        return None
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


# =========================
# FINAL OVERRIDES: all-pair parser, edited-message safe, fixed TP parsing
# =========================

_PRICE_ANY = r"\d{1,7}(?:\.\d+)?"
_FOREX_CURRENCIES = {"EUR","USD","GBP","JPY","AUD","NZD","CAD","CHF"}
_IGNORE_SYMBOLS = {"ENTRY","TARGET","AROUND","SIGNAL","SHORTS","LONGS","SELLING","BUYING","UPDATE","PROFIT","LOSSES","MANAGE"}


def normalize_symbol(symbol: str, text: str = "") -> str:
    raw = (text or "").upper()
    t = raw.replace("/", "").replace("-", "")
    s = (symbol or "").upper().replace("/", "").replace("-", "").replace(" ", "")

    if "NASDAQ 100" in raw or "NASDAQ100" in t or "NAS100" in t or "US100" in t or re.search(r"\bNAS\b", t):
        return "NAS100"
    if "US30" in t or "DOW" in t:
        return "US30"
    if "US500" in t or "SPX500" in t or "SP500" in t or "S&P" in raw:
        return "US500"
    if "GER40" in t or "DAX" in t:
        return "GER40"

    if "BTC" in t or "BITCOIN" in t:
        return "BTCUSD"
    if "ETH" in t or "ETHEREUM" in t:
        return "ETHUSD"
    if "SOL" in t or "SOLANA" in t:
        return "SOLUSD"

    if "XAU" in t or "GOLD" in t:
        return "XAUUSD"
    if "XAG" in t or "SILVER" in t:
        return "XAGUSD"

    # Any normal forex pair, e.g. EURUSD, GBPJPY, AUDCAD
    for m in re.finditer(r"\b([A-Z]{6})\b", t):
        pair = m.group(1)
        if pair in _IGNORE_SYMBOLS:
            continue
        if pair[:3] in _FOREX_CURRENCIES and pair[3:] in _FOREX_CURRENCIES:
            return pair

    if s and s not in _IGNORE_SYMBOLS:
        return s

    return "XAUUSD"


def looks_like_signal(text: str) -> bool:
    t = clean(text).upper()
    has_price = bool(re.search(rf"\b{_PRICE_ANY}\b", t))
    has_action = bool(re.search(r"\b(BUY|BUYS|BUYING|SELL|SELLS|SELLING|LONG|LONGS|SHORT|SHORTS|ENTERING|ENTRY|LIMIT)\b", t))
    has_trade_word = bool(re.search(r"\b(SL|S/L|STOP|STOPLOSS|STOP\s*LOSS|TP\s*\d*|TARGET|TAKE\s*PROFIT|ENTRY|ENTRIES|LIMIT|ZONE|RISK|RISKY)\b", t))
    return has_price and (has_action or has_trade_word)


def risk_from_text(text: str, symbol: str, entry: float, sl: float) -> str:
    t = (text or "").upper()
    if "RISKY" in t or "HIGHER RISK" in t or "HIGH RISK" in t or "VERY HIGH" in t:
        return "HIGH"
    if "MEDIUM RISK" in t:
        return "MEDIUM"
    if "LOW RISK" in t:
        return "LOW"

    distance = abs(float(entry) - float(sl))
    pct = distance / float(entry) if entry else 999
    fam = symbol_family(symbol)

    if fam == "GOLD":
        return "LOW" if distance <= 8 else "MEDIUM" if distance <= 18 else "HIGH"
    if fam == "CRYPTO":
        return "LOW" if pct <= 0.003 else "MEDIUM" if pct <= 0.008 else "HIGH"
    if fam == "INDEX":
        return "LOW" if pct <= 0.0025 else "MEDIUM" if pct <= 0.006 else "HIGH"
    if fam == "FOREX":
        return "LOW" if pct <= 0.0015 else "MEDIUM" if pct <= 0.005 else "HIGH"

    return "LOW" if pct <= 0.003 else "MEDIUM" if pct <= 0.007 else "HIGH"


def _direction_any(up: str):
    if re.search(r"\b(BUY|BUYS|BUYING|LONG|LONGS)\b", up):
        return "BUY"
    if re.search(r"\b(SELL|SELLS|SELLING|SHORT|SHORTS)\b", up):
        return "SELL"
    return None


def _entry_any(up: str):
    patterns = [
        rf"\b(?:ENTRY|ENTRIES|ENTER|ENTERING)\b(?:\s+(?:AROUND|AT|NOW))?\s*[:\-]?\s*({_PRICE_ANY})\s*(?:-|TO|/)?\s*({_PRICE_ANY})?",
        rf"\b(?:BUY|BUYS|BUYING|SELL|SELLS|SELLING|LONG|LONGS|SHORT|SHORTS)\b(?:\s+(?:NOW|LIMIT|STOP|ZONE|AT|AROUND))*\D{{0,80}}({_PRICE_ANY})\s*(?:-|TO|/)\s*({_PRICE_ANY})",
        rf"\b(?:BUY|BUYS|BUYING|SELL|SELLS|SELLING|LONG|LONGS|SHORT|SHORTS)\b(?:\s+(?:NOW|LIMIT|STOP|ZONE|AT|AROUND))*\D{{0,80}}({_PRICE_ANY})",
    ]
    for pat in patterns:
        m = re.search(pat, up)
        if m:
            a = float(m.group(1))
            b = float(m.group(2)) if len(m.groups()) > 1 and m.group(2) else a
            return min(a, b), max(a, b)
    return None


def _sl_any(up: str):
    vals = []
    for pat in [
        rf"\b(?:SL|S/L|STOP\s*LOSS|STOPLOSS|STOP)\b(?:\s+TO)?\s*[:\-]?\s*({_PRICE_ANY})",
        rf"\bSET\s+YOUR\s+STOP\s+LOSS\s+TO\s*({_PRICE_ANY})",
    ]:
        vals += [float(x) for x in re.findall(pat, up)]
    return vals[-1] if vals else None


def _tps_any(text: str):
    vals = []
    tp_open = False

    for line in text.splitlines():
        u = line.upper().strip()

        if not re.search(r"\b(TP|TARGET|TAKE\s*PROFIT)\b", u):
            continue

        if re.search(r"\bOPEN\b", u):
            tp_open = True
            continue

        nums = re.findall(_PRICE_ANY, u)
        if not nums:
            continue

        # Fixes TP1(4513), TP2 18550, TP #3: 1.08000
        # but does NOT break TP 18500.
        if re.search(r"\bTP\s*#?\s*\d+\b", u) and len(nums) > 1:
            try:
                if float(nums[0]) <= 20:
                    nums = nums[1:]
            except Exception:
                pass

        if nums:
            v = float(nums[0])
            if v not in vals:
                vals.append(v)

    return vals, tp_open


def regex_extract(text: str) -> Optional[Dict[str, Any]]:
    raw = clean(text).replace("–", "-").replace("—", "-")
    up = raw.upper()

    if not looks_like_signal(up):
        return None

    direction = _direction_any(up)
    if not direction:
        return None

    symbol = normalize_symbol("", raw)
    entry = _entry_any(up)
    sl = _sl_any(up)
    tps, tp_open = _tps_any(raw)

    if entry is None or sl is None:
        return None
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
