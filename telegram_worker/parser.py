# telegram_bridge/parser.py
# Conservative regex-based trading signal parser.
# It only returns actionable signals with direction + symbol + entry + SL + TP1.

import re
from typing import Optional, Dict, List


SYMBOL_ALIASES = {
    "GOLD": "XAUUSD",
    "XAU": "XAUUSD",
    "XAUUSD": "XAUUSD",
    "XAU/USD": "XAUUSD",
    "NASDAQ": "NAS100",
    "NAS": "NAS100",
    "NAS100": "NAS100",
    "US100": "NAS100",
    "BTC": "BTCUSD",
    "BTCUSD": "BTCUSD",
    "BTC/USD": "BTCUSD",
    "ETH": "ETHUSD",
    "ETHUSD": "ETHUSD",
    "EURUSD": "EURUSD",
    "GBPUSD": "GBPUSD",
    "USDJPY": "USDJPY",
}

IGNORE_WORDS = [
    "BELOW",
    "ABOVE",
    "BIAS",
    "WATCH",
    "IDEA",
    "ANALYSIS",
]


def clean_text(text: str) -> str:
    text = text or ""
    text = text.replace("–", "-").replace("—", "-")
    text = text.replace("@", " ")
    text = re.sub(r"[✅🔥⚡️⚡🎯💎🚨📍🟢🔴]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def find_direction(text: str) -> Optional[str]:
    t = text.upper()
    if re.search(r"\b(BUY|BUYS|LONG)\b", t):
        return "BUY"
    if re.search(r"\b(SELL|SELLS|SHORT)\b", t):
        return "SELL"
    return None


def find_symbol(text: str) -> Optional[str]:
    t = text.upper().replace("/", "")
    for alias, sym in SYMBOL_ALIASES.items():
        a = alias.upper().replace("/", "")
        if re.search(rf"\b{re.escape(a)}\b", t):
            return sym

    # Infer XAUUSD if prices are gold-like.
    nums = [float(x) for x in re.findall(r"\b\d{3,5}(?:\.\d+)?\b", t)]
    if any(2500 <= n <= 6000 for n in nums):
        return "XAUUSD"

    return None


def extract_numbers_after(label_regex: str, text: str) -> List[float]:
    t = text.upper()
    pattern = label_regex + r"\s*[:\-]?\s*([0-9]{1,6}(?:\.\d+)?)"
    return [float(m.group(1)) for m in re.finditer(pattern, t)]


def find_sl(text: str) -> Optional[float]:
    vals = extract_numbers_after(r"\b(SL|STOP\s*LOSS|STOP)\b", text)
    return vals[0] if vals else None


def find_tps(text: str) -> List[float]:
    t = text.upper()
    tps = []

    for pat in [
        r"\bTP\s*1?\s*[:\-]?\s*([0-9]{1,6}(?:\.\d+)?)",
        r"\bTAKE\s*PROFIT\s*1?\s*[:\-]?\s*([0-9]{1,6}(?:\.\d+)?)",
        r"\bTARGET\s*1?\s*[:\-]?\s*([0-9]{1,6}(?:\.\d+)?)",
    ]:
        for m in re.finditer(pat, t):
            tps.append(float(m.group(1)))

    # Explicit TP1 TP2 TP3
    explicit = []
    for label in ["TP1", "TP2", "TP3", "TP4", "TP5"]:
        m = re.search(rf"\b{label}\s*[:\-]?\s*([0-9]{{1,6}}(?:\.\d+)?)", t)
        if m:
            explicit.append(float(m.group(1)))

    if explicit:
        return explicit[:3]

    # Lines like: TP: 4390 4395 4400
    m = re.search(r"\bTP[S]?\s*[:\-]\s*((?:[0-9]{1,6}(?:\.\d+)?\s*){1,5})", t)
    if m:
        nums = [float(x) for x in re.findall(r"[0-9]{1,6}(?:\.\d+)?", m.group(1))]
        if nums:
            return nums[:3]

    # Deduplicate, preserve order
    out = []
    for x in tps:
        if x not in out:
            out.append(x)
    return out[:3]


def find_entry_zone(text: str, direction: str, sl: Optional[float], tps: List[float]) -> Optional[tuple]:
    """
    Supports:
    BUY XAUUSD 4380-4381
    SELL GOLD @ 4380
    Entry 4380-4385
    BUY NOW 4380
    """
    t = text.upper()

    # Entry label
    m = re.search(r"\b(ENTRY|ENTRIES|ZONE)\s*[:\-]?\s*([0-9]{3,6}(?:\.\d+)?)\s*(?:-|TO)?\s*([0-9]{3,6}(?:\.\d+)?)?", t)
    if m:
        a = float(m.group(2))
        b = float(m.group(3)) if m.group(3) else a
        return (min(a, b), max(a, b))

    # Direction + symbol + price range
    m = re.search(r"\b(BUY|BUYS|SELL|SELLS|LONG|SHORT)\b.*?\b(?:XAUUSD|XAU|GOLD|NAS100|NAS|BTCUSD|BTC|EURUSD|GBPUSD|USDJPY)?\b\D{0,20}([0-9]{3,6}(?:\.\d+)?)\s*(?:-|TO)\s*([0-9]{3,6}(?:\.\d+)?)", t)
    if m:
        a = float(m.group(2))
        b = float(m.group(3))
        return (min(a, b), max(a, b))

    # Direction + single price
    m = re.search(r"\b(BUY|BUYS|SELL|SELLS|LONG|SHORT)\b.*?\b(?:XAUUSD|XAU|GOLD|NAS100|NAS|BTCUSD|BTC|EURUSD|GBPUSD|USDJPY)?\b\D{0,20}([0-9]{3,6}(?:\.\d+)?)", t)
    if m:
        p = float(m.group(2))

        # Avoid accidentally using SL or TP as entry.
        if sl and abs(p - sl) < 0.00001:
            return None
        if any(abs(p - tp) < 0.00001 for tp in tps):
            return None

        return (p, p)

    return None


def validate(direction: str, entry_low: float, entry_high: float, sl: float, tps: List[float]) -> Optional[str]:
    mid = (entry_low + entry_high) / 2
    tp1 = tps[0] if tps else None

    if tp1 is None:
        return "missing TP1"

    if direction == "BUY":
        if sl >= mid:
            return "BUY SL is not below entry"
        if tp1 <= mid:
            return "BUY TP1 is not above entry"

    if direction == "SELL":
        if sl <= mid:
            return "SELL SL is not above entry"
        if tp1 >= mid:
            return "SELL TP1 is not below entry"

    # reject huge zones for gold, but keep generous for indices/crypto
    if abs(entry_high - entry_low) > 500:
        return "entry zone too wide"

    return None


def parse_signal(text: str) -> Optional[Dict]:
    raw = text or ""
    text = clean_text(raw)
    if not text:
        return None

    direction = find_direction(text)
    if not direction:
        return None

    symbol = find_symbol(text)
    if not symbol:
        return None

    sl = find_sl(text)
    if sl is None:
        return None

    tps = find_tps(text)
    if not tps:
        return None

    entry = find_entry_zone(text, direction, sl, tps)
    if not entry:
        return None

    entry_low, entry_high = entry
    reason = validate(direction, entry_low, entry_high, sl, tps)
    if reason:
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "sl": sl,
        "tp1": tps[0],
        "tp2": tps[1] if len(tps) > 1 else None,
        "tp3": tps[2] if len(tps) > 2 else None,
    }


if __name__ == "__main__":
    samples = [
        "BUY NOW GOLD 4380-4381 TP1: 4390 TP2: 4395 TP3: 4400 SL: 4370",
        "SELL XAUUSD 4535 - 4538 SL 4550 TP1 4520 TP2 4500",
        "GOLD SELLS BELOW 4535 TO 4384",
    ]

    for s in samples:
        print("RAW:", s)
        print("PARSED:", parse_signal(s))
        print()
