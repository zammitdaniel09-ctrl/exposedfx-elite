import html
import json
import os
import re
from typing import Optional, Dict, Any, List

import requests

from telegram_worker.parser import parse_signal

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-3-haiku-20240307").strip()
USE_CLAUDE = os.environ.get("USE_CLAUDE_SIGNAL_AI", "1").strip() == "1"

CUSTOM_EMOJIS = {
    "DIAMOND": os.environ.get("CUSTOM_EMOJI_DIAMOND", "5427168083074628963"),
    "RED_ALERT": os.environ.get("CUSTOM_EMOJI_RED_ALERT", "5411225014148014586"),
    "UPTREND_CHART": os.environ.get("CUSTOM_EMOJI_UPTREND_CHART", "5244837092042750681"),
    "DOWNTREND_CHART": os.environ.get("CUSTOM_EMOJI_DOWNTREND_CHART", "5246762912428603768"),
    "VERY_GOOD": os.environ.get("CUSTOM_EMOJI_VERY_GOOD", "5206607081334906820"),
    "RED_CROSS": os.environ.get("CUSTOM_EMOJI_RED_CROSS", "5210952531676504517"),
    "PIN_SIGNALS": os.environ.get("CUSTOM_EMOJI_PIN_SIGNALS", "5397782960512444700"),
    "EXCLAMATION_RED": os.environ.get("CUSTOM_EMOJI_EXCLAMATION_RED", "5274099962655816924"),
    "CAUTION_RED": os.environ.get("CUSTOM_EMOJI_CAUTION_RED", "5420323339723881652"),
}

EMOJI_FALLBACKS = {
    "DIAMOND": "💎",
    "RED_ALERT": "🔴",
    "UPTREND_CHART": "📈",
    "DOWNTREND_CHART": "📉",
    "VERY_GOOD": "✅",
    "RED_CROSS": "❌",
    "PIN_SIGNALS": "📌",
    "EXCLAMATION_RED": "❗️",
    "CAUTION_RED": "⚠️",
}


def ce(name: str) -> str:
    doc_id = str(CUSTOM_EMOJIS.get(name, "")).strip()
    emoji = EMOJI_FALLBACKS.get(name, "")
    if doc_id and doc_id.isdigit():
        return f'<tg-emoji emoji-id="{doc_id}">{emoji}</tg-emoji>'
    return emoji


def esc(value) -> str:
    return html.escape(str(value), quote=False)


def clean_text(text: str) -> str:
    return (text or "").replace("\u200b", "").strip()


def price(x) -> str:
    x = float(x)
    if abs(x - int(x)) < 0.00001:
        return str(int(x))
    return (f"{x:.2f}").rstrip("0").rstrip(".")


def compact_range(high, low) -> str:
    hi = price(high)
    lo = price(low)
    if "." in hi or "." in lo:
        return f"{hi}-{lo}"
    if len(hi) == len(lo) and hi[:-2] == lo[:-2]:
        return f"{hi}-{lo[-2:]}"
    return f"{hi}-{lo}"


def source_line(source_name: str, message_id) -> str:
    source_name = esc(source_name or "ExposedFX")
    suffix = f" #{esc(message_id)}" if message_id else ""
    return f"{ce('DIAMOND')} Source: {source_name}{suffix}"


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
    for key in ("tp1", "tp2", "tp3", "tp4", "tp5", "tp6", "tp7", "tp8", "tp9"):
        if parsed.get(key) is not None:
            values.append(float(parsed[key]))
    out = []
    for value in values:
        if value not in out:
            out.append(value)
    return out[:8]


def gold_risk(direction: str, entry_low: float, entry_high: float, sl: float) -> str:
    lo = float(entry_low)
    hi = float(entry_high)
    stop = float(sl)
    distance = max(abs(hi - stop), abs(lo - stop))
    if distance <= 8:
        return "LOW"
    if distance <= 15:
        return "MEDIUM"
    return "HIGH"


def risk_icon(risk: str) -> str:
    if risk == "LOW":
        return ce("VERY_GOOD")
    if risk == "MEDIUM":
        return ce("EXCLAMATION_RED")
    return ce("CAUTION_RED")


def local_parse(text: str) -> Optional[Dict[str, Any]]:
    parsed = parse_signal(text)
    if not parsed:
        return None
    tps = all_tps(text, parsed)
    if not tps:
        return None
    return {
        "is_signal": True,
        "symbol": parsed["symbol"],
        "direction": parsed["direction"],
        "entry_low": float(parsed["entry_low"]),
        "entry_high": float(parsed["entry_high"]),
        "sl": float(parsed["sl"]),
        "tps": tps,
    }


def claude_parse(text: str) -> Optional[Dict[str, Any]]:
    if not (USE_CLAUDE and ANTHROPIC_API_KEY):
        return None

    system = (
        "You extract actionable trading signals from messy Telegram messages. "
        "Return JSON only. If the message is not a trade signal, return {\"is_signal\":false}. "
        "A valid signal must include direction, symbol/instrument, entry or entry zone, stop loss, and at least one take profit. "
        "Normalize GOLD/XAU/XAUUSD to XAUUSD. Keep prices as numbers. "
        "For entry zones, set entry_low to the lower price and entry_high to the higher price. "
        "Return exactly these keys: is_signal, symbol, direction, entry_low, entry_high, sl, tps. "
        "direction must be BUY or SELL. tps must be an array of numeric take profits in order."
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
                "max_tokens": 500,
                "temperature": 0,
                "system": system,
                "messages": [{"role": "user", "content": clean_text(text)[:3500]}],
            },
            timeout=18,
        )
        if res.status_code >= 400:
            return None
        data = res.json()
        content = "".join(part.get("text", "") for part in data.get("content", []) if part.get("type") == "text")
        obj = json.loads(content)
        if not obj.get("is_signal"):
            return None
        direction = str(obj.get("direction", "")).upper()
        symbol = str(obj.get("symbol", "XAUUSD")).upper().replace("GOLD", "XAUUSD").replace("XAU/USD", "XAUUSD")
        entry_low = float(obj["entry_low"])
        entry_high = float(obj.get("entry_high", entry_low))
        sl = float(obj["sl"])
        tps = [float(x) for x in obj.get("tps", []) if x is not None][:8]
        if direction not in ("BUY", "SELL") or not tps:
            return None
        return {
            "is_signal": True,
            "symbol": symbol,
            "direction": direction,
            "entry_low": min(entry_low, entry_high),
            "entry_high": max(entry_low, entry_high),
            "sl": sl,
            "tps": tps,
        }
    except Exception:
        return None


def build_message(sig: Dict[str, Any]) -> str:
    direction = sig["direction"].upper()
    symbol = esc(sig.get("symbol", "XAUUSD").upper())
    lo = float(sig["entry_low"])
    hi = float(sig["entry_high"])
    sl = float(sig["sl"])
    tps = [float(x) for x in sig.get("tps", [])][:8]
    risk = gold_risk(direction, lo, hi, sl)

    if direction == "BUY":
        heading = f"{ce('UPTREND_CHART')}<b>BUY {symbol} INTRADAY ZONE</b>"
        entry_lines = [f"• Buy Point : {esc(price(hi))}"]
        entry_lines.append(f"• Layer Point : {esc(price(lo))}")
    else:
        heading = f"{ce('RED_ALERT')}<b>SELL {symbol} ZONE</b>"
        entry_lines = [f"• Sell Point : {esc(price(lo))}"]
        entry_lines.append(f"• Layer Point : {esc(price(hi))}")

    lines = [heading, "", *entry_lines, f"• Stop Loss : {esc(price(sl))}", ""]
    for idx, tp in enumerate(tps, 1):
        lines.append(f"{ce('PIN_SIGNALS')}TP{idx} - {esc(price(tp))}")
    lines.append(f"{ce('PIN_SIGNALS')}TP{len(tps) + 1} - Open")
    lines += [
        "",
        f"{risk_icon(risk)} RISK: {risk}",
        "",
        "TIPS:",
        f"Breakeven after TP1 HIT {ce('EXCLAMATION_RED')}",
        f"Use correct Risk management {ce('EXCLAMATION_RED')}",
        f"Take spread into consideration when placing SL{ce('EXCLAMATION_RED')}",
        f"THIS IS NOT FINANCIAL ADVICE {ce('CAUTION_RED')}",
    ]
    return "\n".join(lines)


def refine_signal(text: str, source_name: str = "ExposedFX", message_id=None) -> Optional[Dict[str, Any]]:
    text = clean_text(text)
    if not text:
        return None

    sig = claude_parse(text) or local_parse(text)
    if not sig:
        return None

    return {
        "is_signal": True,
        "message": build_message(sig),
        "source": source_line(source_name, message_id),
        "parsed": sig,
    }
