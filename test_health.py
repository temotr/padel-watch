import os, sys, tempfile, datetime as dt
sys.path.insert(0, os.getcwd())
import watchlib as W
os.chdir(tempfile.mkdtemp())
fails = []
def check(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c: fails.append(m)
sent = []
def send(t): sent.append(t); return True
T = dt.datetime(2026, 11, 20, 12, 0, tzinfo=dt.timezone.utc)

# one good run: nothing happens, no file
check(W.health_report("sunset", True, send=send, now=T) is None and not sent and not W.health_path("sunset").exists(), "good run writes nothing")
# first failure: no message yet (threshold 2), file counts 1
check(W.health_report("sunset", False, "no events found on the page", send=send, now=T) is None and not sent, "1st failure is silent")
st = W.health_read("sunset")
check(st and st["fails"] == 1 and not st["warned"], "failure counter = 1")
# second failure: ONE warning
m = W.health_report("sunset", False, "no events found on the page", send=send, now=T + dt.timedelta(minutes=5))
check(m and len(sent) == 1 and "page looks different" in sent[0] and "Sun Set events" in sent[0] and "2 times in a row" in sent[0], "2nd failure sends one warning")
check("no events found" in sent[0], "warning names the reason")
# third, fourth: no more spam
W.health_report("sunset", False, "x", send=send, now=T + dt.timedelta(minutes=10))
W.health_report("sunset", False, "x", send=send, now=T + dt.timedelta(minutes=15))
check(len(sent) == 1, "no repeated warnings")
check(W.health_read("sunset")["since"] == T and W.health_read("sunset")["fails"] == 4, "since stays at the first failure")
# recovery: one message, file reset
m = W.health_report("sunset", True, send=send, now=T + dt.timedelta(minutes=20))
check(m and len(sent) == 2 and "can be read again" in sent[1], "recovery message")
check(W.health_read("sunset") is None and open("health_sunset.txt").read().strip() == "ok", "file back to ok")
check(W.health_report("sunset", True, send=send, now=T) is None and len(sent) == 2, "good runs after recovery are silent")
# threshold 1 (button): warns at once
sent.clear()
W.health_report("courts", False, "no location could be read", threshold=1, send=send, now=T)
check(len(sent) == 1 and "Court checks" in sent[0], "threshold 1 warns immediately")
# a failed Telegram send is retried next run
sent.clear()
W.health_report("events", False, "a", send=lambda t: False, now=T)
W.health_report("events", False, "a", send=lambda t: False, now=T)
check(W.health_read("events")["warned"] is False, "unsent warning is not marked as sent")
W.health_report("events", False, "a", send=send, now=T)
check(len(sent) == 1 and W.health_read("events")["warned"], "warning goes out when Telegram works again")
# detail with odd characters never breaks the file
W.health_report("events", False, "a | b\nc", send=send, now=T)
check(W.health_read("events") is not None, "special characters in the reason are safe")
# broken file reads as ok
open("health_events.txt", "w").write("garbage\n")
check(W.health_read("events") is None, "broken health file is treated as ok")

# ---- end to end: check.py on a page that cannot be read
import check as CK
msgs = []
W.telegram = lambda t: (msgs.append(t), True)[1]
orig = W.health_report
W.health_report = lambda name, ok, detail="", threshold=2, send=None, now=None: orig(name, ok, detail, threshold, send or W.telegram, now)
CK.set_output = lambda n, v: None
os.chdir(tempfile.mkdtemp())
CK.read_page = lambda: "maintenance"
for i in range(3):
    try:
        CK.main(); check(False, "check.py must stop on an unreadable page")
    except SystemExit as e:
        check(e.code == 2, f"run {i + 1}: check.py exits with code 2")
check(len(msgs) == 1 and "Sun Set events" in msgs[0], "3 unreadable runs -> exactly one Telegram warning")
check("fails=3" in open("health_sunset.txt").read(), "health_sunset.txt counts the failures")
def boom(): raise RuntimeError("timeout")
CK.read_page = boom
try:
    CK.main()
except RuntimeError:
    check("fails=4" in open("health_sunset.txt").read() and "RuntimeError" in open("health_sunset.txt").read(), "a crash while loading also counts, reason saved")
# a muted run does not touch the health counter
open("mute.txt", "w").write("sunset 2099-01-01T00:00:00+00:00\n")
CK.main()
check("fails=4" in open("health_sunset.txt").read(), "muted run leaves the counter alone")
os.remove("mute.txt")
# good page again -> recovery message
CK.read_page = lambda: "\n".join(["x", "Upcoming Events", "Past Events", "a", "b", "c"])
msgs.clear()
try:
    CK.main()
except SystemExit:
    pass
check(any("can be read again" in m for m in msgs) and open("health_sunset.txt").read().strip() == "ok", "readable page again -> recovery message, counter reset")

# ---- end to end: courts.py when no location can be read
import courts as C
os.chdir(tempfile.mkdtemp())
C.time.sleep = lambda s: None
def no_page(page, url): raise RuntimeError("blocked")
C.fetch_day = no_page
msgs.clear()
os.environ["MODE"] = "report"          # button: warn at once
try:
    C.run_all(None, now=dt.datetime(2026, 11, 20, 12, 0, tzinfo=C.TZ))
    check(False, "courts must stop when nothing can be read")
except SystemExit as e:
    check(e.code == 2, "courts.py exits with code 2")
check(len(msgs) == 1 and "Court checks" in msgs[0], "button run: warning at once")
os.environ["MODE"] = ""
os.remove("health_courts.txt")
msgs.clear()
for i in range(2):
    try:
        C.run_all(None, now=dt.datetime(2026, 11, 20, 12, 0, tzinfo=C.TZ))
    except SystemExit:
        pass
    check(len(msgs) == (0 if i == 0 else 1), f"automatic run {i + 1}: warning only after the 2nd failure")
print("FAILED:", fails) if fails else print("ALL OK")
sys.exit(1 if fails else 0)
