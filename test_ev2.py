import datetime as dt, os, pathlib, tempfile, sys
sys.path.insert(0, ".")
import events_padel as E

def card(title, loc, date, time, cat, slots, status):
    return f"{title}\n{loc}\n{date}\n{time}\n{cat}\n{slots}\n{status}\n"
cards_txt = "Offener Bewerb\nNovember 2026\n" + "".join([
 card("PD Süßenbrunn NEWCOMER OPEN SPECIAL","PADELDOME Süßenbrunn - Wien","So. 22.11.2026","16:00 - 21:00","Offener Bewerb - Starter","14/16","Anmeldung geschlossen"),   # closed
 card("PD Süßenbrunn AFTERWORK STARTER","PADELDOME Süßenbrunn - Wien","Di. 24.11.2026","18:30 - 23:00","Offener Bewerb - Starter","15/16","Anmeldung offen"),          # ok
 card("Frühschicht Starter","smash | Leopoldau - Wien","Mi. 25.11.2026","09:00 - 14:00","Offener Bewerb - Starter","1/16","Anmeldung offen"),                        # weekday morning -> out
 card("Turbo-Turnier Newcomer 🚅⚡","The Dome - Wien","Di. 01.12.2026","20:00 - 22:45","Offener Bewerb - Starter","0/16","Anmeldung offen"),                              # ok, same title
 card("Turbo-Turnier Newcomer 🚅⚡","The Dome - Wien","Di. 08.12.2026","18:00 - 20:45","Offener Bewerb - Starter","0/16 (2)","Anmeldung offen"),                         # holiday evening ok
 card("Breakfast Club","PADELDOME Erdberg - Wien","Di. 08.12.2026","09:30 - 14:30","Offener Bewerb - Starter","3/9","Anmeldung offen"),                                 # holiday morning -> ok
 card("Saturday Morning","PADELDOME Alte Donau - Wien","Sa. 05.12.2026","10:00 - 15:00","Offener Bewerb - Starter","16/16 (3)","Anmeldung offen"),                      # full -> out
 card("Saturday Morning Newcomer","PADELDOME Alte Donau - Wien","Sa. 05.12.2026","10:00 - 15:00","Offener Bewerb - Starter","6/16 (1)","Anmeldung offen"),               # weekend ok, waitlist 1
 card("Herren Starter Cup","The Dome - Wien","Sa. 05.12.2026","12:00 - 15:00","Herren - Starter","2/16","Anmeldung offen"),                                              # Herren -> out
 card("Padel Loft Starter","Padel Loft - Steiermark","Sa. 05.12.2026","12:00 - 15:00","Offener Bewerb - Starter","2/16","Anmeldung offen"),                              # other state -> out
]) + "Weiter ›\n"
titles=["PD Süßenbrunn NEWCOMER OPEN SPECIAL","PD Süßenbrunn AFTERWORK STARTER","Frühschicht Starter","Turbo-Turnier Newcomer 🚅⚡","Turbo-Turnier Newcomer 🚅⚡","Breakfast Club","Saturday Morning","Saturday Morning Newcomer","Herren Starter Cup","Padel Loft Starter"]
def href(i): return f"https://padel-austria.at/ranked/tournaments/{i:08x}-1111-2222-3333-444444444444"
# case A: one link per card, but extra non-card links in between (counts differ) -> title matching
anchors=[("https://padel-austria.at/locations/padeldome","PADELDOME")]
for i,t in enumerate(titles): anchors += [(href(i), t), (href(i), "Details")]
anchors.append(("https://padel-austria.at/tournaments?page=2","Weiter"))
cards=E.parse_cards(cards_txt, anchors)
print(len(cards),"cards; links ok:", all(c["url"]==href(i) for i,c in enumerate(cards)))
print("same-title cards got different links:", cards[3]["url"]!=cards[4]["url"])
print("wanted:", [(c["title"][:24], c["date"], c["time"]) for c in cards if E.matches(c) and E.wanted(c)])
print("holidays:", E.holiday_name(dt.date(2026,12,8)), E.holiday_name(dt.date(2026,4,6)), E.holiday_name(dt.date(2026,5,14)), E.holiday_name(dt.date(2026,11,17)))
tmp=pathlib.Path(tempfile.mkdtemp()); E.STATE=tmp/"s.txt"
sent=[]; E.notify=lambda t,m,c: sent.append((t,m))
now=dt.datetime(2026,11,18,15,13,tzinfo=E.TZ); os.environ["MODE"]=""
matched=[c for c in cards if E.matches(c)]
E.run(all_cards=cards,now=now)
print("RUN1:", sent[0][0], "| messages:", len(sent))
print(E.courts.plain(sent[0][1]))
print("=== TELEGRAM HTML (part 1, cut) ===")
print(E.courts.telegram_html(sent[0][1])[:1500])
sent.clear(); E.run(all_cards=cards,now=now); print("RUN2 sent:", len(sent))
c2=[dict(c) for c in cards]; c2[0]["status"]="Anmeldung offen"; c2[0]["taken"]=13   # becomes open
c2.append(dict(cards[1], title="NEW Afterwork", time="19:00-22:00", date="2026-11-26"))
E.run(all_cards=c2,now=now); print("RUN3:", sent[0][0]); print(E.courts.plain(sent[0][1])[:900])
# long list is split into several messages
big=[dict(cards[1], title=f"Event {i}", date=(dt.date(2026,12,10)+dt.timedelta(days=i%20)).isoformat()) for i in range(40)]
os.environ["MODE"]="report"; sent.clear(); E.STATE.unlink(); E.run(all_cards=big,now=now)
print("40 events ->", len(sent), "messages, longest:", max(len(m) for _,m in sent), "chars")
