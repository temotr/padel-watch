import os, sys, json, time, tempfile, threading, urllib.error, io
os.environ.update(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="1", GITHUB_TOKEN="g", GITHUB_REPOSITORY="o/r", LISTEN_SECONDS="8")
sys.path.insert(0, os.getcwd())
import tg_listen as L
d = tempfile.mkdtemp(); os.chdir(d)
L.commit_files = lambda *a, **k: None
disp = []; L.dispatch = lambda mode, workflow=None: disp.append(mode)
L.last_run = lambda: None
L.update_status = lambda *a, **k: None
L.check_token_reminder = lambda: None
queue = []; handled = []; waits = []; flaky = [True]
sent = []
def fake_tg(method, **p):
    if method == "getUpdates":
        if "allowed_updates" in p:
            waits.append((p.get("timeout"), p.get("_timeout")))
            if flaky[0] and len(waits) == 2:            # one conflict error in the middle
                flaky[0] = False
                raise urllib.error.HTTPError("u", 409, "Conflict", {}, io.BytesIO(b"{}"))
            end = time.time() + min(int(p.get("timeout") or 0), 1.0)   # fake long poll, shortened
            while time.time() < end and not queue: time.sleep(0.05)
            ups = list(queue); queue.clear(); return {"result": ups}
        return {"result": []}
    if method == "sendMessage": sent.append((time.time(), p["text"])); return {}
    return {}
L.tg = fake_tg
def later(delay, update):
    threading.Timer(delay, lambda: queue.append(update)).start()
t0 = time.time()
later(2.0, {"update_id": 1, "message": {"chat": {"id": 1}, "date": time.time() + 2, "text": "help"}})
later(5.0, {"update_id": 2, "message": {"chat": {"id": 1}, "date": time.time() + 5, "text": "pause"}})
L.main()
total = time.time() - t0
fails = []
def check(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c: fails.append(m)
print("waits:", waits[:5], "total %.1fs" % total)
check(7 <= total <= 11, f"run lasts about LISTEN_SECONDS ({total:.1f}s)")
check(any("Buttons:" in t for _, t in sent), "typed 'help' during the run was answered")
help_at = [ts for ts, t in sent if "Buttons:" in t]
check(help_at and help_at[0] - t0 < 4.5, f"answered {help_at[0]-t0:.1f}s after start (typed at 2.0s)")
check(any("Urgent mode is not running" in t or "pause" in t.lower() for _, t in sent), "second message ('pause') answered too")
check(waits[0][0] == 0 and all(w[1] == w[0] + 15 for w in waits), "first look is instant, later ones long-poll with a longer HTTP timeout")
check(not flaky[0] and len(waits) >= 3, "a 409 conflict did not stop the loop")
sys.exit(1 if fails else 0)
