import os, sys, tempfile, pathlib, datetime as dt
sys.path.insert(0, os.getcwd())
os.environ["MODE"] = "urgent"
import courts as C
d = tempfile.mkdtemp(); os.chdir(d)
flag = pathlib.Path("urgent.txt"); C.URGENT_FLAG = flag
def setflag(t): flag.write_text(t + "\n")
fails = []
def check(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c: fails.append(m)
setflag("on 2026-11-19T10:00:00+00:00")
check(C.urgent_days() == 3 and C.active_weekdays() == set(range(7)) and C.active_starts()[0] == "07:00" and C.active_starts()[-1] == "21:30", "old urgent defaults unchanged")
setflag("on 2026-11-19T10:00:00+00:00 days=7 wd=1111000 from=18 to=20")
check(C.urgent_days() == 7 and C.active_weekdays() == {0,1,2,3}, "days and weekdays read")
check(C.active_starts() == ["18:00","18:30","19:00","19:30","20:00"], "starts 18-20: " + str(C.active_starts()))
print(C.conditions_text())
check(C.conditions_text() == "Next 7 days · Mon-Thu · 2h block · start 18:00-20:00", C.conditions_text())
setflag("on x days=15 wd=0000011 from=7 to=21 main")
check(C.urgent_days() == 15 and C.active_weekdays() == {5,6} and C.active_starts()[0] == "07:00" and C.active_starts()[-1] == "21:30", "15 days, weekend, any time")
print(C.conditions_text())
setflag("on x days=99 wd=0000000 from=20 to=18")
check(C.urgent_days() == 15 and C.active_weekdays() == set(range(7)) and C.active_starts()[0] == "07:00", "broken values fall back safely")
setflag("on x evenings")
check(C.active_starts()[0] == "17:00", "old 'evenings' still works")
setflag("on x days=7 wd=1111000 from=18 to=20")
today = dt.date(2026, 11, 19)   # Thursday
targets = [today + dt.timedelta(days=i) for i in range(C.urgent_days()) if (today + dt.timedelta(days=i)).weekday() in C.active_weekdays()]
print([f"{t:%a %d.%m}" for t in targets])
check([t.weekday() for t in targets] == [3,0,1,2], "targets for 7 days from Thursday: Thu, then Mon-Wed")
C.URGENT_FLAG.write_text("off\n"); os.environ["MODE"] = "report"
check(C.active_starts() == C.START_TIMES and C.active_weekdays() == C.WEEKDAYS, "normal check unchanged")
sys.exit(1 if fails else 0)
