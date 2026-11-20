"""WhatsApp summary: day/time/place parsing, grouping, escaping."""
import datetime as dt, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wa_format as W

today = dt.date(2026, 11, 20)  # Friday
assert W.parse_day("Heute 20.11", today) == today
assert W.parse_day("für morgen Samstag", today) == dt.date(2026, 11, 21)
assert W.parse_day("Sonntag, 22.11., 12.30-13.30 Uhr", today) == dt.date(2026, 11, 22)
assert W.parse_day("Samstag 21.11.2026", today) == dt.date(2026, 11, 21)
assert W.parse_day("game on 2 December", today) == dt.date(2026, 12, 2)
assert W.parse_day("Guten Morgen, suche Spieler", today) is None
assert W.parse_day("am Dienstag", today) == dt.date(2026, 11, 24)
assert W.parse_day("19.11. war toll", today) == dt.date(2026, 11, 19)
assert W.parse_time("⏰ 20-22 oder 21-23")[0] == "20–22"
assert W.parse_time("⏰ 17:30–19:30 Uhr")[0] == "17:30–19:30"
assert W.parse_time("13 bis 15 Uhr")[0] == "13–15"
assert W.parse_time("Sonntag, 22.11., 12.30-13.30 Uhr")[0] == "12:30–13:30"
assert W.parse_time("ab 20 Uhr")[0] == "20"
assert W.parse_time("Level 1,0 - 2,5 am 21.11.")[0] is None, "level range and date are not a time"
assert W.parse_place("Suche\n📍Padeldome Süssenbrunn\n⏰ 20") == "Padeldome Süssenbrunn"
assert W.parse_place("x\n📍 Padelzone Vienna Sporthotel, Baumgasse 83") == "Padelzone Vienna Sporthotel"
assert W.parse_level("a\n📈 high Adv - low exp") == "high Adv - low exp"

now = dt.datetime(2026, 11, 20, 21, 0, tzinfo=dt.timezone.utc)  # 22:00 Vienna
ts = int(now.timestamp())
data = {
 "1": {"sender": "<b>Eve</b>", "group": "G&1", "link": "https://chat.whatsapp.com/Abc1", "timestamp": ts - 600,
       "text": "Suche 2 Spieler\n📍Europahalle\n📅 Samstag 21.11.\n⏰ 13 bis 15 Uhr <script>", "also": [{"name": "G2", "link": ""}]},
 "2": {"sender": "Bob", "group": "G", "link": "javascript:x", "timestamp": ts - 7200, "timestampGuess": True,
       "text": "Suche Spieler heute 20-22 Padeldome Stadlau"},
 "3": {"sender": "Old", "group": "G", "link": "", "timestamp": ts - 99999, "text": "Suchen Spieler am 18.11. 19 Uhr"},
 "4": {"sender": "Cy", "group": "G", "link": "", "queued": True, "text": "Suche Spieler Mittwoch 19 Uhr"},
}
out = W.wa_summary(data, now)
assert "<script>" not in out and "<b>Eve</b>" not in out and "G&amp;1" in out and "javascript:" not in out
assert out.index("TODAY") < out.index("TOMORROW") < out.index("Wed 25.11.")
assert "posted 21:50 (10 min ago) 🆕" in out
assert "posted" not in out.split("Bob")[1].split("👥")[0], "guessed arrival time is not shown"
assert "1 older request(s) hidden" in out and "Old" not in out
assert "G2" in out and "morning digest" in out
assert "· 3 open" in out
big = {str(i): {"sender": "s", "group": "G", "link": "", "text": "Suche Spieler heute " + "x" * 280} for i in range(60)}
assert len(W.wa_summary(big, now)) < 4096
assert W.wa_summary({}, now).startswith("🎾")
print("test_wa_format OK")
