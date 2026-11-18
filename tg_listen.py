"""Telegram remote control for the Padeldome court check.

Run every minute (cron-job.org -> workflow_dispatch). Only messages from your own
chat (TELEGRAM_CHAT_ID) are accepted. Buttons (typed phrases work too):

  Check all options        one full check now, answer = everything free right now
  Urgent padel             next 3 days, any time, all locations, about every 5 minutes
  Urgent evenings only     same, but only starts from 17:00
  Urgent main 4 only       same, but only the 4 main locations
  Extend urgent +48h       restarts the 48 hour timer
  Pause alerts 2h          pauses urgent checks for 2 hours (press again to resume)
  Sun Set events           current list of Sun Set Padel events
  Padel-Austria tournaments  open events from padel-austria.at (Wien, Starter, Offener Bewerb)
  Stop urgent check        ends urgent mode (it also ends by itself after 48 hours)

Urgent mode is remembered in the repo file urgent.txt:
  "off"  or  "on <time> [evenings] [main] [pause=<time>]"
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

WORKFLOW = "courts.yml"
EVENTS_WORKFLOW = "padel_events.yml"
FLAG = "urgent.txt"
URGENT_STATE = "urgent_state.txt"
MAX_AGE_SEC = 30 * 60          # ignore Telegram messages older than this
URGENT_EVERY_SEC = 270         # start an urgent check when the last run is older than this
URGENT_MAX_HOURS = 48          # urgent mode switches itself off after this
PAUSE_HOURS = 2
EVENTS_FILE = "events.txt"
QUIET_FROM, QUIET_TO = 23, 7   # no automatic urgent checks from 23:00 until 07:00 Vienna time

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
REF = os.environ.get("GITHUB_REF_NAME", "main") or "main"

COMMANDS = {
    "check all options": "check",
    "urgent padel": "urgent",
    "urgent evenings only": "evenings",
    "urgent main 4 only": "main",
    "extend urgent +48h": "extend",
    "pause alerts 2h": "pause",
    "sun set events": "events",
    "padel-austria tournaments": "padel_events",
    "padel austria events": "padel_events",
    "stop urgent check": "stop",
    # earlier phrases keep working
    "padeldome check": "check",
    "padeldome urgent": "urgent",
    "stop": "stop",
    "/check": "check",       # also work as slash commands (bot menu)
    "/urgent": "urgent",
    "/stop": "stop",
    "/start": "menu",
    "/menu": "menu",
    "menu": "menu",
}

# Buttons that stay at the bottom of the chat; tapping one sends its text.
KEYBOARD = json.dumps({
    "keyboard": [
        [{"text": "🎾 Check all options"}, {"text": "🚨 Urgent padel"}],
        [{"text": "🌙 Urgent evenings only"}, {"text": "🏠 Urgent main 4 only"}],
        [{"text": "⏱ Extend urgent +48h"}, {"text": "🔕 Pause alerts 2h"}],
        [{"text": "📅 Sun Set events"}, {"text": "🏆 Padel-Austria tournaments"}],
        [{"text": "🛑 Stop urgent check"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
})


# ---- Telegram ---------------------------------------------------------------
def tg(method, **params):
    """Calls the Telegram Bot API. Never prints the URL (it contains the token)."""
    data = urllib.parse.urlencode(params).encode("utf-8")
    for attempt in range(4):
        req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code < 500 and exc.code != 429:
                raise                       # wrong token, bad request ...: retrying does not help
            err = exc
        except (urllib.error.URLError, OSError) as exc:   # e.g. "connection reset by peer"
            err = exc
        if attempt < 3:
            time.sleep(2 * (attempt + 1))
    raise err


def say(text):
    try:
        tg("sendMessage", chat_id=CHAT, text=text, reply_markup=KEYBOARD)
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


def dispatch(mode, workflow=None):
    gh(f"actions/workflows/{workflow or WORKFLOW}/dispatches", {"ref": REF, "inputs": {"mode": mode}})


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
def read_flag():
    """Returns None when urgent mode is off, else {'since', 'opts', 'pause'}."""
    try:
        text = open(FLAG, encoding="utf-8").read().strip()
    except OSError:
        return None
    words = text.split()
    if not words or words[0].lower() != "on":
        return None
    since, pause, opts = None, None, []
    for w in words[1:]:
        if w in ("evenings", "main"):
            opts.append(w)
        elif w.startswith("pause="):
            try:
                pause = dt.datetime.fromisoformat(w[6:])
            except ValueError:
                pass
        elif since is None:
            try:
                since = dt.datetime.fromisoformat(w)
            except ValueError:
                pass
    return {"since": since or dt.datetime.now(dt.timezone.utc), "opts": opts, "pause": pause}


def urgent_since():
    flag = read_flag()
    return flag["since"] if flag else None


def write_flag(on, opts=(), since=None, pause=None, message=None, reset_state=False):
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    if on:
        parts = ["on", (since or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds"), *opts]
        if pause:
            parts.append("pause=" + pause.isoformat(timespec="seconds"))
        line = " ".join(parts)
    else:
        line = "off"
    with open(FLAG, "w", encoding="utf-8") as f:
        f.write(line + "\n")
    remove = [URGENT_STATE] if (reset_state or not on) else []
    commit_files(message or ("Urgent mode " + ("on" if on else "off")), add=[FLAG], remove=remove)


def set_urgent(on, opts=()):
    write_flag(on, opts, reset_state=True)


def opts_text(opts):
    parts = []
    if "evenings" in opts:
        parts.append("starts from 17:00 only")
    if "main" in opts:
        parts.append("main 4 locations only")
    return " · ".join(parts) or "all locations, any time"


def vienna(t):
    return t.astimezone(ZoneInfo("Europe/Vienna"))


def events_text():
    try:
        lines = [l.strip() for l in open(EVENTS_FILE, encoding="utf-8") if l.strip()]
    except OSError:
        return "No event list saved yet. The watcher saves it at its next run."
    out = ["📅 SUN SET EVENTS (list from the last check)", ""]
    for l in lines:
        title, _, rest = l.partition(" | ")
        out.append(f"• {title} ({rest.replace(' / ', ' of ')} spots taken)" if rest and rest != "?" else f"• {title}")
    out += ["", "https://sunsetpadel.at/events"]
    return "\n".join(out)


# ---- Main -------------------------------------------------------------------
def norm(text):
    text = re.sub(r"[^\w\s/+.-]", " ", (text or "").lower())   # drops emoji from the buttons
    return re.sub(r"\s+", " ", text).strip()


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
        elif text:
            other = True
    if updates:
        # Confirm everything we have seen, so it is not processed twice.
        tg("getUpdates", offset=last_id + 1, limit=1, timeout=0)
    return cmds, other


def main():
    if not TOKEN or not CHAT:
        print("Telegram secrets are missing.")
        sys.exit(3)
    try:
        cmds, other = read_commands()
    except (urllib.error.URLError, OSError) as exc:
        if isinstance(exc, urllib.error.HTTPError) and exc.code < 500 and exc.code != 429:
            print(f"ERROR: Telegram refused the request (HTTP {exc.code}). Check the token and chat ID.")
            sys.exit(1)
        # Telegram is sometimes unreachable for a moment. The next run (1 minute later)
        # tries again, so this is not a failure worth an error e-mail.
        print(f"WARNING: Telegram not reachable now ({type(exc).__name__}). Trying again next run.")
        return
    started_now = False
    failed = False

    for cmd in dict.fromkeys(cmds):  # each command once, in order
        try:
            if cmd == "check":
                dispatch("report")
                say("🔎 Checking all locations now. The answer follows in about 3 minutes.")
            elif cmd in ("urgent", "evenings", "main"):
                opts = {"urgent": [], "evenings": ["evenings"], "main": ["main"]}[cmd]
                flag = read_flag()
                if flag and flag["opts"] == opts:
                    say(f"🚨 Urgent mode is already on ({opts_text(opts)}). Send: stop urgent check  to end it.")
                else:
                    set_urgent(True, opts)
                    dispatch("urgent")
                    started_now = True
                    say(f"🚨 Urgent mode {'switched' if flag else 'ON'}: {opts_text(opts)}. "
                        "I check the next 3 days (any weekday, 2h blocks) about every 5 minutes "
                        "and message you as soon as something opens up. No checks 23:00-07:00. "
                        "It ends by itself after 48 hours.")
            elif cmd == "extend":
                flag = read_flag()
                if not flag:
                    say("Urgent mode is not running. Tap: Urgent padel.")
                else:
                    write_flag(True, flag["opts"], pause=flag["pause"], message="Urgent mode extended")
                    ends = vienna(dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=URGENT_MAX_HOURS))
                    say(f"⏱ Urgent mode extended. It now ends {ends:%a %d.%m. %H:%M} (48 hours from now).")
                    started_now = True
            elif cmd == "pause":
                flag = read_flag()
                now_utc = dt.datetime.now(dt.timezone.utc)
                if not flag:
                    say("Urgent mode is not running. Nothing to pause.")
                elif flag["pause"] and flag["pause"] > now_utc:
                    write_flag(True, flag["opts"], since=flag["since"], message="Urgent alerts resumed")
                    say("▶️ Alerts resumed. Checks continue about every 5 minutes.")
                else:
                    until = now_utc + dt.timedelta(hours=PAUSE_HOURS)
                    write_flag(True, flag["opts"], since=flag["since"], pause=until, message="Urgent alerts paused")
                    say(f"🔕 Alerts paused until {vienna(until):%H:%M}. Urgent mode stays on. "
                        "Tap Pause again to resume earlier.")
                    started_now = True
            elif cmd == "events":
                say(events_text())
            elif cmd == "padel_events":
                dispatch("report", EVENTS_WORKFLOW)
                say("🏆 Reading the Padel-Austria tournaments (Wien · Starter · Offener Bewerb · not full). The list follows in about 2 minutes.")
            elif cmd == "menu":
                say("Buttons are ready below.")
            elif cmd == "stop":
                if urgent_since():
                    set_urgent(False)
                    say("🛑 Urgent check stopped.")
                else:
                    say("Urgent mode was not running.")
            print(f"Handled: {cmd}")
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: could not handle '{cmd}' ({type(exc).__name__}).")
            say(f"⚠️ Could not run '{cmd}'. See the run log on GitHub.")
            failed = True
    if other and not cmds:
        say("Use the buttons below.")

    # Urgent mode upkeep: expire after 48 h, start a new check about every 5 minutes.
    flag = read_flag()
    since = flag["since"] if flag else None
    if since and not started_now:
        try:
            age_h = (dt.datetime.now(dt.timezone.utc) - since).total_seconds() / 3600
            if age_h >= URGENT_MAX_HOURS:
                set_urgent(False)
                say("🛑 Urgent mode ended by itself after 48 hours. Tap: Urgent padel  to start again.")
            else:
                run = last_run()
                busy = run and run.get("status") in ("queued", "in_progress", "waiting", "pending")
                created = run and dt.datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
                due = (not run) or (
                    (dt.datetime.now(dt.timezone.utc) - created).total_seconds() >= URGENT_EVERY_SEC
                )
                hour = dt.datetime.now(ZoneInfo("Europe/Vienna")).hour
                quiet = hour >= QUIET_FROM or hour < QUIET_TO
                paused = flag["pause"] and flag["pause"] > dt.datetime.now(dt.timezone.utc)
                if flag["pause"] and not paused:
                    write_flag(True, flag["opts"], since=flag["since"], message="Urgent alerts resumed")
                    say("▶️ Pause is over. Urgent checks continue.")
                if paused:
                    print("Urgent mode on, alerts paused.")
                elif quiet:
                    print("Urgent mode on, quiet hours (23:00-07:00): no check.")
                elif due and not busy:
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