"""Watches the Padel-Austria tournaments calendar (https://padel-austria.at/tournaments).

Keeps only events that match ALL of:
  - Bundesland: Wien
  - Level:      Starter
  - Wettbewerb: Offener Bewerb or Herren
Only events that are NOT full are reported (registered < capacity; the number in brackets is the waiting list).
Sends a Telegram message for new events with free spots and when a full event gets a free spot.
MODE=report (button "Padel Austria events") sends the whole list of matching events now.
"""
import datetime as dt
import os
import pathlib
import re
import sys
import time

import courts
from courts import TZ, WEEKDAY_NAMES, bold, heading, link, notify

URL = "https://padel-austria.at/tournaments"
MAX_PAGES = 12
STATE = pathlib.Path("padel_events_state.txt")
STATE_HEADER = "# v1"

STATES = {"Wien"}
LEVELS = {"Starter"}
KINDS = {"Offener Bewerb", "Herren"}
CONDITION_LINES = [
    "📍 Region: Wien",
    "🎚 Level: Starter",
    "🏁 Type: Offener Bewerb or Herren",
    "✅ Only events that are not full",
]

CAT = re.compile(r"^(Offener Bewerb|Herren|Damen|Mixed|Jugend)\s*-\s*"
                 r"(Starter|Advanced|Expert|Professional|Elite|Masters)$")
DATE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
TIME = re.compile(r"\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2}")
SLOTS = re.compile(r"^(\d+)\s*/\s*(\d+)(?:\s*\((\d+)\))?$")
UUID_HREF = re.compile(r"/tournaments/[0-9a-f]{8}-[0-9a-f-]{27,}$|/solo_tournaments/[0-9a-f-]{36}$")


def parse_cards(text, hrefs=()):
    """Page text -> list of event dicts (one per tournament card)."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    cards = []
    for k, l in enumerate(lines):
        m = CAT.match(l)
        if not m or k + 3 >= len(lines):
            continue
        sm = SLOTS.match(lines[k + 2])
        if not sm:
            continue
        d = k - 1
        while d >= 0 and not DATE.search(lines[d]):
            d -= 1
            if k - d > 8:
                break
        if d < 2 or not DATE.search(lines[d]):
            continue
        dd, mm, yy = map(int, DATE.search(lines[d]).groups())
        tm = next((TIME.search(x).group(0) for x in lines[d:k] if TIME.search(x)), "")
        cards.append({
            "title": lines[d - 2],
            "date": dt.date(yy, mm, dd).isoformat(),
            "time": re.sub(r"\s*[-–]\s*", "-", tm),
            "venue": lines[k - 1],
            "kind": m.group(1),
            "level": m.group(2),
            "state": lines[k + 1],
            "taken": int(sm.group(1)),
            "cap": int(sm.group(2)),
            "wait": int(sm.group(3) or 0),
            "status": lines[k + 3],
            "url": URL,
        })
    unique = list(dict.fromkeys(hrefs))
    if cards and len(unique) == len(cards):
        for c, h in zip(cards, unique):
            c["url"] = h
    return cards


def matches(c):
    return c["state"] in STATES and c["level"] in LEVELS and c["kind"] in KINDS


def has_free(c):
    return c["taken"] < c["cap"]


def key_of(c):
    return "|".join(x.replace("|", "/") for x in (c["date"], c["time"], c["title"], c["venue"]))


def fetch_all(page):
    cards, hrefs = [], []
    for n in range(1, MAX_PAGES + 1):
        page.goto(f"{URL}?page={n}", wait_until="networkidle")
        page.wait_for_timeout(1500)
        text = page.inner_text("body")
        links = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
        links = [h for h in links if UUID_HREF.search(h)]
        got = parse_cards(text, links)
        print(f"Page {n}: {len(got)} events")
        if not got:
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
    out = [STATE_HEADER] + [f"{key_of(c)}|{1 if has_free(c) else 0}" for c in cards]
    STATE.write_text("\n".join(out) + "\n", encoding="utf-8")


def event_block(c, mark="🏆"):
    free = c["cap"] - c["taken"]
    spots = f"{free} spot free" if free == 1 else f"{free} spots free"
    reg = "🔓 registration open" if "geschlossen" not in c["status"].lower() else "🔒 registration closed"
    return [f"{mark} ⏰ {c['time']} · {link(c['title'], c['url'])}",
            f"      📍 {c['venue']} · {c['kind']}",
            f"      👥 {c['taken']}/{c['cap']} · {spots} · {reg}"]


def day_groups(cards, mark):
    lines, current = [], None
    for c in sorted(cards, key=lambda x: (x["date"], x["time"])):
        if c["date"] != current:
            current = c["date"]
            d = dt.date.fromisoformat(current)
            if lines:
                lines.append("")
            lines.append(bold(f"📅 {WEEKDAY_NAMES[d.weekday()]} {d:%d-%m-%Y}"))
        lines += event_block(c, mark)
    return lines


def build(cards, old, forced, now):
    """cards: all events of Wien / Starter / Offener Bewerb or Herren (full or not)."""
    free = [c for c in cards if has_free(c)]
    first = old is None
    new = [c for c in free if not first and key_of(c) not in old]
    freed = [c for c in free if not first and key_of(c) in old and not old[key_of(c)]]
    if not (first or forced or new or freed):
        return None
    sections = ["\n".join([f"🕒 Execution time of the agent: {now:%d.%m.%Y %H:%M}"]),
                "\n".join([heading("📋 CONDITIONS"), ""] + CONDITION_LINES)]
    if new:
        sections.append("\n".join([heading("🆕 NEW EVENTS WITH FREE SPOTS"), ""] + day_groups(new, "🆕")))
    if freed:
        sections.append("\n".join([heading("🔓 SPOT FREED IN A FULL EVENT"), ""] + day_groups(freed, "🔓")))
    if first or forced:
        if free:
            sections.append("\n".join([heading("🏆 ALL EVENTS WITH FREE SPOTS"), ""] + day_groups(free, "🏆")))
        else:
            sections.append("\n".join([heading("🏆 ALL EVENTS WITH FREE SPOTS"), "", "No matching event has a free spot right now."]))
    sections.append(f"🔎 {len(cards)} matching events read, {len(free)} not full")
    if first:
        title = "Padel-Austria tournaments watcher started"
    elif forced and not (new or freed):
        title = "Padel-Austria tournaments (on request)"
    else:
        n = len(new) + len(freed)
        title = f"Padel-Austria: {n} event" + ("s" if n > 1 else "") + " with free spots"
    return title, "\n\n".join(sections)


def run(page=None, all_cards=None, now=None):
    now = now or dt.datetime.now(TZ)
    all_cards = all_cards if all_cards is not None else fetch_all(page)
    if not all_cards:
        print("Page looks empty or changed, the script may need a fix.")
        sys.exit(2)
    cards = [c for c in all_cards if matches(c)]
    print(f"{len(all_cards)} events read, {len(cards)} match the filter.")
    old = load_state()
    forced = os.environ.get("MODE", "").strip().lower() == "report"
    msg = build(cards, old, forced, now)
    if msg:
        notify(msg[0], msg[1], URL)
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