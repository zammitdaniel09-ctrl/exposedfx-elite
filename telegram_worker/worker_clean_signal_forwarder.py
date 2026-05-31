import asyncio
import base64
import logging
import os
import re
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("clean-signal-forwarder")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

SOURCE_CHAT = int(os.environ.get("CLEAN_FORWARD_SOURCE_CHAT", "-5252460120"))
DEST_CHAT = int(os.environ.get("CLEAN_FORWARD_DEST_CHAT", "-5144279180"))

# 1 = send as clean copied message, 0 = Telegram forward
# I recommend 1 so the new group only sees clean signals, no forwarded/source mess.
COPY_MODE = os.environ.get("CLEAN_FORWARD_COPY_MODE", "1").strip() == "1"
STRICT_ONLY_AI_FORMAT = os.environ.get("STRICT_ONLY_AI_FORMAT", "1").strip() == "1"

DATA_DIR = Path(os.environ.get("DATA_DIR") or "./data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

SESSION_BASE = DATA_DIR / "clean_forwarder_session"
SESSION_FILE = DATA_DIR / "clean_forwarder_session.session"


def clean_b64(value: str) -> str:
    return "".join((value or "").split()).strip()


def combined_session_blob() -> str:
    direct = clean_b64(os.environ.get("SESSION_B64", ""))
    if direct:
        return direct

    count_raw = os.environ.get("SESSION_B64_CHUNKS", "").strip()
    if count_raw:
        count = int(count_raw)
        chunks = []
        for i in range(1, count + 1):
            chunk = clean_b64(os.environ.get(f"SESSION_B64_{i}", ""))
            if not chunk:
                raise RuntimeError(f"SESSION_B64_CHUNKS={count}, but SESSION_B64_{i} is missing")
            chunks.append(chunk)
        return "".join(chunks)

    chunks = []
    i = 1
    while True:
        chunk = clean_b64(os.environ.get(f"SESSION_B64_{i}", ""))
        if not chunk:
            break
        chunks.append(chunk)
        i += 1

    if chunks:
        return "".join(chunks)

    raise RuntimeError("No SESSION_B64 / SESSION_B64_1 variables found")


def write_session_file():
    raw = base64.b64decode(combined_session_blob())
    SESSION_FILE.write_bytes(raw)
    log.info(f"Session written: {SESSION_FILE} bytes={len(raw)}")


write_session_file()
client = TelegramClient(str(SESSION_BASE), API_ID, API_HASH)


HEADER_RE = re.compile(
    r"^(?:\s|[^\w])*"
    r"(BUY|SELL)\s+([A-Z0-9/ ]{3,30})\s+(?:INTRADAY\s+)?ZONE\b",
    re.IGNORECASE,
)


def text_of(message) -> str:
    return message.message or message.raw_text or message.text or ""


def is_my_formatted_signal(text: str) -> bool:
    """
    FINAL GROUP RULE:
    Only forward completed AI formatted signal templates.
    Nothing else is allowed.
    """
    t = (text or "").strip()
    low = t.lower()

    if not t:
        return False

    blocked = [
        "source:",
        "exposedfx |",
        "forwarded from",
        "partial",
        "take further",
        "take a partial",
        "running trade",
        "remainder should run",
        "set stop loss",
        "move stop loss",
        "move sl",
        "click close",
        "edit the lot size",
        "pips",
        "ended with",
        "weekly recap",
        "daily recap",
        "results",
        "profit today",
    ]

    if any(x in low for x in blocked):
        return False

    if not HEADER_RE.search(t):
        return False

    required = [
        r"•\s*(?:buy|sell)\s+point\s*:\s*[-+]?\d",
        r"•\s*layer\s+point\s*:\s*[-+]?\d",
        r"•\s*stop\s+loss\s*:\s*[-+]?\d",
        r"tp1\s*-\s*[-+]?\d",
        r"tp2\s*-\s*[-+]?\d",
        r"tp3\s*-\s*[-+]?\d",
        r"tp8\s*-\s*[-+]?\d",
        r"tp9\s*-\s*open",
        r"risk\s*:\s*(?:low|medium|high)",
        r"tips\s*:",
        r"breakeven\s+after\s+tp1\s+hit",
        r"use\s+correct\s+risk\s+management",
        r"take\s+spread\s+into\s+consideration",
        r"this\s+is\s+not\s+financial\s+advice",
    ]

    return all(re.search(pattern, t, re.IGNORECASE) for pattern in required)


@client.on(events.NewMessage(chats=SOURCE_CHAT))
async def on_message(event):
    message = event.message
    text = text_of(message)

    if not is_my_formatted_signal(text):
        log.info(f"[SKIP] msg={message.id} text={text[:80]!r}")
        return

    try:
        if COPY_MODE:
            await client.send_message(
                DEST_CHAT,
                text,
                formatting_entities=getattr(message, "entities", None),
                parse_mode=None,
                link_preview=False,
            )
            log.info(f"[COPIED SIGNAL] msg={message.id} -> {DEST_CHAT}")
        else:
            await client.forward_messages(DEST_CHAT, message)
            log.info(f"[FORWARDED SIGNAL] msg={message.id} -> {DEST_CHAT}")

    except FloodWaitError as exc:
        wait = min(int(exc.seconds) + 1, 60)
        log.warning(f"FloodWait {exc.seconds}s; sleeping {wait}s")
        await asyncio.sleep(wait)

    except Exception as exc:
        log.exception(f"[FAILED] msg={message.id}: {exc}")


async def main():
    await client.connect()

    if not await client.is_user_authorized():
        raise RuntimeError("Telegram session loaded but account is not authorised. Generate a fresh session for this service.")

    me = await client.get_me()
    log.info(f"Logged in as {me.first_name} | id={me.id}")
    log.info(f"Source={SOURCE_CHAT} Destination={DEST_CHAT} CopyMode={COPY_MODE}")
    log.info("Clean formatted signal forwarder running...")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
