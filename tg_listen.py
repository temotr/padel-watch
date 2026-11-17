"""Telegram remote control for the Padeldome court check.

Run every minute (cron-job.org -> workflow_dispatch). Only messages from your own
chat (TELEGRAM_CHAT_ID) are accepted. Commands:

  padeldome check    one full check now, answer = everything free right now
  padeldome urgent   urgent mode: checks today + tomorrow about every 5 minutes
                     and messages you when something new opens up
  stop               ends urgent mode (it also ends by itself after 48 hours)

Urgent mode is remembered in the repo file urgent.txt ("on <time>" or "off").
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

WORKFLOW = "courts.yml"
FLAG = "urgent.txt"
URGENT_STATE = "urgent_state.txt"
MAX_AGE_SEC = 30 * 60          # ignore Telegram messages older than this
URGENT_EVERY_SEC = 270         # start an urgent check when the last run is older than this
URGENT_MAX_HOURS = 48          # urgent mode switches itself off after this

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
REF = os.environ.get("GITHUB_REF_NAME", "main") or "main"

COMMANDS = {
    "padeldome check": "check",
    "padeldome urgent": "urgent",
    "stop": "stop",
}


# ---- Telegram ---------------------------------------------------------------
def tg(method, **params):
    """Calls the Telegram Bot API. Never prints the URL (it contains the token)."""
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def say(text):
    try:
        tg("sendMessage", chat_id=CHAT, text=text)
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: reply failed ({type(exc).__name__}).")


# ---- GitHub -----------------------------------------------------------------
def gh(path, body=None, method=None):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/{path}",
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        method=method or ("POST" if body is not None else "GET"),
        headers={
            "Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def dispatch(mode):
    gh(f"actions/workflows/{WORKFLOW}/dispatches", {"ref": REF, "inputs": {"mode": mode}})


def last_run():
    runs = gh(f"actions/workflows/{WORKFLOW}/runs?per_page=1").get("workflow_runs", [])
    return runs[0] if runs else None


def git(*args, check=True):
    return subprocess.run(["git", *args], check=check, capture_output=True, text=True)


def commit_files(message, add=(), remove=()):
    git("config", "user.name", "watcher")
    git("config", "user.email", "watcher@users.noreply.github.com")
    for f in remove:
        git("rm", "-f", "--ignore-unmatch", f, check=False)
    for f in add:
        git("add", f)
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return
    git("commit", "-m", message)
    for _ in range(3):
        if git("pull", "--rebase", check=False).returncode == 0 and \
                git("push", check=False).returncode == 0:
            return
        time.sleep(5)
    raise RuntimeError("push failed")


# ---- Urgent flag ------------------------------------------------------------
def urgent_since():
    """Returns the start time of urgent mode, or None when it is off."""
    try:
        text = open(FLAG, encoding="utf-8").read().strip()
    except OSError:
        return None
    if not text.lower().startswith("on"):
        return None
    try:
        return dt.datetime.fromisoformat(text.split(None, 1)[1])
    except (IndexError, ValueError):
        return dt.datetime.now(dt.timezone.utc)


def set_urgent(on):
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with open(FLAG, "w", encoding="utf-8") as f:
        f.write(f"on {now}\n" if on else "off\n")
    commit_files("Urgent mode " + ("on" if on else "off"), add=[FLAG], remove=[URGENT_STATE])


# ---- Main -------------------------------------------------------------------
def norm(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def read_commands():
    res = tg("getUpdates", timeout=0, allowed_updates=json.dumps(["message"]))
    updates = res.get("result", [])
    cmds, other, last_id, now = [], False, 0, time.time()
    for u in updates:
        last_id = max(last_id, u["update_id"])
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != str(CHAT):
            print("Ignored a message from another chat.")
            continue
        if now - m.get("date", 0) > MAX_AGE_SEC:
            print("Ignored an old message.")
            continue
        text = norm(m.get("text"))
        if text in COMMANDS:
            cmds.append(COMMANDS[text])
        elif text and text != "/start":
            other = True
    if updates:
        # Confirm everything we have seen, so it is not processed twice.
        tg("getUpdates", offset=last_id + 1, limit=1, timeout=0)
    return cmds, other


def main():
    if not TOKEN or not CHAT:
        print("Telegram secrets are missing.")
        sys.exit(3)
    cmds, other = read_commands()
    started_now = False
    failed = False

    for cmd in dict.fromkeys(cmds):  # each command once, in order
        try:
            if cmd == "check":
                dispatch("report")
                say("🔎 Checking all Padeldome locations now. The answer follows in about 3 minutes.")
            elif cmd == "urgent":
                if urgent_since():
                    say("🚨 Urgent mode is already on. Send: stop  to end it.")
                else:
                    set_urgent(True)
                    dispatch("urgent")
                    started_now = True
                    say("🚨 Urgent mode ON. I check today and tomorrow about every 5 minutes "
                        "and message you as soon as something opens up. "
                        "Send: stop  to end it (it also ends by itself after 48 hours).")
            elif cmd == "stop":
                if urgent_since():
                    set_urgent(False)
                    say("🛑 Urgent mode stopped.")
                else:
                    say("Urgent mode was not running.")
            print(f"Handled: {cmd}")
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: could not handle '{cmd}' ({type(exc).__name__}).")
            say(f"⚠️ Could not run '{cmd}'. See the run log on GitHub.")
            failed = True
    if other and not cmds:
        say('Commands: "padeldome check" (check now), "padeldome urgent" (watch today + tomorrow), '
            '"stop" (end urgent mode).')

    # Urgent mode upkeep: expire after 48 h, start a new check about every 5 minutes.
    since = urgent_since()
    if since and not started_now:
        try:
            age_h = (dt.datetime.now(dt.timezone.utc) - since).total_seconds() / 3600
            if age_h >= URGENT_MAX_HOURS:
                set_urgent(False)
                say("🛑 Urgent mode ended by itself after 48 hours. Send: padeldome urgent  to start again.")
            else:
                run = last_run()
                busy = run and run.get("status") in ("queued", "in_progress", "waiting", "pending")
                created = run and dt.datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
                due = (not run) or (
                    (dt.datetime.now(dt.timezone.utc) - created).total_seconds() >= URGENT_EVERY_SEC
                )
                if due and not busy:
                    dispatch("urgent")
                    print("Urgent check started.")
                else:
                    print("Urgent mode on, next check not due yet.")
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: urgent upkeep failed ({type(exc).__name__}).")
            failed = True
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()