import os; os.environ.setdefault("LISTEN_SECONDS", "0")
import sys, json, time, tempfile, datetime as dt
os.environ.update(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="1", GITHUB_TOKEN="g", GITHUB_REPOSITORY="o/r")
HERE = os.getcwd()
sys.path.insert(0, HERE)
import tg_listen as L
import watchlib as W
d = tempfile.mkdtemp(); os.chdir(d)
commits = []
L.commit_files = lambda message, add=(), remove=(): commits.append((message, list(add)))
disp = []; L.dispatch = lambda mode, workflow=None: disp.append((mode, workflow))
L.last_run = lambda: None
L.update_status = lambda *a, **k: None
sent = []; edits = []; queue = []
def fake_tg(method, **p):
    if method == "getUpdates":
        if "allowed_updates" in p:
            ups = list(queue); queue.clear(); return {"result": ups}
        return {"result": []}
    if method == "sendMessage": sent.append(p); return {}
    if method == "editMessageText": edits.append(p); return {}
    return {}
L.tg = fake_tg
uid = [0]
def msg(t): uid[0]+=1; queue.append({"update_id": uid[0], "message": {"chat": {"id": 1}, "date": time.time(), "text": t}})
def cb(data, mid=7): uid[0]+=1; queue.append({"update_id": uid[0], "callback_query": {"id": "x", "data": data, "message": {"chat": {"id": 1}, "message_id": mid}}})
def run(*actions):
    sent.clear(); edits.clear()
    for a in actions: a()
    L.main()
def datas(p): return [[b["callback_data"] for b in r] for r in json.loads(p["reply_markup"])["inline_keyboard"]]
def texts(p): return [[b["text"] for b in r] for r in json.loads(p["reply_markup"])["inline_keyboard"]]
fails = []
def check(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c: fails.append(m)

# the button exists on the keyboard
kb = json.loads(L.keyboard())["keyboard"]
check(any(b["text"] == "🔕 Mute" for r in kb for b in r), "keyboard has a Mute button")

# tap Mute -> step 1
run(lambda: msg("🔕 Mute"))
check(len(sent) == 1 and "MUTE ALERTS" in sent[0]["text"], "Mute opens the menu")
dd = datas(sent[0]); print(dd)
check(["m:sunset", "m:events"] in dd and ["m:meetup"] in dd and ["m:all"] in dd, "menu offers the 3 watchers and Everything")
check(max(len(x.encode()) for r in dd for x in r) <= 64, "callback data within 64 bytes")
check(not any("r:all" in r for r in dd), "no 'Unmute all' while nothing is muted")

# choose Sun Set -> durations
run(lambda: cb("m:sunset"))
check(len(edits) == 1 and edits[0]["message_id"] == 7 and "For how long" in edits[0]["text"], "choosing a watcher asks for the length")
lens = [x for r in datas(edits[0]) for x in r if x.startswith("u:")]
check(lens == [f"u:sunset:{h}" for h in L.MUTE_HOURS], f"length buttons come from config: {L.MUTE_HOURS}")
check([b for r in texts(edits[0]) for b in r][:5] == ["2 h", "8 h", "1 day", "3 days", "7 days"], "length labels")

# 8 hours -> mute.txt written and committed
t0 = dt.datetime.now(dt.timezone.utc)
run(lambda: cb("u:sunset:8"))
mutes = W.mute_read()
check("sunset" in mutes and 7.9 < (mutes["sunset"] - t0).total_seconds() / 3600 < 8.1, "mute.txt holds Sun Set for 8 hours")
check(commits and commits[-1][1] == ["mute.txt"], "mute.txt was committed")
check(edits and "muted for 8 h" in edits[0]["text"] and json.loads(edits[0]["reply_markup"])["inline_keyboard"] == [], "confirmation replaces the menu")
check("events" not in mutes and "meetup" not in mutes, "other watchers stay on")

# status line
st = L.build_status.__code__ is not None
L.run_times = lambda wf: (dt.datetime.now(dt.timezone.utc), False)
text = L.build_status()
check("🔕 📅 Sun Set events: muted until" in text, "status shows the mute on the watcher line")
check("🏆 Padel-Austria tournaments: ✅" in text or "✅ 🏆 Padel-Austria tournaments" in text, "other watchers unchanged in status")

# menu now offers unmute
run(lambda: msg("mute"))
dd = datas(sent[0])
check(["r:all"] in dd, "'Unmute all' appears when something is muted")
check("muted until" in sent[0]["text"], "menu text lists the running mute")

# Everything for 24 h
run(lambda: cb("m:all"))
check("All alerts" in edits[0]["text"] and "r:all" in [x for r in datas(edits[0]) for x in r], "Everything: length menu with Unmute now")
run(lambda: cb("u:all:24"))
mutes = W.mute_read()
check(set(mutes) == {"sunset", "events", "meetup"}, "Everything mutes all three watchers")
check(all(23.9 < (u - t0).total_seconds() / 3600 < 24.1 for u in mutes.values()), "all for 24 hours")

# unmute one, then all
run(lambda: cb("r:events"))
check("events" not in W.mute_read() and "sunset" in W.mute_read(), "Unmute now removes only that watcher")
run(lambda: cb("r:all"))
check(W.mute_read() == {}, "Unmute all clears everything")
check(open("mute.txt").read() == "", "mute.txt is empty again")

# urgent: only offered while urgent is on; pause is used
run(lambda: msg("mute"))
check(not any("m:urgent" in r for r in datas(sent[0])), "no urgent entry while urgent is off")
open("urgent.txt", "w").write("on " + dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds") + " days=3\n")
run(lambda: msg("mute"))
check(["m:urgent"] in datas(sent[0]) or any("m:urgent" in r for r in datas(sent[0])), "urgent entry appears while urgent is on")
flags = []
L.write_flag = lambda on, opts=(), since=None, pause=None, message=None, reset_state=False: flags.append((on, pause, message))
run(lambda: cb("u:urgent:2"))
check(flags and flags[-1][0] and flags[-1][1] is not None and "paused" in flags[-1][2], "urgent mute = urgent pause with the chosen length")
check("urgent search is paused" in edits[0]["text"], "confirmation mentions the pause")

# garbage never crashes
run(lambda: cb("u:nonsense:5"), lambda: cb("u:sunset:999"), lambda: cb("m:"), lambda: cb("r:zzz"))
check(W.mute_read() == {}, "garbage taps change nothing")

# expired mutes are ignored
open("mute.txt", "w").write("sunset 2000-01-01T00:00:00+00:00\nmeetup garbage\nbogus 2099-01-01T00:00:00+00:00\n")
check(W.mute_read() == {} and W.muted("sunset") is None, "expired / broken lines are ignored")
open("mute.txt", "w").write("sunset 2099-01-01T00:00:00+00:00\n")
check(W.muted("sunset") is not None, "future mute is active")

# watchers honour the mute (no browser is started)
os.environ["MODE"] = ""
sys.path.insert(0, HERE)
import check as CK
import events_padel as EV
out = []
CK.set_output = lambda n, v: out.append((n, v))
CK.read_page = lambda: (_ for _ in ()).throw(AssertionError("page must not be read while muted"))
CK.main()
check(("changed", "false") in out, "check.py skips the run while muted")
open("mute.txt", "w").write("events 2099-01-01T00:00:00+00:00\n")
class NoBrowser:  # importing playwright would fail on purpose: the muted run must return before it
    pass
sys.modules["playwright"] = None; sys.modules["playwright.sync_api"] = None
EV.main()
check(True, "events_padel.py skips the hourly run while muted")
os.environ["MODE"] = "report"
try:
    EV.main()
    check(False, "report button must still run")
except Exception:
    check(True, "the Tournaments button still runs while muted")

print("FAILED:", fails) if fails else print("ALL OK")
sys.exit(1 if fails else 0)
