"""Watches free padel courts at several Padeldome locations and sends an ntfy push.

A court is reported when it is free for a full 2-hour window that starts
at one of START_TIMES, on Monday-Thursday within the next 14 days.
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
START_TIMES = ["18:00", "18:30", "19:00", "19:30"]
DURATION_MIN = 120              # a court must be free for this long
MAX_LINES = 40                  # max lines in one notification
SNAP = pathlib.Path("courts_state.txt")
# --------------------------------------------------------------------------

TZ = ZoneInfo("Europe/Vienna")
STATE_HEADER = "# v2"

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


def find_matches(free, targets):
    """free: {(cid, date): [(start_ts, end_ts), ...]} -> set of (date_iso, cid, 'HH:MM')."""
    matches = set()
    for (cid, date), intervals in free.items():
        if date not in targets:
            continue
        merged = merge(intervals)
        for hhmm in START_TIMES:
            h, m = map(int, hhmm.split(":"))
            s = int(dt.datetime.combine(date, dt.time(h, m), TZ).timestamp())
            if covers(merged, s, s + DURATION_MIN * 60):
                matches.add((date.isoformat(), cid, hhmm))
    return matches


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
    """items: {(loc, date_iso, cid, hhmm): court_name} -> list of text lines."""
    loc_names = dict(LOCATIONS)
    order = {loc: i for i, (loc, _) in enumerate(LOCATIONS)}

    def sortkey(k):
        loc, date, cid, hhmm = k
        num = re.search(r"\d+", items[k])
        return (date, order.get(loc, 99), int(num.group()) if num else 0, items[k], hhmm)

    lines = []
    for k in sorted(items, key=sortkey):
        loc, date, cid, hhmm = k
        d = dt.date.fromisoformat(date)
        h, m = map(int, hhmm.split(":"))
        end = (dt.datetime.combine(d, dt.time(h, m))
               + dt.timedelta(minutes=DURATION_MIN)).strftime("%H:%M")
        lines.append(f"{d:%d-%m-%Y}, {loc_names.get(loc, loc)}, {items[k]}, {hhmm}-{end}")
    return lines


def notify(title, message, click):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("NTFY_TOPIC secret is missing.")
        sys.exit(3)
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers={"Title": title, "Priority": "high", "Click": click},
    )
    urllib.request.urlopen(req, timeout=20).read()


def fetch_day(page, url):
    page.goto(url, wait_until="networkidle")
    page.wait_for_selector("div.court[data-cid]", timeout=20000)
    page.wait_for_timeout(1000)
    return page.evaluate(JS)


def load_state():
    """Returns (first_run, {(loc, date, cid, hhmm): name})."""
    if not SNAP.exists():
        return True, {}
    lines = SNAP.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != STATE_HEADER:
        return True, {}  # old format from an earlier version: start fresh
    old = {}
    for l in lines[1:]:
        parts = l.strip().split("|")
        if len(parts) >= 5:
            old[tuple(parts[:4])] = "|".join(parts[4:])
    return False, old


def save_state(current):
    out = [STATE_HEADER]
    for (loc, date, cid, hhmm), name in sorted(current.items()):
        out.append(f"{loc}|{date}|{cid}|{hhmm}|{name}")
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
        for date_iso, cid, hhmm in find_matches(free, set(targets)):
            current[(loc, date_iso, cid, hhmm)] = names.get(cid, f"Court {cid}")
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

    title = "Padel court free"
    show = current if first_run else new
    if first_run or new:
        lines = describe(show)
        extra = len(lines) - MAX_LINES
        lines = lines[:MAX_LINES] + ([f"... and {extra} more"] if extra > 0 else [])
        if first_run:
            if lines:
                msg = "Watcher started. Free right now:\n" + "\n".join(lines)
            else:
                msg = ("Watcher started. No matching 2h window is free right now. "
                       "You will get a message when one opens.")
        else:
            msg = "New free 2h window:\n" + "\n".join(lines)
        first_loc = sorted(show)[0][0] if show else LOCATIONS[0][0]
        notify(title, msg, f"{BASE}?c={first_loc}")

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