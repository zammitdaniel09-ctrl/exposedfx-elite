import asyncio
import logging
import os
import re

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


def message_text(message):
    return message.message or message.raw_text or message.text or ""


def should_skip(message):
    text = message_text(message).strip()
    if DROP_LINK_ONLY and LINK_ONLY_RE.match(text):
        log.info("[signal hub skipped] plain link")
        return True
    return False


@client.on(events.NewMessage(chats=SIGNAL_SOURCE_CHAT))
async def on_signal_hub_message(event):
    try:
        message = event.message
        if should_skip(message):
            return

        text = message_text(message)
        result = refine_signal(text, "ExposedFX", message.id)
        if not result:
            log.info("[signal hub skipped] not a clean signal")
            return

        await client.send_message(SIGNAL_DEST_CHAT, result["message"], parse_mode="html", link_preview=False)
        if SEND_SOURCE_LINE:
            await client.send_message(SIGNAL_DEST_CHAT, result["source"], parse_mode="html", link_preview=False)

        log.info(f"[signal hub sent] source_msg={message.id} -> {SIGNAL_DEST_CHAT}")
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
    await admin_startup(client)
    asyncio.create_task(admin_loop(client, stats))
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
