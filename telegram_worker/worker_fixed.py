import os
import json
import base64
import asyncio
import logging
from pathlib import Path

import requests
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageMediaWebPage

from telegram_worker.routes import ROUTES
from telegram_worker.parser import parse_signal
from telegram_worker.stats_reporter import WeeklyStats

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("imperium-worker")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SERVER_URL = os.environ.get("SERVER_URL", "").rstrip("/")
AUTO_TOKEN = os.environ.get("AUTO_TOKEN", "change-this-token")
DRY_RUN = os.environ.get("DRY_RUN", "0").strip() == "1"

DATA_DIR = Path(os.environ.get("DATA_DIR") or "./data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
SESSION_BASE = DATA_DIR / "session"
SESSION_FILE = DATA_DIR / "session.session"
MESSAGE_MAP_FILE = DATA_DIR / "message_map.json"

SOURCE_CHATS = sorted(set(r["source_chat"] for r in ROUTES))
POSTED_SIGNAL_KEYS = set()
stats = WeeklyStats(DATA_DIR)


def _clean_b64(value: str) -> str:
    return "".join((value or "").split()).strip()


def combined_login_blob():
    direct = _clean_b64(os.environ.get("SESSION_B64", ""))
    if direct:
        return direct, "SESSION_B64"

    count_raw = os.environ.get("SESSION_B64_CHUNKS", "").strip()
    if count_raw:
        try:
            count = int(count_raw)
        except ValueError:
            raise RuntimeError(f"Invalid SESSION_B64_CHUNKS value: {count_raw}")
        chunks = []
        for i in range(1, count + 1):
            chunk = _clean_b64(os.environ.get(f"SESSION_B64_{i}", ""))
            if not chunk:
                raise RuntimeError(f"SESSION_B64_CHUNKS={count} but SESSION_B64_{i} is missing")
            chunks.append(chunk)
        return "".join(chunks), f"{count} chunks fixed-count"

    chunks = []
    i = 1
    while True:
        chunk = _clean_b64(os.environ.get(f"SESSION_B64_{i}", ""))
        if not chunk:
            break
        chunks.append(chunk)
        i += 1

    if chunks:
        return "".join(chunks), f"{len(chunks)} chunks"
    return "", "none"


def write_login_file():
    blob, source = combined_login_blob()
    if not blob:
        raise RuntimeError("No Telegram login data found in Railway variables.")
    raw = base64.b64decode(blob)
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_bytes(raw)
    log.info(f"Telegram login file written from {source}: {SESSION_FILE} bytes={len(raw)}")


def load_map():
    if not MESSAGE_MAP_FILE.exists():
        return {}
    try:
        return json.loads(MESSAGE_MAP_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_map():
    tmp = MESSAGE_MAP_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(message_map), encoding="utf-8")
    tmp.replace(MESSAGE_MAP_FILE)


write_login_file()
message_map = load_map()
client = TelegramClient(str(SESSION_BASE), API_ID, API_HASH)


def text_of(message):
    return message.message or message.raw_text or message.text or ""


def entities_of(message):
    return getattr(message, "entities", None) or []


def topic_of(message):
    reply = getattr(message, "reply_to", None)
    if not reply:
        return None
    return getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None)


def same_source_and_destination(route, topic_id):
    return (
        route["source_chat"] == route["dest_chat"]
        and route.get("source_topic") == route.get("dest_topic")
        and topic_id == route.get("dest_topic")
    )


def routes_for(chat_id, topic_id):
    found = []
    for route in ROUTES:
        if route["source_chat"] != chat_id:
            continue
        if route["source_topic"] is not None and route["source_topic"] != topic_id:
            continue
        if same_source_and_destination(route, topic_id):
            log.warning(f"[self-route skipped] {route['name']} {chat_id}_{topic_id}")
            continue
        found.append(route)
    return found


def is_real_media(message):
    if not getattr(message, "media", None):
        return False
    return not isinstance(message.media, MessageMediaWebPage)


def map_key(source_chat, source_msg_id, dest_chat, dest_topic):
    return f"{source_chat}:{source_msg_id}:{dest_chat}:{dest_topic}"


def reply_source_ids(message):
    reply = getattr(message, "reply_to", None)
    if not reply:
        return []

    ids = []
    reply_msg_id = getattr(reply, "reply_to_msg_id", None)
    top_id = getattr(reply, "reply_to_top_id", None)

    for value in (reply_msg_id, top_id):
        if value and value not in ids:
            ids.append(value)

    return ids


def mapped_reply_id(message, route):
    for source_msg_id in reply_source_ids(message):
        if route["source_topic"] is not None and source_msg_id == route["source_topic"]:
            continue

        key = map_key(route["source_chat"], source_msg_id, route["dest_chat"], route["dest_topic"])
        mapped = message_map.get(key)

        if mapped:
            try:
                return int(mapped)
            except Exception:
                return None

    return None


def reply_target(message, route):
    reply = getattr(message, "reply_to", None)

    if not reply:
        return route["dest_topic"]

    mapped = mapped_reply_id(message, route)

    if mapped:
        return mapped

    log.info(
        f"[reply fallback] {route['name']} could not find mapped source reply "
        f"ids={reply_source_ids(message)} -> using destination topic {route['dest_topic']}"
    )

    return route["dest_topic"]


def remember_message(src_msg, dst_msg, route):
    if not src_msg or not dst_msg:
        return
    key = map_key(route["source_chat"], src_msg.id, route["dest_chat"], route["dest_topic"])
    message_map[key] = dst_msg.id
    save_map()


def log_stats(route, message, text):
    result = stats.log_message(route, message, text)
    if result:
        log.info(f"[stats logged] {route['name']} {result['status']} {result['pips']} pips")


def maybe_post_signal(route, message, text):
    parsed = parse_signal(text)
    if not parsed:
        return

    sig_key = f"{route['source_chat']}:{message.id}"
    if sig_key in POSTED_SIGNAL_KEYS:
        return
    POSTED_SIGNAL_KEYS.add(sig_key)

    payload = {
        "source": route["name"],
        "source_chat_id": route["source_chat"],
        "source_message_id": message.id,
        "raw_text": text,
        **parsed,
    }

    if DRY_RUN:
        log.info(f"[DRY_RUN signal] {route['name']} {parsed['direction']} {parsed['symbol']}")
        return

    try:
        res = requests.post(
            f"{SERVER_URL}/api/v1/signals",
            json=payload,
            headers={"X-AUTO-TOKEN": AUTO_TOKEN},
            timeout=12,
        )
        if res.status_code >= 400:
            log.warning(f"[signal rejected] {route['name']} {res.status_code}: {res.text}")
        else:
            log.info(f"[signal posted] {route['name']} {parsed['direction']} {parsed['symbol']} -> {res.text}")
    except Exception as exc:
        log.error(f"[signal post failed] {route['name']}: {exc}")


async def copy_one(message, route):
    target_reply = reply_target(message, route)
    text = text_of(message)
    entities = entities_of(message)

    if DRY_RUN:
        log.info(f"[DRY_RUN copy] {route['name']}")
        return None

    if is_real_media(message):
        sent = await client.send_file(
            route["dest_chat"],
            message.media,
            caption=text if text else None,
            formatting_entities=entities if text else None,
            parse_mode=None,
            reply_to=target_reply,
        )
    else:
        sent = await client.send_message(
            route["dest_chat"],
            text if text else "Unsupported message type.",
            formatting_entities=entities if text else None,
            parse_mode=None,
            reply_to=target_reply,
            link_preview=True,
        )

    remember_message(message, sent, route)
    return sent


async def copy_album(messages, route):
    first = messages[0]
    target_reply = reply_target(first, route)
    files = []
    caption = None
    caption_entities = None

    for msg in messages:
        if is_real_media(msg):
            files.append(msg.media)
        if caption is None:
            txt = text_of(msg)
            if txt:
                caption = txt
                caption_entities = entities_of(msg)

    if DRY_RUN:
        log.info(f"[DRY_RUN album] {route['name']} items={len(files)}")
        return

    if not files:
        return

    sent = await client.send_file(
        route["dest_chat"],
        files,
        caption=caption,
        formatting_entities=caption_entities,
        parse_mode=None,
        reply_to=target_reply,
    )

    if isinstance(sent, list):
        for src, dst in zip(messages, sent):
            remember_message(src, dst, route)
    else:
        remember_message(first, sent, route)


@client.on(events.NewMessage(chats=SOURCE_CHATS))
async def on_message(event):
    message = event.message
    if getattr(message, "grouped_id", None):
        return

    chat_id = event.chat_id
    topic_id = topic_of(message)
    routes = routes_for(chat_id, topic_id)
    if not routes:
        return

    text = text_of(message)
    for route in routes:
        try:
            await copy_one(message, route)
            if text:
                maybe_post_signal(route, message, text)
                log_stats(route, message, text)
            direction = "outgoing" if getattr(message, "out", False) else "incoming"
            log.info(f"[copied:{direction}] {route['name']} source={chat_id}_{topic_id} -> dest={route['dest_chat']}_{route['dest_topic']}")
        except FloodWaitError as exc:
            log.warning(f"FloodWait {exc.seconds}s")
            await asyncio.sleep(min(exc.seconds + 1, 60))
        except Exception as exc:
            log.error(f"[copy failed] {route['name']}: {exc}")


@client.on(events.Album(chats=SOURCE_CHATS))
async def on_album(event):
    if not event.messages:
        return

    first = event.messages[0]
    chat_id = event.chat_id
    topic_id = topic_of(first)
    routes = routes_for(chat_id, topic_id)
    if not routes:
        return

    text = ""
    for msg in event.messages:
        text = text_of(msg)
        if text:
            break

    for route in routes:
        try:
            await copy_album(event.messages, route)
            if text:
                maybe_post_signal(route, first, text)
                log_stats(route, first, text)
            direction = "outgoing" if getattr(first, "out", False) else "incoming"
            log.info(f"[album copied:{direction}] {route['name']} items={len(event.messages)}")
        except Exception as exc:
            log.error(f"[album failed] {route['name']}: {exc}")


async def main():
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram login file loaded but account is not authorised. Regenerate the local session and Railway chunks.")

    me = await client.get_me()
    log.info(f"Logged in as {me.first_name} | id={me.id}")
    log.info(f"SERVER_URL={SERVER_URL}")
    log.info(f"DATA_DIR={DATA_DIR}")
    log.info(f"DRY_RUN={DRY_RUN}")
    log.info(f"Watching {len(SOURCE_CHATS)} source chats")
    log.info("Imperium fixed Telegram worker running...")
    asyncio.create_task(stats.loop(client))
    log.info("Weekly stats reporter running for Sunday 00:00 Europe/Malta")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
