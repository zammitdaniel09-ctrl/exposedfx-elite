import json
import os
import re
from typing import Any, Dict, Optional

import requests

from telegram_worker.signal_refiner import build_message, source_line

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-3-haiku-20240307").strip()
USE_CLAUDE = os.environ.get("USE_CLAUDE_SIGNAL_AI", "1").strip() == "1"
AUTO_TP_IF_MISSING = os.environ.get("AUTO_TP_IF_MISSING", "1").strip() == "1"

PRICE_RE = r"\d{1,7}(?:\.\d+)?"
FOREX_CURRENCIES = {"EUR", "USD", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF"}
IGNORE_SYMBOLS = {
    "ENTRY", "TARGET", "AROUND", "SIGNAL", "SHORTS", "LONGS", "SELLING",
    "BUYING", "UPDATE", "PROFIT", "LOSSES", "MANAGE", "RISKYY", "RISKED"
}


def clean(text: str) -> str:
    return (text or "").replace("\u200b", " ").replace("\xa0", " ").strip()


def normalize_symbol(symbol: str, text: str = "") -> str:
    raw = (text or "").upper()
    t = raw.replace("/", "").replace("-", "").replace(" ", "")
    s = (symbol or "").upper().replace("/", "").replace("-", "").replace(" ", "")

    if "NASDAQ100" in t or "NASDAQ" in raw or "NAS100" in t or "US100" in t or re.search(r"\bNAS\b", raw):
        return "NAS100"
    if "US30" in t or "DOW" in raw:
        return "US30"
    if "US500" in t or "SPX500" in t or "SP500" in t or "S&P" in raw:
        return "US500"
    if "GER40" in t or "DAX" in raw:
        return "GER40"
    if "UK100" in t:
        return "UK100"

    if "BTC" in t or "BITCOIN" in raw or s in {"BTC", "BTCUSD", "BTCUSDT"}:
        return "BTCUSD"
    if "ETH" in t or "ETHEREUM" in raw or s in {"ETH", "ETHUSD", "ETHUSDT"}:
        return "ETHUSD"
    if "SOL" in t or "SOLANA" in raw or s in {"SOL", "SOLUSD", "SOLUSDT"}:
        return "SOLUSD"
    if "XRP" in t:
        return "XRPUSD"

    if "XAU" in t or "GOLD" in raw or s in {"XAU", "XAUUSD", "GOLD"}:
        return "XAUUSD"
    if "XAG" in t or "SILVER" in raw or s in {"XAG", "XAGUSD", "SILVER"}:
        return "XAGUSD"

    for m in re.finditer(r"\b([A-Z]{6})\b", raw.replace("/", "")):
        pair = m.group(1)
        if pair in IGNORE_SYMBOLS:
            continue
        if pair[:3] in FOREX_CURRENCIES and pair[3:] in FOREX_CURRENCIES:
            return pair

    if s and s not in IGNORE_SYMBOLS:
        return s
    return "XAUUSD"


def symbol_family(symbol: str) -> str:
    s = (symbol or "").upper().replace("/", "")
    if s.startswith("XAU") or s == "GOLD":
        return "GOLD"
    if s.startswith("XAG") or s == "SILVER":
        return "METAL"
    if any(x in s for x in ("BTC", "ETH", "SOL", "XRP", "USDT")):
        return "CRYPTO"
    if any(x in s for x in ("NAS", "US100", "US30", "US500", "SPX", "SP500", "GER", "DAX", "UK100")):
        return "INDEX"
    if len(s) == 6 and s[:3] in FOREX_CURRENCIES and s[3:] in FOREX_CURRENCIES:
        return "FOREX"
    return "OTHER"


def looks_like_signal(text: str) -> bool:
    t = clean(text).upper()
    has_price = bool(re.search(rf"\b{PRICE_RE}\b", t))
    has_action = bool(re.search(r"\b(BUY|BUYS|BUYING|SELL|SELLS|SELLING|LONG|LONGS|SHORT|SHORTS|ENTERING|ENTRY|LIMIT)\b", t))
    has_trade_word = bool(re.search(r"\b(SL|S/L|STOP|STOPLOSS|STOP\s*LOSS|TP\s*#?\s*\d*|TARGET\s*#?\s*\d*|TAKE\s*PROFIT|ENTRY|ENTRIES|LIMIT|ZONE|RISK|RISKY)\b", t))
    has_symbol = bool(re.search(r"\b(XAUUSD|XAGUSD|GOLD|SILVER|BTC|ETH|SOL|NAS100|NASDAQ|US100|US30|US500|GER40|DAX|[A-Z]{6})\b", t))
    return has_price and (has_action or has_trade_word or has_symbol)


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
    if fam == "METAL":
        return "LOW" if pct <= 0.004 else "MEDIUM" if pct <= 0.010 else "HIGH"
    if fam == "CRYPTO":
        return "LOW" if pct <= 0.003 else "MEDIUM" if pct <= 0.008 else "HIGH"
    if fam == "INDEX":
        return "LOW" if pct <= 0.0025 else "MEDIUM" if pct <= 0.006 else "HIGH"
    if fam == "FOREX":
        return "LOW" if pct <= 0.0015 else "MEDIUM" if pct <= 0.005 else "HIGH"
    return "LOW" if pct <= 0.003 else "MEDIUM" if pct <= 0.007 else "HIGH"


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
    if fam == "FOREX":
        return max(risk_distance * 0.5, entry * 0.0008)
    if fam == "INDEX":
        return max(risk_distance * 0.5, entry * 0.0015)
    if fam == "GOLD":
        return max(risk_distance * 0.5, 5)
    if fam == "METAL":
        return max(risk_distance * 0.5, entry * 0.004)
    if fam == "CRYPTO":
        return max(risk_distance * 0.5, entry * 0.002)
    return max(risk_distance * 0.5, entry * 0.002)


def estimate_tps(symbol: str, direction: str, entry: float, sl: float, tps: list[float], tp_open: bool = False) -> list[float]:
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


def direction_from_text(text: str) -> Optional[str]:
    up = text.upper()
    if re.search(r"\b(BUY|BUYS|BUYING|LONG|LONGS)\b", up):
        return "BUY"
    if re.search(r"\b(SELL|SELLS|SELLING|SHORT|SHORTS)\b", up):
        return "SELL"
    return None


def extract_entry(text: str) -> Optional[tuple[float, float]]:
    up = text.upper().replace("–", "-").replace("—", "-")
    patterns = [
        rf"\b(?:ENTRY|ENTRIES|ENTER|ENTERING)\b(?:\s+(?:AROUND|AT|NOW))?\s*[:\-]?\s*({PRICE_RE})\s*(?:-|TO|/)\s*({PRICE_RE})",
        rf"\b(?:ENTRY|ENTRIES|ENTER|ENTERING)\b(?:\s+(?:AROUND|AT|NOW))?\s*[:\-]?\s*({PRICE_RE})",
        rf"\b(?:BUY|BUYS|BUYING|SELL|SELLS|SELLING|LONG|LONGS|SHORT|SHORTS)\b(?:\s+(?:NOW|LIMIT|STOP|ZONE|AT|AROUND))*\D{{0,100}}({PRICE_RE})\s*(?:-|TO|/)\s*({PRICE_RE})",
        rf"\b(?:BUY|BUYS|BUYING|SELL|SELLS|SELLING|LONG|LONGS|SHORT|SHORTS)\b(?:\s+(?:NOW|LIMIT|STOP|ZONE|AT|AROUND))*\D{{0,100}}({PRICE_RE})",
    ]
    for pat in patterns:
        m = re.search(pat, up)
        if m:
            a = float(m.group(1))
            b = float(m.group(2)) if len(m.groups()) > 1 and m.group(2) else a
            return min(a, b), max(a, b)
    return None


def extract_sl(text: str) -> Optional[float]:
    up = text.upper()
    vals = []
    patterns = [
        rf"\b(?:SL|S/L|STOP\s*LOSS|STOPLOSS|STOP)\b(?:\s+TO)?\s*[:\-]?\s*({PRICE_RE})",
        rf"\bSET\s+YOUR\s+STOP\s+LOSS\s+TO\s*({PRICE_RE})",
    ]
    for pat in patterns:
        vals.extend(float(x) for x in re.findall(pat, up))
    return vals[-1] if vals else None


def extract_tps(text: str) -> tuple[list[float], bool]:
    vals = []
    tp_open = False
    tp_line_re = re.compile(r"\b(?:TP\s*#?\s*\d*|TARGET\s*#?\s*\d*|TAKE\s*PROFIT\s*#?\s*\d*)\b", re.I)

    for line in clean(text).splitlines():
        u = line.upper().strip()
        if not tp_line_re.search(u):
            continue
        if re.search(r"\bOPEN\b", u):
            tp_open = True
            continue
        nums = re.findall(PRICE_RE, u)
        if not nums:
            continue
        # Use last number: TP1 1.08550 -> 1.08550, TP1(4513) -> 4513, TP 18500 -> 18500.
        v = float(nums[-1])
        if v not in vals:
            vals.append(v)
    return vals, tp_open


def regex_extract(text: str) -> Optional[Dict[str, Any]]:
    raw = clean(text)
    if not looks_like_signal(raw):
        return None

    direction = direction_from_text(raw)
    if not direction:
        return None

    symbol = normalize_symbol("", raw)
    entry = extract_entry(raw)
    sl = extract_sl(raw)
    tps, tp_open = extract_tps(raw)

    if entry is None or sl is None:
        return None
    if not tps and not tp_open:
        if AUTO_TP_IF_MISSING:
            tp_open = True
        else:
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
        "risk": risk_from_text(raw, symbol, mid, sl),
        "layer_point": estimate_layer(direction, entry[0], entry[1], sl),
    }


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

    system = (
        "Extract an actionable trading signal from messy Telegram text. Return JSON only. "
        "Accept any market: forex, metals, crypto, indices. "
        "A complete signal needs direction, entry, and stop loss. If no TP is provided, treat it as TP open so targets can be estimated. "
        "Use latest/current stop loss if several are shown. "
        "For TP open with no numbers, return tps=[] and tp_open=true. "
        "Return: is_signal, symbol, direction, entry_low, entry_high, sl, tps, tp_open, risk."
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
                "system": system,
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
        entry_low = float(obj["entry_low"])
        entry_high = float(obj.get("entry_high", entry_low))
        sl = float(obj["sl"])
        symbol = normalize_symbol(str(obj.get("symbol", "")), text)
        tps = []
        for item in obj.get("tps", []) or []:
            try:
                tps.append(float(item))
            except Exception:
                pass
        regex_tps, regex_open = extract_tps(text)
        if regex_tps:
            tps = regex_tps
        tp_open = bool(obj.get("tp_open", False)) or regex_open or "OPEN" in clean(text).upper()
        if not tps and not tp_open:
            if AUTO_TP_IF_MISSING:
                tp_open = True
            else:
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
