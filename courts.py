"""Watches free padel courts at several Padeldome locations and sends an ntfy push.

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

# ---- Settings you may want to change -------------------------------------
BASE = "https://www.padeldome.wien/reservierung"
LOCATIONS = [  # (id from the link ?c=..., name shown in the notification)
    ("5175", "The Dome Stadlau"),
    ("2665", "Padeldome Erdberg"),
    ("2667", "Padeldome Süßenbrunn"),
    ("3216", "Padeldome Alte Donau"),
]
FAR_LOCATIONS = [  # alternative locations: shown at the end, never start an alert on their own
    ("2668", "Padeldome Alterlaa"),
    ("3660", "Racketworld"),
    ("4797", "Europahalle"),
    ("4858", "Playpadel Gänserndorf"),
]
# Locations on another website: (reservation page, name of the date parameter in the link)
OTHER_SITES = {
    "3660": ("https://www.racketworld.wien/reservierung", "t"),
    "4797": ("https://reservierung.europahalle.at/reservierung", "d"),
    "4858": ("https://playpadel.tennisplatz.info/reservierung", ("d", "t")),  # both, to be safe
}
DAYS_AHEAD = 15                 # today + 14 days (16.11 -> 30.11)
BOOK_AHEAD_DAYS = 14            # the club allows booking only 14 days before the start
WEEKDAYS = {0, 1, 2, 3}         # 0 = Monday ... 3 = Thursday
START_TIMES = ["18:00", "18:30", "19:00", "19:30", "20:00"]
DURATION_MIN = 120              # a court must be free for this long
MAX_WINDOWS = 25                # max windows listed in one notification
POSTCODES = {  # shown in brackets after the location name (Vienna postcode = district)
    "5175": "1220",   # The Dome Stadlau (22nd district)
    "2665": "1030",   # Padeldome Erdberg (3rd district)
    "2667": "1220",   # Padeldome Süßenbrunn, Weingartenallee 22
    "3216": "1210",   # Padeldome Alte Donau, Arbeiterstrandbadstraße 87A
    "2668": "1230",   # Padeldome Alterlaa, Anton-Baumgartner-Straße 40
    "3660": "1160",   # Racketworld (Ottakring, 16th district)
    "4797": "1230",   # Europahalle, An den Steinfeldern 2A
    "4858": "2230",   # Playpadel Gänserndorf (Lower Austria, outside Vienna)
}
URGENT_DAYS = 3                 # urgent mode: today and the next 2 days, any weekday
URGENT_FIRST, URGENT_LAST = "07:00", "21:30"   # urgent mode: every half-hour start in between
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
    return set(range(7)) if urgent_mode() else WEEKDAYS


def active_starts():
    """Start times to look for: your evening rules, or every half hour in urgent mode."""
    if not urgent_mode():
        return START_TIMES
    out, t = [], int(URGENT_FIRST[:2]) * 60 + int(URGENT_FIRST[3:])
    last = int(URGENT_LAST[:2]) * 60 + int(URGENT_LAST[3:])
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


BOLD_ON, BOLD_OFF = "\x02", "\x03"   # markers; ntfy shows plain text, Telegram shows bold
UL_ON, UL_OFF = "\x04", "\x05"       # underline (Telegram only)
ALT_HEADER = "➕ ALTERNATIVE LOCATIONS FREE (farther away)"
NOTHING = "nothing free for set booking conditions"


def bold(text):
    return f"{BOLD_ON}{text}{BOLD_OFF}"


def underline(text):
    return f"{UL_ON}{text}{UL_OFF}"


def plain(text):
    for m in (BOLD_ON, BOLD_OFF, UL_ON, UL_OFF):
        text = text.replace(m, "")
    return text


def telegram_html(text):
    out = html.escape(text, quote=False)
    for mark, tag in ((BOLD_ON, "<b>"), (BOLD_OFF, "</b>"), (UL_ON, "<u>"), (UL_OFF, "</u>")):
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
            lines.append(f"❌ {bold(short_label(loc))}: {NOTHING}")
        else:
            lines.append(f"✅ {bold(short_label(loc))}")
            lines.extend(day_lines)
    return lines


def checked_line_urgent(report):
    days = sorted({d for rows in report.values() for d, *_ in rows})
    bad = [f"{short_label(loc).split(' ', 1)[1]} {d:%d.%m}"
           for loc, rows in report.items() for d, slots, n, ok in rows if not ok and not n]
    span = f"{days[0]:%d.%m}-{days[-1]:%d.%m}" if len(days) > 1 else f"{days[0]:%d.%m}"
    if bad:
        return f"🔎 CHECKED: {span} · ⚠️ not read: " + ", ".join(bad)
    return f"🔎 CHECKED: {span} · all {len(report)} locations ✅"


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
            lines.append(f"{prefix}{loc_label(loc)} · {text}")
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
        lines.append(f"{'🆕 ' if all_new else ''}{name}")
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


def conditions_text():
    if urgent_mode():
        return f"Next {URGENT_DAYS} days · any day · {DURATION_MIN / 60:g}h block · any time"
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
    lines = [f"🔎 CHECKED {days[0]:%d.%m}-{days[-1]:%d.%m} · {day_txt}"]
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
        short = short_label(loc)
        mark = ""
        if bad:
            lines.append(f"⚠️ {mark}{short} · not read: " + ", ".join(bad))
        elif free:
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


def send_telegram(title, message, click):
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
            {"inline_keyboard": [[{"text": "Open booking page", "url": click}]]}
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


def notify(title, message, click, tags="tennis"):
    sent = False
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=plain(message).encode("utf-8"),
            headers={"Title": title, "Priority": "high", "Click": click, "Tags": tags},
        )
        urllib.request.urlopen(req, timeout=20).read()
        sent = True
    if send_telegram(title, message, click):
        sent = True
    if not sent:
        print("No notification channel is configured (NTFY_TOPIC or Telegram secrets).")
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


def run_all(page, today=None, now=None):
    now = now or dt.datetime.now(TZ)
    today = today or now.date()
    urgent = urgent_mode()
    if urgent:
        if not urgent_is_on():
            print("Urgent mode is switched off. Nothing to do.")
            return
    days = URGENT_DAYS if urgent else DAYS_AHEAD
    targets = [
        today + dt.timedelta(days=i)
        for i in range(days)
        if (today + dt.timedelta(days=i)).weekday() in active_weekdays()
    ]
    first_run, old = load_state()
    if urgent and not targets:
        print("Urgent mode: the next days are not Mon-Thu, nothing to check.")
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

    for loc, loc_name in LOCATIONS + FAR_LOCATIONS:
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
            # A day counts as "read" when evening slots (free or booked) were found.
            # Today is exempt: its evening slots may already be in the past.
            ok = d in seen_dates and (s[3] > 0 or d == today)
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
        sys.exit(2)

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
            sections.append("\n".join(describe_urgent(near_cur, () if first_run else near_new, report)))
            if far_cur:
                far = describe_far(far_cur, () if first_run else new, report)
                sections.append("\n".join([bold(underline(ALT_HEADER)), ""] + far))
            sections.append(checked_line_urgent(report) + '\nSend "stop" to end urgent mode.')
            title = make_title(show, first_run)
            notify(title, "\n\n".join(sections), click_url(sorted(show or near_cur)[0][0] if (show or near_cur) else LOCATIONS[0][0]))
            save_state(current)
            return
        if forced and not first_run:
            # On request (Telegram "padeldome check"): always answer with everything free now.
            if near_new:
                sections.append("\n".join(["🆕 NEW FREE WINDOWS SINCE LAST RUN", ""] + describe(near_new, "🆕 ")))
            still = {k: v for k, v in near_cur.items() if k not in near_new}
            if still:
                sections.append("\n".join(["🟢 FREE RIGHT NOW (already reported before)", ""] + describe(still)))
            if not near_cur:
                sections.append("No matching 2h window is free right now at the main locations.")
        elif first_run:
            body = describe(near_cur) or ["No matching 2h window is free right now.",
                                          "You will get a message when one opens."]
            sections.append("\n".join(["🟢 FREE RIGHT NOW (first run)", ""] + body))
        else:
            sections.append("\n".join(["🆕 NEW FREE WINDOWS SINCE LAST RUN", ""] + describe(near_new, "🆕 ")))
            still = {k: v for k, v in near_cur.items() if k not in near_new}
            if still:
                sections.append("\n".join(["♻️ STILL FREE (already reported before)", ""] + describe(still)))
        if far_cur:
            sections.append("\n".join([bold(underline(ALT_HEADER)), ""] + describe_far(far_cur, () if first_run else new, report)))
        sections.append("\n".join(coverage_footer(report, now)))
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
        finally:
            browser.close()


if __name__ == "__main__":
    main()
