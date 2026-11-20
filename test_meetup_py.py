import os, threading, http.server, importlib
os.environ["TELEGRAM_BOT_TOKEN"] = "x"; os.environ["TELEGRAM_CHAT_ID"] = "1"
BODY = ("# v1\n2099-02-12|19:00|Afterwork Indoor Padel at Padelzone Floridsdorf|Padelzone Floridsdorf|€20.00|13|https://www.meetup.com/g/events/1/\n"
        "2099-02-12|21:00|Late <Padel> & more|||5|\n"
        "2099-05-16|16:00|Friday Padel Night||||https://www.meetup.com/g/events/2/\n"
        "2000-01-01|10:00|Old one|||1|https://x\n")
mode = {"body": BODY}
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(mode["body"].encode())
    def log_message(self, *a): pass
srv = http.server.HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["MEETUP_EVENTS_URL"] = f"http://127.0.0.1:{srv.server_port}/exec?key=k"
import tg_listen as t
out = t.meetup_text(); print(out)
assert "Afterwork Indoor Padel" in out and "Old one" not in out and "Late &lt;Padel&gt; &amp; more" in out
assert out.count("Fri 16.05.") == 1 and out.count("Thu 12.02.") == 0 or "12.02." in out
mode["body"] = "forbidden"; assert "did not answer correctly" in t.meetup_text()
mode["body"] = "# v1\n"; assert "No upcoming padel events" in t.meetup_text()
t.MEETUP_URL = ""; assert "not connected" in t.meetup_text()
assert t.COMMANDS["meetup events"] == "meetup" and "Meetup" in t.keyboard()
# status line
t.MEETUP_URL = os.environ["MEETUP_EVENTS_URL"]
import datetime as dt
now = dt.datetime.now(dt.timezone.utc)
iso = lambda d: d.isoformat(timespec="seconds")
mode["body"] = f"# v1 checked={iso(now - dt.timedelta(minutes=3))} changed={iso(now - dt.timedelta(days=1))}\n"
line = t.meetup_status_line({}, now)
print(line); assert line.startswith("✅ 🤝 Meetup events (every 5 min): last check") and "change" not in line
mode["body"] = f"# v1 checked={iso(now - dt.timedelta(minutes=3))} changed=-\n"
assert t.meetup_status_line({}, now).endswith("last check " + t.stamp(now - dt.timedelta(minutes=3)))
mode["body"] = f"# v1 checked={iso(now - dt.timedelta(minutes=45))} changed=-\n"
assert t.meetup_status_line({}, now).startswith("⚠️") and "no recent check" in t.meetup_status_line({}, now)
mode["body"] = "# v1\n"
assert "update the Google script" in t.meetup_status_line({}, now)
mode["body"] = "forbidden"; assert t.meetup_status_line({}, now).startswith("❓")
assert t.meetup_status_line({"meetup": now + dt.timedelta(hours=2)}, now).startswith("🔕")
t.MEETUP_URL = ""; assert "not connected yet" in t.meetup_status_line({}, now)
print("ALL OK")
