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



def is_trade_management_update(text: str) -> bool:
    raw = clean(text)
    t = raw.upper()
    low = raw.lower()

    management_words = [
        "partial",
        "take further",
        "take a partial",
        "running trade",
        "remainder should run",
        "set stop loss",
        "move stop loss",
        "move sl",
        "sl to",
        "stop loss to",
        "click close",
        "edit the lot size",
        "close half",
        "close 50",
        "secure profits",
        "book profits",
        "being cautious",
        "just looking after us",
    ]

    recap_words = [
        "pips",
        "ended with",
        "we ended",
        "weekly recap",
        "daily recap",
        "results",
        "profit today",
        "pips secured",
        "pips banked",
    ]

    has_management = any(x in low for x in management_words)
    has_recap = any(x in low for x in recap_words)

    has_direction = bool(re.search(r"\b(BUY|BUYS|BUYING|SELL|SELLS|SELLING|LONG|LONGS|SHORT|SHORTS)\b", t))
    has_entry = bool(re.search(r"\b(ENTRY|ENTRIES|ENTER|ENTERING|BUY\s+LIMIT|SELL\s+LIMIT|BUY\s+ZONE|SELL\s+ZONE|BUY\s+NOW|SELL\s+NOW)\b", t))
    has_sl = bool(re.search(r"\b(SL|S/L|STOP\s*LOSS|STOPLOSS)\b\s*(?:TO)?\s*[:\-]?\s*\d", t))

    # Block updates/recaps unless they clearly contain a fresh setup.
    if (has_management or has_recap) and not (has_direction and has_entry and has_sl):
        return True

    return False


def has_strict_new_signal_requirements(text: str) -> bool:
    raw = clean(text)
    t = raw.upper()

    if is_trade_management_update(raw):
        return False

    has_direction = bool(re.search(r"\b(BUY|BUYS|BUYING|SELL|SELLS|SELLING|LONG|LONGS|SHORT|SHORTS)\b", t))

    has_entry = bool(
        re.search(r"\b(ENTRY|ENTRIES|ENTER|ENTERING)\b[\s\S]{0,80}\d", t)
        or re.search(r"\b(BUY|BUYS|SELL|SELLS)\s+(?:LIMIT|ZONE|NOW)?\b[\s\S]{0,80}\d", t)
        or re.search(r"\b(LONG|LONGS|SHORT|SHORTS)\b[\s\S]{0,80}\d", t)
    )

    has_sl = bool(re.search(r"\b(SL|S/L|STOP\s*LOSS|STOPLOSS)\b\s*(?:TO)?\s*[:\-]?\s*\d", t))

    return has_direction and has_entry and has_sl



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
    if not has_strict_new_signal_requirements(text):
        return False
    t = clean(text).upper()
    has_price = bool(re.search(rf"\b{PRICE_RE}\b", t))
    has_action = bool(re.search(r"\b(BUY|BUYS|BUYING|SELL|SELLS|SELLING|LONG|LONGS|SHORT|SHORTS|ENTERING|ENTRY|LIMIT)\b", t))
    has_trade_word = bool(re.search(r"\b(SL|S/L|STOP|STOPLOSS|STOP\s*LOSS|TP\s*#?\s*\d*|TARGET\s*#?\s*\d*|TAKE\s*PROFIT|ENTRY|ENTRIES|LIMIT|ZONE|RISK|RISKY)\b", t))
    has_symbol = bool(re.search(r"\b(XAUUSD|XAGUSD|GOLD|SILVER|BTC|ETH|SOL|NAS100|NASDAQ|US100|US30|US500|GER40|DAX|[A-Z]{6})\b", t))
    return has_price and (has_action or has_trade_word or has_symbol)


def forex_pip_size(symbol: str) -> float:
    s = (symbol or "").upper().replace("/", "")
    return 0.01 if s.endswith("JPY") else 0.0001


def forex_risk_thresholds(symbol: str) -> tuple[float, float]:
    """Return tight/medium limits in pips for each FX pair style."""
    s = (symbol or "").upper().replace("/", "")
    if s.endswith("JPY"):
        if s in {"GBPJPY", "EURJPY"}:
            return 35, 80
        return 25, 60
    if s.startswith("GBP") or s.endswith("GBP"):
        return 20, 55
    if s in {"EURUSD", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD", "EURGBP", "AUDCAD", "AUDNZD"}:
        return 15, 40
    return 20, 50


def percentage_risk_thresholds(symbol: str) -> tuple[float, float]:
    """Return tight/medium limits as decimals, e.g. 0.003 = 0.30%."""
    s = (symbol or "").upper().replace("/", "")
    fam = symbol_family(s)

    if s.startswith("BTC"):
        return 0.0035, 0.0090
    if s.startswith("ETH"):
        return 0.0045, 0.0120
    if s.startswith("SOL"):
        return 0.0075, 0.0180
    if s.startswith("XRP"):
        return 0.0100, 0.0250

    if s in {"NAS100", "US100"}:
        return 0.0025, 0.0065
    if s == "US30":
        return 0.0020, 0.0055
    if s in {"US500", "SPX500", "SP500"}:
        return 0.0020, 0.0050
    if s in {"GER40", "DAX", "DAX40"}:
        return 0.0025, 0.0060

    if s.startswith("XAG"):
        return 0.0040, 0.0100

    if fam == "CRYPTO":
        return 0.0040, 0.0100
    if fam == "INDEX":
        return 0.0025, 0.0060
    if fam == "METAL":
        return 0.0040, 0.0100
    return 0.0030, 0.0070


def dynamic_risk(symbol: str, entry: float, sl: float) -> str:
    """
    Inverted practical signal risk:
    tighter stop = HIGH risk because normal noise can hit SL easier;
    wider stop = LOW risk because the setup has more breathing room.
    """
    symbol = (symbol or "").upper().replace("/", "")
    entry = float(entry)
    sl = float(sl)
    distance = abs(entry - sl)
    pct = distance / entry if entry else 999
    fam = symbol_family(symbol)

    if fam == "GOLD":
        if distance <= 6:
            return "HIGH"
        if distance <= 15:
            return "MEDIUM"
        return "LOW"

    if fam == "FOREX":
        pip_size = forex_pip_size(symbol)
        pips = distance / pip_size
        tight_pips, medium_pips = forex_risk_thresholds(symbol)
        if pips <= tight_pips:
            return "HIGH"
        if pips <= medium_pips:
            return "MEDIUM"
        return "LOW"

    tight_pct, medium_pct = percentage_risk_thresholds(symbol)
    if pct <= tight_pct:
        return "HIGH"
    if pct <= medium_pct:
        return "MEDIUM"
    return "LOW"



def xau_pip_value() -> float:
    # Gold rule: 10 pips = $1, so 1 pip = $0.10
    return 0.1


def pip_value_for_symbol(symbol: str) -> float:
    fam = symbol_family(symbol)
    s = (symbol or "").upper().replace("/", "")

    if fam == "GOLD":
        return xau_pip_value()
    if fam == "FOREX":
        return forex_pip_size(s)
    if fam == "INDEX":
        return 1.0
    if fam == "CRYPTO":
        return 1.0
    if fam == "METAL":
        return 0.01
    return 1.0


def expand_shorthand_price(first: float, second_text: str) -> float:
    """
    Expands shorthand ranges:
    4498-96 -> 4498-4496
    4492-89 -> 4492-4489
    4502-00 -> 4502-4500
    """
    raw = str(second_text).strip()

    if "." in raw:
        return float(raw)

    try:
        second = float(raw)
    except Exception:
        return float(first)

    if abs(first) >= 1000 and 0 <= second < 100:
        first_int = str(int(abs(first)))
        digits = raw.zfill(len(raw))
        prefix = first_int[:-len(digits)]
        candidate = float(prefix + digits)

        # Handles rollover cases like 4502-99 -> 4499
        if candidate > first and (candidate - first) > 50:
            candidate -= 100

        return candidate

    return second


def convert_distance_sl_if_needed(symbol: str, direction: str, entry_mid: float, sl_value: float, text: str) -> float:
    """
    Converts distance-style SL into chart price.
    Example: Gold buy 4496 / SL 40pip -> SL 4492
    """
    raw = clean(text)
    up = raw.upper()

    has_sl_distance_word = bool(
        re.search(r"\b(?:SL|S/L|STOP\s*LOSS|STOPLOSS|STOP)\b\s*[:\-]?\s*\d+(?:\.\d+)?\s*(?:PIP|PIPS|POINT|POINTS)\b", up)
        or re.search(r"\b\d+(?:\.\d+)?\s*(?:PIP|PIPS|POINT|POINTS)\b", up)
    )

    if not has_sl_distance_word:
        return float(sl_value)

    entry_mid = float(entry_mid)
    sl_value = float(sl_value)

    # If already a real chart price close to entry, leave it.
    if entry_mid and abs(sl_value - entry_mid) / entry_mid < 0.10:
        return sl_value

    distance = sl_value * pip_value_for_symbol(symbol)

    if direction == "BUY":
        return entry_mid - distance
    return entry_mid + distance


def invalid_tp_for_direction(direction: str, entry: float, tp: float) -> bool:
    if direction == "BUY":
        return tp <= entry
    return tp >= entry


def risk_from_text(text: str, symbol: str, entry: float, sl: float) -> str:
    t = (text or "").upper()
    if "RISKY" in t or "HIGHER RISK" in t or "HIGH RISK" in t or "VERY HIGH" in t:
        return "HIGH"
    if "MEDIUM RISK" in t:
        return "MEDIUM"
    if "LOW RISK" in t:
        return "LOW"
    return dynamic_risk(symbol, entry, sl)


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
    raw = clean(text)
    up = raw.upper().replace("–", "-").replace("—", "-")

    patterns = [
        # Entry: 4498-96 / Entry 4498 to 4496
        rf"\b(?:ENTRY|ENTRIES|ENTER|ENTERING)\b(?:\s+(?:AROUND|AT|NOW))?\s*[:\-]?\s*({PRICE_RE})\s*(?:-|TO|/)\s*({PRICE_RE})",
        rf"\b(?:ENTRY|ENTRIES|ENTER|ENTERING)\b(?:\s+(?:AROUND|AT|NOW))?\s*[:\-]?\s*({PRICE_RE})",

        # Buy/Sell same-line entries only. Do NOT cross into SL/TP lines.
        rf"\b(?:BUY|BUYS|BUYING|SELL|SELLS|SELLING|LONG|LONGS|SHORT|SHORTS)\b(?:\s+(?:NOW|LIMIT|STOP|ZONE|AT|AROUND))*[^\n]{0,80}?({PRICE_RE})\s*(?:-|TO|/)\s*({PRICE_RE})",
        rf"\b(?:BUY|BUYS|BUYING|SELL|SELLS|SELLING|LONG|LONGS|SHORT|SHORTS)\b(?:\s+(?:NOW|LIMIT|STOP|ZONE|AT|AROUND))*[^\n]{0,80}?({PRICE_RE})",
    ]

    for pat in patterns:
        m = re.search(pat, up)
        if not m:
            continue

        a = float(m.group(1))
        if len(m.groups()) > 1 and m.group(2):
            b = expand_shorthand_price(a, m.group(2))
        else:
            b = a

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
        v = float(nums[-1])
        if v not in vals:
            vals.append(v)
    return vals, tp_open


def regex_extract(text: str) -> Optional[Dict[str, Any]]:
    raw = clean(text)
    if not has_strict_new_signal_requirements(raw):
        return None
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
    sl = convert_distance_sl_if_needed(symbol, direction, mid, sl, raw)

    # Remove impossible TPs that are on the wrong side.
    tps = [tp for tp in tps if not invalid_tp_for_direction(direction, mid, float(tp))]

    if not tps and not tp_open:
        if AUTO_TP_IF_MISSING:
            tp_open = True
        else:
            return None

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
    if not has_strict_new_signal_requirements(text):
        return None
    if not (USE_CLAUDE and ANTHROPIC_API_KEY):
        return None

    system = (
        "Extract an actionable trading signal from messy Telegram text. Return JSON only. "
        "Accept any market: forex, metals, crypto, indices. "
        "A complete signal needs direction, entry, and stop loss. If no TP is provided, treat it as TP open so targets can be estimated. If stop loss is written as pip distance, keep the numeric pip distance and the system will convert it. For XAUUSD/GOLD, 10 pips equals 1 dollar, so 40 pips equals 4 dollars. Expand shorthand ranges such as 4498-96 as 4498 to 4496, not 4498 to 96. Do not use SL or TP prices as entry when entry is missing. "
        "Use latest/current stop loss if several are shown. "
        "For TP open with no numbers, return tps=[] and tp_open=true. "
        "Do not decide risk unless the text explicitly says low risk, medium risk, high risk, higher risk, risky, or very high. Otherwise leave risk empty so the system calculates risk dynamically by pair. "
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
        sl = convert_distance_sl_if_needed(symbol, direction, mid, sl, text)
        tps = [tp for tp in tps if not invalid_tp_for_direction(direction, mid, float(tp))]
        explicit_risk = str(obj.get("risk", "")).upper()
        if explicit_risk in ("LOW", "MEDIUM", "HIGH") and any(x in clean(text).upper() for x in ("LOW RISK", "MEDIUM RISK", "HIGH RISK", "HIGHER RISK", "VERY HIGH", "RISKY")):
            risk = explicit_risk
        else:
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


def extract_and_format(text: str, source_name: str = "ExposedFX", message_id=None) -> Optional[Dict[str, Any]]:
    if not has_strict_new_signal_requirements(text):
        return None
    sig = regex_extract(text) or claude_extract(text)
    if not sig:
        return None
    return {
        "is_signal": True,
        "message": build_message(sig),
        "source": source_line(source_name, message_id),
        "parsed": sig,
    }
