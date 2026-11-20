import os; os.environ.setdefault("LISTEN_SECONDS", "0")
import sys, time, tempfile, datetime as dt
os.environ.update(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="1", GITHUB_TOKEN="g", GITHUB_REPOSITORY="o/r")
sys.path.insert(0, os.getcwd())
import tg_listen as L
os.chdir(tempfile.mkdtemp())
L.commit_files = lambda *a, **k: None
L.update_status = lambda *a, **k: None
L.QUIET_FROM, L.QUIET_TO = 99, -1                      # no quiet hours in this test
L.tg = lambda method, **p: {"result": []}
disp = []; L.dispatch = lambda mode, workflow=None: disp.append(mode)
fails = []
def check(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c: fails.append(m)
open("urgent.txt", "w").write("on " + dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds") + " days=3\n")
def run_with(conclusion, minutes_ago, status="completed"):
    disp.clear()
    created = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)).isoformat()
    L.last_run = lambda: {"status": status, "conclusion": conclusion, "created_at": created}
    L.process([], False)
    return list(disp)
check(run_with("success", 6) == ["urgent"], "last run fine, 6 min ago -> next check starts")
check(run_with("success", 2) == [], "last run fine, 2 min ago -> not yet")
check(run_with("failure", 6) == [], "last run FAILED, 6 min ago -> waits (back-off)")
check(run_with("failure", 16) == ["urgent"], "last run FAILED, 16 min ago -> tries again")
check(run_with("cancelled", 6) == ["urgent"], "cancelled run is not a failure")
check(run_with(None, 6, status="in_progress") == [], "a run that is still going -> no second one")
print("FAILED:", fails) if fails else print("ALL OK")
sys.exit(1 if fails else 0)
