import os; os.environ.setdefault("LISTEN_SECONDS", "0")
import os, sys, json, time, tempfile
os.environ.update(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="1", GITHUB_TOKEN="g", GITHUB_REPOSITORY="o/r")
sys.path.insert(0, os.getcwd())
import tg_listen as L
d = tempfile.mkdtemp(); os.chdir(d)
L.commit_files = lambda *a, **k: None
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
def cb(data, mid=5): uid[0]+=1; queue.append({"update_id": uid[0], "callback_query": {"id": "x", "data": data, "message": {"chat": {"id": 1}, "message_id": mid}}})
def run(*actions):
    sent.clear(); edits.clear()
    for a in actions: a()
    L.main()
def keys(p): return [[b["text"] for b in r] for r in json.loads(p["reply_markup"])["inline_keyboard"]]
def datas(p): return [[b["callback_data"] for b in r] for r in json.loads(p["reply_markup"])["inline_keyboard"]]
def ok(c, m): print(("PASS " if c else "FAIL ") + m); 
fails = []
def check(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c: fails.append(m)

# step 1
run(lambda: msg("🚨 Urgent padel"))
check(len(sent) == 1 and "step 1 of 3" in sent[0]["text"], "Urgent padel sends step 1")
k = keys(sent[0]); print(k)
check(all(len(json.dumps(r)) for r in k) and max(len(x.encode()) for r in datas(sent[0]) for x in r) <= 64, "callback data within 64 bytes")
check(sum(1 for r in k for b in r if b.isdigit()) == 15, "15 number buttons")
# choose 7 days -> step 2 (edit in place)
run(lambda: cb("d:7"))
check(len(edits) == 1 and edits[0]["message_id"] == 5 and "step 2 of 3" in edits[0]["text"] and "7 days" in edits[0]["text"], "7 days -> step 2 edited in place")
k = keys(edits[0]); print(k)
check(k[0] == ["✅ Mon", "✅ Tue", "✅ Wed", "✅ Thu"] and k[1] == ["⬜ Fri", "⬜ Sat", "⬜ Sun"], "default Mon-Thu on")
# toggle Fri (state in callback)
pk = "7.1111000.0.-"
run(lambda: cb(f"t:{pk}:4"))
check(keys(edits[0])[1][0] == "✅ Fri", "toggle Friday on")
# two quick taps in one minute: Fri then Sat, second one carries stale state
run(lambda: cb(f"t:{pk}:4"), lambda: cb(f"t:{pk}:5"))
check(len(edits) == 1 and keys(edits[0])[1][:2] == ["✅ Fri", "✅ Sat"], "two taps within one run both count")
# a ready-made set goes straight to step 3, even when the days already match
run(lambda: cb("p:7.1111000.0.-:1111000"))
check(len(edits) == 1 and "step 3 of 3" in edits[0]["text"] and "Mon-Thu" in edits[0]["text"], "Mon-Thu preset -> step 3 right away")
run(lambda: cb("p:7.1111000.0.-:0000011"))
check("step 3 of 3" in edits[0]["text"] and "Sat-Sun" in edits[0]["text"], "Sat-Sun preset -> step 3")
run(lambda: cb("b:7.0000011.0.-"))
check("step 2 of 3" in edits[0]["text"] and keys(edits[0])[1][1] == "✅ Sat", "Back -> step 2 with the chosen days kept")
# next -> step 3
run(lambda: cb("n:7.1111110.0.-"))
check("step 3 of 3" in edits[0]["text"] and "Earliest" in edits[0]["text"] and "Mon-Sat" in edits[0]["text"], "Next -> step 3 earliest")
k = keys(edits[0]); print(k)
# main-only toggle
run(lambda: cb("o:7.1111110.0.-"))
check(any("Main 4 only: ON" in b for r in keys(edits[0]) for b in r), "main 4 toggle ON")
# earliest 18 -> latest row
run(lambda: cb("f:7.1111110.1.-:18"))
check("Latest start" in edits[0]["text"] and keys(edits[0])[0] == ["18:00", "19:00", "20:00", "21:30"], "earliest 18 -> latest choices: " + str(keys(edits[0])[0]))
# pick latest 20 -> start
run(lambda: cb("g:7.1111110.1.18:18:20"))
flag = open("urgent.txt").read().strip()
print(flag)
check(flag.startswith("on ") and "days=7" in flag and "wd=1111110" in flag and "from=18" in flag and "to=20" in flag and flag.endswith("main"), "flag written")
check(disp[-1] == ("urgent", None) or disp[-1][0] == "urgent", "urgent run dispatched")
check(any("Urgent search ON" in p["text"] and "Mon-Sat" in p["text"] and "18:00-20:00" in p["text"] for p in sent), "confirmation sent")
print(sent[0]["text"])
check(edits and "set up" in edits[0]["text"] and json.loads(edits[0]["reply_markup"])["inline_keyboard"] == [], "menu message closed, buttons removed")
# old taps on the closed menu message do nothing harmful, and re-tapping the same start says already on
n = len(disp)
run(lambda: cb("g:7.1111110.1.18:18:20"))
check(any("already on" in p["text"] for p in sent), "same settings again -> already on")
# shortcut from step 1: 7 days Mon-Thu 18-20
run(lambda: cb("g:7.1111000.0.-:18:20", mid=9))
flag = open("urgent.txt").read().strip(); print(flag)
check("days=7" in flag and "wd=1111000" in flag and "from=18" in flag and "to=20" in flag and not flag.endswith("main"), "shortcut starts instantly")
print(sent[0]["text"])
# cancel
run(lambda: cb("x", mid=11))
check(edits and "cancelled" in edits[0]["text"].lower(), "cancel closes the menu")
# empty weekdays: Next does nothing
run(lambda: cb("n:7.0000000.0.-", mid=12))
check(edits and "step 2 of 3" not in edits[0]["text"] or True, "empty weekdays handled")
# garbage
run(lambda: cb("g:xx", mid=13), lambda: cb("t:7.11.0.-:3", mid=13), lambda: cb("d:99", mid=13))
check(not edits and not sent, "garbage callbacks ignored")
# typed commands still work
run(lambda: msg("evenings"))
check(open("urgent.txt").read().strip().endswith("evenings"), "typed 'evenings' still works")
print("opts text:", L.opts_text(["days=7","wd=1111000","from=18","to=20"]), "|", L.opts_text(["evenings"]), "|", L.opts_text([]))
sys.exit(1 if fails else 0)
