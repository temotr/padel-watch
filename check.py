import os
import pathlib
import sys

from playwright.sync_api import sync_playwright

URL = "https://sunsetpadel.at/events"
SNAP = pathlib.Path("events.txt")


def set_output(value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"changed={value}\n")


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
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

# An event title is the line right before its date line,
# e.g. "Thursday 12 November at 18:00 ... (Austria) ..."
titles = [
    section[i - 1]
    for i in range(1, len(section))
    if "(Austria)" in section[i] and " at " in section[i]
]

old = set(SNAP.read_text().splitlines()) if SNAP.exists() else None
SNAP.write_text("\n".join(titles) + "\n")

if old is None:
    print("First run: baseline saved, no alert.")
    set_output("false")
else:
    new = [t for t in titles if t not in old]
    if new:
        print("NEW EVENTS:")
        for t in new:
            print("  +", t)
        set_output("true")
    else:
        print("No new events. Currently listed:", len(titles))
        set_output("false")
