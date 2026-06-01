import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from pathlib import Path

from telethon import events

from telegram_worker.worker_fixed import client, stats
from telegram_worker.admin_features import ADMIN_CHAT, admin_startup, admin_loop, handle_admin_command
from telegram_worker.universal_signal_ai import extract_and_format, looks_like_signal

log = logging.getLogger("exposedfx-ai-signal-formatter")

PRICE_RE = r"\d{1,7}(?:\.\d+)?"


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

DATA_DIR = Path(os.environ.get("DATA_DIR") or "./data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
SIGNAL_PACKET_MAP_FILE = DATA_DIR / "signal_packet_map.json"
DELETE_OLD_SIGNAL_PACKET_ON_EDIT = os.environ.get("DELETE_OLD_SIGNAL_PACKET_ON_EDIT", "1").strip() == "1"
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



def load_packet_map():
    if not SIGNAL_PACKET_MAP_FILE.exists():
        return {}
    try:
        return json.loads(SIGNAL_PACKET_MAP_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_packet_map():
    try:
        tmp = SIGNAL_PACKET_MAP_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(signal_packet_map), encoding="utf-8")
        tmp.replace(SIGNAL_PACKET_MAP_FILE)
    except Exception as exc:
        log.warning(f"[signal packet map save failed] {exc}")


def signal_packet_key(message, key):
    return f"{SIGNAL_SOURCE_CHAT}:{key}:{getattr(message, 'id', None)}"


async def delete_existing_signal_packet(message, key):
    if not DELETE_OLD_SIGNAL_PACKET_ON_EDIT:
        return False

    pkey = signal_packet_key(message, key)
    old_ids = signal_packet_map.get(pkey) or []

    if not old_ids:
        return False

    try:
        ids = [int(x) for x in old_ids if x]
        if ids:
            await client.delete_messages(SIGNAL_DEST_CHAT, ids)
            log.info(f"[signal packet cleanup] deleted old packet source_msg={message.id} dest_ids={ids}")
        signal_packet_map.pop(pkey, None)
        save_packet_map()
        return True
    except Exception as exc:
        log.warning(f"[signal packet cleanup failed] source_msg={message.id} ids={old_ids}: {exc}")
        return False


def remember_signal_packet(message, key, sent_messages):
    pkey = signal_packet_key(message, key)
    ids = []

    for msg in sent_messages:
        mid = getattr(msg, "id", None)
        if mid:
            ids.append(int(mid))

    if ids:
        signal_packet_map[pkey] = ids
        save_packet_map()
        log.info(f"[signal packet mapped] source_msg={message.id} -> dest_ids={ids}")



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


def looks_like_fresh_signal_start(text: str) -> bool:
    """Detect a new setup so stale incomplete buffers do not swallow the next signal."""
    t = (text or "").upper()
    has_direction = bool(re.search(r"\b(BUY|BUYS|BUYING|SELL|SELLS|SELLING|LONG|LONGS|SHORT|SHORTS)\b", t))
    has_entry = bool(re.search(rf"\b(ENTRY|ENTRIES|ENTER|ENTERING)\b[^\n]{{0,40}}{PRICE_RE}", t))
    has_symbol = bool(re.search(r"\b(XAUUSD|XAGUSD|GOLD|SILVER|BTC|ETH|SOL|NAS100|NASDAQ|US100|US30|US500|GER40|DAX|[A-Z]{6})\b", t))
    return has_direction and (has_entry or has_symbol)


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
    """
    Forward/copy the original and return the sent message,
    so the AI format and source can reply to it.
    """
    if not FORWARD_SIGNAL_CANDIDATES:
        return None
    try:
        sent = await client.forward_messages(SIGNAL_DEST_CHAT, message)
        log.info(f"[signal hub original forwarded] msg={message.id} topic={topic_id_of(message)}")
        return sent
    except Exception as exc:
        log.warning(f"Forward original failed, sending text copy instead: {exc}")
        sent = await client.send_message(SIGNAL_DEST_CHAT, text, parse_mode=None, link_preview=False)
        return sent


async def send_full_signal(message, result, key, original_text, forward_raw=True):
    sig = signature_for(result, key)
    if not remember_signature(sig):
        log.info("[signal hub skipped] duplicate signal packet")
        return False

    try:
        # If this source message was edited and already produced a packet,
        # delete the old original + old AI format + old source first.
        await delete_existing_signal_packet(message, key)

        original_sent = None
        sent_messages = []

        if forward_raw:
            original_sent = await forward_original(message, original_text)
            if original_sent:
                sent_messages.append(original_sent)

        reply_to_id = getattr(original_sent, "id", None)

        ai_sent = await client.send_message(
            SIGNAL_DEST_CHAT,
            result["message"],
            parse_mode="html",
            link_preview=False,
            reply_to=reply_to_id,
        )
        sent_messages.append(ai_sent)

        if SEND_SOURCE_LINE:
            source_sent = await client.send_message(
                SIGNAL_DEST_CHAT,
                result["source"],
                parse_mode="html",
                link_preview=False,
                reply_to=reply_to_id or getattr(ai_sent, "id", None),
            )
            sent_messages.append(source_sent)

        remember_signal_packet(message, key, sent_messages)

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

        # First try this message alone. Complete setups should not enter the partial buffer.
        result = extract_and_format(text, source_name, message.id)
        if result:
            await send_full_signal(message, result, key, text, forward_raw=True)
            return

        if not PARTIAL_BUFFER_ENABLED:
            log.info("[signal hub skipped] not a clean signal")
            return

        if not looks_like_signal(text):
            log.info("[signal hub skipped] not signal-like")
            return

        trim_buffer(key)

        # If a previous incomplete signal is stuck and a new setup starts, clear it.
        if buffers[key] and looks_like_fresh_signal_start(text):
            log.info(f"[signal hub buffer reset] fresh setup detected key={key}")
            buffers[key].clear()

        first_piece = len(buffers[key]) == 0
        buffers[key].append({"ts": time.time(), "id": message.id, "text": text, "message": message})
        # Do NOT forward candidate messages yet.
        # Only forward the original after the setup is fully confirmed and formatted.

        combined = combined_text_for(key)
        result = extract_and_format(combined, source_name, message.id)
        if result:
            raw_msg = first_buffer_message(key) or message
            await send_full_signal(raw_msg, result, key, message_text(raw_msg).strip(), forward_raw=True)
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
    log.info(f"DELETE_OLD_SIGNAL_PACKET_ON_EDIT={DELETE_OLD_SIGNAL_PACKET_ON_EDIT}")
    log.info("AI/source replies to the forwarded original: True")
    log.info(f"Universal AI extractor active for any pair")
    log.info(f"Partial signal buffer: {PARTIAL_BUFFER_ENABLED} | window={BUFFER_WINDOW_SECONDS}s | max={BUFFER_MAX_MESSAGES}")
    await admin_startup(client)
    asyncio.create_task(admin_loop(client, stats))
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
