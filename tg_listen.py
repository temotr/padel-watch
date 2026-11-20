
"""Telegram remote control for the Padeldome court check.

Run every minute (cron-job.org -> workflow_dispatch). Only messages from your own
chat (TELEGRAM_CHAT_ID) are accepted.
"""
import datetime as dt
import html
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

import watchlib as W

# ==========================================
# ⚠️ CHANGE THIS TO YOUR WISPBYTE URL
# ==========================================
WISPBYTE_URL = "http://78.154.103.34:15688/summary"
# ==========================================

WORKFLOW = "courts.yml"
EVENTS_WORKFLOW = "padel_events.yml"
FLAG = "urgent.txt"
URGENT_STATE = "urgent_state.txt"
LISTEN_SECONDS = int(os.environ.get("LISTEN_SECONDS", "50"))   # how long one run keeps listening (0 = look once)
MAX_AGE_SEC = 30 * 60          # ignore Telegram messages older than this
# Settings come from config.toml (the numbers here are only the fallback)
URGENT_EVERY_SEC = W.get("urgent", "every_sec", 270)   # start an urgent check when the last run is older than this
URGENT_BACKOFF_SEC = W.get("urgent", "backoff_sec", 900)   # after a FAILED check, wait this long (a website that does not answer is not hammered)
URGENT_MAX_HOURS = W.get("urgent", "max_hours", 48)     # urgent mode switches itself off after this
PAUSE_HOURS = W.get("urgent", "pause_hours", 2)
EVENTS_FILE = "events.txt"
STATUS_FILE = "status_msg.txt"   # id of the pinned status message

def _date(text, default):
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return default

TOKEN_EXPIRES = _date(W.get("bot", "token_expires", "2026-12-12"), dt.date(2026, 12, 12))
REMIND_DAYS = tuple(n for n in W.get("bot", "remind_days", [14, 7, 3, 1, 0]) if isinstance(n, int))  # at 09:00 Vienna time, this many days before
REMINDERS_FILE = "reminders.txt"  # remembers which reminders were already sent
STATUS_EVERY_MIN = W.get("bot", "status_every_min", 5)   # the status message is refreshed about every 5 minutes

WATCHERS = [
    ("📅 Sun Set events", "watch.yml", 45),
    ("🎾 Court checks", "courts.yml", None),          # urgent runs: checked separately below
    ("🏆 Padel-Austria tournaments", "padel_events.yml", 180),
]
CADENCE = {   # shown in brackets in the status. Only text: change it in config.toml when you change the cron-job.org schedule
    "watch.yml": "every 5 min", "courts.yml": "on request + urgent", "padel_events.yml": "hourly", "urgent": "checked every 5 min",
    "meetup": "every 5 min",
}
CADENCE.update({k: v for k, v in W.get("bot", "cadence", {}).items() if k in CADENCE and isinstance(v, str)})
HEALTH_KEY = {"watch.yml": "sunset", "courts.yml": "courts", "padel_events.yml": "events"}   # health_<key>.txt
MUTE_KEY = {"watch.yml": "sunset", "padel_events.yml": "events"}
MUTE_LABELS = {"sunset": "📅 Sun Set events", "events": "🏆 Tournaments", "meetup": "🤝 Meetup events"}
MUTE_HOURS = [h for h in W.get("bot", "mute_hours", [2, 8, 24, 72, 168]) if isinstance(h, int) and 0 < h <= 24 * 60] or [2, 8, 24]
QUIET_FROM, QUIET_TO = W.get("urgent", "quiet_from", 23), W.get("urgent", "quiet_to", 7)   # no automatic urgent checks in between (Vienna time)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
MEETUP_URL = os.environ.get("MEETUP_EVENTS_URL", "")   # private address of the Google script (secret)
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
REF = os.environ.get("GITHUB_REF_NAME", "main") or "main"

COMMANDS = {
    "check all options": "check",
    "urgent padel": "urgent",
    "urgent evenings only": "start_evenings",
    "urgent main 4 only": "start_main",
    "extend urgent +48h": "extend",
    "pause alerts 2h": "pause",
    "resume alerts": "pause",
    "all locations": "start_all",
    "sun set events": "events",
    "meetup events": "meetup",
    "meetup": "meetup",
    "mute": "mute",
    "mute alerts": "mute",
    "unmute": "mute",
    "padel-austria tournaments": "padel_events",
    "padel austria events": "padel_events",
    "stop urgent check": "stop",
    "whatsapp summary": "whatsapp_summary", # <-- NEW
    # short words you can type
    "pause": "pause",
    "extend": "extend",
    "evenings": "start_evenings",
    "evening": "start_evenings",
    "main 4": "start_main",
    "main": "start_main",
    "sun set": "events",
    "tournaments": "padel_events",
    "help": "menu",
    "status": "status",
    "check": "check",
    "urgent": "urgent",
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
def keyboard():
    """Bottom buttons. Urgent off: normal buttons. Urgent on: Stop, Pause and Extend appear."""
    flag = read_flag()
    if not flag:
        rows = [["🎾 Check all options", "🏆 Padel-Austria tournaments"],
                ["🚨 Urgent padel", "📅 Sun Set events"],
                ["🤝 Meetup events", "📱 WhatsApp Summary"], # <-- NEW BUTTON
                ["🔕 Mute"]]
    else:
        rows = [["🎾 Check all options", "🏆 Padel-Austria tournaments"],
                ["📅 Sun Set events", "🛑 Stop urgent check"],
                ["⏱ Extend urgent +48h", "🔕 Mute"],
                ["🤝 Meetup events", "📱 WhatsApp Summary"]] # <-- NEW BUTTON
    return json.dumps({
        "keyboard": [[{"text": t} for t in row] for row in rows],
        "resize_keyboard": True,
        "is_persistent": True,
    })


# ---- Urgent menu: 3 steps with small buttons, one message that is edited in place ----------
WIZ_KINDS = ("d", "t", "p", "n", "f", "o", "g", "b", "x", "m", "u", "r")
WIZ = []                      # taps on menu buttons found in this run: (message_id, callback data)
WIZ_EARLIEST = [7, 9, 12, 16, 17, 18]
WIZ_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WIZ_MAX_DAYS = W.get("courts", "days_ahead", 15)   # today + 14 days: the club books at most 14 days ahead


def wiz_pack(st):
    return f"{st['days']}.{st['mask']}.{st['main']}.{'-' if st['frm'] is None else st['frm']}"


def wiz_unpack(text):
    try:
        days, mask, main, frm = text.split(".")
        st = {"days": int(days), "mask": mask, "main": int(main),
              "frm": None if frm == "-" else int(frm), "step": 2}
        if not (1 <= st["days"] <= WIZ_MAX_DAYS and len(mask) == 7 and set(mask) <= {"0", "1"}
                and st["main"] in (0, 1) and (st["frm"] is None or 0 <= st["frm"] <= 23)):
            return None
        return st
    except (ValueError, AttributeError):
        return None


def wiz_btn(text, data):
    return {"text": text, "callback_data": data}


def wiz_render(st):
    """(text, inline keyboard json) for the current step of the menu."""
    pk = wiz_pack(st) if st else ""
    if st is None:                                                    # step 1
        text = ("<b><u>🚨 URGENT SEARCH · step 1 of 3</u></b>\n\n<b>How many days ahead?</b>\n"
                "Today counts as day 1. The club books up to 14 days ahead, so 15 is the maximum.")
        nums = [wiz_btn(str(n), f"d:{n}") for n in range(1, WIZ_MAX_DAYS + 1)]
        rows = [[wiz_btn("⚡ Evenings Mon-Thu · next 10 days · 18-20", "g:10.1111000.0.-:18:20")],
                [wiz_btn("⚡ As before: 3 days · any day · any time", "g:3.1111111.0.-:7:21")]]
        rows += [nums[i:i + 5] for i in range(0, len(nums), 5)]
        rows.append([wiz_btn("❌ Cancel", "x")])
        return text, json.dumps({"inline_keyboard": rows})
    summary = f"📅 <b>{st['days']} days</b>"
    if st["step"] == 2:
        text = (f"<b><u>🚨 URGENT SEARCH · step 2 of 3</u></b>\n{summary}\n\n"
                "<b>Which weekdays?</b> Tap single days and then Next, or tap a ready-made set to go on right away.")
        day_btns = [wiz_btn(("✅ " if c == "1" else "⬜ ") + WIZ_DAY_NAMES[i], f"t:{pk}:{i}")
                    for i, c in enumerate(st["mask"])]
        rows = [day_btns[:4], day_btns[4:],
                [wiz_btn("Mon-Thu", f"p:{pk}:1111000"), wiz_btn("All days", f"p:{pk}:1111111"),
                 wiz_btn("Sat-Sun", f"p:{pk}:0000011")],
                [wiz_btn("➡️ Next", f"n:{pk}")],
                [wiz_btn("❌ Cancel", "x")]]
        return text, json.dumps({"inline_keyboard": rows})
    where = "main 4 locations only" if st["main"] else "all locations"
    summary += f" · 🗓 <b>{wd_text(st['mask'])}</b> · 📍 {where}"
    if st["frm"] is None:                                              # step 3, earliest start
        text = (f"<b><u>🚨 URGENT SEARCH · step 3 of 3</u></b>\n{summary}\n\n"
                "<b>Earliest start of the game?</b> (a game lasts 2 hours)\n"
                "Or tap a shortcut to start the search right away.")
        rows = [[wiz_btn("⚡ 18-20 evening", f"g:{pk}:18:20"), wiz_btn("⚡ 17-21", f"g:{pk}:17:21"),
                 wiz_btn("⚡ Any time", f"g:{pk}:7:21")],
                [wiz_btn(f"{h:02d}:00", f"f:{pk}:{h}") for h in WIZ_EARLIEST],
                [wiz_btn(("🏠 Main 4 only: ON" if st["main"] else "🏠 Main 4 only: OFF"), f"o:{pk}")],
                [wiz_btn("⬅️ Back", f"b:{pk}"), wiz_btn("❌ Cancel", "x")]]
        return text, json.dumps({"inline_keyboard": rows})
    text = (f"<b><u>🚨 URGENT SEARCH · step 3 of 3</u></b>\n{summary}\n"
            f"⏰ earliest start <b>{st['frm']:02d}:00</b>\n\n<b>Latest start?</b> The search starts right after your tap.")
    hours = list(range(st["frm"], 22)) or [21]
    btns = [wiz_btn(f"{h:02d}:{'30' if h >= 21 else '00'}", f"g:{pk}:{st['frm']}:{h}") for h in hours]
    rows = [btns[i:i + 5] for i in range(0, len(btns), 5)]
    rows.append([wiz_btn("⬅️ Earlier start", f"n:{pk}"), wiz_btn("❌ Cancel", "x")])
    return text, json.dumps({"inline_keyboard": rows})


def wiz_send_first():
    text, markup = wiz_render(None)
    try:
        tg("sendMessage", chat_id=CHAT, text=text, parse_mode="HTML", reply_markup=markup)
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not send the urgent menu ({type(exc).__name__}).")


def wiz_edit(mid, text, markup=None):
    try:
        tg("editMessageText", chat_id=CHAT, message_id=mid, text=text, parse_mode="HTML",
           reply_markup=markup or json.dumps({"inline_keyboard": []}))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore") if hasattr(exc, "read") else ""
        if "not modified" not in body.lower():
            print(f"WARNING: could not update the urgent menu: HTTP {exc.code} {body[:150]}")
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not update the urgent menu ({type(exc).__name__}).")


def wiz_start(st, first, last):
    """Switches urgent mode on with the chosen settings. True when it was started."""
    opts = [f"days={st['days']}", f"wd={st['mask']}", f"from={first}", f"to={last}"]
    if st["main"]:
        opts.append("main")
    flag = read_flag()
    if flag and flag["opts"] == opts:
        say(f"🚨 Urgent mode is already on with these settings ({opts_text(opts)}).")
        return False
    set_urgent(True, opts)
    dispatch("urgent")
    today = dt.datetime.now(ZoneInfo("Europe/Vienna")).date()
    n_days = sum(1 for i in range(st["days"]) if st["mask"][(today + dt.timedelta(days=i)).weekday()] == "1")
    minutes = n_days * (4 if st["main"] else 8) * 2.8 / 60          # about 2.8 s per page and location
    text = (f"🚨 Urgent search {'switched' if flag else 'ON'}\n"
            f"📅 Next {st['days']} days (today counts as day 1)\n"
            f"🗓 {wd_text(st['mask'])}\n"
            f"🕕 Starts {first:02d}:00-{last:02d}:{'30' if last >= 21 else '00'}, 2h block\n"
            f"📍 {'Main 4 locations only' if st['main'] else 'All locations'}\n"
            "🔁 About every 5 minutes, ends after 48 hours, no checks 23:00-07:00.")
    if minutes >= 4:
        text += (f"\n⏱ One round takes about {minutes:.0f} minutes with this many days, so a message can come a "
                 "little later." + ("" if st["main"] else " 'Main 4 only' makes it about twice as fast."))
    say(text + "\n\nButtons below: Pause · Extend · Stop")
    return True


def wiz_run(ops):
    """Applies the taps in the order they came (several taps within a minute are fine), then redraws each
    menu message once. Returns True when an urgent search was started."""
    started = False
    cache = {}                       # message id -> state dict, or "cancel" / "done"
    for mid, data in ops:
        parts = data.split(":")
        kind = parts[0]
        if kind == "x":
            cache[mid] = "cancel"
            continue
        if kind == "d":
            try:
                n = int(parts[1])
            except (IndexError, ValueError):
                continue
            if 1 <= n <= WIZ_MAX_DAYS:
                cache[mid] = {"days": n, "mask": "1111000", "main": 0, "frm": None, "step": 2}
            continue
        st = cache.get(mid)
        if isinstance(st, str):
            continue                  # already cancelled or started: later taps on old buttons are ignored
        if kind in ("m", "u", "r"):   # mute menu (it has no state: everything is in the button data)
            cache[mid] = ("mute", *mute_apply(kind, parts[1:]))
            continue
        if isinstance(st, tuple):
            continue
        if st is None:
            st = wiz_unpack(parts[1]) if len(parts) > 1 else None
        if st is None:
            continue
        try:
            if kind == "t":
                i = int(parts[2])
                mask = list(st["mask"])
                mask[i] = "0" if mask[i] == "1" else "1"
                st["mask"], st["step"] = "".join(mask), 2
            elif kind == "p":                  # a ready-made set of days: choose it and go on to step 3
                if len(parts[2]) == 7 and "1" in parts[2] and set(parts[2]) <= {"0", "1"}:
                    st["mask"] = parts[2]
                    st["step"], st["frm"] = 3, None
            elif kind == "b":                  # back to the weekdays
                st["step"], st["frm"] = 2, None
            elif kind == "n":
                if "1" in st["mask"]:
                    st["step"], st["frm"] = 3, None
            elif kind == "f":
                st["step"], st["frm"] = 3, int(parts[2])
            elif kind == "o":
                st["main"] = 0 if st["main"] else 1
                st["step"] = 3
            elif kind == "g":
                first, last = int(parts[2]), int(parts[3])
                if "1" in st["mask"] and 0 <= first <= last <= 23:
                    if wiz_start(st, first, last):
                        started = True
                    cache[mid] = "done"
                    continue
        except (IndexError, ValueError):
            continue
        cache[mid] = st
    for mid, st in cache.items():
        if isinstance(st, tuple):
            wiz_edit(mid, st[1], st[2])
        elif st == "cancel":
            wiz_edit(mid, "❌ Cancelled.")
        elif st == "done":
            wiz_edit(mid, "✅ Urgent search set up, details in the message below.")
        else:
            wiz_edit(mid, *wiz_render(st))
    return started


# ---- Mute menu: what should be quiet, and for how long (one message, edited in place) ---------
def hours_label(h):
    return f"{h} h" if h < 24 else f"{h // 24} day" + ("s" if h >= 48 else "")


def mute_items():
    """[(key, label, muted until or None)]: the three watchers, plus the urgent search while it is on."""
    mutes = W.mute_read()
    items = [(k, MUTE_LABELS[k], mutes.get(k)) for k in W.MUTE_NAMES]
    flag = read_flag()
    if flag:
        paused = flag["pause"] if flag["pause"] and flag["pause"] > dt.datetime.now(dt.timezone.utc) else None
        items.append(("urgent", "🚨 Urgent search", paused))
    return items


def mute_keys(key):
    if key == "all":
        return [k for k, _, _ in mute_items()]
    return [key] if key in [k for k, _, _ in mute_items()] else []


def mute_set(keys, until):
    """Mutes the given watchers until `until` (None = switch them on again). Saves mute.txt / urgent.txt."""
    mutes = W.mute_read()
    for k in keys:
        if k == "urgent":
            flag = read_flag()
            if flag:
                write_flag(True, flag["opts"], since=flag["since"], pause=until,
                           message="Urgent alerts paused" if until else "Urgent alerts resumed")
        elif until:
            mutes[k] = until
        else:
            mutes.pop(k, None)
    with open(W.MUTE_FILE, "w", encoding="utf-8") as f:
        f.write(W.mute_text(mutes))
    commit_files("Mute list changed", add=[str(W.MUTE_FILE)])


def mute_render(pick=None):
    """(text, inline keyboard json): step 1 = what, step 2 = how long."""
    items = mute_items()
    if pick is None:
        lines = ["<b><u>🔕 MUTE ALERTS</u></b>", "", "<b>What should be quiet?</b>", ""]
        for _, label, until in items:
            lines.append(f"{label}: " + (f"🔕 muted until <b>{vienna(until):%a %d.%m. %H:%M}</b>" if until else "🔔 on"))
        lines += ["", "While muted, nothing is checked. When the mute ends you get what is new since before."]
        btns = [wiz_btn(("🔕 " if until else "") + label, f"m:{key}") for key, label, until in items]
        rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
        rows.append([wiz_btn("🔕 Everything", "m:all")])
        if any(until for _, _, until in items):
            rows.append([wiz_btn("🔔 Unmute all", "r:all")])
        rows.append([wiz_btn("❌ Close", "x")])
        return "\n".join(lines), json.dumps({"inline_keyboard": rows})
    names = dict((k, label) for k, label, _ in items)
    label = "All alerts" if pick == "all" else names.get(pick, pick)
    muted_now = any(until for k, _, until in items if pick == "all" or k == pick)
    text = f"<b><u>🔕 MUTE ALERTS</u></b>\n{label}\n\n<b>For how long?</b>"
    btns = [wiz_btn(hours_label(h), f"u:{pick}:{h}") for h in MUTE_HOURS]
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    if muted_now:
        rows.append([wiz_btn("🔔 Unmute now", f"r:{pick}")])
    rows.append([wiz_btn("⬅️ Back", "m:menu"), wiz_btn("❌ Cancel", "x")])
    return text, json.dumps({"inline_keyboard": rows})


def mute_apply(kind, args):
    """Handles one tap of the mute menu. Returns (text, keyboard json) for the menu message."""
    try:
        if kind == "m":
            return mute_render(None if args[0] == "menu" else args[0])
        keys = mute_keys(args[0])
        if not keys:
            return mute_render(None)
        names = dict((k, label) for k, label, _ in mute_items())
        label = "All alerts" if args[0] == "all" else names[args[0]]
        if kind == "u":
            hours = int(args[1])
            if hours not in MUTE_HOURS:
                return mute_render(None)
            until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=hours)
            mute_set(keys, until)
            text = (f"🔕 <b>{label}</b> muted for {hours_label(hours)}, until <b>{vienna(until):%a %d.%m. %H:%M}</b>.\n"
                    "Then you get what is new since before. To end it earlier: tap Mute again.")
            if "urgent" in keys:
                text += "\n🚨 The urgent search is paused until then."
        else:                                                         # kind == "r"
            mute_set(keys, None)
            text = f"🔔 <b>{label}</b> switched on again."
        return text, json.dumps({"inline_keyboard": []})
    except (IndexError, ValueError):
        return mute_render(None)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: mute menu failed ({type(exc).__name__}).")
        return "⚠️ Could not change the mute. See the run log on GitHub.", json.dumps({"inline_keyboard": []})


def mute_send_menu():
    text, markup = mute_render(None)
    try:
        tg("sendMessage", chat_id=CHAT, text=text, parse_mode="HTML", reply_markup=markup)
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not send the mute menu ({type(exc).__name__}).")


# Small buttons under the "which urgent check?" message
URGENT_CHOICES = json.dumps({"inline_keyboard": [
    [{"text": "🚨 All locations, any time", "callback_data": "start_all"}],
    [{"text": "🌙 Evenings only (from 17:00)", "callback_data": "start_evenings"}],
    [{"text": "🏠 Main 4 locations only", "callback_data": "start_main"}],
]})
CALLBACKS = {"start_all": "start_all", "start_evenings": "start_evenings", "start_main": "start_main"}

HELP = (
    "Buttons: Check all options · Padel-Austria tournaments · Urgent padel · Sun Set events · Meetup events · Mute.\n"
    "Mute: silence Sun Set, tournaments, Meetup or the urgent search for 2 hours up to 7 days.\n"
    "Urgent padel opens a 3-step menu: days ahead, weekdays, start times.\n"
    "While urgent mode is on you also get Stop and Extend. To pause the urgent search, use Mute.\n\n"
    "You can also type: stop · pause · extend · evenings · main 4 · sun set · status · help"
)


# ---- Telegram ---------------------------------------------------------------
def tg(method, **params):
    """Calls the Telegram Bot API. Never prints the URL (it contains the token)."""
    http_timeout = params.pop("_timeout", 20)
    data = urllib.parse.urlencode(params).encode("utf-8")
    for attempt in range(4):
        req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data)
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as r:
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


def say(text, markup=None):
    try:
        tg("sendMessage", chat_id=CHAT, text=text, reply_markup=markup or keyboard())
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
        if w in ("evenings", "main") or w.split("=")[0] in ("days", "wd", "from", "to"):
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


def wd_text(mask):
    days = [i for i, c in enumerate(mask) if c == "1"]
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    if len(days) == 7:
        return "any day"
    if len(days) > 1 and days == list(range(days[0], days[-1] + 1)):
        return f"{names[days[0]]}-{names[days[-1]]}"
    return ", ".join(names[i] for i in days)


def opts_text(opts):
    cfg = {w.split("=")[0]: w.split("=", 1)[1] for w in opts if "=" in w}
    parts = []
    if "days" in cfg:
        parts.append(f"next {cfg['days']} days")
    if len(cfg.get("wd", "")) == 7:
        parts.append(wd_text(cfg["wd"]))
    if "from" in cfg or "to" in cfg:
        first = int(cfg.get("from", 7))
        last = int(cfg.get("to", 21))
        parts.append(f"starts {first:02d}:00-{last:02d}:{'30' if last >= 21 else '00'}")
    if "evenings" in opts:
        parts.append("starts from 17:00 only")
    if "main" in opts:
        parts.append("main 4 locations only")
    elif cfg:
        parts.append("all locations")
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


def say_html(text):
    try:
        tg("sendMessage", chat_id=CHAT, text=text, parse_mode="HTML", reply_markup=keyboard(),
           disable_web_page_preview="true")
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: reply failed ({type(exc).__name__}).")


def meetup_fetch():
    """The raw answer of the Google script, or None when it cannot be reached."""
    try:
        req = urllib.request.Request(MEETUP_URL, headers={"User-Agent": "padel-watch"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: Meetup list not reachable ({type(exc).__name__}).")
        return None


def meetup_status_line(mutes, now):
    """One status line for Meetup: when the Google script last checked your mails."""
    label = f"🤝 Meetup events ({CADENCE['meetup']})"
    until = mutes.get("meetup")
    if until:
        return f"🔕 {label}: muted until {stamp(until)}"
    if not MEETUP_URL:
        return f"⚪ {label}: not connected yet"
    body = meetup_fetch()
    if body is None or not body.startswith("# v1"):
        return f"❓ {label}: could not read the list"
    head = body.splitlines()[0]

    def moment(name):
        m = re.search(rf"{name}=(\S+)", head)
        try:
            return dt.datetime.fromisoformat(m.group(1).replace("Z", "+00:00")) if m else None
        except ValueError:
            return None

    checked = moment("checked")
    if checked is None:
        return f"⚠️ {label}: last check unknown (update the Google script)"
    text = f"{label}: last check {stamp(checked)}"
    if (now - checked).total_seconds() > 20 * 60:
        return "⚠️ " + text + " · no recent check"
    return "✅ " + text


def meetup_text():
    """Upcoming padel events from the Meetup mails (list kept by the Google script)."""
    if not MEETUP_URL:
        return ("🤝 The Meetup list is not connected yet. Add the secret MEETUP_EVENTS_URL in GitHub "
                "(see the setup guide).")
    try:
        req = urllib.request.Request(MEETUP_URL, headers={"User-Agent": "padel-watch"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: Meetup list not reachable ({type(exc).__name__}).")
        return "🤝 Could not read the Meetup list right now. Try again in a minute."
    if not body.startswith("# v1"):
        print("WARNING: Meetup list answer was not the expected list (wrong or expired address?).")
        return "🤝 The Meetup list address did not answer correctly. Check the secret MEETUP_EVENTS_URL."
    today = dt.datetime.now(ZoneInfo("Europe/Vienna")).date()
    rows = []
    for line in body.splitlines()[1:]:
        f = (line.split("|") + [""] * 7)[:7]
        try:
            day = dt.date.fromisoformat(f[0])
        except ValueError:
            continue
        if day >= today:
            rows.append((day, *f[1:]))
    e = html.escape
    out = ["<b><u>🤝 MEETUP · upcoming padel events</u></b>",
           "<i>From your Meetup e-mail alerts, not finished yet</i>", ""]
    if not rows:
        out.append("No upcoming padel events known yet.")
    last = None
    for day, tm, title, venue, price, going, url in rows:
        if day != last:
            if last is not None:
                out.append("")
            out.append(f"<b><u>{day:%a %d.%m.}</u></b>")
            last = day
        name = f'<a href="{e(url)}">{e(title)}</a>' if url else e(title)
        out.append(f"{'⏰ ' + tm + '  ' if tm else ''}🏆 <i>{name}</i>")
        extra = [x for x in (f"📍 <b>{e(venue)}</b>" if venue else "",
                             f"💶 {e(price)}" if price else "",
                             f"👥 {e(going)} going" if going else "") if x]
        if extra:
            out.append("   " + " · ".join(extra))
    return "\n".join(out)


# ---- Main -------------------------------------------------------------------
def norm(text):
    text = re.sub(r"[^\w\s/+.-]", " ", (text or "").lower())   # drops emoji from the buttons
    return re.sub(r"\s+", " ", text).strip()


def read_commands(wait=0):
    """Reads new messages and taps. With wait > 0 Telegram holds the request open until something arrives
    (long polling), so an answer can follow within a second or two."""
    res = tg("getUpdates", timeout=wait, _timeout=wait + 15,
             allowed_updates=json.dumps(["message", "callback_query"]))
    updates = res.get("result", [])
    cmds, other, last_id, now = [], False, 0, time.time()
    for u in updates:
        last_id = max(last_id, u["update_id"])
        cb = u.get("callback_query")
        if cb:                                       # tap on a small button under a message
            if str((cb.get("message") or {}).get("chat", {}).get("id")) != str(CHAT):
                continue
            try:
                tg("answerCallbackQuery", callback_query_id=cb["id"])
            except Exception:  # noqa: BLE001
                pass
            mid = (cb.get("message") or {}).get("message_id")
            data = cb.get("data") or ""
            if mid and data[:1] in WIZ_KINDS and (data == "x" or data[1:2] == ":"):
                WIZ.append((mid, data))          # urgent menu: the message is redrawn after all taps are read
                continue
            if mid:
                try:   # remove the small buttons so they are not tapped twice
                    tg("editMessageReplyMarkup", chat_id=CHAT, message_id=mid,
                       reply_markup=json.dumps({"inline_keyboard": []}))
                except Exception:  # noqa: BLE001
                    pass
            if cb.get("data") in CALLBACKS:
                cmds.append(CALLBACKS[cb["data"]])
            continue
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


# ---- Token reminder -----------------------------------------------------------
def check_token_reminder():
    """At 09:00 Vienna time, 14 / 7 / 3 / 1 days before the token expires (and on the day) send one reminder."""
    now = dt.datetime.now(ZoneInfo("Europe/Vienna"))
    left = (TOKEN_EXPIRES - now.date()).days
    if now.hour != 9 or left not in REMIND_DAYS:
        return
    tag = f"token-{TOKEN_EXPIRES.isoformat()}:{left}"
    try:
        done = open(REMINDERS_FILE, encoding="utf-8").read().split()
    except OSError:
        done = []
    if tag in done:
        return
    when = "TODAY" if left == 0 else ("tomorrow" if left == 1 else f"in {left} days")
    say(f"🔑 Reminder: the GitHub token that cron-job.org uses expires {when} ({TOKEN_EXPIRES:%a %d.%m.%Y}). "
        "After that, Sun Set alerts and urgent checks stop.\n\n"
        "To renew: GitHub → Settings → Developer settings → Fine-grained tokens → Regenerate. "
        "Then paste the new token into the Authorization header of each cron-job.org job "
        "(\"Bearer <token>\"). Never paste the token into this chat. "
        "Afterwards change token_expires in config.toml to the new date.")
    with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(done + [tag]) + "\n")
    commit_files("Token reminder sent", add=[REMINDERS_FILE])


# ---- Pinned status message ---------------------------------------------------
def run_times(workflow):
    """(time of the last successful run, last finished run was a failure?) from the GitHub API."""
    runs = gh(f"actions/workflows/{workflow}/runs?per_page=8").get("workflow_runs", [])
    done = [r for r in runs if r.get("status") == "completed"]
    ok = next((r for r in done if r.get("conclusion") == "success"), None)
    t = dt.datetime.fromisoformat(ok["updated_at"].replace("Z", "+00:00")) if ok else None
    running = next((r for r in runs if r.get("status") == "in_progress"), None)   # a check that has just started counts too
    if running and running.get("run_started_at"):
        started = dt.datetime.fromisoformat(running["run_started_at"].replace("Z", "+00:00"))
        if t is None or started > t:
            t = started
    failed = bool(done) and done[0].get("conclusion") not in ("success", "cancelled", "skipped")
    return t, failed


def stamp(t, now=None):
    """Always day, date and time in Vienna time, e.g. 'Tue 17.11. 16:29'."""
    return f"{vienna(t):%a %d.%m. %H:%M}"


def ago(t, now):
    mins = int((now - t).total_seconds() // 60)
    return "just now" if mins < 1 else (f"{mins} min ago" if mins < 90 else f"{mins // 60} h ago")


def build_status():
    now = dt.datetime.now(dt.timezone.utc)
    flag = read_flag()
    lines = ["📌 PADEL WATCH · status", ""]
    if flag:
        ends = vienna(flag["since"] + dt.timedelta(hours=URGENT_MAX_HOURS))
        extra = opts_text(flag["opts"])
        if flag["pause"] and flag["pause"] > now:
            lines.append(f"🚨 Urgent: ON, ⏸ paused until {stamp(flag['pause'], now)}")
        else:
            lines.append(f"🚨 Urgent: ON ({CADENCE['urgent']})")
        lines.append(f"   {extra} · ends {ends:%a %d.%m. %H:%M}")
    else:
        lines.append("🚨 Urgent: off")
    hour = vienna(now).hour
    quiet = hour >= QUIET_FROM or hour < QUIET_TO
    problems = []
    mutes = W.mute_read()
    for label, workflow, limit in WATCHERS:
        label = f"{label} ({CADENCE[workflow]})"
        until = mutes.get(MUTE_KEY.get(workflow, ""))
        if until:
            lines.append(f"🔕 {label}: muted until {stamp(until)}")
            continue
        try:
            t, failed = run_times(workflow)
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{label}: ❓ could not read ({type(exc).__name__})")
            continue
        if workflow == "courts.yml":
            watching = flag and not quiet and not (flag["pause"] and flag["pause"] > now)
            limit = 15 if watching else None
        text = f"{label}: " + (f"last check {stamp(t)}" if t else "no check yet")
        stale = limit is not None and (t is None or (now - t).total_seconds() / 60 > limit)
        if failed:
            text = "❌ " + text + " · last run FAILED"
            bad = W.health_read(HEALTH_KEY.get(workflow, ""))
            if bad:
                text += f" · page could not be read since {stamp(bad['since'])}"
            problems.append(label)
        elif stale:
            text = "⚠️ " + text + " · no recent check"
            problems.append(label)
        else:
            text = "✅ " + text
        lines.append(text)
    lines.append(meetup_status_line(mutes, now))
    lines += ["", "⚙️ GITHUB"]   # a failing watcher is marked on its own line (❌ / ⚠️), no summary line needed
    left = (TOKEN_EXPIRES - vienna(now).date()).days
    if left < 0:
        lines.append(f"❌ 🔑 Token for cron-job.org EXPIRED on {TOKEN_EXPIRES:%a %d.%m.%Y}")
    else:
        icon = "❌" if left <= 3 else ("⚠️" if left <= 14 else "✅")
        lines.append(f"{icon} 🔑 Token for cron-job.org expires {TOKEN_EXPIRES:%a %d.%m.%Y} · {left} days left")
    if REPO:
        lines.append(f"🔗 https://github.com/{REPO}/actions")
    return "\n".join(lines)


def pinned_status_id():
    """The status message is the one our bot pinned: ask Telegram, no repo file needed."""
    try:
        chat = tg("getChat", chat_id=CHAT).get("result", {})
        pin = chat.get("pinned_message") or {}
        text = str(pin.get("text", ""))
        if pin and text.startswith("📌 PADEL WATCH"):
            return int(pin["message_id"])
        print(f"STATUS: getChat gave pinned_message={'yes' if pin else 'no'}, "
              f"text starts with {text[:20]!r}, from={pin.get('from')}")
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not ask Telegram for the pinned message ({type(exc).__name__}).")
    return None


def save_via_api(path, text, message):
    """Saves a file in the repo through the GitHub API (more reliable than git push races)."""
    import base64
    sha = None
    try:
        sha = gh(f"contents/{path}?ref={REF}").get("sha")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    body = {"message": message, "branch": REF,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii")}
    if sha:
        body["sha"] = sha
    gh(f"contents/{path}", body, method="PUT")


def update_status(force=False, create=False):
    """Edits the pinned status message. It is created ONLY when create=True (you typed 'status').
    Automatic runs never create a message, so they can never spam the chat. Never raises."""
    now = dt.datetime.now(ZoneInfo("Europe/Vienna"))
    if not force and now.minute % STATUS_EVERY_MIN > 1:   # minutes :00/:01, :05/:06 ... (runs can be late)
        print("STATUS: not due this minute.")
        return
    try:
        text = build_status()
        mid = pinned_status_id()
        source = "pinned message"
        if not mid:
            source = "repo file"
            try:
                mid = int(open(STATUS_FILE, encoding="utf-8").read().strip())
            except (OSError, ValueError):
                pass
        if not mid and not create:
            print("STATUS: no status message known. Type 'status' in the chat to create it.")
            return
        print(f"STATUS: message id {mid} (from {source})" if mid else "STATUS: no status message known, creating one.")
        if mid:
            try:
                tg("editMessageText", chat_id=CHAT, message_id=mid, text=text,
                   disable_web_page_preview="true")
                return
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "ignore") if hasattr(exc, "read") else ""
                low = body.lower()
                if "not modified" in low:
                    return
                gone = exc.code in (400, 404) and ("not found" in low or "invalid" in low or "can't be edited" in low)
                print(f"STATUS: edit of message {mid} failed: HTTP {exc.code} {body[:200]}")
                if not gone:
                    return          # temporary problem: never create a second message because of it
                if not create:
                    print("STATUS: the status message is gone. Type 'status' to create a new one.")
                    return
                print("STATUS: the old status message is gone, creating a new one.")
            except (urllib.error.URLError, OSError) as exc:
                print(f"STATUS: edit of message {mid} failed ({type(exc).__name__}), will retry next time.")
                return
        # No reply_markup here: Telegram refuses to edit a message that was sent with a button keyboard.
        # The buttons stay on screen from the other bot messages.
        res = tg("sendMessage", chat_id=CHAT, text=text, disable_web_page_preview="true")
        mid = res["result"]["message_id"]
        try:
            tg("pinChatMessage", chat_id=CHAT, message_id=mid, disable_notification="true")
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: could not pin the status message ({type(exc).__name__}). Pin it by hand.")
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            f.write(f"{mid}\n")
        try:   # backup for the pinned-message lookup
            save_via_api(STATUS_FILE, f"{mid}\n", "Status message id")
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: could not save the status message id ({type(exc).__name__}).")
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: status update failed ({type(exc).__name__}).")


def process(cmds, other):
    """Handles what you tapped or typed, then the regular upkeep. Returns True when something failed."""
    started_now = False
    failed = False
    wiz_ops = list(WIZ)
    WIZ.clear()

    for cmd in dict.fromkeys(cmds):  # each command once, in order
        try:
            if cmd == "check":
                dispatch("report")
                say("🔎 Checking all locations now. The answer follows in about 3 minutes.")
            elif cmd == "urgent":
                wiz_send_first()
            elif cmd in ("start_all", "start_evenings", "start_main"):
                opts = {"start_all": [], "start_evenings": ["evenings"], "start_main": ["main"]}[cmd]
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
                        "It ends by itself after 48 hours.\n\nButtons below: Pause · Extend · Stop")
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
            elif cmd == "meetup":
                say_html(meetup_text())
            elif cmd == "mute":
                mute_send_menu()
            elif cmd == "whatsapp_summary": # <-- NEW HANDLER
                try:
                    req = urllib.request.Request(WISPBYTE_URL, headers={"User-Agent": "padel-watch"})
                    with urllib.request.urlopen(req, timeout=10) as r:
                        data = json.loads(r.read().decode("utf-8"))
                        
                    if not data:
                        say("No active WhatsApp games found right now.")
                    else:
                        summary = "🎾 <b>ACTIVE WHATSAPP REQUESTS</b>\n\n"
                        for i, (key, m) in enumerate(data.items(), 1):
                            summary += f"<b>{i}. From {m.get('sender', 'Unknown')} ({m.get('group', 'Unknown')})</b>\n{m.get('text', '')}\n\n---\n"
                        say_html(summary)
                except Exception as exc:
                    print(f"WARNING: WhatsApp summary failed ({type(exc).__name__}).")
                    say("❌ Could not reach the WhatsApp Wispbyte server. Is it online?")
            elif cmd == "padel_events":
                dispatch("report", EVENTS_WORKFLOW)
                say("🏆 Reading the Padel-Austria tournaments "
                    f"({' · '.join(sorted(W.get('tournaments', 'states', ['Wien'])))} · "
                    f"{' · '.join(sorted(W.get('tournaments', 'levels', ['Starter'])))} · "
                    f"{' · '.join(sorted(W.get('tournaments', 'kinds', ['Offener Bewerb'])))} · not full). "
                    "The list follows in about 2 minutes.")
            elif cmd == "status":
                update_status(force=True, create=True)
                say("📌 Status updated. It stays pinned at the top of this chat.")
            elif cmd == "menu":
                say(HELP)
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
    if other and not cmds and not wiz_ops:
        say(HELP)
    if wiz_ops:
        try:
            if wiz_run(wiz_ops):
                started_now = True
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: urgent menu failed ({type(exc).__name__}).")
            failed = True

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
                failed_last = bool(run) and run.get("conclusion") == "failure"
                due = (not run) or (
                    (dt.datetime.now(dt.timezone.utc) - created).total_seconds()
                    >= (URGENT_BACKOFF_SEC if failed_last else URGENT_EVERY_SEC)
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
    try:
        check_token_reminder()
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: token reminder failed ({type(exc).__name__}).")
    update_status(force=bool(cmds) or bool(wiz_ops))
    return failed


def main():
    _now = dt.datetime.now(ZoneInfo("Europe/Vienna"))
    print(f"tg_listen v13 · started {_now:%H:%M:%S} Vienna · listening up to {LISTEN_SECONDS} s · "
          "status edit runs at minutes ending 0/1 and 5/6")
    if not TOKEN or not CHAT:
        print("Telegram secrets are missing.")
        sys.exit(3)
    deadline = time.time() + LISTEN_SECONDS
    failed, first = False, True
    while True:
        wait = 0 if first else int(min(20, max(1, deadline - time.time())))
        t0 = time.time()
        try:
            cmds, other = read_commands(wait)
        except (urllib.error.URLError, OSError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code < 500 and exc.code not in (429, 409):
                print(f"ERROR: Telegram refused the request (HTTP {exc.code}). Check the token and chat ID.")
                sys.exit(1)
            # Telegram is sometimes unreachable for a moment (or another listener is still finishing).
            # Waiting a few seconds and trying again is enough; no failure e-mail needed.
            print(f"WARNING: Telegram not reachable now ({type(exc).__name__}). Trying again.")
            if first and LISTEN_SECONDS <= 0:
                return
            first = False
            if time.time() >= deadline:
                break
            time.sleep(2)
            continue
        if first or cmds or other or WIZ:
            failed = process(cmds, other) or failed
        first = False
        if time.time() >= deadline - 1:
            break
        if not cmds and time.time() - t0 < 1:
            time.sleep(1)               # never hammer Telegram if it answers instantly
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
