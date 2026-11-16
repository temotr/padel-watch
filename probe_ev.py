"""One-time probe: shows how the two Eversports booking pages are built.

Writes probe_ev.txt (summary) and probe_ev_page_1.txt / probe_ev_page_2.txt
(page HTML, shortened). Nothing is booked or changed on the websites.
"""
import pathlib
import re

from playwright.sync_api import sync_playwright

URLS = [
    "https://www.eversports.at/sb/padelzone-wien-or-floridsdorf-powered-by-cupra",
    "https://www.eversports.at/s/padelzone-wien-or-vienna-sporthotel",
]

JS = """
() => {
  const out = [];
  const all = [...document.querySelectorAll("body *")];
  const norm = (el) => el.textContent.replace(/\\s+/g, " ").trim();
  const short = (s, n) => (s || "").slice(0, n);

  // Most common background colours, with one sample element of each.
  const groups = {};
  for (const el of all) {
    const bg = getComputedStyle(el).backgroundColor;
    if (!bg || bg === "rgba(0, 0, 0, 0)" || bg === "transparent") continue;
    (groups[bg] = groups[bg] || []).push(el);
  }
  Object.entries(groups).sort((a, b) => b[1].length - a[1].length).slice(0, 14)
    .forEach(([bg, els]) => {
      out.push("COLOR " + bg + " x" + els.length +
        "\\n  SAMPLE: " + short(els[0].outerHTML, 500) +
        "\\n  PARENT: " + short(els[0].parentElement ? els[0].parentElement.outerHTML : "", 300));
    });

  // Court names and the row they sit in.
  const leaves = all.filter((el) => el.children.length === 0);
  const courtLabels = leaves.filter((el) =>
    /^(outdoor|indoor|court|platz|padel)/i.test(norm(el)) && norm(el).length < 60);
  out.push("COURT LABELS: " + courtLabels.length);
  courtLabels.slice(0, 6).forEach((el) => {
    out.push("LABEL '" + norm(el) + "' tag=" + el.tagName + " class=" + el.className +
      "\\n  ROW: " + short((el.parentElement && el.parentElement.parentElement || el).outerHTML, 1500));
  });

  // Hour labels and date text.
  leaves.filter((el) => /^\\d{1,2}:\\d{2}$/.test(norm(el))).slice(0, 4).forEach((el) => {
    const r = el.getBoundingClientRect();
    out.push("TIME '" + norm(el) + "' rect=" + [r.x, r.y, r.width, r.height].map(Math.round) +
      " class=" + el.className);
  });
  leaves.filter((el) => /(monday|tuesday|wednesday|thursday|friday|saturday|sunday|montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag)/i.test(norm(el)) && norm(el).length < 40)
    .slice(0, 6).forEach((el) => out.push("DATE TEXT '" + norm(el) + "' class=" + el.className +
      " parent=" + short(el.parentElement ? el.parentElement.outerHTML : "", 300)));

  // Buttons and links that may change the day or the sport.
  const clickable = [...document.querySelectorAll("button, a, [role=button]")];
  out.push("BUTTONS/LINKS: " + clickable.length);
  clickable.slice(0, 60).forEach((el) => {
    out.push("CLICK tag=" + el.tagName + " text='" + short(norm(el), 40) + "' aria=" +
      (el.getAttribute("aria-label") || "") + " href=" + (el.getAttribute("href") || "") +
      " data-testid=" + (el.getAttribute("data-testid") || ""));
  });
  return out.join("\\n\\n");
}
"""

lines = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    for n, url in enumerate(URLS, 1):
        lines.append("=" * 70)
        lines.append(f"PAGE {n}: {url}")
        ctx = browser.new_context(viewport={"width": 1400, "height": 1000}, locale="en-GB")
        page = ctx.new_page()
        resps = []
        page.on("response", lambda r, resps=resps: resps.append(r))
        try:
            resp = page.goto(url, wait_until="networkidle", timeout=60000)
            lines.append(f"HTTP status: {resp.status if resp else '?'}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"goto failed: {exc}")
        # Close a cookie banner if there is one.
        try:
            page.get_by_role(
                "button",
                name=re.compile(r"accept all|alle akzeptieren|akzeptieren|accept|zustimmen|agree", re.I),
            ).first.click(timeout=4000)
            page.wait_for_timeout(1500)
        except Exception:  # noqa: BLE001
            lines.append("(no cookie button clicked)")
        for _ in range(6):  # scroll so lazy parts load
            page.mouse.wheel(0, 700)
            page.wait_for_timeout(600)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(2000)

        lines.append(f"Final URL: {page.url}")
        lines.append(f"Title: {page.title()}")
        try:
            body = page.inner_text("body")
        except Exception as exc:  # noqa: BLE001
            body = f"(could not read body: {exc})"
        lines.append("BODY TEXT (first 3000 chars):\n" + body[:3000])
        try:
            lines.append("DOM REPORT:\n" + page.evaluate(JS))
        except Exception as exc:  # noqa: BLE001
            lines.append(f"DOM report failed: {exc}")

        lines.append("NETWORK (xhr/fetch):")
        for r in resps:
            try:
                if r.request.resource_type not in ("xhr", "fetch"):
                    continue
                ct = r.headers.get("content-type", "")
                keep = 3000 if re.search(r"slot|calendar|availab|court|booking|schedule", r.url, re.I) else 400
                body_txt = ""
                if re.search(r"json|html|text", ct):
                    try:
                        body_txt = r.text()[:keep]
                    except Exception:  # noqa: BLE001
                        body_txt = "(body not available)"
                lines.append(f"  {r.status} {r.request.method} {r.url}\n    type={ct}\n    {body_txt}")
            except Exception:  # noqa: BLE001
                continue

        html = page.content()
        pathlib.Path(f"probe_ev_page_{n}.txt").write_text(html[:400000], encoding="utf-8")
        ctx.close()
    browser.close()

pathlib.Path("probe_ev.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n".join(lines)[:3000])
