import os
import pathlib
import re
import sys

from playwright.sync_api import sync_playwright

URL = "https://sunsetpadel.at/events"
SNAP = pathlib.Path("events.txt")

# True  = email on ANY change of the participant count (can be often)
# False = email only when a FULL event gets a free spot
ALERT_ON_ANY_COUNT_CHANGE = True


def set_output(value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"changed={value}\n")


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

lines = [l.strip() for l in text.splitlines() if l.strip()]

# Fail loudly (you get an email) if the page looks broken.
if len(lines) < 5 or "Upcoming Events" not in lines:
    print("Page looks empty or changed, the script may need a fix.")
    sys.exit(2)

# Only look at the "Upcoming Events" section, ignore past events.
start = lines.index("Upcoming Events")
end = lines.index("Past Events", start) if "Past Events" in lines[start:] else len(lines)
section = lines[start + 1:end]

# Each event: title line, then a date line, then description,
# then "N / M participants".
date_idx = [
    i for i in range(1, len(section))
    if "(Austria)" in section[i] and " at " in section[i]
]

events = {}
for n, i in enumerate(date_idx):
    title = section[i - 1]
    stop = date_idx[n + 1] - 1 if n + 1 < len(date_idx) else len(section)
    count = cap = None
    for l in section[i + 1:stop]:
        m = re.search(r"(\d+)\s*/\s*(\d+)\s*participants", l)
        if m:
            count, cap = int(m.group(1)), int(m.group(2))
            break
    events[title] = (count, cap)

# Load the previous snapshot (lines look like: "Title | 20 / 45").
first_run = not SNAP.exists()
old = {}
if not first_run:
    for l in SNAP.read_text().splitlines():
        if not l.strip():
            continue
        t, _, rest = l.partition(" | ")
        m = re.match(r"(\d+) / (\d+)", rest)
        old[t] = (int(m.group(1)), int(m.group(2))) if m else None

# Save the new snapshot.
out = []
for t, (c, cap) in events.items():
    out.append(f"{t} | {c} / {cap}" if c is not None else f"{t} | ?")
SNAP.write_text("\n".join(out) + "\n")

if first_run:
    print("First run: baseline saved, no alert.")
    set_output("false")
    sys.exit(0)

msgs = []
for t, (c, cap) in events.items():
    if t not in old:
        msgs.append(f"NEW EVENT: {t} ({c} / {cap} participants)")
    elif old[t] is not None and c is not None:
        oc, ocap = old[t]
        if oc != c:
            if ALERT_ON_ANY_COUNT_CHANGE:
                msgs.append(f"PARTICIPANTS: {t}: {oc} -> {c} / {cap}")
            elif oc >= ocap and c < cap:
                msgs.append(f"SPOT FREE: {t}: {oc} -> {c} / {cap}")

if msgs:
    print("CHANGES:")
    for m in msgs:
        print("  +", m)
    set_output("true")
else:
    print("No changes. Events listed:", len(events))
    set_output("false")