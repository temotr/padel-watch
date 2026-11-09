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

# If the page came back (almost) empty, fail loudly so you get an email
# instead of silently missing events.
if len(lines) < 5:
    print("Page looks empty, the site may have changed or blocked the check.")
    sys.exit(2)

old = set(SNAP.read_text().splitlines()) if SNAP.exists() else None
SNAP.write_text("\n".join(lines) + "\n")

if old is None:
    print("First run: baseline saved, no alert.")
    set_output("false")
else:
    new = [l for l in lines if l not in old]
    if new:
        print("NEW LINES ON PAGE:")
        for l in new:
            print("  +", l)
        set_output("true")
    else:
        print("No changes.")
        set_output("false")
