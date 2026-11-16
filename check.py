import os
import pathlib
import re
import sys

URL = "https://sunsetpadel.at/events"
SNAP = pathlib.Path("events.txt")


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


def event_line(title, e):
    c, cap = e["count"], e["cap"]
    return f"{title} ({c} / {cap} participants)" if c is not None else title


def build_changes(events, old):
    """Returns (title, message) or None when nothing changed.

    The message lists the changes first (highlighted), then all other current
    events, one line each.
    """
    changed = []   # highlighted lines
    changed_titles = set()
    for t, e in events.items():
        c, cap = e["count"], e["cap"]
        if t not in old:
            changed.append(f"🆕 NEW EVENT: {event_line(t, e)}")
            changed_titles.add(t)
        elif old[t] is not None and c is not None:
            oc, ocap = old[t]
            if c < oc:
                changed.append(f"🔻 PARTICIPANTS DOWN: {t}: {oc} -> {c} / {cap}")
                changed_titles.add(t)
    if not changed:
        return None
    others = [event_line(t, e) for t, e in events.items() if t not in changed_titles]
    lines = changed[:]
    if others:
        lines += ["", "Other current events:"] + others
    if len(lines) > MAX_EVENT_LINES:
        lines = lines[:MAX_EVENT_LINES] + ["..."]
    return "Sun Set Padel", "\n".join(lines)


def main():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
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
        text = page.inner_text("body")
        browser.close()

    events = parse_events(text)
    if events is None:
        # Fail loudly (you get an email) if the page looks broken.
        print("Page looks empty or changed, the script may need a fix.")
        sys.exit(2)

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
