import os
import json
import base64
import asyncio
import logging
import time
import re
import hashlib
from pathlib import Path

import requests
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageMediaWebPage

from telegram_worker.runtime_guard import start_runtime_guard, alert_crash
from telegram_worker.provider_profiles import is_promo_text
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
FORWARD_EDITED_MESSAGES = os.environ.get("FORWARD_EDITED_MESSAGES", "1").strip() == "1"
PROCESS_GROUPED_MESSAGES_IN_NEW_HANDLER = os.environ.get("PROCESS_GROUPED_MESSAGES_IN_NEW_HANDLER", "1").strip() == "1"
ENABLE_ALBUM_HANDLER = os.environ.get("ENABLE_ALBUM_HANDLER", "0").strip() == "1"

DATA_DIR = Path(os.environ.get("DATA_DIR") or "./data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
SESSION_BASE = DATA_DIR / "session"
SESSION_FILE = DATA_DIR / "session.session"
MESSAGE_MAP_FILE = DATA_DIR / "message_map.json"
DEDUP_FILE = DATA_DIR / "dedupe_map.json"
DEDUP_WINDOW_SECONDS = int(os.environ.get("DEDUP_WINDOW_SECONDS", "900"))

CROSS_SOURCE_DEDUP_DEST_TOPICS_RAW = os.environ.get("CROSS_SOURCE_DEDUP_DEST_TOPICS", "1927").strip()
CROSS_SOURCE_DEDUP_DEST_TOPICS = {
    int(x)
    for x in re.split(r"[,\s]+", CROSS_SOURCE_DEDUP_DEST_TOPICS_RAW)
    if x.strip()
}

BLOCKED_DEST_CHAT = int(os.environ.get("BLOCKED_DEST_CHAT", "-1003918958200"))
BLOCKED_DEST_TOPICS_RAW = os.environ.get("BLOCKED_DEST_TOPICS", "1").strip()
BLOCKED_DEST_TOPICS = {
    int(x)
    for x in re.split(r"[,\s]+", BLOCKED_DEST_TOPICS_RAW)
    if x.strip()
}


BLOCKED_SENDER_IDS_RAW = os.environ.get("BLOCKED_SENDER_IDS", "7556281143").strip()
BLOCKED_SENDER_IDS = {
    int(x)
    for x in re.split(r"[,\s]+", BLOCKED_SENDER_IDS_RAW)
    if x.strip()
}

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



def load_dedupe():
    if not DEDUP_FILE.exists():
        return {}
    try:
        return json.loads(DEDUP_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_dedupe():
    try:
        cutoff = time.time() - DEDUP_WINDOW_SECONDS
        old = list(content_dedupe_map.keys())
        for k in old:
            if float(content_dedupe_map.get(k, 0)) < cutoff:
                content_dedupe_map.pop(k, None)

        tmp = DEDUP_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(content_dedupe_map), encoding="utf-8")
        tmp.replace(DEDUP_FILE)
    except Exception as exc:
        log.warning(f"[dedupe save failed] {exc}")


def normalise_for_dedupe(text):
    text = (text or "").lower()
    text = text.replace("\\ufe0f", "").replace("\\u200d", "").replace("\\u200b", "")
    text = re.sub(r"\\s+", "", text)
    return text.strip()


def media_tag(message):
    if not getattr(message, "media", None):
        return "no-media"

    # Keep albums/screenshots safer: exact duplicate media/text gets blocked,
    # different images with same caption can still pass.
    media_id = getattr(getattr(message, "photo", None), "id", None)
    if media_id:
        return f"photo:{media_id}"

    document = getattr(message, "document", None)
    if document:
        return f"doc:{getattr(document, 'id', '')}:{getattr(document, 'size', '')}"

    return "media"


def dedupe_key(route, message, text):
    """
    Normal dedupe keeps sources separate.
    For selected destination topics, dedupe across different source groups too,
    because providers often forward each other's exact messages.
    """
    cross_source = int(route["dest_topic"]) in CROSS_SOURCE_DEDUP_DEST_TOPICS

    source_chat_key = "ANY_SOURCE" if cross_source else str(route["source_chat"])
    source_topic_key = "ANY_TOPIC" if cross_source else str(route.get("source_topic"))

    # For text/caption messages in cross-source dedupe topics, prioritise text.
    # This blocks duplicate forwarded posts even if Telegram gives different media ids.
    media_key = "TEXT_OR_CAPTION" if cross_source and normalise_for_dedupe(text) else media_tag(message)

    base = "|".join([
        source_chat_key,
        source_topic_key,
        str(route["dest_chat"]),
        str(route["dest_topic"]),
        normalise_for_dedupe(text),
        media_key,
    ])
    return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()


def is_recent_duplicate(route, message, text):
    key = dedupe_key(route, message, text)
    last = float(content_dedupe_map.get(key, 0) or 0)
    return (time.time() - last) <= DEDUP_WINDOW_SECONDS


def remember_dedupe(route, message, text):
    key = dedupe_key(route, message, text)
    content_dedupe_map[key] = time.time()
    save_dedupe()


def existing_destination_id(message, route):
    key = map_key(route["source_chat"], message.id, route["dest_chat"], route["dest_topic"])
    mapped = message_map.get(key)
    try:
        return int(mapped) if mapped else None
    except Exception:
        return None


async def delete_existing_destination(message, route):
    existing = existing_destination_id(message, route)
    if not existing:
        return False
    try:
        await client.delete_messages(route["dest_chat"], existing)
        log.info(f"[edited cleanup] deleted old forwarded msg {existing} for source {message.id}")
        return True
    except Exception as exc:
        log.warning(f"[edited cleanup failed] source={message.id} dest_msg={existing}: {exc}")
        return False



write_login_file()
message_map = load_map()
content_dedupe_map = load_dedupe()
client = TelegramClient(str(SESSION_BASE), API_ID, API_HASH)


def text_of(message):
    return message.message or message.raw_text or message.text or ""


def entities_of(message):
    return getattr(message, "entities", None) or []




def sender_ids_for_message(message):
    ids = set()

    for attr in ("sender_id", "from_id"):
        obj = getattr(message, attr, None)
        if isinstance(obj, int):
            ids.add(int(obj))
        else:
            for sub in ("user_id", "channel_id", "chat_id"):
                val = getattr(obj, sub, None)
                if val:
                    try:
                        ids.add(int(val))
                    except Exception:
                        pass

    fwd = getattr(message, "fwd_from", None)
    if fwd:
        for attr in ("from_id", "saved_from_peer"):
            obj = getattr(fwd, attr, None)
            if isinstance(obj, int):
                ids.add(int(obj))
            else:
                for sub in ("user_id", "channel_id", "chat_id"):
                    val = getattr(obj, sub, None)
                    if val:
                        try:
                            ids.add(int(val))
                        except Exception:
                            pass

    return ids


def is_blocked_sender(message):
    ids = sender_ids_for_message(message)
    return bool(ids & BLOCKED_SENDER_IDS)


def known_source_topics_for_chat(chat_id):
    return {
        int(r["source_topic"])
        for r in ROUTES
        if r["source_chat"] == chat_id and r.get("source_topic") is not None
    }


def unique_routes_for_source_chat(chat_id):
    """
    Fallback for groups where many source topics all go to ONE destination topic.
    If Telegram gives bad/missing topic metadata for photo/video replies, we can still forward.
    """
    source_routes = [r for r in ROUTES if r["source_chat"] == chat_id]
    if not source_routes:
        return []

    unique_dests = {(r["dest_chat"], r["dest_topic"]) for r in source_routes}
    if len(unique_dests) != 1:
        return []

    first = dict(source_routes[0])
    first["source_topic"] = None
    first["name"] = first.get("name", "fallback") + " MEDIA FALLBACK"
    return [first]



def topic_of(message, chat_id=None):
    """
    Robust Telegram forum topic detection.

    Important fix:
    For photo/video replies, reply_to_msg_id may be the message being replied to,
    NOT the forum topic id. Only trust it when it matches a known route topic.
    """
    known_topics = known_source_topics_for_chat(chat_id) if chat_id is not None else set()

    for attr in ("reply_to_top_id", "top_msg_id"):
        value = getattr(message, attr, None)
        if value:
            try:
                return int(value)
            except Exception:
                pass

    reply = getattr(message, "reply_to", None)
    if reply:
        for attr in ("reply_to_top_id", "top_msg_id"):
            value = getattr(reply, attr, None)
            if value:
                try:
                    return int(value)
                except Exception:
                    pass

    direct_reply_id = getattr(message, "reply_to_msg_id", None)
    if direct_reply_id:
        try:
            direct_reply_id = int(direct_reply_id)
            # Only treat reply_to_msg_id as topic if it is actually one of our route topics.
            if not known_topics or direct_reply_id in known_topics:
                return direct_reply_id
        except Exception:
            pass

    if reply:
        reply_msg_id = getattr(reply, "reply_to_msg_id", None)
        if reply_msg_id:
            try:
                reply_msg_id = int(reply_msg_id)
                if not known_topics or reply_msg_id in known_topics:
                    return reply_msg_id
            except Exception:
                pass

    return None



def same_source_and_destination(route, topic_id):
    return (
        route["source_chat"] == route["dest_chat"]
        and route.get("source_topic") == route.get("dest_topic")
        and topic_id == route.get("dest_topic")
    )



def is_blocked_destination(route):
    try:
        return (
            int(route.get("dest_chat")) == BLOCKED_DEST_CHAT
            and int(route.get("dest_topic")) in BLOCKED_DEST_TOPICS
        )
    except Exception:
        return False


def routes_for(chat_id, topic_id, message=None):
    found = []
    for route in ROUTES:
        if route["source_chat"] != chat_id:
            continue

        if is_blocked_destination(route):
            log.warning(
                f"[blocked destination] route={route.get('name')} "
                f"source={route.get('source_chat')}_{route.get('source_topic')} "
                f"blocked_dest={route.get('dest_chat')}_{route.get('dest_topic')}"
            )
            continue

        if route["source_topic"] is not None and route["source_topic"] != topic_id:
            continue
        if same_source_and_destination(route, topic_id):
            log.warning(f"[self-route skipped] {route['name']} {chat_id}_{topic_id}")
            continue
        found.append(route)

    if found:
        return found

    # Media/reply fallback: if this source group has one destination topic,
    # forward there even when Telegram gives missing/wrong topic id.
    if message is not None and is_real_media(message):
        fallback = [r for r in unique_routes_for_source_chat(chat_id) if not is_blocked_destination(r)]
        if fallback:
            log.warning(
                f"[media route fallback] source={chat_id}_{topic_id} "
                f"msg={getattr(message, 'id', None)} -> "
                f"dest={fallback[0]['dest_chat']}_{fallback[0]['dest_topic']}"
            )
            return fallback

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


async def send_media_exact(message, route, target_reply, text, entities):
    """
    Robust media copier:
    1. Try direct Telethon media resend.
    2. If that fails, download and re-upload.
    This preserves photos/videos/documents + captions + caption entities.
    """
    try:
        return await client.send_file(
            route["dest_chat"],
            message.media,
            caption=text if text else None,
            formatting_entities=entities if text else None,
            parse_mode=None,
            reply_to=target_reply,
        )
    except Exception as exc:
        log.warning(f"[media direct copy failed] msg={message.id} route={route['name']}: {exc}")

    cache_dir = DATA_DIR / "media_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    downloaded = None
    try:
        downloaded = await message.download_media(file=str(cache_dir / f"{route['dest_topic']}_{message.id}"))
        if not downloaded:
            raise RuntimeError("download_media returned no file path")

        sent = await client.send_file(
            route["dest_chat"],
            downloaded,
            caption=text if text else None,
            formatting_entities=entities if text else None,
            parse_mode=None,
            reply_to=target_reply,
        )

        return sent

    finally:
        if downloaded:
            try:
                Path(downloaded).unlink(missing_ok=True)
            except Exception:
                pass


async def ensure_replied_message_copied(message, route, depth=0):
    """
    If source message replies to another source message and that parent was not copied yet,
    copy the parent first, then this message can reply to the correct destination message.
    This prevents detached replies in ExposedFX topics.
    """
    if depth > 2:
        return False

    for source_msg_id in reply_source_ids(message):
        try:
            source_msg_id = int(source_msg_id)
        except Exception:
            continue

        if route.get("source_topic") is not None and source_msg_id == int(route["source_topic"]):
            continue

        key = map_key(route["source_chat"], source_msg_id, route["dest_chat"], route["dest_topic"])
        if message_map.get(key):
            return True

        try:
            parent = await client.get_messages(route["source_chat"], ids=source_msg_id)
            if not parent:
                continue

            await copy_one(parent, route, edited=False, ensure_reply=False)
            log.info(
                f"[reply parent copied] route={route['name']} "
                f"parent_source={source_msg_id} -> dest={route['dest_chat']}_{route['dest_topic']}"
            )
            return True

        except Exception as exc:
            log.warning(
                f"[reply parent copy failed] route={route['name']} "
                f"parent_source={source_msg_id}: {exc}"
            )

    return False


async def copy_one(message, route, edited=False, ensure_reply=True):
    if edited:
        await delete_existing_destination(message, route)

    text = text_of(message)
    entities = entities_of(message)

    if ensure_reply:
        await ensure_replied_message_copied(message, route)

    target_reply = reply_target(message, route)

    if DRY_RUN:
        log.info(f"[DRY_RUN copy] {route['name']}")
        return None

    if is_real_media(message):
        log.info(
            f"[media copy] msg={message.id} route={route['name']} "
            f"has_caption={bool(text)} reply_to={target_reply}"
        )
        sent = await send_media_exact(message, route, target_reply, text, entities)

    else:
        if not text:
            log.info(f"[skip empty unsupported] route={route['name']} msg={message.id}")
            return None

        sent = await client.send_message(
            route["dest_chat"],
            text,
            formatting_entities=entities if text else None,
            parse_mode=None,
            reply_to=target_reply,
            link_preview=True,
        )

    remember_message(message, sent, route)
    return sent


async def copy_album(messages, route):
    first = messages[0]
    await ensure_replied_message_copied(first, route)
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


async def handle_single_message(event, edited=False):
    message = event.message

    if is_blocked_sender(message):
        log.warning(f"[blocked sender] ids={sorted(sender_ids_for_message(message))} msg={getattr(message, 'id', None)}")
        return

    if is_promo_text(text_of(message), topic_of(message)):
        log.info(f"[promo blocked incoming] msg={getattr(message, 'id', None)}")
        return
    if getattr(message, "grouped_id", None) and not PROCESS_GROUPED_MESSAGES_IN_NEW_HANDLER:
        return

    chat_id = event.chat_id
    topic_id = topic_of(message, chat_id)
    routes = routes_for(chat_id, topic_id, message)
    if not routes:
        log.info(f"[no route] source={chat_id}_{topic_id} msg={getattr(message, 'id', None)} text={text_of(message)[:80]!r}")
        return

    text = text_of(message)

    for route in routes:
        try:
            if not edited and is_recent_duplicate(route, message, text):
                log.info(f"[duplicate skipped] {route['name']} source={chat_id}_{topic_id} msg={message.id}")
                continue

            await copy_one(message, route, edited=edited)
            remember_dedupe(route, message, text)

            if text:
                maybe_post_signal(route, message, text)
                log_stats(route, message, text)

            direction = "outgoing" if getattr(message, "out", False) else "incoming"
            edit_tag = ":edited" if edited else ""
            log.info(f"[copied{edit_tag}:{direction}] {route['name']} source={chat_id}_{topic_id} -> dest={route['dest_chat']}_{route['dest_topic']}")

        except FloodWaitError as exc:
            log.warning(f"FloodWait {exc.seconds}s")
            await asyncio.sleep(min(exc.seconds + 1, 60))

        except Exception as exc:
            log.error(f"[copy failed] {route['name']}: {exc}")


@client.on(events.NewMessage(chats=SOURCE_CHATS))
async def on_message(event):
    await handle_single_message(event, edited=False)


@client.on(events.MessageEdited(chats=SOURCE_CHATS))
async def on_message_edited(event):
    if FORWARD_EDITED_MESSAGES:
        await handle_single_message(event, edited=True)


@client.on(events.Album(chats=SOURCE_CHATS))
async def on_album(event):
    if not ENABLE_ALBUM_HANDLER:
        return
    if not event.messages:
        return

    first = event.messages[0]

    if any(is_blocked_sender(m) for m in event.messages):
        ids = sorted(set().union(*(sender_ids_for_message(m) for m in event.messages)))
        log.warning(f"[blocked sender album] ids={ids} first_msg={getattr(first, 'id', None)}")
        return

    chat_id = event.chat_id
    topic_id = topic_of(first, chat_id)
    routes = routes_for(chat_id, topic_id, first)
    if not routes:
        return

    text = ""
    for msg in event.messages:
        text = text_of(msg)
        if text:
            break

    for route in routes:
        try:
            if text and is_recent_duplicate(route, first, text):
                log.info(f"[album duplicate skipped] {route['name']} source={chat_id}_{topic_id} items={len(event.messages)}")
                continue

            await copy_album(event.messages, route)
            if text:
                remember_dedupe(route, first, text)
            if text:
                maybe_post_signal(route, first, text)
                log_stats(route, first, text)
            direction = "outgoing" if getattr(first, "out", False) else "incoming"
            log.info(f"[album copied:{direction}] {route['name']} items={len(event.messages)}")
        except Exception as exc:
            log.error(f"[album failed] {route['name']}: {exc}")


async def main():
    await start_runtime_guard("imperium-telegram-worker", log)
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram login file loaded but account is not authorised. Regenerate the local session and Railway chunks.")

    me = await client.get_me()
    log.info(f"Logged in as {me.first_name} | id={me.id}")
    log.info(f"SERVER_URL={SERVER_URL}")
    log.info(f"DATA_DIR={DATA_DIR}")
    log.info(f"DRY_RUN={DRY_RUN}")
    log.info(f"Watching {len(SOURCE_CHATS)} source chats: {SOURCE_CHATS}")
    log.info(f"Loaded {len(ROUTES)} routes")
    log.info(f"FORWARD_EDITED_MESSAGES={FORWARD_EDITED_MESSAGES}")
    log.info(f"DEDUP_WINDOW_SECONDS={DEDUP_WINDOW_SECONDS}")
    log.info(f"CROSS_SOURCE_DEDUP_DEST_TOPICS={sorted(CROSS_SOURCE_DEDUP_DEST_TOPICS)}")
    log.info(f"BLOCKED_DEST_CHAT={BLOCKED_DEST_CHAT} BLOCKED_DEST_TOPICS={sorted(BLOCKED_DEST_TOPICS)}")
    log.info(f"BLOCKED_SENDER_IDS={sorted(BLOCKED_SENDER_IDS)}")
    log.info(f"PROCESS_GROUPED_MESSAGES_IN_NEW_HANDLER={PROCESS_GROUPED_MESSAGES_IN_NEW_HANDLER}")
    log.info(f"ENABLE_ALBUM_HANDLER={ENABLE_ALBUM_HANDLER}")
    log.info("Exact media/reply copy hardening active: True")
    log.info("Imperium fixed Telegram worker running...")
    asyncio.create_task(stats.loop(client))
    log.info("Weekly stats reporter running for Sunday 00:00 Europe/Malta")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
