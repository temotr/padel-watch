"""Formats the WhatsApp requests for Telegram: grouped by day, key facts first. Everything from WhatsApp is escaped."""
import datetime as dt
import html
import re
from zoneinfo import ZoneInfo

VIENNA = ZoneInfo("Europe/Vienna")
LINK_RE = re.compile(r"https://chat\.whatsapp\.com/[A-Za-z0-9]+")
WEEKDAYS = {"montag": 0, "monday": 0, "dienstag": 1, "tuesday": 1, "mittwoch": 2, "wednesday": 2,
            "donnerstag": 3, "thursday": 3, "freitag": 4, "friday": 4, "samstag": 5, "saturday": 5,
            "sonntag": 6, "sunday": 6}
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "mär": 3, "apr": 4, "may": 5, "mai": 5, "jun": 6, "jul": 7, "aug": 8,
          "sep": 9, "oct": 10, "okt": 10, "nov": 11, "dec": 12, "dez": 12}
VENUES = r"padeldome|the dome|padelzone|\bpz\b|europahalle|racketworld|playpadel|sporthotel|alterlaa|stadlau|erdberg|süßenbrunn|süssenbrunn|alte donau"


def _clean_line(s):
    return re.sub(r"^[^\w€]+", "", s).strip()


def parse_day(text, today):
    """The day a post is about (a date) or None. today = date in Vienna."""
    t = text.lower()
    for m in re.finditer(r"\b(\d{1,2})\.\s?(\d{1,2})\.(\d{4}|\d{2})?(?!\d)", t):
        d, mo = int(m.group(1)), int(m.group(2))
        if not (1 <= d <= 31 and 1 <= mo <= 12):
            continue
        y = int(m.group(3)) if m.group(3) else today.year
        y += 2000 if y < 100 else 0
        try:
            day = dt.date(y, mo, d)
        except ValueError:
            continue
        if not m.group(3) and day < today - dt.timedelta(days=5):
            day = dt.date(y + 1, mo, d)
        return day
    m = re.search(r"\b(\d{1,2})\.?\s+(jan|feb|mar|mär|apr|may|mai|jun|jul|aug|sep|oct|okt|nov|dec|dez)[a-zä]*", t)
    if m:
        try:
            day = dt.date(today.year, MONTHS[m.group(2)], int(m.group(1)))
            return day if day >= today - dt.timedelta(days=5) else day.replace(year=today.year + 1)
        except ValueError:
            pass
    if re.search(r"\b(heute|today)\b", t):
        return today
    if re.search(r"(?<!guten )\b(morgen|tomorrow)\b", t):
        return today + dt.timedelta(days=1)
    for name, wd in WEEKDAYS.items():
        if re.search(rf"\b{name}\b", t):
            return today + dt.timedelta(days=(wd - today.weekday()) % 7)
    return None


def _hm(h, m):
    return f"{int(h)}:{m}" if m else f"{int(h)}"


def parse_time(text):
    """First time range like 20-22, 17:30-19:30, 13 bis 15 Uhr; else a single time; else None. Returns (label, start_minutes)."""
    for m in re.finditer(r"(?<![\d.,:])(\d{1,2})(?:[:.]([0-5]\d))?\s*(?:uhr)?\s*(?:-|–|—|bis|to)\s*(\d{1,2})(?:[:.]([0-5]\d))?(?![\d,])", text.lower()):
        h1, h2 = int(m.group(1)), int(m.group(3))
        if 6 <= h1 <= 23 and 0 <= h2 <= 24 and h1 != h2:
            start = h1 * 60 + int(m.group(2) or 0)
            return f"{_hm(h1, m.group(2))}–{_hm(h2, m.group(4))}", start
    m = re.search(r"(?<![\d.,])(\d{1,2}):([0-5]\d)(?![\d.])|(?<![\d.,])(\d{1,2})\s*uhr", text.lower())
    if m:
        h, mi = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), None)
        if 6 <= int(h) <= 23:
            return _hm(h, mi if mi and mi != "00" else None), int(h) * 60 + int(mi or 0)
    return None, 9999


def parse_place(text):
    for line in text.splitlines():
        if re.search(r"^[\s]*[📍📌]", line):
            return (_clean_line(line).split(',')[0])[:60] or None
    for line in text.splitlines():
        if re.search(VENUES, line.lower()) and len(line) < 80:
            return _clean_line(re.sub(r"(?i)^(ort|location|wo)\s*:\s*", "", _clean_line(line))).split(',')[0][:60]
    return None


def parse_level(text):
    for line in text.splitlines():
        if re.search(r"^[\s]*[📈🎯]", line) or re.match(r"(?i)\s*level\s*:", line):
            return re.sub(r"(?i)^level\s*:\s*", "", _clean_line(line))[:50] or None
    m = re.search(r"(?i)\b((?:low|mid|high)[\s–/-]*(?:mid|high)?[\s–/-]*(?:advanced|adv|intermediate|exp\w*)|anfänger\w*|beginner|starter|c[12])\b", text)
    return m.group(1) if m else None


def group_html(name, link):
    g = html.escape(str(name or "Unknown"))
    link = str(link or "")
    return f'<a href="{link}">{g}</a>' if LINK_RE.fullmatch(link) else g


def ago(minutes):
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{int(minutes)} min ago"
    return f"{int(minutes // 60)} h ago"


def day_header(day, today):
    label = day.strftime("%a %d.%m.")
    if day == today:
        return f"TODAY · {label}"
    if day == today + dt.timedelta(days=1):
        return f"TOMORROW · {label}"
    return label


def wa_summary(data, now=None, limit=3900):
    now = now or dt.datetime.now(dt.timezone.utc)
    local = now.astimezone(VIENNA)
    today = local.date()
    items, hidden = [], 0
    for m in data.values():
        text = str(m.get("text", ""))
        day = parse_day(text, today)
        if day and day < today:
            hidden += 1                     # the game day has passed
            continue
        label, start = parse_time(text)
        items.append({"m": m, "text": text, "day": day, "time": label, "start": start,
                      "place": parse_place(text), "level": parse_level(text)})
    items.sort(key=lambda x: (x["day"] is None, x["day"] or today, x["start"]))

    head = f"🎾 <b>Padel requests from WhatsApp · {len(items)} open</b>\nUpdated {local:%H:%M}"
    blocks, last = [], "-"
    n = 0
    for it in items:
        m = it["m"]
        key = it["day"]
        if key != last:
            title = day_header(key, today) if key else "OTHER · day not recognised"
            blocks.append(f"\n<b>━━ {html.escape(title)} ━━</b>")
            last = key
        n += 1
        facts = " · ".join(x for x in (f"⏰ {html.escape(it['time'])}" if it["time"] else "",
                                       f"📍 {html.escape(it['place'])}" if it["place"] else "") if x)
        lines = [f"<b>{n}.</b> {facts}" if facts else f"<b>{n}.</b>"]
        who = f"👤 {html.escape(str(m.get('sender', 'Unknown')))}"
        ts = m.get("timestamp")
        if ts and not m.get("timestampGuess"):
            posted = dt.datetime.fromtimestamp(float(ts), dt.timezone.utc)
            mins = (now - posted).total_seconds() / 60
            who += f" · posted {posted.astimezone(VIENNA):%H:%M} ({ago(mins)})" + (" 🆕" if mins < 60 else "")
        lines.append(who)
        if it["level"]:
            lines.append(f"📈 {html.escape(it['level'])}")
        known = sum(bool(x) for x in (it["time"], it["place"], it["level"]))
        flat = re.sub(r"\s+", " ", it["text"]).strip()
        cut = 90 if known >= 2 else 300
        lines.append(f"<i>“{html.escape(flat[:cut])}{'…' if len(flat) > cut else ''}”</i>")
        groups = [group_html(m.get("group"), m.get("link"))] + [
            group_html(a.get("name"), a.get("link")) for a in (m.get("also") or []) if isinstance(a, dict)]
        shown = " + ".join(groups[:3]) + (f" + {len(groups) - 3} more" if len(groups) > 3 else "")
        if m.get("queued"):
            shown += " · 🌙 in the morning digest"
        lines.append(f"👥 {shown}")
        blocks.append(("\n" if not blocks[-1].startswith("\n<b>━━") else "") + "\n".join(lines))
    foot = f"\n\n<i>{hidden} older request(s) hidden (day already passed)</i>" if hidden else ""
    out, used = [head], len(head) + len(foot)
    for i, b in enumerate(blocks):
        if used + len(b) + 1 > limit:
            out.append(f"\n…and more (list too long)")
            break
        out.append(b)
        used += len(b) + 1
    return "\n".join(out) + foot