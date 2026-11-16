"""Watches free padel courts at several Padeldome locations and sends an ntfy push.

A 2-hour window is reported when it starts at one of START_TIMES, on
Monday-Thursday within the next 14 days, and is free at one location either
on a single court or on several courts directly after each other
(e.g. Platz 1 18:00-19:00 + Platz 2 19:00-20:00).
You get a message only when such a window is NEWLY free.
"""
import datetime as dt
import os
import pathlib
import re
import sys
import time
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
DAYS_AHEAD = 14                 # look this many days ahead (including today)
WEEKDAYS = {0, 1, 2, 3}         # 0 = Monday ... 3 = Thursday
START_TIMES = ["18:00", "18:30", "19:00", "19:30", "20:00"]
DURATION_MIN = 120              # a court must be free for this long
MAX_WINDOWS = 25                # max windows listed in one notification
SNAP = pathlib.Path("courts_state.txt")
# --------------------------------------------------------------------------

TZ = ZoneInfo("Europe/Vienna")
STATE_HEADER = "# v3"

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
      const free = s.classList.contains("av") && !s.classList.contains("res");
      slots.push([court.dataset.cid, Number(s.dataset.begin), r.height, free]);
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


def chain(courts_, order, s, e, tol=120):
    """Cover [s, e] with free intervals of several courts, fewest changes.

    courts_: {cid: merged free intervals}. Returns [(cid, from_ts, to_ts)] or None.
    """
    t = s
    segs = []
    while t < e - tol:
        best = None
        for cid, merged in courts_.items():
            for a, b in merged:
                if a <= t + tol and b > t + tol:
                    cand = (b, -order.get(cid, 999), cid)
                    if best is None or cand > best:
                        best = cand
        if best is None or len(segs) >= 8:
            return None
        b, _, cid = best
        segs.append((cid, t, min(b, e)))
        t = b
    return segs


def find_windows(free, names, targets):
    """free: {(cid, date): [(start_ts, end_ts), ...]}.

    Returns {(date_iso, 'HH:MM'): 'Platz 3, 19:30-21:30'} for one location.
    """
    by_date = {}
    for (cid, date), intervals in free.items():
        if date in targets:
            by_date.setdefault(date, {})[cid] = merge(intervals)
    order = {cid: i for i, cid in enumerate(names)}

    def nm(cid):
        return names.get(cid, f"Court {cid}")

    out = {}
    for date, courts_ in by_date.items():
        for hhmm in START_TIMES:
            h, m = map(int, hhmm.split(":"))
            s = int(dt.datetime.combine(date, dt.time(h, m), TZ).timestamp())
            e = s + DURATION_MIN * 60
            end_txt = hhmm_of(e)
            singles = sorted(
                (c for c, mv in courts_.items() if covers(mv, s, e)),
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


def ingest(data, free, names, seen_dates, seen_slots):
    """Add one page's slots to the collected data. Returns (total, free) counts."""
    ppm = data["pxPerMin"] or (25 / 30)
    n_total = n_free = 0
    for cid, name in data["names"].items():
        names.setdefault(cid, name)
    for cid, begin, height, is_free in data["slots"]:
        if begin <= 0 or (cid, begin) in seen_slots:
            continue
        seen_slots.add((cid, begin))
        date = dt.datetime.fromtimestamp(begin, TZ).date()
        seen_dates.add(date)
        n_total += 1
        if is_free:
            n_free += 1
            free.setdefault((cid, date), []).append((begin, begin + height / ppm * 60))
    return n_total, n_free


def describe(items):
    """items: {(loc, date_iso, hhmm): text} -> lines grouped by day, e.g.

    Tuesday 17-11-2026
    • Padeldome Erdberg · Platz 2 · 18:00-20:00
    """
    loc_names = dict(LOCATIONS)
    order = {loc: i for i, (loc, _) in enumerate(LOCATIONS)}
    keys = sorted(items, key=lambda k: (k[1], order.get(k[0], 99), k[2]))
    shown, extra = keys[:MAX_WINDOWS], max(0, len(keys) - MAX_WINDOWS)

    lines, current_day = [], None
    for loc, date, hhmm in shown:
        if date != current_day:
            current_day = date
            d = dt.date.fromisoformat(date)
            if lines:
                lines.append("")
            lines.append(f"{WEEKDAY_NAMES[d.weekday()]} {d:%d-%m-%Y}")
        lines.append(f"• {loc_names.get(loc, loc)} · {items[(loc, date, hhmm)]}")
    if extra:
        lines.append("")
        lines.append(f"... and {extra} more")
    return lines


def make_title(show, first_run):
    if first_run:
        return "Court watcher started"
    return "Free padel court" if len(show) == 1 else f"{len(show)} free padel windows"


def notify(title, message, click, tags="tennis"):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("NTFY_TOPIC secret is missing.")
        sys.exit(3)
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers={"Title": title, "Priority": "high", "Click": click, "Tags": tags},
    )
    urllib.request.urlopen(req, timeout=20).read()


def fetch_day(page, url):
    page.goto(url, wait_until="networkidle")
    page.wait_for_selector("div.court[data-cid]", timeout=20000)
    page.wait_for_timeout(1000)
    return page.evaluate(JS)


def load_state():
    """Returns (first_run, {(loc, date, hhmm): text})."""
    if not SNAP.exists():
        return True, {}
    lines = SNAP.read_text(encoding="utf-8").splitlines()
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
    SNAP.write_text("\n".join(out) + "\n", encoding="utf-8")


def run_all(page, today=None):
    today = today or dt.datetime.now(TZ).date()
    targets = [
        today + dt.timedelta(days=i)
        for i in range(DAYS_AHEAD)
        if (today + dt.timedelta(days=i)).weekday() in WEEKDAYS
    ]
    first_run, old = load_state()
    current = {}
    ok_locations = 0

    for loc, loc_name in LOCATIONS:
        free, names = {}, {}
        seen_dates, seen_slots = set(), set()
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
            n_total, n_free = ingest(data, free, names, seen_dates, seen_slots)
            total += n_total
            print(f"{loc_name} {target}: {n_total} slots on page, {n_free} free")
            time.sleep(1)  # be gentle with the website

        if total == 0:
            print(f"WARNING: {loc_name}: no slots found, keeping previous state.")
            for k, v in old.items():
                if k[0] == loc:
                    current[k] = v
            continue

        ok_locations += 1
        for (date_iso, hhmm), text in find_windows(free, names, set(targets)).items():
            current[(loc, date_iso, hhmm)] = text
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

    show = current if first_run else new
    if first_run or new:
        lines = describe(show)
        if first_run:
            if lines:
                msg = "Free right now:\n" + "\n".join(lines)
            else:
                msg = ("No matching 2h window is free right now. "
                       "You will get a message when one opens.")
        else:
            msg = "\n".join(lines)
        first_loc = sorted(show)[0][0] if show else LOCATIONS[0][0]
        notify(make_title(show, first_run), msg, f"{BASE}?c={first_loc}")

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
