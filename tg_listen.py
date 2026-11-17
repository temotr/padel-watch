"""Listens for the Telegram message "padeldome check" and starts the court check.

Run every minute (cron-job.org -> workflow_dispatch). Only messages from your own
chat (TELEGRAM_CHAT_ID) are accepted. Handled messages are confirmed to Telegram,
so each message triggers exactly one run.
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

PHRASE = "padeldome check"
WORKFLOW = "courts.yml"
MAX_AGE_SEC = 30 * 60          # ignore messages older than this

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
REF = os.environ.get("GITHUB_REF_NAME", "main") or "main"


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


def start_check():
    body = json.dumps({"ref": REF, "inputs": {"report": "true"}}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    urllib.request.urlopen(req, timeout=20).read()


def norm(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def main():
    if not TOKEN or not CHAT:
        print("Telegram secrets are missing.")
        sys.exit(3)
    res = tg("getUpdates", timeout=0, allowed_updates=json.dumps(["message"]))
    updates = res.get("result", [])
    if not updates:
        print("No new messages.")
        return

    trigger = False
    other = False
    now = time.time()
    last_id = 0
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
        if text == PHRASE:
            trigger = True
        elif text and text != "/start":
            other = True

    # Confirm everything we have seen, so it is not processed twice.
    tg("getUpdates", offset=last_id + 1, limit=1, timeout=0)

    if trigger:
        try:
            start_check()
            say("🔎 Checking all Padeldome locations now. The answer follows in about 3 minutes.")
            print("Court check started.")
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: could not start the court check ({type(exc).__name__}).")
            say("⚠️ Could not start the court check. See the run log on GitHub.")
            sys.exit(1)
    elif other:
        say(f'Send "{PHRASE}" to check the free courts now.')


if __name__ == "__main__":
    main()