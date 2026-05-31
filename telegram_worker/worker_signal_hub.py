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
from telegram_worker.universal_signal_ai import extract_and_format, looks_like_signal

log = logging.getLogger("exposedfx-ai-signal-formatter")


def chat_id_from_env(name, default):
    raw = os.environ.get(name, default).strip()
    if raw.startswith("http") and "#" in raw:
        raw = raw.split("#", 1)[1].split("_", 1)[0]
    raw = raw.replace("/", "").strip()
    return int(raw)


def topic_set_from_env(name, default):
    raw = os.environ.get(name, default).strip()
    out = set()
    for part in re.split(r"[,\s]+", raw):
        part = part.strip()
        if not part:
            continue
        if part.startswith("http") and "_" in part:
            part = part.rsplit("_", 1)[-1]
        part = part.replace("/", "")
        try:
            out.add(int(part))
        except ValueError:
            pass
    return out


SIGNAL_SOURCE_CHAT = chat_id_from_env("SIGNAL_SOURCE_CHAT", "-1003918958200")
SIGNAL_DEST_CHAT = chat_id_from_env("SIGNAL_DEST_CHAT", "-5252460120")
SEND_SOURCE_LINE = os.environ.get("SEND_SOURCE_LINE", "1").strip() == "1"
DROP_LINK_ONLY = os.environ.get("DROP_LINK_ONLY", "1").strip() == "1"
LINK_ONLY_RE = re.compile(r"^(?:https?://|t\.me/|www\.)\S+$", re.IGNORECASE)

FORWARD_SIGNAL_CANDIDATES = os.environ.get("FORWARD_SIGNAL_CANDIDATES", "1").strip() == "1"
PARTIAL_BUFFER_ENABLED = os.environ.get("PARTIAL_SIGNAL_BUFFER", "1").strip() == "1"
BUFFER_WINDOW_SECONDS = int(os.environ.get("SIGNAL_BUFFER_SECONDS", "600"))
BUFFER_MAX_MESSAGES = int(os.environ.get("SIGNAL_BUFFER_MAX_MESSAGES", "8"))
DEFAULT_ALLOWED_TOPICS = "23,430,28,2,31,35,11,363,25,29,33,20,362,17,34,22,9,36,10,14,4,16,13,12,8,7,6,5,3"
ALLOWED_SOURCE_TOPICS = topic_set_from_env("ALLOWED_SOURCE_TOPICS", DEFAULT_ALLOWED_TOPICS)

buffers = defaultdict(lambda: deque(maxlen=BUFFER_MAX_MESSAGES))
sent_signatures = deque(maxlen=300)
sent_signature_set = set()

TOPIC_NAMES = {
    2: "Triad FX",
    3: "Topic 3",
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
    16: "ICT Trader",
    17: "1% VIP SIGNALS",
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


def source_channel_id():
    text = str(SIGNAL_SOURCE_CHAT)
    if text.startswith("-100"):
        return int(text[4:])
    return abs(SIGNAL_SOURCE_CHAT)


def event_is_from_source(event):
    cid = getattr(event, "chat_id", None)
    if cid == SIGNAL_SOURCE_CHAT:
        return True
    channel_id = source_channel_id()
    msg = getattr(event, "message", None)
    peer = getattr(msg, "peer_id", None)
    if getattr(peer, "channel_id", None) == channel_id:
        return True
    if getattr(peer, "chat_id", None) == abs(SIGNAL_SOURCE_CHAT):
        return True
    if cid == channel_id:
        return True
    return False


def message_text(message):
    return message.message or message.raw_text or message.text or ""


def topic_id_of(message):
    direct_top = getattr(message, "reply_to_top_id", None)
    if direct_top:
        return int(direct_top)
    direct_msg = getattr(message, "reply_to_msg_id", None)
    if direct_msg and getattr(message, "is_topic_message", False):
        return int(direct_msg)
    reply = getattr(message, "reply_to", None)
    if not reply:
        return None
    top_id = getattr(reply, "reply_to_top_id", None)
    if top_id:
        return int(top_id)
    msg_id = getattr(reply, "reply_to_msg_id", None)
    if msg_id:
        return int(msg_id)
    return None


def topic_label(topic_id):
    if topic_id is None:
        return "Main Chat"
    return TOPIC_NAMES.get(int(topic_id), f"Topic {topic_id}")


def should_skip(message):
    text = message_text(message).strip()
    if not text:
        return True
    topic_id = topic_id_of(message)
    if topic_id is not None and ALLOWED_SOURCE_TOPICS and int(topic_id) not in ALLOWED_SOURCE_TOPICS:
        log.info(f"[signal hub skipped] topic not in whitelist topic={topic_id}")
        return True
    if DROP_LINK_ONLY and LINK_ONLY_RE.match(text):
        log.info("[signal hub skipped] plain link")
        return True
    return False


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


def first_buffer_message(key):
    trim_buffer(key)
    if buffers[key]:
        return buffers[key][0].get("message")
    return None


def remember_signature(sig):
    if sig in sent_signature_set:
        return False
    sent_signature_set.add(sig)
    sent_signatures.append(sig)
    while len(sent_signature_set) > len(sent_signatures):
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
        ",".join(str(x) for x in parsed.get("tps", [])[:8]) if isinstance(parsed.get("tps"), list) else "",
    ])
    if base.count("|") < 5:
        base = result.get("message", "")
    return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()


async def forward_original(message, text):
    if not FORWARD_SIGNAL_CANDIDATES:
        return False
    try:
        await client.forward_messages(SIGNAL_DEST_CHAT, message)
        log.info(f"[signal hub original forwarded] msg={message.id} topic={topic_id_of(message)}")
        return True
    except Exception as exc:
        log.warning(f"Forward original failed, sending text copy instead: {exc}")
        await client.send_message(SIGNAL_DEST_CHAT, text, parse_mode=None, link_preview=False)
        return True


async def send_full_signal(message, result, key, original_text, forward_raw=True):
    # One signature controls the whole packet: original forward + AI format + source.
    # This prevents duplicate original forwards when Telegram emits the same signal twice
    # or when partial-buffer parsing also resolves the same setup.
    sig = signature_for(result, key)
    if not remember_signature(sig):
        log.info("[signal hub skipped] duplicate signal packet")
        return False

    try:
        if forward_raw:
            await forward_original(message, original_text)
        await client.send_message(SIGNAL_DEST_CHAT, result["message"], parse_mode="html", link_preview=False)
        if SEND_SOURCE_LINE:
            await client.send_message(SIGNAL_DEST_CHAT, result["source"], parse_mode="html", link_preview=False)
    except Exception as exc:
        log.exception(f"[signal hub packet send failed] {exc}")
        return False

    buffers[key].clear()
    log.info(f"[signal hub sent] source_msg={message.id} topic={topic_id_of(message)} -> {SIGNAL_DEST_CHAT}")
    return True


@client.on(events.NewMessage())
@client.on(events.MessageEdited())
async def on_signal_hub_message(event):
    try:
        if not event_is_from_source(event):
            return

        message = event.message
        if should_skip(message):
            return

        text = message_text(message).strip()
        key = buffer_key(message)
        source_name = source_name_for(message)
        log.info(f"[signal hub seen] msg={message.id} topic={topic_id_of(message)} text={text[:80]}")

        result = extract_and_format(text, source_name, message.id)
        if result:
            await send_full_signal(message, result, key, text)
            return

        if not PARTIAL_BUFFER_ENABLED:
            log.info("[signal hub skipped] not a clean signal")
            return

        if looks_like_signal(text):
            trim_buffer(key)
            first_piece = len(buffers[key]) == 0
            buffers[key].append({"ts": time.time(), "id": message.id, "text": text, "message": message})
            if first_piece:
                await forward_original(message, text)
        else:
            log.info("[signal hub skipped] not signal-like")
            return

        combined = combined_text_for(key)
        result = extract_and_format(combined, source_name, message.id)
        if result:
            raw_msg = first_buffer_message(key) or message
            await send_full_signal(raw_msg, result, key, message_text(raw_msg).strip(), forward_raw=False)
            return

        log.info(f"[signal hub waiting] partial message stored key={key} topic={topic_id_of(message)} size={len(buffers[key])}")
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
    log.info(f"Signal hub source: {SIGNAL_SOURCE_CHAT} | source channel id: {source_channel_id()}")
    log.info(f"Signal hub destination: {SIGNAL_DEST_CHAT}")
    log.info(f"Allowed topics: {sorted(ALLOWED_SOURCE_TOPICS)}")
    log.info(f"Forward original only after confirmed signal: {FORWARD_SIGNAL_CANDIDATES}")
    log.info(f"Universal AI extractor active for any pair")
    log.info(f"Partial signal buffer: {PARTIAL_BUFFER_ENABLED} | window={BUFFER_WINDOW_SECONDS}s | max={BUFFER_MAX_MESSAGES}")
    await admin_startup(client)
    asyncio.create_task(admin_loop(client, stats))
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
