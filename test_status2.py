import os; os.environ.setdefault("LISTEN_SECONDS", "0")
import os, sys, tempfile, datetime as dt, urllib.error, io
os.environ.update(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="1", GITHUB_TOKEN="g", GITHUB_REPOSITORY="o/r")
sys.path.insert(0, "."); import tg_listen as L
os.chdir(tempfile.mkdtemp()); L.commit_files = lambda *a, **k: None
now = dt.datetime.now(dt.timezone.utc)
ok = {"status": "completed", "conclusion": "success", "updated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
saved = []
def fake_gh(path, body=None, method=None):
    if path.startswith("contents/"):
        saved.append(method); return {}
    return {"workflow_runs": [ok]}
L.gh = fake_gh
PIN = None; EDIT_ERR = ""; calls = []
def fake_tg(method, **p):
    calls.append(method)
    if method == "getChat": return {"result": {"pinned_message": PIN}}
    if method == "sendMessage": return {"result": {"message_id": 77}}
    if method == "editMessageText" and EDIT_ERR:
        raise urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(EDIT_ERR.encode()))
    return {}
L.tg = fake_tg
def run(label, **kw):
    calls.clear(); saved.clear(); L.update_status(force=True, **kw); print(f"{label:<62} -> {calls} saved={saved}")
for i in range(3): run("automatic run, nothing known (x3)")
run("you type 'status' -> create + pin + save via API", create=True)
PIN = {"message_id": 77, "text": "📌 PADEL WATCH · status", "from": {"is_bot": True}}
for i in range(2): run("automatic run, message pinned (x2) -> edit only")
os.remove("status_msg.txt"); run("id file lost, still pinned -> edit only")
EDIT_ERR = "Bad Request: message to edit not found"; run("message deleted, automatic run -> nothing")
run("message deleted, you type 'status' -> create new", create=True)
EDIT_ERR = "Bad Request: message is not modified"; run("not modified -> nothing")
