"""Watches free padel courts at several Padeldome locations and sends a Telegram message.

A 2-hour window is reported when it starts at one of START_TIMES, on
Monday-Thursday within the next 14 days, and is free at one location either
on a single court or on several courts directly after each other
(e.g. Platz 1 18:00-19:00 + Platz 2 19:00-20:00).
You get a message only when such a window is NEWLY free.
"""
import datetime as dt
import html
import json
import os
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

# ---- Settings: they live in config.toml (the values below are only the fallback) --------
import watchlib as W

BASE = "https://www.padeldome.wien/reservierung"
_D_LOC = [  # id from the link ?c=..., name shown in the notification, postcode (= district)
    {"id": "5175", "name": "The Dome Stadlau", "postcode": "1220"},
    {"id": "2665", "name": "Padeldome Erdberg", "postcode": "1030"},
    {"id": "2667", "name": "Padeldome Süßenbrunn", "postcode": "1220"},
    {"id": "3216", "name": "Padeldome Alte Donau", "postcode": "1210"},
]
_D_FAR = [  # alternative locations: shown at the end, never start an alert on their own
    {"id": "2668", "name": "Padeldome Alterlaa", "postcode": "1230"},
    {"id": "3660", "name": "Racketworld", "postcode": "1160",
     "site": "https://www.racketworld.wien/reservierung", "date_param": "t"},
    {"id": "4797", "name": "Europahalle", "postcode": "1230",
     "site": "https://reservierung.europahalle.at/reservierung", "date_param": "d"},
    {"id": "4858", "name": "Playpadel Gänserndorf", "postcode": "2230",
     "site": "https://playpadel.tennisplatz.info/reservierung", "date_param": "dt"},
]
# Padelzone is booked on Eversports, which blocks automatic reading (Cloudflare check).
# These are only shown as links at the end of urgent messages, to be opened by hand.
_D_PADELZONE = [
    {"name": "Padelzone Vienna Sporthotel (1030)", "url": "https://www.eversports.at/sb/padelzone-wien-or-vienna-sporthotel"},
    {"name": "Padelzone Floridsdorf (1210)", "url": "https://www.eversports.at/sb/padelzone-wien-or-floridsdorf-powered-by-cupra"},
    {"name": "Padelzone C&C Wienerberg (1100)", "url": "https://www.eversports.at/sb/padelzone-wien-or-candc-wienerberg"},
    {"name": "Padelzone Hagenbrunn (2102) · Sport König", "url": "https://www.eversports.at/sb/padelzone-hagenbrunn-or-sport-koenig"},
    {"name": "Padelzone Stockerau (2000)", "url": "https://www.eversports.at/sb/padelzone-stockerau"},
]
_loc = W.table("courts", "locations", ("id", "name"), _D_LOC)
_far = W.table("courts", "far_locations", ("id", "name"), _D_FAR)
LOCATIONS = [(r["id"], r["name"]) for r in _loc]
FAR_LOCATIONS = [(r["id"], r["name"]) for r in _far]
PADELZONE_LINKS = [(r["name"], r["url"]) for r in W.table("courts", "padelzone", ("name", "url"), _D_PADELZONE)]
# Locations on another website: (reservation page, name of the date parameter in the link)
OTHER_SITES = {r["id"]: (r["site"], ("d", "t") if r["date_param"] == "dt" else r["date_param"])
               for r in _far if isinstance(r.get("site"), str) and isinstance(r.get("date_param"), str)}
POSTCODES = {r["id"]: r["postcode"] for r in _loc + _far if isinstance(r.get("postcode"), str)}
DAYS_AHEAD = W.get("courts", "days_ahead", 15)               # today + 14 days
BOOK_AHEAD_DAYS = W.get("courts", "book_ahead_days", 14)     # the club allows booking only 14 days before the start
WEEKDAYS = W.weekday_set(W.get("courts", "weekdays", ["Mon", "Tue", "Wed", "Thu"]), {0, 1, 2, 3})
START_TIMES = W.get("courts", "start_times", ["18:00", "18:30", "19:00", "19:30", "20:00"])
DURATION_MIN = W.get("courts", "duration_min", 120)          # a court must be free for this long
MAX_WINDOWS = W.get("courts", "max_windows", 25)             # max windows listed in one notification
MIN_SLOTS_READ = W.get("courts", "min_slots_read", 12)       # a day with this many slots on the page counts as read
URGENT_DAYS = W.get("urgent", "days", 3)                     # urgent mode: today and the next 2 days, any weekday
URGENT_FIRST = W.get("urgent", "first", "07:00")             # urgent mode: every half-hour start in between
URGENT_LAST = W.get("urgent", "last", "21:30")
SNAP = pathlib.Path("courts_state.txt")
URGENT_SNAP = pathlib.Path("urgent_state.txt")
URGENT_FLAG = pathlib.Path("urgent.txt")   # "on <time>" while urgent mode is switched on
# --------------------------------------------------------------------------

TZ = ZoneInfo("Europe/Vienna")
STATE_HEADER = "# v3"


def page_url(loc, ts):
    """Link to one day of a location (the day is given as the timestamp of 08:00 Vienna time)."""
    base, params = OTHER_SITES.get(loc, (BASE, "d"))
    params = (params,) if isinstance(params, str) else params
    return f"{base}?c={loc}" + "".join(f"&{p}={ts}" for p in params)


def click_url(loc):
    base, _ = OTHER_SITES.get(loc, (BASE, "d"))
    return f"{base}?c={loc}"


def urgent_mode():
    return os.environ.get("MODE", "").strip().lower() == "urgent"


def active_weekdays():
    return urgent_cfg()["weekdays"] if urgent_mode() else WEEKDAYS


def urgent_words():
    try:
        return URGENT_FLAG.read_text(encoding="utf-8").split()
    except OSError:
        return []


def urgent_opts():
    """Filters stored in urgent.txt after the start time: 'evenings' and/or 'main'."""
    return {w for w in urgent_words() if w in ("evenings", "main")}


def urgent_cfg():
    """Settings from the urgent menu, stored in urgent.txt as words: days=7 wd=1111000 from=18 to=20.
    Missing or broken words fall back to the old urgent behaviour (3 days, any weekday, any time)."""
    cfg = {"days": URGENT_DAYS, "weekdays": set(range(7)), "first": None, "last": None}
    for w in urgent_words():
        key, _, val = w.partition("=")
        try:
            if key == "days":
                cfg["days"] = max(1, min(int(val), DAYS_AHEAD))
            elif key == "wd" and len(val) == 7 and set(val) <= {"0", "1"} and "1" in val:
                cfg["weekdays"] = {i for i, c in enumerate(val) if c == "1"}
            elif key == "from" and 0 <= int(val) <= 23:
                cfg["first"] = int(val)
            elif key == "to" and 0 <= int(val) <= 23:
                cfg["last"] = int(val)
        except ValueError:
            pass
    if cfg["first"] is not None and cfg["last"] is not None and cfg["last"] < cfg["first"]:
        cfg["first"] = cfg["last"] = None
    return cfg


def urgent_days():
    return urgent_cfg()["days"]


def active_starts():
    """Start times to look for: your evening rules, or every half hour in urgent mode."""
    if not urgent_mode():
        return START_TIMES
    cfg = urgent_cfg()
    first = f"{cfg['first']:02d}:00" if cfg["first"] is not None else ("17:00" if "evenings" in urgent_opts() else URGENT_FIRST)
    out, t = [], int(first[:2]) * 60 + int(first[3:])
    last = int(URGENT_LAST[:2]) * 60 + int(URGENT_LAST[3:])
    if cfg["last"] is not None:   # "latest start 21" means 21:30, the last start the club offers
        last = cfg["last"] * 60 + (30 if cfg["last"] >= 21 else 0)
    while t <= last:
        out.append(f"{t // 60:02d}:{t % 60:02d}")
        t += 30
    return out


def urgent_is_on():
    try:
        return URGENT_FLAG.read_text(encoding="utf-8").strip().lower().startswith("on")
    except OSError:
        return False

JS = """
() => {
  const norm = (el) => el.textContent.replace(/\\s+/g, " ").trim();
  const labelY = (text) => {
    const el = [...document.querySelectorAll("span")].find(
      (e) => e.children.length === 0 && norm(e) === text
    );
    return el ? el.getBoundingClientRect().y : null;
  };
  const y8 = labelY("08:00");
  const y18 = labelY("18:00");
  const pxPerMin = y8 !== null && y18 !== null ? (y18 - y8) / 600 : null;

  const courts = [...document.querySelectorAll("div.court[data-cid]")];
  const order = [];
  courts.forEach((c) => {
    if (!order.includes(c.dataset.cid)) order.push(c.dataset.cid);
  });
  const heads = [...document.querySelectorAll(".day-head .day-courts .court")]
    .map((e) => norm(e))
    .filter((t) => t);
  const names = {};
  order.forEach((cid, i) => {
    names[cid] = heads[i] || ("Platz " + (i + 1));
  });

  const slots = [];
  courts.forEach((court) => {
    court.querySelectorAll(":scope > .slot").forEach((s) => {
      const r = s.getBoundingClientRect();
      const res = s.classList.contains("res");
      const free = s.classList.contains("av") && !res;
      slots.push([court.dataset.cid, Number(s.dataset.begin), r.height, free, res]);
    });
  });
  return { pxPerMin: pxPerMin, names: names, slots: slots };
}
"""


def merge(intervals, tol=120):
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1] + tol:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def covers(merged, s, e, tol=120):
    return any(a <= s + tol and b >= e - tol for a, b in merged)


WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                 "Saturday", "Sunday"]


def hhmm_of(ts):
    return dt.datetime.fromtimestamp(ts, TZ).strftime("%H:%M")


def half_hour(ts):
    """Round a timestamp to the nearest :00 / :30 (page pixels are not exact)."""
    return round(ts / 1800) * 1800


def tiles(slots, s, e, tol=120):
    """True when bookable slots follow each other exactly from s to e.

    A court's slots are fixed blocks (1 hour, starting at :00 on some courts and at :30 on
    others), so a booking can only start where a slot starts.
    """
    t = s
    while t < e - tol:
        nxt = next((b for a, b in slots if abs(a - t) <= tol and b > t + tol), None)
        if nxt is None:
            return False
        t = nxt
    return True


def chain(courts_, order, s, e, tol=120):
    """Cover [s, e] with slots of several courts, one slot after the other, fewest changes.

    courts_: {cid: [(slot_start, slot_end), ...]}. Returns [(cid, from_ts, to_ts)] or None.
    """
    t, prev, segs = s, None, []
    while t < e - tol:
        cands = [(cid, b) for cid, sl in courts_.items() for a, b in sl
                 if abs(a - t) <= tol and b > t + tol]
        if not cands or len(segs) >= 8:
            return None
        cid, b = max(cands, key=lambda c: (c[0] == prev, -order.get(c[0], 999)))
        segs.append((cid, t, min(b, e)))
        t, prev = b, cid
    return segs


def find_windows(free, names, targets):
    """free: {(cid, date): [(start_ts, end_ts), ...]}.

    Returns {(date_iso, 'HH:MM'): 'Platz 3, 19:30-21:30'} for one location.
    """
    by_date = {}
    for (cid, date), intervals in free.items():
        if date in targets:
            by_date.setdefault(date, {})[cid] = sorted(intervals)
    order = {cid: i for i, cid in enumerate(names)}

    def nm(cid):
        return names.get(cid, f"Court {cid}")

    out = {}
    for date, courts_ in by_date.items():
        for hhmm in active_starts():
            h, m = map(int, hhmm.split(":"))
            s = int(dt.datetime.combine(date, dt.time(h, m), TZ).timestamp())
            e = s + DURATION_MIN * 60
            end_txt = hhmm_of(e)
            singles = sorted(
                (c for c, sl in courts_.items() if tiles(sl, s, e)),
                key=lambda c: order.get(c, 999),
            )
            if singles:
                shown = " / ".join(nm(c) for c in singles[:4])
                if len(singles) > 4:
                    shown += f" / +{len(singles) - 4} more"
                out[(date.isoformat(), hhmm)] = f"{shown} · {hhmm}-{end_txt}"
                continue
            segs = chain(courts_, order, s, e)
            if segs:
                parts = []
                for i, (cid, a, b) in enumerate(segs):
                    a_txt = hhmm if i == 0 else hhmm_of(half_hour(a))
                    b_txt = end_txt if i == len(segs) - 1 else hhmm_of(half_hour(b))
                    parts.append(f"{nm(cid)} {a_txt}-{b_txt}")
                out[(date.isoformat(), hhmm)] = " + ".join(parts)
    return out


def ingest(data, free, names, seen_dates, seen_slots, stats=None, only_date=None):
    """Add one page's slots to the collected data. Returns (total, free) counts."""
    ppm = data["pxPerMin"] or (25 / 30)
    n_total = n_free = 0
    for cid, name in data["names"].items():
        names.setdefault(cid, name)
    for cid, begin, height, is_free, is_res in data["slots"]:
        if begin <= 0 or (cid, begin) in seen_slots:
            continue
        date = dt.datetime.fromtimestamp(begin, TZ).date()
        if only_date is not None and date != only_date:
            continue  # other days on the page are not fully loaded: ignore them
        seen_slots.add((cid, begin))
        seen_dates.add(date)
        n_total += 1
        if stats is not None:
            row = stats.setdefault(date, [0, 0, 0, 0])
            # slots, free, free at 18:00-21:59, evening slots that are free or booked
            row[0] += 1
            evening = 18 <= dt.datetime.fromtimestamp(begin, TZ).hour <= 21
            if is_free:
                row[1] += 1
                if evening:
                    row[2] += 1
            if evening and (is_free or is_res):
                row[3] += 1
        if is_free:
            n_free += 1
            length = max(1800, round(height / ppm * 60 / 1800) * 1800)  # 3600 for 1-hour slots
            free.setdefault((cid, date), []).append((begin, begin + length))
    return n_total, n_free


NUMBERS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]


BOLD_ON, BOLD_OFF = "\x02", "\x03"   # markers, turned into Telegram HTML
UL_ON, UL_OFF = "\x04", "\x05"       # underline (Telegram only)
IT_ON, IT_OFF = "\x11", "\x12"       # italic (Telegram only)
BQ_ON, BQ_OFF = "\x13", "\x14"       # quote block (Telegram only)
SL_A, SL_B, SL_C = "\x0e", "\x0f", "\x10"   # soft link: tappable name in Telegram
LINK_A, LINK_B, LINK_C = "\x06", "\x07", "\x08"   # link: LINK_A url LINK_B name LINK_C
ALT_HEADER = "OTHER LOCATIONS FREE RIGHT NOW (farther away)"
NOTHING = "nothing free for set booking conditions"


def bold(text):
    return f"{BOLD_ON}{text}{BOLD_OFF}"


def heading(text):
    """Section heading: bold and underlined in Telegram, Telegram only."""
    return f"{BOLD_ON}{UL_ON}{text}{UL_OFF}{BOLD_OFF}"


def underline(text):
    return f"{UL_ON}{text}{UL_OFF}"


def link(name, url):
    return f"{LINK_A}{url}{LINK_B}{name}{LINK_C}"


def slink(name, url):
    return f"{SL_A}{url}{SL_B}{name}{SL_C}"


def plain(text):
    text = re.sub(f"{SL_A}(.*?){SL_B}(.*?){SL_C}", r"\2", text)
    # ntfy: "Name: https://..." (the URL is tappable there)
    text = re.sub(f"{LINK_A}(.*?){LINK_B}(.*?){LINK_C}", r"\2: \1", text)
    for m in (BOLD_ON, BOLD_OFF, UL_ON, UL_OFF, IT_ON, IT_OFF, BQ_ON, BQ_OFF):
        text = text.replace(m, "")
    return text


def telegram_html(text):
    out = html.escape(text, quote=False)
    out = re.sub(f"[{LINK_A}{SL_A}](.*?)[{LINK_B}{SL_B}](.*?)[{LINK_C}{SL_C}]",
                 lambda m: f'<a href="{m.group(1).replace(chr(34), "%22")}">{m.group(2)}</a>', out)
    for mark, tag in ((BOLD_ON, "<b>"), (BOLD_OFF, "</b>"), (UL_ON, "<u>"), (UL_OFF, "</u>"),
                      (IT_ON, "<i>"), (IT_OFF, "</i>"), (BQ_ON, "<blockquote>"), (BQ_OFF, "</blockquote>")):
        out = out.replace(mark, tag)
    return out


def is_far(loc):
    return loc in {l for l, _ in FAR_LOCATIONS}


def loc_name(loc):
    """'Padeldome Süßenbrunn (1220)'"""
    name = dict(LOCATIONS).get(loc) or dict(FAR_LOCATIONS).get(loc, loc)
    plz = POSTCODES.get(loc)
    return f"{name} ({plz})" if plz else name


def loc_label(loc):
    """'3️⃣ Padeldome Süßenbrunn (1220)' - the number is the position in LOCATIONS."""
    ids = [l for l, _ in LOCATIONS + FAR_LOCATIONS]
    if loc in ids:
        idx = ids.index(loc)
        return f"{NUMBERS[idx] if idx < len(NUMBERS) else '•'} {loc_name(loc)}"
    return f"• {loc_name(loc)}"


def minutes(hhmm):
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def short_courts(courts):
    """'Platz 8 / Platz 5 / Platz 6' -> 'Platz 8 / 5 / 6'."""
    parts = courts.split(" / ")
    return " / ".join([parts[0]] + [p[6:] if p.startswith("Platz ") else p for p in parts[1:]])


def compress_day(rows):
    """rows: [(start 'HH:MM', text)] of one location and day -> [(sort_key, line_text)].

    Windows with the same courts that follow each other (18:00, 18:30, 19:00 ...) become
    one line: 'Platz 8 / 5 · 18:00-22:00 (4h free)'. Chains over several courts stay as they are.
    """
    out, singles = [], {}
    for hhmm, text in sorted(rows):
        if " + " in text or " · " not in text:
            out.append((hhmm, text))
            continue
        courts, _, rng = text.rpartition(" · ")
        singles.setdefault(courts, []).append((hhmm, rng.partition("-")[2]))
    for courts, lst in singles.items():
        runs, run = [], [lst[0]]
        for item in lst[1:]:
            if minutes(item[0]) <= minutes(run[-1][1]):
                run.append(item)
            else:
                runs.append(run)
                run = [item]
        runs.append(run)
        for run in runs:
            first, last_end = run[0][0], run[-1][1]
            line = f"{short_courts(courts)} · {first}-{last_end}"
            if len(run) > 1:
                line += f" ({(minutes(last_end) - minutes(first)) / 60:g}h free)"
            out.append((first, line))
    return sorted(out)


EVENING_FROM = 17 * 60


def short_label(loc):
    """'3️⃣ Süßenbrunn (1220)' (without the word Padeldome)."""
    return loc_label(loc).replace("Padeldome ", "")


def court_count(courts):
    """'Platz 8 / 5 / 6 / 7 / +2 more' -> 6"""
    n = 0
    for part in courts.split(" / "):
        m = re.match(r"\+(\d+)", part)
        n += int(m.group(1)) if m else 1
    return n


def block_info(first, line):
    """Reads one compressed line back into numbers (start, end, courts, text)."""
    times = re.findall(r"\d\d:\d\d", line)
    if " + " in line or " · " not in line:       # chain over several courts
        return {"start": times[0] if times else first, "end": times[-1] if times else first,
                "courts": None, "n": 1, "text": line}
    courts, _, rng = line.rpartition(" · ")
    a, b = re.match(r"(\d\d:\d\d)-(\d\d:\d\d)", rng).groups()
    return {"start": a, "end": b, "courts": courts, "n": court_count(courts), "text": line}


def hours_of(b):
    return (minutes(b["end"]) - minutes(b["start"])) / 60


def suggestion(b):
    """What to book: the first court of the block, e.g. 'Platz 8 12:30-15:30 (3h)'."""
    if b["courts"] is None:
        return b["text"]
    return f"{b['courts'].split(' / ')[0]} {b['start']}-{b['end']} ({hours_of(b):g}h)"


def describe_summary(items, prefix=""):
    """Urgent mode: per day and location ONE line with all free time ranges plus a booking tip.

    Wednesday 18-11-2026
    3️⃣ Süßenbrunn (1220) · 08:00-17:30 · up to 4 courts
       💡 Platz 8 11:30-17:30 (6h)
       🌙 Platz 7 18:30-21:30 (3h)
    """
    order = {loc: i for i, (loc, _) in enumerate(LOCATIONS)}
    grouped = {}
    for (loc, date, hhmm), text in items.items():
        if not is_far(loc):
            grouped.setdefault((date, loc), []).append((hhmm, text))
    lines, current_day, count, extra = [], None, 0, 0
    for (date, loc) in sorted(grouped, key=lambda k: (k[0], order.get(k[1], 99))):
        if count >= MAX_WINDOWS:
            extra += 1
            continue
        blocks = [block_info(f, t) for f, t in compress_day(grouped[(date, loc)])]
        spans = []
        for st, en in sorted((minutes(b["start"]), minutes(b["end"])) for b in blocks):
            if spans and st <= spans[-1][1]:
                spans[-1][1] = max(spans[-1][1], en)
            else:
                spans.append([st, en])
        span_txt = " · ".join(f"{a // 60:02d}:{a % 60:02d}-{b // 60:02d}:{b % 60:02d}" for a, b in spans)
        peak = max(b["n"] for b in blocks)
        if date != current_day:
            current_day = date
            d = dt.date.fromisoformat(date)
            if lines:
                lines.append("")
            lines.append(bold(f"{WEEKDAY_NAMES[d.weekday()]} {d:%d-%m-%Y}"))
        head = f"{prefix}{short_label(loc)} · {span_txt}"
        if peak > 1:
            head += f" · up to {peak} courts"
        lines.append(head)
        best = max(blocks, key=lambda b: (hours_of(b), b["courts"] is not None, -minutes(b["start"])))
        lines.append(f"   💡 {suggestion(best)}")
        evening = [b for b in blocks if minutes(b["start"]) >= EVENING_FROM]
        if evening:
            best_ev = max(evening, key=lambda b: (hours_of(b), b["courts"] is not None, -minutes(b["start"])))
            if best_ev is not best:
                lines.append(f"   🌙 {suggestion(best_ev)}")
        count += 1
    if extra:
        lines.append("")
        lines.append(f"... and {extra} more")
    return lines


def describe_urgent(near_cur, new_keys, report):
    """Urgent mode body: one block per location, one line per day with the bookable ranges.

    3️⃣ Süßenbrunn (1220)
    Wed 18.11: 08:00-17:30 · 🆕 18:30-21:30 · up to 4 courts
    """
    days = sorted({d for rows in report.values() for d, *_ in rows})
    grouped = {}
    for (loc, date, hhmm), text in near_cur.items():
        grouped.setdefault((loc, date), []).append((hhmm, text))
    new_by = {}
    for (loc, date, hhmm) in new_keys:
        new_by.setdefault((loc, date), []).append(minutes(hhmm))
    lines = []
    for loc, _ in LOCATIONS:
        if report.get(loc) is None:
            continue
        day_lines = []
        for d in days:
            iso = d.isoformat()
            rows = grouped.get((loc, iso))
            label = f"{WEEKDAY_NAMES[d.weekday()][:3]} {d:%d.%m}"
            if not rows:
                day_lines.append(f"{label}: –")
                continue
            blocks = [block_info(f, t) for f, t in compress_day(rows)]
            spans = []
            for st, en in sorted((minutes(b["start"]), minutes(b["end"])) for b in blocks):
                if spans and st <= spans[-1][1]:
                    spans[-1][1] = max(spans[-1][1], en)
                else:
                    spans.append([st, en])
            fresh = new_by.get((loc, iso), [])
            parts = []
            for a, b in spans:
                is_new = any(a <= m < b for m in fresh)
                parts.append(f"{'🆕 ' if is_new else ''}{a // 60:02d}:{a % 60:02d}-{b // 60:02d}:{b % 60:02d}")
            text = f"{label}: " + " · ".join(parts)
            peak = max(b["n"] for b in blocks)
            if peak > 1:
                text += f" · up to {peak} courts"
            day_lines.append(text)
        if lines:
            lines.append("")
        if all(l.endswith(": –") for l in day_lines):
            lines.append(f"❌ {bold(slink(short_label(loc), click_url(loc)))}: {NOTHING}")
        else:
            first_day = min(d for (l, d) in grouped if l == loc)
            lines.append(f"✅ {bold(slink(short_label(loc), day_url(loc, first_day)))}")
            lines.extend(day_lines)
    return lines


def padelzone_block():
    return "\n".join([heading("PADELZONE · NOT CHECKED AUTOMATICALLY (open to check)"), ""]
                     + [f"🔗 {link(name, url)}" for name, url in PADELZONE_LINKS])


def checked_lines_urgent(report):
    days = sorted({d for rows in report.values() for d, *_ in rows})
    bad = [f"{short_label(loc).split(' ', 1)[1]} {d:%d.%m}"
           for loc, rows in report.items() for d, slots, n, ok in rows if not ok and not n]
    span = f"{days[0]:%d.%m}-{days[-1]:%d.%m}" if len(days) > 1 else f"{days[0]:%d.%m}"
    head = heading(f"CHECKED LOCATIONS {span} · next {urgent_days()} days")
    if bad:
        return [head, "", "⚠️ not read: " + ", ".join(bad)]
    return [head, "", f"✅ all {len(report)} locations read"]


def describe(items, prefix=""):
    """Main locations: {(loc, date_iso, hhmm): text} -> lines grouped by day, e.g.

    Tuesday 17-11-2026  (the date line is bold in Telegram)
    2️⃣ Padeldome Erdberg · Platz 2 · 18:00-20:00
    """
    if urgent_mode():
        return describe_summary(items, prefix)
    order = {loc: i for i, (loc, _) in enumerate(LOCATIONS)}
    grouped = {}
    for (loc, date, hhmm), text in items.items():
        if is_far(loc):
            continue
        grouped.setdefault((date, loc), []).append((hhmm, text))
    lines, current_day, count, extra = [], None, 0, 0
    for (date, loc) in sorted(grouped, key=lambda k: (k[0], order.get(k[1], 99))):
        for _, text in compress_day(grouped[(date, loc)]):
            if count >= MAX_WINDOWS:
                extra += 1
                continue
            if date != current_day:
                current_day = date
                d = dt.date.fromisoformat(date)
                if lines:
                    lines.append("")
                lines.append(bold(f"{WEEKDAY_NAMES[d.weekday()]} {d:%d-%m-%Y}"))
            lines.append(f"{prefix}{slink(loc_label(loc), day_url(loc, date))} · {text}")
            count += 1
    if extra:
        lines.append("")
        lines.append(f"... and {extra} more")
    return lines


def describe_far(items, new=(), report=None):
    """Alternative locations: per location one line per time range, with the days after it.

    🆕 Europahalle (1230)
    20:00-22:00 · Thu 19.11 · Mon 23.11
    Alterlaa (1230) · nothing free
    """
    by_loc = {}
    for (loc, date, hhmm) in items:
        if is_far(loc):
            by_loc.setdefault(loc, {}).setdefault(date, []).append(minutes(hhmm))
    new_set = {(k[0], k[1], minutes(k[2])) for k in new}
    lines, nothing = [], []
    for loc, _ in FAR_LOCATIONS:
        days = by_loc.get(loc)
        name = short_label(loc)
        if not days:
            continue
        ranges, all_new = {}, bool(new_set)
        for date in sorted(days):
            spans = []
            for st in sorted(days[date]):
                en = st + DURATION_MIN
                if spans and st <= spans[-1][1]:
                    spans[-1][1] = max(spans[-1][1], en)
                else:
                    spans.append([st, en])
            d = dt.date.fromisoformat(date)
            for a_, b_ in spans:
                is_new = any((loc, date, m) in new_set for m in days[date] if a_ <= m < b_)
                all_new = all_new and is_new
                ranges.setdefault((a_, b_), []).append(
                    (f"{WEEKDAY_NAMES[d.weekday()][:3]} {d:%d.%m}", is_new))
        if lines:
            lines.append("")
        lines.append(f"{'🆕 ' if all_new else ''}{slink(name, day_url(loc, min(days)))}")
        for (a_, b_), day_list in sorted(ranges.items()):
            rng = f"{a_ // 60:02d}:{a_ % 60:02d}-{b_ // 60:02d}:{b_ % 60:02d}"
            if b_ - a_ > DURATION_MIN:
                rng += f" ({(b_ - a_) / 60:g}h free)"
            lines.append(f"{rng} · " + " · ".join(
                f"{'🆕 ' if (n and not all_new) else ''}{lbl}" for lbl, n in day_list))
    if nothing:
        if lines:
            lines.append("")
        lines.extend(nothing)
    return lines


def weekday_text(days):
    days = sorted(days)
    if len(days) == 7:
        return "any day"
    names = [WEEKDAY_NAMES[i][:3] for i in days]
    if len(days) > 1 and days == list(range(days[0], days[-1] + 1)):
        return f"{names[0]}-{names[-1]}"
    return ", ".join(names)


def conditions_text():
    if urgent_mode():
        opts = urgent_opts()
        cfg = urgent_cfg()
        starts = active_starts()
        if cfg["first"] is not None or cfg["last"] is not None:
            when = f"start {starts[0]}-{starts[-1]}"
        else:
            when = "from 17:00" if "evenings" in opts else "any time"
        where = " · main 4 only" if "main" in opts else ""
        return (f"Next {cfg['days']} days · {weekday_text(cfg['weekdays'])} · "
                f"{DURATION_MIN / 60:g}h block · {when}{where}")
    days = sorted(WEEKDAYS)
    names = [WEEKDAY_NAMES[i][:3] for i in days]
    contiguous = days == list(range(days[0], days[-1] + 1))
    day_txt = f"{names[0]}-{names[-1]}" if contiguous and len(names) > 1 else ", ".join(names)
    hours = DURATION_MIN / 60
    hours_txt = f"{hours:g}h"
    return f"{day_txt} · {hours_txt} window · start {START_TIMES[0]}-{START_TIMES[-1]}"


def header_lines(now):
    return [f"🕒 Execution time of the agent: {now:%d.%m.%Y %H:%M}",
            f"📋 Conditions for booking: {conditions_text()}"]


def coverage_footer(report, now):
    """Short overview of what was checked: one line per location."""
    days = sorted({d for rows in report.values() for d, *_ in rows})
    wd = sorted(WEEKDAYS)
    names = [WEEKDAY_NAMES[i][:3] for i in wd]
    day_txt = f"{names[0]}-{names[-1]}" if wd == list(range(wd[0], wd[-1] + 1)) and len(wd) > 1 else ", ".join(names)
    lines = [heading(f"CHECKED LOCATIONS {days[0]:%d.%m}-{days[-1]:%d.%m} · {day_txt} only"), ""]
    later = set()

    def status(rows):
        free = [f"{d:%d.%m}" for d, slots, n, ok in rows if n]
        bad = []
        for d, slots, n, ok in rows:
            if n or ok:
                continue
            if bookable(d.isoformat(), START_TIMES[-1], now):
                bad.append(f"{d:%d.%m}")
            else:
                later.add(d)
        return free, bad

    for loc, name in LOCATIONS + FAR_LOCATIONS:
        rows = report.get(loc)
        if rows is None:
            continue
        free, bad = status(rows)
        short = slink(short_label(loc), click_url(loc))
        mark = ""
        if bad:
            lines.append(f"⚠️ {mark}{short} · not read: " + ", ".join(bad))
        elif free:
            first_iso = min(d for d, slots, n, ok in rows if n).isoformat()
            short = slink(short_label(loc), day_url(loc, first_iso))
            lines.append(f"✅ {mark}{short} · free on " + ", ".join(free) + " (see above)")
        else:
            lines.append(f"❌ {mark}{short} · {NOTHING}")
    if later:
        lines.append("⏳ " + ", ".join(f"{d:%d.%m}" for d in sorted(later))
                     + " · not bookable yet (opens 14 days before each start)")
    return lines


def make_title(show, first_run):
    # Count places per day (a 4h block is one option, not five windows).
    n = len({(k[0], k[1]) for k in show})
    if urgent_mode():
        if first_run:
            return "Urgent watch started"
        return "URGENT: free padel court" if n == 1 else f"URGENT: {n} free padel options"
    if first_run:
        return "Court watcher started"
    return "Free padel court" if n == 1 else f"{n} free padel options"


def day_url(loc, date_iso):
    ts = int(dt.datetime.combine(dt.date.fromisoformat(date_iso), dt.time(8, 0), TZ).timestamp())
    return page_url(loc, ts)


def free_buttons(current):
    """One button per location with something free: opens that location on its earliest free day."""
    first = {}
    for (loc, date, _hhmm) in current:
        if loc not in first or date < first[loc]:
            first[loc] = date
    out = []
    for loc, _ in LOCATIONS + FAR_LOCATIONS:
        if loc in first:
            d = dt.date.fromisoformat(first[loc])
            out.append((f"{short_label(loc)} · {WEEKDAY_NAMES[d.weekday()][:3]} {d:%d.%m}",
                        day_url(loc, first[loc])))
    return out


def send_telegram(title, message, click, buttons=()):
    """Optional second channel. Needs the secrets TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    text = f"🎾 {title}\n\n{message}"
    if len(text) > 3500:  # Telegram's limit is 4096 characters (HTML tags count too)
        text = plain(text)[:3490].rsplit("\n", 1)[0] + "\n…"
    data = {
        "chat_id": chat,
        "parse_mode": "HTML",
        "text": telegram_html(text),
        "disable_web_page_preview": "true",
        "reply_markup": json.dumps(
            {"inline_keyboard": [[{"text": t, "url": u}] for t, u in list(buttons)[:8]]
             or [[{"text": "Open booking page", "url": click}]]}
        ),
    }
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=urllib.parse.urlencode(data).encode("utf-8"),
    )
    for attempt in range(3):
        try:
            urllib.request.urlopen(req, timeout=20).read()
            return True
        except Exception as exc:  # noqa: BLE001  (never print the URL: it contains the token)
            print(f"WARNING: Telegram message failed ({type(exc).__name__}), attempt {attempt + 1}.")
            if attempt < 2:
                time.sleep(3)
    return False


def notify(title, message, click, tags="tennis", buttons=()):
    if not send_telegram(title, message, click, buttons):
        print("Telegram message was not sent (secrets missing or network error).")
        sys.exit(3)


def fetch_day(page, url):
    page.goto(url, wait_until="networkidle")
    page.wait_for_selector("div.court[data-cid]", timeout=20000)
    page.wait_for_timeout(1000)
    return page.evaluate(JS)


def snap_path():
    return URGENT_SNAP if urgent_mode() else SNAP


def load_state():
    """Returns (first_run, {(loc, date, hhmm): text})."""
    path = snap_path()
    if not path.exists():
        return True, {}
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != STATE_HEADER:
        return True, {}  # old format from an earlier version: start fresh
    old = {}
    for l in lines[1:]:
        parts = l.strip().split("|")
        if len(parts) >= 4:
            old[tuple(parts[:3])] = "|".join(parts[3:])
    return False, old


def save_state(current):
    out = [STATE_HEADER]
    for (loc, date, hhmm), text in sorted(current.items()):
        out.append(f"{loc}|{date}|{hhmm}|{text}")
    snap_path().write_text("\n".join(out) + "\n", encoding="utf-8")


def bookable(date_iso, hhmm, now):
    """True when a window starting at date/hhmm can already be booked (<= 14 days ahead)."""
    h, m = map(int, hhmm.split(":"))
    start = dt.datetime.combine(dt.date.fromisoformat(date_iso), dt.time(h, m), TZ)
    return start <= now + dt.timedelta(days=BOOK_AHEAD_DAYS)


def health_threshold():
    """On request (button) you hear about a problem at once; automatic runs wait for a second failure."""
    return 1 if os.environ.get("MODE", "").strip().lower() == "report" else 2


def run_all(page, today=None, now=None):
    now = now or dt.datetime.now(TZ)
    today = today or now.date()
    urgent = urgent_mode()
    if urgent:
        if not urgent_is_on():
            print("Urgent mode is switched off. Nothing to do.")
            return
    days = urgent_days() if urgent else DAYS_AHEAD
    targets = [
        today + dt.timedelta(days=i)
        for i in range(days)
        if (today + dt.timedelta(days=i)).weekday() in active_weekdays()
    ]
    first_run, old = load_state()
    if urgent and not targets:
        print("Urgent mode: none of the next days matches the chosen weekdays, nothing to check.")
        if first_run:
            notify(make_title({}, True),
                   "\n\n".join(["\n".join(header_lines(now)),
                                 "Nothing to check. Urgent mode stays on until you send: stop"]),
                   click_url(LOCATIONS[0][0]))
        save_state({})
        return
    current = {}
    report = {}
    ok_locations = 0

    scan = LOCATIONS if (urgent and "main" in urgent_opts()) else LOCATIONS + FAR_LOCATIONS
    for loc, loc_name in scan:
        free, names = {}, {}
        seen_dates, seen_slots = set(), set()
        stats = {}
        total = 0
        for target in targets:
            if target in seen_dates:
                continue
            ts = int(dt.datetime.combine(target, dt.time(8, 0), TZ).timestamp())
            try:
                data = fetch_day(page, page_url(loc, ts))
            except Exception as exc:  # noqa: BLE001
                print(f"WARNING: {loc_name}: could not read {target}: {exc}")
                continue
            n_total, n_free = ingest(data, free, names, seen_dates, seen_slots, stats, only_date=target)
            total += n_total
            print(f"{loc_name} {target}: {n_total} slots on page, {n_free} free")
            time.sleep(1)  # be gentle with the website

        overview = ", ".join(
            f"{WEEKDAY_NAMES[d.weekday()][:3]} {d:%d.%m}: {s[0]} slots, {s[1]} free, {s[2]} free 18-22h"
            for d, s in sorted(stats.items()) if d in set(targets)
        )
        not_found = [f"{d:%d.%m}" for d in targets if d not in seen_dates]
        print(f"OVERVIEW {loc_name}: {overview or 'nothing'}")
        if not_found:
            print(f"WARNING: {loc_name}: days not found on the page: {', '.join(not_found)}")

        if total == 0:
            print(f"WARNING: {loc_name}: no slots found, keeping previous state.")
            for k, v in old.items():
                if k[0] == loc:
                    current[k] = v
            report[loc] = [(d, 0, 0, False) for d in targets]
            continue

        ok_locations += 1
        windows = find_windows(free, names, set(targets))
        per_day = {}
        for (date_iso, hhmm), text in windows.items():
            if not bookable(date_iso, hhmm, now):
                continue  # free, but the club does not allow booking it yet
            if urgent:
                h, m = map(int, hhmm.split(":"))
                if dt.datetime.combine(dt.date.fromisoformat(date_iso), dt.time(h, m), TZ) <= now:
                    continue  # already started
            current[(loc, date_iso, hhmm)] = text
            per_day[date_iso] = per_day.get(date_iso, 0) + 1
        rows = []
        for d in targets:
            s = stats.get(d, [0, 0, 0, 0])
            # A day counts as "read" when evening slots (free or booked) were found, or when
            # the page simply shows a normal number of slots (some venues block their evenings
            # on certain days). A broken page only carries a handful of slots.
            # Today is exempt: its evening slots may already be in the past.
            ok = d in seen_dates and (s[3] > 0 or s[0] >= MIN_SLOTS_READ or d == today)
            # Not bookable yet (more than 14 days ahead) counts as "not checkable".
            if not bookable(d.isoformat(), START_TIMES[-1], now):
                ok = False
            rows.append((d, s[0], per_day.get(d.isoformat(), 0), ok))
        report[loc] = rows
        # Days we could not load keep their old state (no false "new" alerts).
        missing = {d.isoformat() for d in targets if d not in seen_dates}
        for k, v in old.items():
            if k[0] == loc and k[1] in missing:
                current[k] = v

    if ok_locations == 0:
        print("No location could be read. The page may have changed.")
        W.health_report("courts", False, "no location could be read (page empty or changed)", threshold=health_threshold())
        sys.exit(2)
    W.health_report("courts", True)

    new = {k: v for k, v in current.items() if k not in old}
    print("Matching windows now:", len(current), "| new:", len(new))

    forced = (os.environ.get("FORCE_REPORT", "").strip().lower() == "true"
              or os.environ.get("MODE", "").strip().lower() == "report")
    near_cur = {k: v for k, v in current.items() if not is_far(k[0])}
    far_cur = {k: v for k, v in current.items() if is_far(k[0])}
    near_new = {k: v for k, v in new.items() if not is_far(k[0])}
    show = near_cur if first_run else near_new
    # Farther locations are listed at the end but never start an alert on their own.
    if first_run or near_new or forced:
        sections = ["\n".join(header_lines(now))]
        if urgent:
            # Urgent mode: one summary block per location, new ranges marked with 🆕.
            sections.append("\n".join([heading("FREE RIGHT NOW"), ""]
                                      + describe_urgent(near_cur, () if first_run else near_new, report)))
            if far_cur:
                far = describe_far(far_cur, () if first_run else new, report)
                sections.append("\n".join([heading(ALT_HEADER), ""] + far))
            sections.append("\n".join(checked_lines_urgent(report)))
            sections.append(padelzone_block())
            sections.append('Send "stop" to end urgent mode.')
            title = make_title(show, first_run)
            notify(title, "\n\n".join(sections), click_url(sorted(show or near_cur)[0][0] if (show or near_cur) else LOCATIONS[0][0]))
            save_state(current)
            return
        main = [heading("FREE RIGHT NOW"), ""]
        if first_run:
            main += describe(near_cur) or ["No matching 2h window is free right now.",
                                           "You will get a message when one opens."]
        else:
            still = {k: v for k, v in near_cur.items() if k not in near_new}
            if near_new:
                main += ["🆕 New since last run", ""] + describe(near_new, "🆕 ")
            if still:
                if near_new:
                    main += ["", "♻️ Still free (reported before)", ""]
                main += describe(still)
            if not near_cur:
                main.append("No matching 2h window is free right now at the main locations.")
        sections.append("\n".join(main))
        if far_cur:
            sections.append("\n".join([heading(ALT_HEADER), ""] + describe_far(far_cur, () if first_run else new, report)))
        sections.append("\n".join(coverage_footer(report, now)))
        sections.append(padelzone_block())
        pick = show or near_cur
        first_loc = sorted(pick)[0][0] if pick else LOCATIONS[0][0]
        title = "Padeldome check (on request)" if forced and not first_run else make_title(show, first_run)
        notify(title, "\n\n".join(sections), click_url(first_loc))

    save_state(current)


def main():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        # We only need the page structure, not images or fonts.
        page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "font", "media")
            else route.continue_(),
        )
        try:
            run_all(page)
        except Exception as exc:  # noqa: BLE001
            W.health_report("courts", False, f"{type(exc).__name__}: {str(exc)[:80]}", threshold=health_threshold())
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    main()