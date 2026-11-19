import os; os.environ.setdefault("LISTEN_SECONDS", "0")
import os, sys, json, time, tempfile
os.environ.update(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="1", GITHUB_TOKEN="g", GITHUB_REPOSITORY="o/r")
sys.path.insert(0, ".")
import tg_listen as L
d = tempfile.mkdtemp(); os.chdir(d)
L.commit_files = lambda *a, **k: None
disp = []; L.dispatch = lambda mode, workflow=None: disp.append((mode, workflow))
L.last_run = lambda: None
sent = []; queue = []
def fake_tg(method, **p):
    if method == "getUpdates":
        if "allowed_updates" in p:
            ups = list(queue); queue.clear(); return {"result": ups}
        return {"result": []}
    if method == "sendMessage":
        sent.append(p); return {}
    return {}
L.tg = fake_tg
uid = [0]
def msg(text):
    uid[0] += 1; queue.append({"update_id": uid[0], "message": {"chat": {"id": 1}, "date": time.time(), "text": text}})
def cb(data):
    uid[0] += 1; queue.append({"update_id": uid[0], "callback_query": {"id": "x", "data": data, "message": {"chat": {"id": 1}, "message_id": 5}}})
def kb(p):
    m = json.loads(p["reply_markup"])
    return [[b["text"] for b in r] for r in m["keyboard"]] if "keyboard" in m else ("INLINE", [[b["text"] for b in r] for r in m["inline_keyboard"]])
def step(label, *actions):
    sent.clear()
    for a in actions: a()
    L.main()
    print(f"--- {label}")
    for p in sent: print("  say:", p["text"][:70].replace("\n"," | "), "\n  keys:", kb(p))
    print("  flag:", open("urgent.txt").read().strip() if os.path.exists("urgent.txt") else "(none)", "| dispatched:", disp[-1:] )
step("1 idle, 'help'", lambda: msg("help"))
step("2 tap Urgent padel", lambda: msg("🚨 Urgent padel"))
step("3 choose Evenings (small button)", lambda: cb("start_evenings"))
step("4 pause", lambda: msg("🔕 Pause alerts 2h"))
step("5 resume", lambda: msg("▶️ Resume alerts"))
step("6 stop", lambda: msg("🛑 Stop urgent check"))
step("7 Sun Set + tournaments", lambda: msg("📅 Sun Set events"), lambda: msg("🏆 Padel-Austria tournaments"))
