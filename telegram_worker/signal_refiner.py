import re
from typing import Optional, Dict, Any, List

from telegram_worker.parser import parse_signal


def clean_text(text: str) -> str:
    return (text or "").replace("\u200b", "").strip()


def price(x) -> str:
    x = float(x)
    if abs(x - int(x)) < 0.00001:
        return str(int(x))
    return (f"{x:.2f}").rstrip("0").rstrip(".")


def all_tps(text: str, parsed: Dict[str, Any]) -> List[float]:
    t = clean_text(text).upper().replace(",", " ")
    values = []
    patterns = [
        r"\bTP\s*(?:#?\s*\d+)?\s*[:\-]?\s*([0-9]{3,6}(?:\.\d+)?)",
        r"\bTARGET\s*(?:#?\s*\d+)?\s*[:\-]?\s*([0-9]{3,6}(?:\.\d+)?)",
    ]
    for pat in patterns:
        for m in re.finditer(pat, t):
            values.append(float(m.group(1)))
    for key in ("tp1", "tp2", "tp3"):
        if parsed.get(key) is not None:
            values.append(float(parsed[key]))
    out = []
    for value in values:
        if value not in out:
            out.append(value)
    return out[:5]


def source_line(source_name: str, message_id) -> str:
    source_name = source_name or "ExposedFX"
    suffix = f" #{message_id}" if message_id else ""
    return f"Source: {source_name}{suffix}"


def format_entry(entry_low: float, entry_high: float) -> str:
    lo = float(entry_low)
    hi = float(entry_high)
    if abs(lo - hi) < 0.00001:
        return price(lo)
    return f"{price(hi)}-{price(lo)}"


def refine_signal(text: str, source_name: str = "ExposedFX", message_id=None) -> Optional[Dict[str, Any]]:
    text = clean_text(text)
    if not text:
        return None

    parsed = parse_signal(text)
    if not parsed:
        return None

    direction = parsed["direction"]
    symbol = parsed["symbol"]
    entry = format_entry(parsed["entry_low"], parsed["entry_high"])
    sl = price(parsed["sl"])
    tps = all_tps(text, parsed)
    if not tps:
        return None

    lines = [
        f"💎 {direction} {symbol} ZONE",
        "",
        f"✅Entry point : {entry}",
        f"❌Stop Loss :{sl}",
        "",
    ]

    for idx, tp in enumerate(tps[:3], 1):
        lines.append(f"📍TP{idx} -{price(tp)}")

    open_idx = min(len(tps[:3]) + 1, 4)
    lines.append(f"📍TP{open_idx} - OPEN")

    lines += [
        "",
        "TIPS:",
        "Breakeven after TP1 HIT ❗",
        "Use correct Risk management ❗",
        "THIS IS NOT A FINANCIAL ADVICE ⚠️",
    ]

    return {
        "is_signal": True,
        "message": "\n".join(lines),
        "source": source_line(source_name, message_id),
        "parsed": parsed,
    }
