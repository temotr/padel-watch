"""Shared helpers for all watchers: settings file, mute list, page-health tracking.

 * config.toml   all settings you may want to change (locations, weekdays, times, filters ...)
                A missing or broken file never stops anything: the built-in defaults are used.
 * mute.txt      "<watcher> <until, UTC>" lines written by the Telegram menu "Mute"
 * health_*.txt  one file per watcher: how many runs in a row could not read the website
"""
import datetime as dt
import json
import os
import pathlib
import time
import tomllib
import urllib.parse
import urllib.request

CONFIG_FILE = pathlib.Path(os.environ.get("PADEL_CONFIG", "config.toml"))
MUTE_FILE = pathlib.Path("mute.txt")
MUTE_NAMES = ("sunset", "events", "meetup")           # watchers that can be muted ("urgent" has its own pause)
HEALTH_NAMES = {"sunset": "📅 Sun Set events", "courts": "🎾 Court checks", "events": "🏆 Padel-Austria tournaments"}

_cache = {}


# ---- settings ---------------------------------------------------------------------------------
def config():
    """The parsed config.toml ({} when it is missing or has a typing mistake)."""
    if "cfg" not in _cache:
        try:
            _cache["cfg"] = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _cache["cfg"] = {}
        except (OSError, ValueError) as exc:   # tomllib.TOMLDecodeError is a ValueError
            print(f"WARNING: {CONFIG_FILE} could not be read ({exc}). Built-in defaults are used.")
            _cache["cfg"] = {}
    return _cache["cfg"]


def reload_config():
    _cache.pop("cfg", None)


def get(section, key, default):
    """config.toml value, or the default when it is missing or has the wrong kind (number / text / list)."""
    value = config().get(section, {}).get(key)
    if value is None:
        return default
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(value, bool):
        return default
    if isinstance(default, (int, float)) and not isinstance(default, bool):
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default
    if isinstance(default, str):
        return value if isinstance(value, str) else default
    if isinstance(default, list):
        return value if isinstance(value, list) and value else default
    if isinstance(default, dict):
        return value if isinstance(value, dict) else default
    return value


def table(section, key, fields, default):
    """A list of tables like [[courts.locations]]: returns a list of dicts that contain all `fields` as text."""
    rows = config().get(section, {}).get(key)
    if not isinstance(rows, list) or not rows:
        return default
    out = []
    for row in rows:
        if not isinstance(row, dict) or not all(isinstance(row.get(f), str) and row.get(f) for f in fields):
            print(f"WARNING: config.toml [{section}.{key}] has an incomplete entry, defaults are used.")
            return default
        out.append(row)
    return out


WEEKDAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def weekday_set(names, default):
    """['Mon','Tue'] -> {0, 1}. Anything unknown -> default."""
    try:
        out = {WEEKDAY_SHORT.index(str(n).strip()[:3].title()) for n in names}
    except ValueError:
        return default
    return out or default


def minutes(text, default):
    """'18:00' -> 1080"""
    try:
        h, m = str(text).split(":")
        return int(h) * 60 + int(m)
    except ValueError:
        return default


# ---- Telegram (plain text, never prints the token) --------------------------------------------
def telegram(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    for attempt in range(3):
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage", data=data), timeout=20).read()
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: Telegram message failed ({type(exc).__name__}), attempt {attempt + 1}.")
            time.sleep(2)
    return False


# ---- mute list --------------------------------------------------------------------------------
def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def mute_read(path=None, now=None):
    """{'sunset': until, ...} for mutes that are still running."""
    now = now or now_utc()
    out = {}
    try:
        lines = (path or MUTE_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        parts = line.split()
        if len(parts) != 2 or parts[0] not in MUTE_NAMES:
            continue
        try:
            until = dt.datetime.fromisoformat(parts[1])
        except ValueError:
            continue
        if until.tzinfo is None:
            until = until.replace(tzinfo=dt.timezone.utc)
        if until > now:
            out[parts[0]] = until
    return out


def muted(name, now=None):
    """Until when this watcher is muted (None = not muted)."""
    return mute_read(now=now).get(name)


def mute_text(mutes):
    return "".join(f"{n} {u.isoformat(timespec='seconds')}\n" for n, u in sorted(mutes.items()))


# ---- page health ------------------------------------------------------------------------------
def health_path(name):
    return pathlib.Path(f"health_{name}.txt")


def health_read(name):
    """None when the last run was fine, else {'fails': n, 'since': datetime, 'warned': bool, 'detail': text}."""
    try:
        text = health_path(name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("fails="):
        return None
    d = {}
    for part in text.split(" | "):
        k, _, v = part.partition("=")
        d[k] = v
    try:
        return {"fails": int(d["fails"]), "since": dt.datetime.fromisoformat(d["since"]),
                "warned": d.get("warned") == "1", "detail": d.get("detail", "")}
    except (KeyError, ValueError):
        return None


def health_write(name, state):
    text = "ok" if state is None else (f"fails={state['fails']} | since={state['since'].isoformat(timespec='seconds')}"
                                       f" | warned={'1' if state['warned'] else '0'} | detail={state['detail']}")
    health_path(name).write_text(text + "\n", encoding="utf-8")


def health_report(name, ok, detail="", threshold=2, send=telegram, now=None):
    """Call once per run. A run that cannot read the website counts as a failure; after `threshold` failures
    in a row you get ONE Telegram warning, and one message when the page works again.
    Returns the message that was sent (or None)."""
    now = now or now_utc()
    old = health_read(name)
    label = HEALTH_NAMES.get(name, name)
    if ok:
        if old is None:
            return None
        health_write(name, None)
        if old["warned"]:
            msg = f"✅ {label}: the page can be read again. Everything works as before."
            send(msg)
            return msg
        return None
    state = old or {"fails": 0, "since": now, "warned": False, "detail": ""}
    state["fails"] += 1
    state["detail"] = detail.replace("|", "/").replace("\n", " ")[:120]
    msg = None
    if not state["warned"] and state["fails"] >= threshold:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        msg = (f"⚠️ PADEL WATCH · page looks different\n\n{label}: the page could not be read "
               f"{state['fails']} time{'s' if state['fails'] != 1 else ''} in a row.\n"
               f"Reason: {state['detail'] or 'no data found'}\n\n"
               "Either the website is down for a moment or it changed its layout and the script needs a fix. "
               "Until then you get no alerts from this watcher. I will message you when it works again."
               + (f"\n\nRun log: https://github.com/{repo}/actions" if repo else ""))
        if send(msg):
            state["warned"] = True
    health_write(name, state)
    return msg
