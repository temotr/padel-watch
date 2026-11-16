import datetime
import pathlib
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

BASE = "https://www.padeldome.wien/reservierung?c=5175"

# The site uses a timestamp for 08:00 Vienna time of the day, e.g. &d=1794812400
tz = ZoneInfo("Europe/Vienna")
day = datetime.datetime.now(tz).replace(hour=8, minute=0, second=0, microsecond=0)
day = day + datetime.timedelta(days=1)
url = f"{BASE}&d={int(day.timestamp())}"
print("Probing:", url)

JS = """
() => {
  const out = [];
  const all = [...document.querySelectorAll("body *")];
  const rgb = (el) => {
    const m = getComputedStyle(el).backgroundColor.match(/rgba?\\((\\d+), *(\\d+), *(\\d+)/);
    return m ? [+m[1], +m[2], +m[3]] : null;
  };
  const rect = (el) => {
    const r = el.getBoundingClientRect();
    return [
      Math.round(r.x + window.scrollX), Math.round(r.y + window.scrollY),
      Math.round(r.width), Math.round(r.height)
    ].join(",");
  };
  const norm = (el) => el.textContent.replace(/\\s+/g, " ").trim();

  // 1) The most common background colours, with one sample of each.
  const groups = {};
  for (const el of all) {
    const bg = getComputedStyle(el).backgroundColor;
    if (!bg || bg === "rgba(0, 0, 0, 0)" || bg === "transparent") continue;
    (groups[bg] = groups[bg] || []).push(el);
  }
  Object.entries(groups)
    .sort((a, b) => b[1].length - a[1].length)
    .slice(0, 12)
    .forEach(([bg, els]) => {
      const first = els[0];
      out.push(
        "COLOR " + bg + " x" + els.length +
        "\\n  SAMPLE: " + first.outerHTML.slice(0, 500) +
        "\\n  PARENT: " + (first.parentElement ? first.parentElement.outerHTML.slice(0, 250) : "")
      );
    });

  // 2) Elements with a PALE GREEN background (the free boxes).
  const pale = all.filter((el) => {
    const c = rgb(el);
    if (!c) return false;
    const [r, g, b] = c;
    return g >= 200 && r >= 180 && b >= 180 && g - r >= 4 && g - b >= 4;
  });
  out.push("PALE GREEN ELEMENTS: " + pale.length);
  pale.slice(0, 12).forEach((el) => {
    out.push(
      "PALE: tag=" + el.tagName + " class=" + el.className +
      " bg=" + getComputedStyle(el).backgroundColor +
      " rect(x,y,w,h)=" + rect(el) +
      " text='" + norm(el).slice(0, 20) + "'" +
      " attrs=" + [...el.attributes].map((a) => a.name + "=" + a.value).join(";").slice(0, 200)
    );
  });

  // 3) Positions of the court names, times and day names.
  const wanted = ["Platz 1", "Platz 2", "Platz 16", "08:00", "18:00", "19:30", "22:30"];
  const leaves = all.filter((el) => el.children.length === 0);
  for (const label of wanted) {
    leaves
      .filter((el) => norm(el) === label)
      .slice(0, 3)
      .forEach((el) => out.push("LABEL '" + label + "' rect=" + rect(el) + " tag=" + el.tagName + " class=" + el.className));
  }
  leaves
    .filter((el) => /^(Montag|Dienstag|Mittwoch|Donnerstag)/.test(norm(el)))
    .slice(0, 4)
    .forEach((el) => out.push("DAY '" + norm(el).slice(0, 30) + "' rect=" + rect(el)));

  // 4) A booked box, for comparison.
  const booked = all.find((el) => el.children.length === 0 && norm(el) === "Turnier");
  if (booked) {
    out.push("BOOKED SAMPLE: " + booked.outerHTML.slice(0, 600));
    if (booked.parentElement) {
      out.push("  PARENT: " + booked.parentElement.outerHTML.slice(0, 400));
    }
  }
  return out.join("\\n\\n");
}
"""

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(3000)
    html = page.content()
    report = page.evaluate(JS)
    browser.close()

pathlib.Path("probe_page.txt").write_text(html)
pathlib.Path("probe.txt").write_text(url + "\n\n" + report + "\n")
print(report[:3000])