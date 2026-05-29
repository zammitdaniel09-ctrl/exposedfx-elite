# telegram_worker/worker.py
import os, json, base64, asyncio, logging
from pathlib import Path
import requests
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageMediaWebPage
from telegram_worker.routes import ROUTES
from telegram_worker.parser import parse_signal

logging.basicConfig(level=os.environ.get("LOG_LEVEL","INFO").upper(), format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log=logging.getLogger("imperium-worker")
API_ID=int(os.environ["API_ID"]); API_HASH=os.environ["API_HASH"]
SERVER_URL=os.environ.get("SERVER_URL","").rstrip("/"); AUTO_TOKEN=os.environ.get("AUTO_TOKEN","change-this-token")
DATA_DIR=Path(os.environ.get("DATA_DIR") or ("/data" if Path("/data").exists() else "./data")); DATA_DIR.mkdir(parents=True,exist_ok=True)
SESSION_PATH=DATA_DIR/"session"; SESSION_FILE=DATA_DIR/"session.session"; MAP_FILE=DATA_DIR/"message_map.json"
DRY_RUN=os.environ.get("DRY_RUN","0").strip()=="1"
if not SESSION_FILE.exists() and os.environ.get("SESSION_B64"):
    SESSION_FILE.write_bytes(base64.b64decode(os.environ["SESSION_B64"])); log.info("Restored Telegram session from SESSION_B64")
client=TelegramClient(str(SESSION_PATH), API_ID, API_HASH)
SOURCE_CHATS=sorted(set(r["source_chat"] for r in ROUTES)); processed=set()
try: msg_map=json.loads(MAP_FILE.read_text(encoding="utf-8")) if MAP_FILE.exists() else {}
except Exception: msg_map={}

def save_map():
    tmp=MAP_FILE.with_suffix(".tmp"); tmp.write_text(json.dumps(msg_map),encoding="utf-8"); tmp.replace(MAP_FILE)
def text(m): return (m.raw_text or m.text or m.message or "") if m else ""
def topic(m):
    r=getattr(m,"reply_to",None); return (getattr(r,"reply_to_top_id",None) or getattr(r,"reply_to_msg_id",None)) if r else None
def routes_for(chat,top):
    return [r for r in ROUTES if r["source_chat"]==chat and (r["source_topic"] is None or r["source_topic"]==top)]
def media(m): return bool(getattr(m,"media",None)) and not isinstance(m.media, MessageMediaWebPage)
def key(sc,mid,dc,dt): return f"{sc}:{mid}:{dc}:{dt}"
def dest_reply(m,r):
    rep=getattr(m,"reply_to",None); rid=getattr(rep,"reply_to_msg_id",None) if rep else None
    if rid and not (r.get("source_topic") and rid==r["source_topic"]):
        got=msg_map.get(key(r["source_chat"],rid,r["dest_chat"],r["dest_topic"]))
        if got: return int(got)
    return r["dest_topic"]
def store(src,dst,r):
    if src and dst:
        msg_map[key(r["source_chat"],src.id,r["dest_chat"],r["dest_topic"])]=dst.id; save_map()
def post_signal(r,m,raw):
    if not SERVER_URL or not raw: return
    parsed=parse_signal(raw)
    if not parsed: return
    dk=f"{r['source_chat']}:{m.id}"
    if dk in processed: return
    processed.add(dk)
    payload={"source":r["name"],"source_chat_id":r["source_chat"],"source_message_id":m.id,"raw_text":raw,**parsed}
    if DRY_RUN: log.info(f"[DRY_RUN signal] {r['name']} {parsed['direction']} {parsed['symbol']}"); return
    try:
        res=requests.post(f"{SERVER_URL}/api/v1/signals",json=payload,headers={"X-AUTO-TOKEN":AUTO_TOKEN},timeout=12)
        if res.status_code>=400: log.warning(f"[signal rejected] {r['name']} {res.status_code}: {res.text}")
        else: log.info(f"[signal-posted] {r['name']} id={res.json().get('id')} {parsed['direction']} {parsed['symbol']}")
    except Exception as e: log.error(f"[signal-post-failed] {r['name']}: {e}")
async def send_one(m,r):
    if DRY_RUN: return "dry-run-copy"
    raw=text(m); ents=getattr(m,"entities",None); reply_to=dest_reply(m,r)
    if media(m): sent=await client.send_file(r["dest_chat"],m.media,caption=raw or None,formatting_entities=ents if raw else None,reply_to=reply_to)
    elif raw: sent=await client.send_message(r["dest_chat"],raw,formatting_entities=ents,reply_to=reply_to,link_preview=True)
    else: sent=await client.send_message(r["dest_chat"],"Unsupported message type.",reply_to=reply_to)
    store(m,sent,r); return "sent-as-self"
async def send_album(msgs,r):
    if DRY_RUN: return "dry-run-album"
    files=[m.media for m in msgs if media(m)]; cap=None; ents=None
    for m in msgs:
        if text(m): cap=text(m); ents=getattr(m,"entities",None); break
    if not files: return await send_one(msgs[0],r)
    sent=await client.send_file(r["dest_chat"],files,caption=cap,formatting_entities=ents,reply_to=dest_reply(msgs[0],r))
    if isinstance(sent,list):
        for a,b in zip(msgs,sent): store(a,b,r)
    else: store(msgs[0],sent,r)
    return f"sent-album-{len(files)}"
async def safe(coro):
    try: return await coro
    except FloodWaitError as e:
        await asyncio.sleep(min(e.seconds+1,60)); return "floodwait-retry-needed"
    except Exception as e: return f"send-failed: {e}"
@client.on(events.Album(chats=SOURCE_CHATS))
async def on_album(ev):
    if not ev.messages: return
    chat=ev.chat_id; top=topic(ev.messages[0]); raw=next((text(m) for m in ev.messages if text(m)),"")
    for r in routes_for(chat,top):
        status=await safe(send_album(ev.messages,r)); post_signal(r,ev.messages[0],raw); log.info(f"[{status}] {r['name']} album source={chat}_{top} -> dest={r['dest_chat']}_{r['dest_topic']}")
@client.on(events.NewMessage(chats=SOURCE_CHATS,incoming=True))
async def on_msg(ev):
    if getattr(ev.message,"grouped_id",None): return
    chat=ev.chat_id; top=topic(ev.message); raw=text(ev.message)
    for r in routes_for(chat,top):
        status=await safe(send_one(ev.message,r)); post_signal(r,ev.message,raw); log.info(f"[{status}] {r['name']} source={chat}_{top} -> dest={r['dest_chat']}_{r['dest_topic']}")
async def main():
    await client.start(); me=await client.get_me(); log.info(f"Logged in as {me.first_name} | id={me.id}"); log.info(f"SERVER_URL={SERVER_URL}"); log.info(f"Watching {len(SOURCE_CHATS)} source chats"); await client.run_until_disconnected()
if __name__=="__main__": asyncio.run(main())

