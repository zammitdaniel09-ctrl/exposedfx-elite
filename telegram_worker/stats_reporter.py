import re
import sqlite3
import time
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from openpyxl import Workbook


DEST_CHAT = -100395216203
DEST_TOPIC = None
TZ_NAME = "Europe/Malta"


def parse_stats_result(text):
    text = (text or "").replace(",", " ")
    upper = re.sub(r"\s+", " ", text.upper()).strip()
    if not upper:
        return None
    if any(x in upper for x in ["PIP VALUE", "WHAT IS A PIP", "EXAMPLE"]):
        return None

    pip_vals = []
    for m in re.finditer(r"([+-]?\d+(?:\.\d+)?)\s*(?:PIP|PIPS)\b", upper):
        pip_vals.append(float(m.group(1)))
    pips = max(pip_vals, key=lambda x: abs(x)) if pip_vals else None

    stopped = any(x in upper for x in ["SL HIT", "STOP LOSS HIT", "HIT SL", "STOPPED OUT"])
    if stopped:
        return {"status": "LOSS", "pips": -abs(pips) if pips is not None else 0.0}
    if pips is None:
        return None
    if pips < 0:
        return {"status": "LOSS", "pips": pips}

    result_words = ["PIPS", "TP HIT", "PROFIT", "CLOSED", "SECURED", "BANKED", "BOOKED", "CAUGHT", "SMASHED"]
    if any(x in upper for x in result_words):
        return {"status": "WIN", "pips": abs(pips)}
    return None


class WeeklyStats:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "stats.sqlite"
        self.report_dir = self.data_dir / "reports"
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.tz = ZoneInfo(TZ_NAME)
        self.init_db()

    def connect(self):
        con = sqlite3.connect(str(self.db_path))
        con.row_factory = sqlite3.Row
        return con

    def init_db(self):
        con = self.connect()
        con.execute("CREATE TABLE IF NOT EXISTS results(id INTEGER PRIMARY KEY AUTOINCREMENT, msg_key TEXT UNIQUE, source TEXT, status TEXT, pips REAL, raw_text TEXT, created_at REAL)")
        con.execute("CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY AUTOINCREMENT, week_no INTEGER, start_ts REAL, end_ts REAL, total_pips REAL, file_path TEXT, sent_at REAL)")
        con.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
        con.commit(); con.close()

    def log_message(self, route, message, text):
        result = parse_stats_result(text)
        if not result:
            return None
        msg_key = f"{route.get('source_chat')}:{getattr(message, 'id', '')}"
        con = self.connect()
        try:
            con.execute("INSERT INTO results(msg_key, source, status, pips, raw_text, created_at) VALUES(?,?,?,?,?,?)", (msg_key, route.get("name", "Unknown"), result["status"], result["pips"], text, time.time()))
            con.commit()
            return result
        except sqlite3.IntegrityError:
            return None
        finally:
            con.close()

    def period(self):
        now = datetime.now(self.tz)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        this_sunday = today - timedelta(days=(today.weekday() - 6) % 7)
        start = this_sunday - timedelta(days=7)
        end = this_sunday
        return start.timestamp(), end.timestamp(), start.strftime("%d %b %Y"), (end - timedelta(seconds=1)).strftime("%d %b %Y")

    def should_send(self):
        now = datetime.now(self.tz)
        if now.weekday() != 6 or now.hour != 0:
            return False
        start_ts, _, _, _ = self.period()
        key = f"sent:{int(start_ts)}"
        con = self.connect(); row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone(); con.close()
        return row is None

    def mark_sent(self, start_ts):
        con = self.connect(); con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (f"sent:{int(start_ts)}", str(time.time()))); con.commit(); con.close()

    def next_week_no(self):
        con = self.connect(); row = con.execute("SELECT MAX(week_no) AS n FROM reports").fetchone(); con.close()
        return int(row["n"] or 0) + 1

    def build_report(self):
        start_ts, end_ts, start_label, end_label = self.period()
        con = self.connect()
        rows = con.execute("SELECT * FROM results WHERE created_at>=? AND created_at<? ORDER BY created_at", (start_ts, end_ts)).fetchall()
        week_no = self.next_week_no()
        wins = sum(1 for r in rows if r["status"] == "WIN")
        losses = sum(1 for r in rows if r["status"] == "LOSS")
        total = wins + losses
        total_pips = sum(float(r["pips"] or 0) for r in rows)
        avg_pips = total_pips / total if total else 0
        win_rate = wins / total * 100 if total else 0
        by_source = {}
        for r in rows:
            by_source[r["source"]] = by_source.get(r["source"], 0) + float(r["pips"] or 0)
        best_source = max(by_source.items(), key=lambda x: x[1])[0] if by_source else "N/A"
        best_trade = max([r for r in rows if r["status"] == "WIN"], key=lambda r: float(r["pips"] or 0), default=None)
        best_trade_text = f"{best_trade['source']} | {float(best_trade['pips']):.1f} pips" if best_trade else "N/A"

        wb = Workbook()
        ws = wb.active; ws.title = "Summary"
        ws.append([f"ExposedFX Preview - Week {week_no}"])
        ws.append(["Period", f"{start_label} - {end_label}"])
        ws.append(["Wins", wins]); ws.append(["Losses", losses]); ws.append(["Win Rate", f"{win_rate:.2f}%"])
        ws.append(["Total Pips", round(total_pips, 1)]); ws.append(["Average Pips", round(avg_pips, 1)])
        ws.append(["Best Signal Group", best_source]); ws.append(["Best Trade", best_trade_text])
        trades = wb.create_sheet("Trades"); trades.append(["Date", "Source", "Result", "Pips", "Message"])
        for r in rows:
            dt = datetime.fromtimestamp(float(r["created_at"]), self.tz).strftime("%d %b %Y %H:%M")
            trades.append([dt, r["source"], r["status"], float(r["pips"] or 0), r["raw_text"]])
        file_path = self.report_dir / f"ExposedFX_Preview_Week_{week_no}.xlsx"
        wb.save(file_path)
        con.execute("INSERT INTO reports(week_no,start_ts,end_ts,total_pips,file_path,sent_at) VALUES(?,?,?,?,?,?)", (week_no, start_ts, end_ts, total_pips, str(file_path), time.time()))
        con.commit()
        best_week = con.execute("SELECT week_no,total_pips FROM reports ORDER BY total_pips DESC LIMIT 1").fetchone()
        con.close()
        caption = f"📊 ExposedFX Preview Weekly Stats - Week {week_no}\nPeriod: {start_label} - {end_label}\n\n✅ Wins: {wins}\n❌ Losses: {losses}\n🎯 Win Rate: {win_rate:.2f}%\n📈 Total Pips: {total_pips:.1f}\n⚡ Average Pips: {avg_pips:.1f}\n🏆 Best Group: {best_source}\n🥇 Best Trade: {best_trade_text}\n🔥 Best Week: Week {best_week['week_no']} | {float(best_week['total_pips']):.1f} pips\n\nFull spreadsheet attached."
        return start_ts, file_path, caption

    async def loop(self, client):
        while True:
            try:
                if self.should_send():
                    start_ts, file_path, caption = self.build_report()
                    await client.send_file(DEST_CHAT, file_path, caption=caption, reply_to=DEST_TOPIC)
                    self.mark_sent(start_ts)
            except Exception as exc:
                print(f"[stats reporter failed] {exc}")
            await asyncio.sleep(60)
