"""Watches the Padel-Austria tournaments calendar (https://padel-austria.at/tournaments).

Keeps only events that match ALL of:
  - Bundesland: Wien
  - Level:      Starter
  - Wettbewerb: Offener Bewerb (Herren is excluded)
Only tournaments with OPEN registration that are NOT full are reported (registered < capacity;
the number in brackets is the waiting list). Mon-Fri only from 18:00; weekends and public holidays any time.
Sends a Telegram message for new such tournaments and when a tournament becomes open / gets a free spot.
MODE=report (button "Padel Austria events") sends the whole list of matching events now.
"""
import datetime as dt
import os
import pathlib
import re
import sys
import time

import courts
from courts import BQ_OFF, BQ_ON, IT_OFF, IT_ON, TZ, WEEKDAY_NAMES, bold, heading, link, notify

URL = "https://padel-austria.at/tournaments"
MAX_PAGES = 12
STATE = pathlib.Path("padel_events_state.txt")
STATE_HEADER = "# v2"

STATES = {"Wien"}
LEVELS = {"Starter"}
KINDS = {"Offener Bewerb"}
CONDITION_LINES = [
    "📍 Region: Wien  ·  🎚 Level: Starter  ·  🏁 Type: Offener Bewerb",
    "🔓 Only open registrations that are not full",
    "🕕 Mon-Fri: start from 18:00  ·  🌴 weekends and public holidays: any time",
]

CAT = re.compile(r"^(Offener Bewerb|Herren|Damen|Mixed|Jugend)\s*-\s*([A-Za-zÄÖÜäöüß]+)$")
DATE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
TIME = re.compile(r"\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2}")
SLOTS = re.compile(r"^(\d+)\s*/\s*(\d+)(?:\s*\((\d+)\))?$")
UUID_HREF = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
EVENING_FROM = 18 * 60       # Mon-Fri: only tournaments starting at 18:00 or later


ALL_STATES = {"Wien", "Niederösterreich", "Oberösterreich", "Steiermark", "Tirol", "Kärnten",
              "Salzburg", "Vorarlberg", "Burgenland"}
MONTH = re.compile(r"^(Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)\s+\d{4}$")


def split_lines(text):
    # Cells may be separated by newlines, tabs or several spaces: treat all of them as line breaks.
    return [x.strip() for l in text.splitlines() for x in re.split(r"\t+| {2,}", l) if x.strip()]


def card_status(block):
    """The registration button text: the line right after the spots (e.g. 'Anmeldung geschlossen')."""
    for j, l in enumerate(block):
        if SLOTS.match(l):
            for cand in block[j + 1:j + 3]:
                if (len(cand) <= 40 and not MONTH.match(cand) and not CAT.match(cand)
                        and not DATE.search(cand) and not TIME.search(cand)
                        and not re.match(r"^(‹|›|«|»|Zurück|Weiter|\d+$)", cand)):
                    return cand
            break
    return next((l for l in block if "anmeld" in l.lower()), "")


def parse_cards(text, anchors=()):
    """Page text -> list of event dicts. Every card contains one date line; the order of the
    other cells (venue, state, level, spots, status) does not matter."""
    lines = split_lines(text)
    dates = [i for i, l in enumerate(lines) if DATE.search(l) and not CAT.match(l)]
    starts = []
    for d in dates:
        prev = lines[d - 1] if d >= 1 else ""
        if d >= 2 and any(prev.endswith(f" - {st}") for st in ALL_STATES):
            starts.append((d - 2, d, prev))    # title, "Venue - State", date
        else:
            starts.append((d - 1, d, ""))      # title, date
    cards = []
    for n, (t, d, locline) in enumerate(starts):
        if t < 0:
            continue
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        block = [l for l in lines[d:end] if not MONTH.match(l)]
        cat = next((CAT.match(l) for l in block if CAT.match(l)), None)
        sm = next((SLOTS.match(l) for l in block if SLOTS.match(l)), None)
        venue, state = "", None
        for st in ALL_STATES:                   # "PADELDOME Süßenbrunn - Wien"
            if locline.endswith(f" - {st}"):
                venue, state = locline[: -len(f" - {st}")].strip(), st
        if state is None:                       # state as a cell of its own
            state = next((l for l in block if l in ALL_STATES), None)
        status = card_status(block)
        if not (cat and sm and state):
            continue
        dd, mm, yy = map(int, DATE.search(lines[d]).groups())
        tm = next((TIME.search(x).group(0) for x in block[:3] if TIME.search(x)), "")
        skip = {cat.group(0), state, status}
        if not venue:
            venue = next((l for l in block[1:] if l not in skip and not SLOTS.match(l)
                          and not TIME.search(l) and not DATE.search(l)), "")
        cards.append({
            "title": lines[t],
            "date": dt.date(yy, mm, dd).isoformat(),
            "time": re.sub(r"\s*[-–]\s*", "-", tm),
            "venue": venue,
            "kind": cat.group(1),
            "level": cat.group(2),
            "state": state,
            "taken": int(sm.group(1)),
            "cap": int(sm.group(2)),
            "wait": int(sm.group(3) or 0),
            "status": status,
            "url": URL,
        })
    attach_links(cards, anchors)
    return cards


def attach_links(cards, anchors):
    """anchors: [(href, text)] of the page. Give every card the link of ITS tournament."""
    anchors = [(h, t) for h, t in anchors if UUID_HREF.search(h)]
    unique = list(dict.fromkeys(h for h, _ in anchors))
    if cards and len(unique) == len(cards):          # one link per card, same order
        for c, h in zip(cards, unique):
            c["url"] = h
        return
    # otherwise match by title; equal titles are matched in page order
    by_title = {}
    for h, t in anchors:
        for line in split_lines(t):
            seen = by_title.setdefault(line, [])
            if h not in seen:
                seen.append(h)
    used = {}
    for c in cards:
        hs = by_title.get(c["title"], [])
        i = used.get(c["title"], 0)
        if i < len(hs):
            c["url"] = hs[i]
            used[c["title"]] = i + 1


def matches(c):
    return c["state"] in STATES and c["level"] in LEVELS and c["kind"] in KINDS


def has_free(c):
    return c["taken"] < c["cap"]


def registration_open(c):
    """Open unless the button says it is closed (wording of open registrations varies)."""
    return not any(w in c["status"].lower() for w in ("geschlossen", "beendet", "abgelaufen"))


def easter(year):
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return dt.date(year, month, day)


def holiday_name(d):
    """Austrian public holiday (Feiertag) of this date, or None."""
    fixed = {(1, 1): "Neujahr", (1, 6): "Heilige Drei Könige", (5, 1): "Staatsfeiertag",
             (8, 15): "Mariä Himmelfahrt", (10, 26): "Nationalfeiertag", (11, 1): "Allerheiligen",
             (12, 8): "Mariä Empfängnis", (12, 25): "Christtag", (12, 26): "Stefanitag"}
    if (d.month, d.day) in fixed:
        return fixed[(d.month, d.day)]
    e = easter(d.year)
    moving = {1: "Ostermontag", 39: "Christi Himmelfahrt", 50: "Pfingstmontag", 60: "Fronleichnam"}
    delta = (d - e).days
    return moving.get(delta)


def start_minutes(c):
    m = re.match(r"(\d{1,2}):(\d{2})", c["time"])
    return int(m.group(1)) * 60 + int(m.group(2)) if m else 0


def day_ok(c):
    """Mon-Fri only from 18:00; weekends and public holidays any time."""
    d = dt.date.fromisoformat(c["date"])
    if d.weekday() >= 5 or holiday_name(d):
        return True
    return start_minutes(c) >= EVENING_FROM


def wanted(c):
    """Open registration, not full, and at a time that suits you."""
    return registration_open(c) and has_free(c) and day_ok(c)


def key_of(c):
    return "|".join(x.replace("|", "/") for x in (c["date"], c["time"], c["title"], c["venue"]))


def debug_dump(page, text):
    """Page 1 had no events: show what the browser really got (public page, no secrets)."""
    lines = split_lines(text)
    date_idx = [i for i, l in enumerate(lines) if DATE.search(l)]
    print("---- DEBUG v2: title:", page.title(), "| text length:", len(text),
          "| lines:", len(lines), "| lines with a dd.mm.yyyy date:", len(date_idx))
    if date_idx:
        lo = max(0, date_idx[0] - 4)
        print(f"-- lines {lo}..{lo + 45} (around the first date)")
        for i in range(lo, min(len(lines), lo + 45)):
            print(f"{i:03d}: {lines[i][:140]}")
    else:
        print("-- no date found. Last 50 lines:")
        for i in range(max(0, len(lines) - 50), len(lines)):
            print(f"{i:03d}: {lines[i][:140]}")
        print("-- cards present in the HTML? 'Anmeldung' count:",
              page.content().count("Anmeldung"), "| html length:", len(page.content()))
    print("---- END DEBUG")


def fetch_all(page):
    cards, hrefs = [], []
    for n in range(1, MAX_PAGES + 1):
        page.goto(f"{URL}?page={n}", wait_until="networkidle")
        page.wait_for_timeout(1500)
        text = page.inner_text("body")
        links = page.eval_on_selector_all("a[href]", "els => els.map(e => [e.href, e.innerText])")
        got = parse_cards(text, links)
        if not got and n == 1:
            page.wait_for_timeout(8000)          # the list may load late: look once more
            text = page.inner_text("body")
            links = page.eval_on_selector_all("a[href]", "els => els.map(e => [e.href, e.innerText])")
            got = parse_cards(text, links)
        print(f"Page {n}: {len(got)} events")
        if not got:
            if n == 1:
                debug_dump(page, text)
            break
        cards += got
        time.sleep(1)
    return cards


def load_state():
    if not STATE.exists():
        return None
    lines = STATE.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != STATE_HEADER:
        return None
    out = {}
    for l in lines[1:]:
        k, sep, flag = l.rpartition("|")
        if sep and k:
            out[k] = flag == "1"
    return out


def save_state(cards):
    out = [STATE_HEADER] + [f"{key_of(c)}|{1 if wanted(c) else 0}" for c in cards]
    STATE.write_text("\n".join(out) + "\n", encoding="utf-8")


def spots_icon(free):
    return "🟢" if free >= 5 else ("🟡" if free >= 2 else "🔴")


def event_block(c, mark=""):
    """One tournament as a quote block (4 lines). Title and location both open this tournament."""
    free = c["cap"] - c["taken"]
    spots = f"{free} spot free" if free == 1 else f"{free} spots free"
    wait = f"⏳ waiting list: {c['wait']}" if c["wait"] else "⏳ no waiting list"
    return "\n".join([
        BQ_ON + f"{spots_icon(free)} {mark}{bold('⏰ ' + c['time'])}",
        f"🏆 {IT_ON}{link(c['title'], c['url'])}{IT_OFF}",
        f"📍 {bold(link(c['venue'], c['url']))} · {c['kind']}",
        f"👥 {bold(str(c['taken']) + '/' + str(c['cap']))} · {spots} · {wait}" + BQ_OFF,
    ])


def day_groups(cards, mark=""):
    """List of strings, one per day: bold date line + one quote block per tournament."""
    out, current, buf = [], None, []

    def flush():
        if buf:
            out.append("\n".join(buf))

    for c in sorted(cards, key=lambda x: (x["date"], x["time"])):
        if c["date"] != current:
            flush()
            current = c["date"]
            d = dt.date.fromisoformat(current)
            tag = ""
            hol = holiday_name(d)
            if hol:
                tag = f"  ·  🎉 {hol}"
            elif d.weekday() >= 5:
                tag = "  ·  🌴 weekend"
            buf = [bold(f"📅 {WEEKDAY_NAMES[d.weekday()]} {d:%d-%m-%Y}") + tag]
        buf.append(event_block(c, mark))
    flush()
    return out


MAX_MESSAGE = 3300      # Telegram allows 4096 characters; long lists are sent in several messages


def pack(units):
    """Join units with blank lines into messages that are not too long."""
    messages, cur = [], ""
    for u in units:
        if cur and len(cur) + 2 + len(u) > MAX_MESSAGE:
            messages.append(cur)
            cur = u
        else:
            cur = f"{cur}\n\n{u}" if cur else u
    if cur:
        messages.append(cur)
    return messages


def build(cards, old, forced, now):
    """cards: all Wien / Starter / Offener Bewerb tournaments. Returns (title, [messages]) or None."""
    good = [c for c in cards if wanted(c)]
    first = old is None
    new = [c for c in good if not first and key_of(c) not in old]
    opened = [c for c in good if not first and key_of(c) in old and not old[key_of(c)]]
    if not (first or forced or new or opened):
        return None
    units = [f"🕒 Execution time of the agent: {now:%d.%m.%Y %H:%M}",
             "\n".join([heading("📋 CONDITIONS"), ""] + CONDITION_LINES)]
    if new:
        units += [heading("🆕 NEW TOURNAMENTS (OPEN)")] + day_groups(new, "🆕 ")
    if opened:
        units += [heading("🔓 NOW OPEN / SPOT FREE")] + day_groups(opened, "🔓 ")
    if first or forced:
        units.append(heading(f"🏆 ALL OPEN TOURNAMENTS ({len(good)})"))
        units += day_groups(good) if good else ["No tournament matches right now."]
    units.append(f"🔎 {len(cards)} Wien / Starter / Offener Bewerb tournaments read, {len(good)} match your rules")
    if first:
        title = "Padel-Austria tournaments watcher started"
    elif forced and not (new or opened):
        title = "Padel-Austria tournaments (on request)"
    else:
        n = len(new) + len(opened)
        title = f"Padel-Austria: {n} open tournament" + ("s" if n > 1 else "")
    return title, pack(units)


def run(page=None, all_cards=None, now=None):
    now = now or dt.datetime.now(TZ)
    all_cards = all_cards if all_cards is not None else fetch_all(page)
    if not all_cards:
        print("Page looks empty or changed, the script may need a fix.")
        sys.exit(2)
    cards = [c for c in all_cards if matches(c)]
    print(f"{len(all_cards)} events read, {len(cards)} match region/level/type.")
    from collections import Counter
    print("Registration texts seen:", dict(Counter(c["status"] or "(none)" for c in cards)))
    print(f"{sum(1 for c in cards if wanted(c))} pass all rules (open, not full, time).")
    old = load_state()
    forced = os.environ.get("MODE", "").strip().lower() == "report"
    msg = build(cards, old, forced, now)
    if msg:
        title, parts = msg
        for i, part in enumerate(parts):
            notify(title if i == 0 else f"{title} ({i + 1}/{len(parts)})", part, URL)
            time.sleep(1)
    save_state(cards)


def main():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        run(page)
        browser.close()


if __name__ == "__main__":
    main()