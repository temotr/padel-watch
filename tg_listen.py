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
    "whatsapp summary": "whatsapp_summary", # <-- WhatsApp Button
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
                ["🤝 Meetup events", "📱 WhatsApp Summary"],
                ["🔕 Mute"]]
    else:
        rows = [["🎾 Check all options", "🏆 Padel-Austria tournaments"],
                ["📅 Sun Set events", "🛑 Stop urgent check"],
                ["⏱ Extend urgent +48h", "🔕 Mute"],
                ["🤝 Meetup events", "📱 WhatsApp Summary"]]
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
    "Buttons: Check all options · Padel-Austria tournaments · Urgent padel · Sun Set events · Meetup events
