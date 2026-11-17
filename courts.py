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
FAR_LOCATIONS = [  # farther away: shown compactly at the end, never trigger an alert alone
    ("2668", "Padeldome Alterlaa"),
]
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
}
URGENT_DAYS = 3                 # urgent mode: today and the next 2 days, any weekday
URGENT_FIRST, URGENT_LAST = "07:00", "21:30"   # urgent mode: every half-hour start in between
SNAP = pathlib.Path("courts_state.txt")
URGENT_SNAP = pathlib.Path("urgent_state.txt")
URGENT_FLAG = pathlib.Path("urgent.txt")   # "on <time>" while urgent mode is switched on
# --------------------------------------------------------------------------

TZ = ZoneInfo("Europe/Vienna")
STATE_HEADER = "# v3"


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


def bold(text):
    return f"{BOLD_ON}{text}{BOLD_OFF}"


def plain(text):
    return text.replace(BOLD_ON, "").replace(BOLD_OFF, "")


def telegram_html(text):
    return html.escape(text, quote=False).replace(BOLD_ON, "<b>").replace(BOLD_OFF, "</b>")


def is_far(loc):
    return loc in {l for l, _ in FAR_LOCATIONS}


def loc_name(loc):
    """'Padeldome Süßenbrunn (1220)'"""
    name = dict(LOCATIONS).get(loc) or dict(FAR_LOCATIONS).get(loc, loc)
    plz = POSTCODES.get(loc)
    return f"{name} ({plz})" if plz else name


def loc_label(loc):
    """'3️⃣ Padeldome Süßenbrunn (1220)' - the number is the position in LOCATIONS."""
    names = dict(LOCATIONS)
    if loc in names:
        idx = [l for l, _ in LOCATIONS].index(loc)
        num = NUMBERS[idx] if idx < len(NUMBERS) else "•"
        return f"{num} {loc_name(loc)}"
    return f"➕ {loc_name(loc)}"


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


PARTS = [("🌅 Morning", 0, 12 * 60), ("☀️ Afternoon", 12 * 60, 17 * 60), ("🌙 Evening", 17 * 60, 24 * 60)]


def short_label(loc):
    """'3️⃣ Süßenbrunn (1220)' (without the word Padeldome)."""
    return loc_label(loc).replace("Padeldome ", "")


def describe_by_part(items, prefix=""):
    """Urgent mode: per day, morning / afternoon / evening, one line per location."""
    order = {loc: i for i, (loc, _) in enumerate(LOCATIONS)}
    grouped = {}
    for (loc, date, hhmm), text in items.items():
        if not is_far(loc):
            grouped.setdefault((date, loc), []).append((hhmm, text))
    days = {}
    for (date, loc), rows in grouped.items():
        for start, text in compress_day(rows):
            part = next(p for p, a, b in PARTS if a <= minutes(start) < b)
            days.setdefault(date, {}).setdefault(part, {}).setdefault(loc, []).append(
                text.replace(" · ", " ", 1))
    lines, count, extra = [], 0, 0
    for date in sorted(days):
        d = dt.date.fromisoformat(date)
        if lines:
            lines.append("")
        lines.append(bold(f"{WEEKDAY_NAMES[d.weekday()]} {d:%d-%m-%Y}"))
        for part, _, _ in PARTS:
            by_loc = days[date].get(part)
            if not by_loc:
                continue
            lines.append(part)
            for loc in sorted(by_loc, key=lambda l: order.get(l, 99)):
                if count >= MAX_WINDOWS:
                    extra += 1
                    continue
                lines.append(f"{prefix}{short_label(loc)}: " + " · ".join(by_loc[loc]))
                count += 1
    if extra:
        lines.append("")
        lines.append(f"... and {extra} more")
    return lines


def describe(items, prefix=""):
    """Main locations: {(loc, date_iso, hhmm): text} -> lines grouped by day, e.g.

    Tuesday 17-11-2026  (the date line is bold in Telegram)
    2️⃣ Padeldome Erdberg · Platz 2 · 18:00-20:00
    """
    if urgent_mode():
        return describe_by_part(items, prefix)
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


def describe_far(items, new=()):
    """Farther locations, very short: one line per location with the free time ranges per day."""
    by_loc = {}
    for (loc, date, hhmm) in items:
        if is_far(loc):
            by_loc.setdefault(loc, {}).setdefault(date, []).append(minutes(hhmm))
    lines = []
    for loc, _ in FAR_LOCATIONS:
        days = by_loc.get(loc)
        if not days:
            continue
        pieces = []
        for date in sorted(days):
            spans = []
            for st in sorted(days[date]):
                en = st + DURATION_MIN
                if spans and st <= spans[-1][1]:
                    spans[-1][1] = max(spans[-1][1], en)
                else:
                    spans.append([st, en])
            d = dt.date.fromisoformat(date)
            rng = ", ".join(f"{a // 60:02d}:{a % 60:02d}-{b // 60:02d}:{b % 60:02d}" for a, b in spans)
            is_new = any(k[0] == loc and k[1] == date for k in new)
            pieces.append(f"{'🆕 ' if is_new else ''}{WEEKDAY_NAMES[d.weekday()][:3]} {d:%d.%m} {rng}")
        lines.append(f"{loc_name(loc)}: " + " · ".join(pieces))
    return lines


def conditions_text():
    if urgent_mode():
        return f"any day · {DURATION_MIN / 60:g}h window · any start time"
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
    """report: {loc: [(date, slots, windows, ok), ...]} -> lines for the end of a push."""
    lines = ["🔎 DAYS CHECKED"]
    for loc, _ in LOCATIONS:
        rows = report.get(loc)
        if rows is None:
            continue
        free = [f"{d:%d.%m} ({n} {'window' if n == 1 else 'windows'})"
                for d, slots, n, ok in rows if n]
        none = [f"{d:%d.%m}" for d, slots, n, ok in rows if ok and not n]
        bad = [f"{d:%d.%m}" for d, slots, n, ok in rows if not ok and not n]
        lines.append("")
        lines.append(loc_label(loc))
        if free:
            lines.append("✅ free: " + ", ".join(free))
        if none:
            lines.append("– none fit: " + ", ".join(none))
        if bad:
            lines.append("⚠️ not read / not bookable yet: " + ", ".join(bad))
    for loc, name in FAR_LOCATIONS:
        rows = report.get(loc)
        if rows is None:
            continue
        free = [f"{d:%d.%m}" for d, slots, n, ok in rows if n]
        bad = [f"{d:%d.%m}" for d, slots, n, ok in rows if not ok and not n]
        bits = ["✅ " + ", ".join(free)] if free else ["none fit"]
        if bad:
            bits.append("⚠️ " + ", ".join(bad))
        if lines and lines[-1] != "" and not any(l.startswith("➕") for l in lines):
            lines.append("")
        lines.append(f"➕ {loc_name(loc)}: " + " · ".join(bits))
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
    try:
        urllib.request.urlopen(req, timeout=20).read()
        return True
    except Exception as exc:  # noqa: BLE001  (never print the URL: it contains the token)
        print(f"WARNING: Telegram message failed ({type(exc).__name__}).")
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
                   f"{BASE}?c={LOCATIONS[0][0]}")
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
                data = fetch_day(page, f"{BASE}?c={loc}&d={ts}")
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
        if urgent and not first_run:
            sections.append("🚨 URGENT MODE: checking the next 3 days (any time) until you send: stop")
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
            sections.append("\n".join(["➕ FARTHER LOCATIONS (just in case)", ""] + describe_far(far_cur, () if first_run else new)))
        sections.append("\n".join(coverage_footer(report, now)))
        pick = show or near_cur
        first_loc = sorted(pick)[0][0] if pick else LOCATIONS[0][0]
        title = "Padeldome check (on request)" if forced and not first_run else make_title(show, first_run)
        notify(title, "\n\n".join(sections), f"{BASE}?c={first_loc}")

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
