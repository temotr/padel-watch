import datetime as dt
import os
import pathlib
import re
import sys
from zoneinfo import ZoneInfo

import watchlib as W

URL = W.get("sunset", "url", "https://sunsetpadel.at/events")   # settings: config.toml
SNAP = pathlib.Path("events.txt")
TZ = ZoneInfo("Europe/Vienna")


def set_output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        if "\n" in value:
            f.write(f"{name}<<EOF_{name.upper()}\n{value}\nEOF_{name.upper()}\n")
        else:
            f.write(f"{name}={value}\n")


def parse_events(text):
    """Returns {title: info} for the 'Upcoming Events' section, or None if the page looks wrong."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) < 5 or "Upcoming Events" not in lines:
        return None
    start = lines.index("Upcoming Events")
    end = lines.index("Past Events", start) if "Past Events" in lines[start:] else len(lines)
    section = lines[start + 1:end]

    # Each event: title, date line, location, description, "N / M participants", price.
    date_idx = [
        i for i in range(1, len(section))
        if "(Austria)" in section[i] and " at " in section[i]
    ]
    events = {}
    for n, i in enumerate(date_idx):
        stop = date_idx[n + 1] - 1 if n + 1 < len(date_idx) else len(section)
        info = {
            "when": section[i].split(" (Austria)")[0].strip(),
            "where": "",
            "count": None,
            "cap": None,
            "price": "",
        }
        if i + 1 < stop and len(section[i + 1]) <= 60:
            info["where"] = section[i + 1]
        for j in range(i + 1, stop):
            m = re.search(r"(\d+)\s*/\s*(\d+)\s*participants", section[j])
            if m:
                info["count"], info["cap"] = int(m.group(1)), int(m.group(2))
                if j + 1 < stop and re.match(r"^[\d.,]+\s*€$", section[j + 1]):
                    info["price"] = section[j + 1]
                break
        events[section[i - 1]] = info
    return events


def load_old():
    """Previous snapshot: lines look like 'Title | 20 / 45'. None on the first run."""
    if not SNAP.exists():
        return None
    old = {}
    for l in SNAP.read_text(encoding="utf-8").splitlines():
        if not l.strip():
            continue
        t, _, rest = l.partition(" | ")
        m = re.match(r"(\d+) / (\d+)", rest)
        old[t] = (int(m.group(1)), int(m.group(2))) if m else None
    return old


MAX_EVENT_LINES = 40
CONDITIONS = "new event · fewer participants than before"


def event_line(title, e):
    c, cap = e["count"], e["cap"]
    return f"{title} ({c} / {cap} participants)" if c is not None else title


def build_changes(events, old, now=None):
    """Returns (title, message) or None when nothing changed.

    Layout: checking time and conditions, then the changes (new events,
    fewer participants), then all other current events, then a data check.
    """
    now = now or dt.datetime.now(TZ)
    new_lines, down_lines, changed = [], [], set()
    for t, e in events.items():
        c, cap = e["count"], e["cap"]
        if t not in old:
            new_lines.append("🆕 " + event_line(t, e))
            changed.add(t)
        elif old[t] is not None and c is not None:
            oc, ocap = old[t]
            if c < oc:
                down_lines.append(f"🔻 {t}: {oc} -> {c} / {cap}")
                changed.add(t)
    if not new_lines and not down_lines:
        return None

    others = [event_line(t, e) for t, e in events.items() if t not in changed]
    sections = ["\n".join([
        f"🕒 Execution time of the agent: {now:%d.%m.%Y %H:%M}",
        f"📋 Conditions for notification: {CONDITIONS}",
    ])]
    if new_lines:
        sections.append("\n".join(["🆕 NEW EVENTS SINCE LAST RUN", ""] + new_lines))
    if down_lines:
        sections.append("\n".join(["🔻 PARTICIPANTS DOWN SINCE LAST RUN", ""] + down_lines))
    if others:
        sections.append("\n".join(["♻️ OTHER CURRENT EVENTS (no change)", ""] + others))
    sections.append("\n".join([
        "🔎 EVENTS CHECKED",
        "",
        f"{len(events)} upcoming events read from sunsetpadel.at/events (last run: {len(old)})",
    ]))

    parts = []
    if new_lines:
        parts.append(f"{len(new_lines)} new event" + ("s" if len(new_lines) > 1 else ""))
    if down_lines:
        parts.append(f"{len(down_lines)} spot update" + ("s" if len(down_lines) > 1 else ""))
    title = "Sun Set Padel: " + ", ".join(parts)

    message = "\n\n".join(sections)
    lines = message.split("\n")
    if len(lines) > MAX_EVENT_LINES + 12:
        message = "\n".join(lines[:MAX_EVENT_LINES + 12]) + "\n..."
    return title, message


def read_page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            # Skip images, fonts and videos: we only need the text, and this
            # keeps the load on the website small.
            page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in ("image", "font", "media")
                else route.continue_(),
            )
            page.goto(URL, wait_until="networkidle")
            page.wait_for_timeout(3000)
            return page.inner_text("body")
        finally:
            browser.close()


def main():
    until = W.muted("sunset")
    if until:   # muted from the Telegram menu: nothing is checked or saved, so you get the news after the mute
        print(f"Muted until {until.astimezone(TZ):%a %d.%m. %H:%M} Vienna time. Nothing checked.")
        set_output("changed", "false")
        return
    try:
        text = read_page()
    except Exception as exc:  # noqa: BLE001
        W.health_report("sunset", False, f"{type(exc).__name__}: {str(exc)[:80]}")
        raise

    events = parse_events(text)
    if events is None:
        # Fail loudly (a failed run is marked in the status message) if the page looks broken.
        print("Page looks empty or changed, the script may need a fix.")
        W.health_report("sunset", False, "no events found on the page")
        sys.exit(2)
    W.health_report("sunset", True)

    old = load_old()
    out = []
    for t, e in events.items():
        out.append(f"{t} | {e['count']} / {e['cap']}" if e["count"] is not None else f"{t} | ?")
    SNAP.write_text("\n".join(out) + "\n", encoding="utf-8")

    if old is None:
        print("First run: baseline saved, no alert.")
        set_output("changed", "false")
        return

    result = build_changes(events, old)
    if result:
        title, message = result
        print(title)
        print(message)
        set_output("title", title)
        set_output("summary", message)
        set_output("changed", "true")
    else:
        print("No changes. Events listed:", len(events))
        set_output("changed", "false")


if __name__ == "__main__":
    main()
