import os, sys, json, subprocess, tempfile, tomllib, datetime as dt, re, pathlib
HERE = os.getcwd()
fails = []
def check(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c: fails.append(m)

PROBE = """
import json, sys
sys.path.insert(0, %r)
import courts as C, events_padel as E, tg_listen as L
print(json.dumps({
 "loc": C.LOCATIONS, "far": C.FAR_LOCATIONS, "pz": C.PADELZONE_LINKS, "other": C.OTHER_SITES, "post": C.POSTCODES,
 "days": C.DAYS_AHEAD, "book": C.BOOK_AHEAD_DAYS, "wd": sorted(C.WEEKDAYS), "starts": C.START_TIMES, "dur": C.DURATION_MIN,
 "maxw": C.MAX_WINDOWS, "minslots": C.MIN_SLOTS_READ, "ud": C.URGENT_DAYS, "uf": C.URGENT_FIRST, "ul": C.URGENT_LAST,
 "states": sorted(E.STATES), "levels": sorted(E.LEVELS), "kinds": sorted(E.KINDS), "eve": E.EVENING_FROM, "pages": E.MAX_PAGES,
 "cond": E.CONDITION_LINES,
 "every": L.URGENT_EVERY_SEC, "backoff": L.URGENT_BACKOFF_SEC, "maxh": L.URGENT_MAX_HOURS, "pause": L.PAUSE_HOURS, "tok": str(L.TOKEN_EXPIRES),
 "remind": list(L.REMIND_DAYS), "st": L.STATUS_EVERY_MIN, "q": [L.QUIET_FROM, L.QUIET_TO], "mute": L.MUTE_HOURS, "cad": L.CADENCE}))
""" % HERE
def probe(config_path):
    env = dict(os.environ, PADEL_CONFIG=config_path, TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="1")
    r = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, env=env, cwd=tempfile.mkdtemp())
    if r.returncode:
        print(r.stderr[-600:])
    return json.loads(r.stdout.strip().splitlines()[-1]), r.stdout

real = os.path.join(HERE, "config.toml")
cfg = tomllib.load(open(real, "rb"))
check(set(cfg) >= {"courts", "urgent", "tournaments", "bot", "sunset"}, "config.toml has all sections")

# the file and the built-in defaults give identical settings
a, _ = probe(real)
b, out = probe("/nonexistent/config.toml")
check(a == b, "config.toml equals the built-in defaults (nothing lost when the file is missing)")
if a != b:
    for k in a:
        if a[k] != b[k]: print("  differs:", k, a[k], b[k])

# sanity of the values
ids = [r["id"] for r in cfg["courts"]["locations"] + cfg["courts"]["far_locations"]]
check(len(ids) == len(set(ids)) and all(i.isdigit() for i in ids), "location ids are unique numbers")
check(all(re.fullmatch(r"\d{2}:\d{2}", t) for t in cfg["courts"]["start_times"]), "start times look like HH:MM")
check(all(w in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun") for w in cfg["courts"]["weekdays"]), "weekday names are valid")
check(dt.date.fromisoformat(cfg["bot"]["token_expires"]) is not None, "token_expires is a date")
check(all(isinstance(n, int) for n in cfg["bot"]["remind_days"] + cfg["bot"]["mute_hours"]), "reminder days and mute hours are numbers")
check(0 < cfg["courts"]["days_ahead"] <= 15, "days_ahead within 1-15")

# a typing mistake falls back to the defaults instead of stopping the watchers
tmp = tempfile.mkdtemp()
broken = os.path.join(tmp, "broken.toml"); open(broken, "w").write("[courts\ndays_ahead = = 3\n")
c, out = probe(broken)
check(c == a and "could not be read" in out or c == a, "broken file -> defaults")

# wrong kind of value -> that one value falls back, the rest is used
mixed = os.path.join(tmp, "mixed.toml")
open(mixed, "w").write('[courts]\ndays_ahead = "ten"\nweekdays = ["Fri", "Sat"]\nduration_min = 90\n[tournaments]\nstates = ["Wien", "Niederösterreich"]\n[bot]\ntoken_expires = "not a date"\n')
m, _ = probe(mixed)
check(m["days"] == 15, "text instead of number -> default")
check(m["wd"] == [4, 5] and m["dur"] == 90, "valid values are used")
check(m["states"] == ["Niederösterreich", "Wien"] and "Niederösterreich" in m["cond"][0], "tournament filter and its message follow the file")
check(m["tok"] == "2026-12-12", "invalid date -> default")

# an unknown weekday name -> default weekdays
bad = os.path.join(tmp, "bad.toml"); open(bad, "w").write('[courts]\nweekdays = ["Mon", "Funday"]\n')
check(probe(bad)[0]["wd"] == [0, 1, 2, 3], "unknown weekday name -> default weekdays")

# incomplete location entry -> default list
inc = os.path.join(tmp, "inc.toml"); open(inc, "w").write('[[courts.locations]]\nid = "1"\n')
check(probe(inc)[0]["loc"] == a["loc"], "incomplete location -> default locations")

# a changed location list is used (and the urgent/normal code can import)
loc = os.path.join(tmp, "loc.toml"); open(loc, "w").write('[[courts.locations]]\nid = "9"\nname = "Test Club"\npostcode = "1010"\n')
l, _ = probe(loc)
check(l["loc"] == [["9", "Test Club"]] and l["post"].get("9") == "1010", "own location list works")
print("FAILED:", fails) if fails else print("ALL OK")
sys.exit(1 if fails else 0)
