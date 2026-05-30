import asyncio
import hashlib
import logging
import os
import re
import time
from collections import defaultdict, deque

from telethon import events

from telegram_worker.worker_fixed import client, stats
from telegram_worker.admin_features import ADMIN_CHAT, admin_startup, admin_loop, handle_admin_command
from telegram_worker.signal_refiner import refine_signal

log = logging.getLogger("exposedfx-ai-signal-formatter")


def chat_id_from_env(name, default):
    raw = os.environ.get(name, default).strip()
    if raw.startswith("http") and "#" in raw:
        raw = raw.split("#", 1)[1].split("_", 1)[0]
    raw = raw.replace("/", "").strip()
    return int(raw)


SIGNAL_SOURCE_CHAT = chat_id_from_env("SIGNAL_SOURCE_CHAT", "-1003918958200")
SIGNAL_DEST_CHAT = chat_id_from_env("SIGNAL_DEST_CHAT", "-5252460120")
SEND_SOURCE_LINE = os.environ.get("SEND_SOURCE_LINE", "1").strip() == "1"
DROP_LINK_ONLY = os.environ.get("DROP_LINK_ONLY", "1").strip() == "1"
LINK_ONLY_RE = re.compile(r"^(?:https?://|t\.me/|www\.)\S+$", re.IGNORECASE)

PARTIAL_BUFFER_ENABLED = os.environ.get("PARTIAL_SIGNAL_BUFFER", "1").strip() == "1"
BUFFER_WINDOW_SECONDS = int(os.environ.get("SIGNAL_BUFFER_SECONDS", "600"))
BUFFER_MAX_MESSAGES = int(os.environ.get("SIGNAL_BUFFER_MAX_MESSAGES", "8"))

buffers = defaultdict(lambda: deque(maxlen=BUFFER_MAX_MESSAGES))
sent_signatures = deque(maxlen=300)
sent_signature_set = set()

TOPIC_NAMES = {
    2: "Triad FX",
    4: "Gold Trader Sunny",
    5: "NS Trades",
    6: "Platinum Intro Channel",
    7: "TGF Montana",
    8: "BroadFX",
    9: "T Marz",
    10: "GTMO VIP",
    11: "Trading Central FX VIP",
    12: "SOL Gibbs",
    13: "Sniper Pro Academy",
    14: "McGarry and Gunter VIP",
    15: "Olly Matthews",
    16: "ICT Trader",
    17: "1% VIP SIGNALS",
    18: "LIFETIME VIP",
    20: "Master Premium",
    22: "Dropout VIP",
    23: "Market Slayers VIP",
    25: "Premium I Live Trade",
    28: "KEY / ALCHEMIST",
    29: "A4xXAUr PREMIUM",
    31: "MANJOX TRADES",
    33: "GotMeKayed",
    34: "MSC Premium",
    35: "BOLZEGHA VIP",
    36: "ELX Premium",
    362: "Route 362",
    363: "R363 Signals",
    430: "Route 430",
}


def message_text(message):
    return message.message or message.raw_text or message.text or ""


def should_skip(message):
    text = message_text(message).strip()
    if not text:
        return True
    if DROP_LINK_ONLY and LINK_ONLY_RE.match(text):
        log.info("[signal hub skipped] plain link")
        return True
    return False


def topic_id_of(message):
    reply = getattr(message, "reply_to", None)
    if not reply:
        return None
    return getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None)


def topic_label(topic_id):
    if topic_id is None:
        return "Main Chat"
    return TOPIC_NAMES.get(int(topic_id), f"Topic {topic_id}")


def source_name_for(message):
    topic_id = topic_id_of(message)
    return f"ExposedFX | {topic_label(topic_id)}"


def buffer_key(message):
    topic_id = topic_id_of(message)
    return str(topic_id or "main")


def trim_buffer(key):
    now = time.time()
    dq = buffers[key]
    while dq and now - dq[0]["ts"] > BUFFER_WINDOW_SECONDS:
        dq.popleft()


def combined_text_for(key):
    trim_buffer(key)
    return "\n".join(item["text"] for item in buffers[key] if item["text"].strip())


def remember_signature(sig):
    if sig in sent_signature_set:
        return False
    sent_signature_set.add(sig)
    sent_signatures.append(sig)
    while len(sent_signature_set) > len(sent_signatures):
        # Keep the set roughly aligned if the deque discards old signatures.
        sent_signature_set.clear()
        sent_signature_set.update(sent_signatures)
    return True


def signature_for(result, key):
    parsed = result.get("parsed") or {}
    base = "|".join([
        str(key),
        str(parsed.get("symbol", "")),
        str(parsed.get("direction", "")),
        str(parsed.get("entry_low", "")),
        str(parsed.get("entry_high", "")),
        str(parsed.get("sl", "")),
        ",".join(str(x) for x in parsed.get("tps", [])[:5]) if isinstance(parsed.get("tps"), list) else "",
    ])
    if base.count("|") < 5:
        base = result.get("message", "")
    return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()


async def send_result(message, result, key):
    sig = signature_for(result, key)
    if not remember_signature(sig):
        log.info("[signal hub skipped] duplicate formatted signal")
        return False

    await client.send_message(SIGNAL_DEST_CHAT, result["message"], parse_mode="html", link_preview=False)
    if SEND_SOURCE_LINE:
        await client.send_message(SIGNAL_DEST_CHAT, result["source"], parse_mode="html", link_preview=False)

    buffers[key].clear()
    log.info(f"[signal hub sent] source_msg={message.id} topic={topic_id_of(message)} -> {SIGNAL_DEST_CHAT}")
    return True


@client.on(events.NewMessage(chats=SIGNAL_SOURCE_CHAT))
async def on_signal_hub_message(event):
    try:
        message = event.message
        if should_skip(message):
            return

        text = message_text(message).strip()
        key = buffer_key(message)
        source_name = source_name_for(message)

        # First try the current message alone, so complete signals are instant.
        result = refine_signal(text, source_name, message.id)
        if result:
            await send_result(message, result, key)
            return

        if not PARTIAL_BUFFER_ENABLED:
            log.info("[signal hub skipped] not a clean signal")
            return

        # Store partial messages per topic. If a provider sends entry first, then SL/TP later,
        # the combined text becomes a valid signal and one clean AI signal is sent.
        buffers[key].append({"ts": time.time(), "id": message.id, "text": text})
        combined = combined_text_for(key)
        result = refine_signal(combined, source_name, message.id)
        if result:
            await send_result(message, result, key)
            return

        log.info(f"[signal hub waiting] partial message stored key={key} size={len(buffers[key])}")
    except Exception as exc:
        log.exception(f"[signal hub failed] {exc}")


@client.on(events.NewMessage(chats=ADMIN_CHAT))
async def on_admin_message(event):
    try:
        await handle_admin_command(event, client, stats)
    except Exception as exc:
        log.error(f"[admin command failed] {exc}")


async def main():
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram session loaded but account is not authorised. Regenerate session chunks.")

    me = await client.get_me()
    log.info(f"Logged in as {me.first_name} | id={me.id}")
    log.info(f"Signal hub source: {SIGNAL_SOURCE_CHAT}")
    log.info(f"Signal hub destination: {SIGNAL_DEST_CHAT}")
    log.info(f"Partial signal buffer: {PARTIAL_BUFFER_ENABLED} | window={BUFFER_WINDOW_SECONDS}s | max={BUFFER_MAX_MESSAGES}")
    await admin_startup(client)
    asyncio.create_task(admin_loop(client, stats))
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
