import asyncio
import logging
import os

from telethon import events

from telegram_worker.worker_clean import client, stats, SOURCE_CHATS, blocked, install_guard
from telegram_worker.admin_features import ADMIN_CHAT, admin_startup, admin_loop, handle_admin_command
from telegram_worker.signal_refiner import refine_signal

log = logging.getLogger("imperium-signal-hub")

SIGNAL_SOURCE_CHAT = int(os.environ.get("SIGNAL_SOURCE_CHAT", "-1003918958200"))
SIGNAL_DEST_CHAT = int(os.environ.get("SIGNAL_DEST_CHAT", "-5252460120"))
SEND_SOURCE_LINE = os.environ.get("SEND_SOURCE_LINE", "1").strip() == "1"


@client.on(events.NewMessage(chats=SIGNAL_SOURCE_CHAT))
async def on_signal_hub_message(event):
    try:
        if blocked(event):
            return

        message = event.message
        text = message.message or message.raw_text or message.text or ""
        result = refine_signal(text, "ExposedFX", message.id)
        if not result:
            log.info("[signal hub skipped] not a clean signal")
            return

        await client.send_message(SIGNAL_DEST_CHAT, result["message"], parse_mode=None, link_preview=False)
        if SEND_SOURCE_LINE:
            await client.send_message(SIGNAL_DEST_CHAT, result["source"], parse_mode=None, link_preview=False)

        log.info(f"[signal hub sent] source_msg={message.id} -> {SIGNAL_DEST_CHAT}")
    except Exception as exc:
        log.error(f"[signal hub failed] {exc}")


@client.on(events.NewMessage(chats=ADMIN_CHAT))
async def on_admin_message(event):
    try:
        await handle_admin_command(event, client, stats)
    except Exception as exc:
        log.error(f"[admin command failed] {exc}")


async def main():
    install_guard()
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram login file loaded but account is not authorised. Regenerate the local session and Railway chunks.")

    me = await client.get_me()
    log.info(f"Logged in as {me.first_name} | id={me.id}")
    log.info(f"Signal hub source: {SIGNAL_SOURCE_CHAT}")
    log.info(f"Signal hub destination: {SIGNAL_DEST_CHAT}")
    await admin_startup(client)
    asyncio.create_task(stats.loop(client))
    asyncio.create_task(admin_loop(client, stats))
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
