import asyncio
import logging
import os
import re

from telethon import events
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageMediaWebPage

import telegram_worker.worker_fixed as base
from telegram_worker.admin_features import ADMIN_CHAT, admin_startup, admin_loop, handle_admin_command

# IMPORTANT:
# worker_fixed registers old handlers when imported.
# We clear them so this worker becomes the ONLY forwarding logic.
base.client._event_builders = []

client = base.client
log = logging.getLogger("imperium-threadfix")

# Supports several env names, but defaults to the blocked Lorax ID.
raw_skip_ids = (
    os.environ.get("SKIP_IDS")
    or os.environ.get("MUTED_SENDER_IDS")
    or os.environ.get("BLOCKED_IDS")
    or "7556281143"
)

SKIP_IDS = {
    int(x.strip())
    for x in raw_skip_ids.replace(" ", "").split(",")
    if x.strip()
}

DROP_LINK_ONLY = os.environ.get("DROP_LINK_ONLY", "1").strip() == "1"
LINK_ONLY = re.compile(r"^(https?://|t\.me/|www\.)\S+$", re.I)

SEEN_MESSAGES = set()
SEEN_ALBUMS = set()
SEEN_SIGNALS = set()


def text_of(message):
    return message.message or message.raw_text or message.text or ""


def entities_of(message):
    return getattr(message, "entities", None) or []


def peer_ids(peer):
    found = set()

    if not peer:
        return found

    for attr in ("user_id", "channel_id", "chat_id"):
        value = getattr(peer, attr, None)

        if value:
            found.add(int(value))

    return found


def sender_ids(event, message):
    found = set()

    if event is not None and getattr(event, "sender_id", None):
        found.add(int(event.sender_id))

    if message is not None:
        if getattr(message, "sender_id", None):
            found.add(int(message.sender_id))

        found |= peer_ids(getattr(message, "from_id", None))

        forwarded = getattr(message, "fwd_from", None)

        if forwarded:
            found |= peer_ids(getattr(forwarded, "from_id", None))
            found |= peer_ids(getattr(forwarded, "saved_from_peer", None))

    return found


def should_skip(event, message):
    ids = sender_ids(event, message)

    if ids & SKIP_IDS:
        log.info(f"[blocked sender skipped] ids={sorted(ids)} msg_id={getattr(message, 'id', None)}")
        return True

    body = text_of(message).strip() if message else ""

    if DROP_LINK_ONLY and LINK_ONLY.match(body):
        log.info(f"[link-only skipped] msg_id={getattr(message, 'id', None)}")
        return True

    return False


def topic_of(message):
    reply = getattr(message, "reply_to", None)

    if not reply:
        return None

    return getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None)


def valid_routes(chat_id, topic_id):
    routes = []

    for route in base.ROUTES:
        if route["source_chat"] != chat_id:
            continue

        if route["source_topic"] is not None and route["source_topic"] != topic_id:
            continue

        # Prevent self-loop if source and destination are the exact same topic.
        if (
            route["source_chat"] == route["dest_chat"]
            and route.get("source_topic") == route.get("dest_topic")
            and topic_id == route.get("dest_topic")
        ):
            log.warning(f"[self-route skipped] {route['name']} {chat_id}_{topic_id}")
            continue

        routes.append(route)

    return routes


def is_real_media(message):
    return bool(getattr(message, "media", None)) and not isinstance(message.media, MessageMediaWebPage)


def map_key(source_chat, source_message_id, dest_chat, dest_topic):
    return f"{source_chat}:{source_message_id}:{dest_chat}:{dest_topic}"


def reply_source_ids(message):
    reply = getattr(message, "reply_to", None)

    if not reply:
        return []

    found = []

    # reply_to_msg_id is usually the actual message being replied to.
    # reply_to_top_id is usually the forum topic root.
    for value in (getattr(reply, "reply_to_msg_id", None), getattr(reply, "reply_to_top_id", None)):
        if value and value not in found:
            found.append(value)

    return found


def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip().lower()


def destination_topic_of(message):
    reply = getattr(message, "reply_to", None)

    if not reply:
        return None

    return getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None)


async def recover_reply_target(message, route):
    for source_reply_id in reply_source_ids(message):
        # Do not reply to topic root as if it was an actual message.
        if route["source_topic"] is not None and source_reply_id == route["source_topic"]:
            continue

        key = map_key(
            route["source_chat"],
            source_reply_id,
            route["dest_chat"],
            route["dest_topic"],
        )

        mapped = base.message_map.get(key)

        if mapped:
            return int(mapped)

        # Recovery mode:
        # If Railway lost the message map after redeploy, fetch the original source message,
        # then search recent ExposedFX destination messages for identical text.
        try:
            original = await client.get_messages(route["source_chat"], ids=source_reply_id)
            original_text = clean_text(text_of(original))
        except Exception as exc:
            log.info(f"[reply recovery source fetch failed] source_msg={source_reply_id}: {exc}")
            original_text = ""

        if not original_text:
            continue

        try:
            async for dest_message in client.iter_messages(route["dest_chat"], limit=1000):
                dest_text = clean_text(text_of(dest_message))

                if not dest_text:
                    continue

                # Prefer exact text match.
                if dest_text == original_text:
                    base.message_map[key] = dest_message.id
                    base.save_map()

                    log.info(f"[reply recovered] source_msg={source_reply_id} -> dest_msg={dest_message.id}")
                    return int(dest_message.id)

        except Exception as exc:
            log.info(f"[reply recovery destination search failed] {exc}")

    return None


async def reply_target(message, route):
    if not getattr(message, "reply_to", None):
        return route["dest_topic"]

    found = await recover_reply_target(message, route)

    if found:
        return found

    log.info(
        f"[reply fallback] {route['name']} reply_ids={reply_source_ids(message)} "
        f"-> destination topic {route['dest_topic']}"
    )

    return route["dest_topic"]


def remember_message(source_message, dest_message, route):
    if not source_message or not dest_message:
        return

    key = map_key(
        route["source_chat"],
        source_message.id,
        route["dest_chat"],
        route["dest_topic"],
    )

    base.message_map[key] = dest_message.id
    base.save_map()


def mark_seen(message, route, kind):
    key = f"{kind}:{route['source_chat']}:{getattr(message, 'id', None)}:{route['dest_chat']}:{route['dest_topic']}"

    if key in SEEN_MESSAGES:
        return False

    SEEN_MESSAGES.add(key)

    if len(SEEN_MESSAGES) > 8000:
        SEEN_MESSAGES.clear()

    return True


def maybe_post_signal_once(route, message, body):
    parsed = base.parse_signal(body)

    if not parsed:
        return

    signal_key = f"{route['source_chat']}:{message.id}"

    if signal_key in SEEN_SIGNALS:
        return

    SEEN_SIGNALS.add(signal_key)

    if len(SEEN_SIGNALS) > 8000:
        SEEN_SIGNALS.clear()

    if base.DRY_RUN:
        log.info(f"[DRY_RUN signal] {route['name']} {parsed.get('direction')} {parsed.get('symbol')}")
        return

    try:
        import requests

        response = requests.post(
            f"{base.SERVER_URL}/api/v1/signals",
            json={
                "source": route["name"],
                "source_chat_id": route["source_chat"],
                "source_message_id": message.id,
                "raw_text": body,
                **parsed,
            },
            headers={"X-AUTO-TOKEN": base.AUTO_TOKEN},
            timeout=12,
        )

        if response.status_code >= 400:
            log.warning(f"[signal rejected] {route['name']} {response.status_code}: {response.text}")
        else:
            log.info(f"[signal posted] {route['name']} -> {response.text}")

    except Exception as exc:
        log.error(f"[signal post failed] {route['name']}: {exc}")


async def send_one(message, route):
    if not mark_seen(message, route, "single"):
        log.info(f"[duplicate skipped] {route['name']} msg={message.id}")
        return None

    target_reply = await reply_target(message, route)
    body = text_of(message)
    formatting = entities_of(message)

    if is_real_media(message):
        try:
            sent = await client.send_file(
                route["dest_chat"],
                message.media,
                caption=body if body else None,
                formatting_entities=formatting if body else None,
                parse_mode=None,
                reply_to=target_reply,
            )
        except Exception as exc:
            log.warning(f"[media formatting fallback] {route['name']}: {exc}")

            sent = await client.send_file(
                route["dest_chat"],
                message.media,
                caption=body if body else None,
                parse_mode=None,
                reply_to=target_reply,
            )

    else:
        try:
            sent = await client.send_message(
                route["dest_chat"],
                body if body else "Unsupported message type.",
                formatting_entities=formatting if body else None,
                parse_mode=None,
                reply_to=target_reply,
                link_preview=True,
            )
        except Exception as exc:
            log.warning(f"[text formatting fallback] {route['name']}: {exc}")

            sent = await client.send_message(
                route["dest_chat"],
                body if body else "Unsupported message type.",
                parse_mode=None,
                reply_to=target_reply,
                link_preview=True,
            )

    remember_message(message, sent, route)
    return sent


async def send_album(messages, route):
    first = messages[0]
    album_id = getattr(first, "grouped_id", None) or first.id
    album_key = f"album:{route['source_chat']}:{album_id}:{route['dest_chat']}:{route['dest_topic']}"

    if album_key in SEEN_ALBUMS:
        log.info(f"[album duplicate skipped] {route['name']} album={album_id}")
        return

    SEEN_ALBUMS.add(album_key)

    if len(SEEN_ALBUMS) > 3000:
        SEEN_ALBUMS.clear()

    target_reply = await reply_target(first, route)

    files = []
    caption = None
    caption_entities = None

    for message in messages:
        if should_skip(None, message):
            return

        if is_real_media(message):
            files.append(message.media)

        if caption is None:
            body = text_of(message)

            if body:
                caption = body
                caption_entities = entities_of(message)

    if not files:
        return

    try:
        sent = await client.send_file(
            route["dest_chat"],
            files,
            caption=caption,
            formatting_entities=caption_entities,
            parse_mode=None,
            reply_to=target_reply,
        )
    except Exception as exc:
        log.warning(f"[album formatting fallback] {route['name']}: {exc}")

        sent = await client.send_file(
            route["dest_chat"],
            files,
            caption=caption,
            parse_mode=None,
            reply_to=target_reply,
        )

    if isinstance(sent, list):
        for source, destination in zip(messages, sent):
            remember_message(source, destination, route)
    else:
        remember_message(first, sent, route)


@client.on(events.NewMessage(chats=base.SOURCE_CHATS))
async def on_new_message(event):
    message = event.message

    # Albums are handled by the album handler.
    # This prevents album messages from also being copied one-by-one.
    if getattr(message, "grouped_id", None):
        return

    if should_skip(event, message):
        return

    routes = valid_routes(event.chat_id, topic_of(message))

    for route in routes:
        try:
            await send_one(message, route)

            body = text_of(message)

            if body:
                maybe_post_signal_once(route, message, body)
                base.log_stats(route, message, body)

            log.info(f"[copied] {route['name']} msg={message.id}")

        except FloodWaitError as exc:
            log.warning(f"[FloodWait] {exc.seconds}s")
            await asyncio.sleep(min(exc.seconds + 1, 60))

        except Exception as exc:
            log.error(f"[copy failed] {route['name']}: {exc}")


@client.on(events.Album(chats=base.SOURCE_CHATS))
async def on_album(event):
    if not event.messages:
        return

    first = event.messages[0]

    if should_skip(event, first):
        return

    routes = valid_routes(event.chat_id, topic_of(first))

    for route in routes:
        try:
            await send_album(event.messages, route)

            body = ""

            for message in event.messages:
                body = text_of(message)

                if body:
                    break

            if body:
                maybe_post_signal_once(route, first, body)
                base.log_stats(route, first, body)

            log.info(f"[album copied] {route['name']} items={len(event.messages)}")

        except Exception as exc:
            log.error(f"[album failed] {route['name']}: {exc}")


@client.on(events.NewMessage(chats=ADMIN_CHAT))
async def on_admin_message(event):
    try:
        await handle_admin_command(event, client, base.stats)
    except Exception as exc:
        log.error(f"[admin command failed] {exc}")


async def main():
    await client.connect()

    if not await client.is_user_authorized():
        raise RuntimeError("Telegram session is not authorised. Regenerate the Railway session chunks.")

    me = await client.get_me()

    log.info(f"Threadfix online as {me.id}")
    log.info(f"Blocked IDs: {sorted(SKIP_IDS)}")
    log.info(f"Drop link-only messages: {DROP_LINK_ONLY}")
    log.info("Reply recovery, media captions, album dedupe, and duplicate protection are active.")

    await admin_startup(client)

    asyncio.create_task(base.stats.loop(client))
    asyncio.create_task(admin_loop(client, base.stats))

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
