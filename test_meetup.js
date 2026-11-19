const fs = require('fs');
const src = fs.readFileSync('meetup_to_telegram.gs', 'utf8');

const mail1 = `<html><body><table><tr><td>THURSDAY</td></tr>
<tr><td><a href="https://www.meetup.com/sports-meetup-vienna/events/312345678/?utm_source=x&eventOrigin=email">&gt; Afterwork Indoor Padel at Padelzone Floridsdorf</a></td></tr>
<tr><td>Sports Meetup Vienna</td></tr>
<tr><td>Thursday, February 12, 2099<br>7:00 PM Europe/Vienna</td></tr>
<tr><td>Padelzone Floridsdorf<br>Katharina-Scheiter-Gasse 1<br>Vienna</td></tr>
<tr><td>€20.00</td></tr><tr><td>RSVP today</td></tr>
<tr><td>13 Members going, including:</td></tr><tr><td>Wolfgang</td></tr></table></body></html>`;
const mail2 = `<html><body><table><tr><td>TUESDAY</td></tr>
<tr><td><a href="https://www.meetup.com/sports-meetup-vienna/events/300111222/">Tuesday (bi-weekly) Padel, pre tournament rehearsal</a></td></tr>
<tr><td>Sports Meetup Vienna</td></tr>
<tr><td>Tuesday, May 20, 2025</td></tr><tr><td>6:00 PM Europe/Vienna</td></tr>
<tr><td>Sportcenter Donaucity</td></tr><tr><td>Arbeiterstrandbadstrasse 128, 1220 Wien.</td></tr>
<tr><td>More events from this group</td></tr>
<tr><td><a href="https://www.meetup.com/sports-meetup-vienna/events/300999001/">Wednesday Beachvolleyball, Alte Donau</a><br>Wednesday, May 14, 2025 8:00 PM · 15 attending <a href="https://www.meetup.com/sports-meetup-vienna/events/300999001/">RSVP</a></td></tr>
<tr><td><a href="https://www.meetup.com/sports-meetup-vienna/events/300999002/">Friday Padel Night, Floridsdorf</a><br>Friday, May 16, 2099 4:00 PM · 9 attending <a href="https://www.meetup.com/sports-meetup-vienna/events/300999002/">RSVP</a></td></tr>
</table></body></html>`;

function mk(id, subject, html, daysAgo) {
  return { getId: () => id, getSubject: () => subject, getBody: () => html, getDate: () => new Date(Date.now() - daysAgo * 86400000) };
}
const store = {TELEGRAM_BOT_TOKEN:'x', TELEGRAM_CHAT_ID:'1'}; const sent = [];
const g = {
  PropertiesService: { getScriptProperties: () => ({
    getProperty: k => (k in store ? store[k] : null), setProperty: (k, v) => { store[k] = v; },
    deleteProperty: k => { delete store[k]; }, getProperties: () => Object.assign({}, store) }) },
  GmailApp: { search: () => [{ getMessages: () => G.msgs }] },
  UrlFetchApp: { fetch: (u, o) => {
    if (u.includes("raw.githubusercontent.com")) {                 // the mute list in GitHub
      if (G.muteFails) throw new Error("network");
      return { getResponseCode: () => (G.mute === null ? 404 : 200), getContentText: () => G.mute || "" };
    }
    sent.push(o.payload.text); return { getResponseCode: () => 200, getContentText: () => "" }; } },
  Utilities: { formatDate: (d, tz, f) => d.toISOString().slice(0, 10), getUuid: () => "a-b-c-d" },
  ScriptApp: { getProjectTriggers: () => [], newTrigger: () => ({ timeBased: () => ({ everyMinutes: () => ({ create() {} }) }) }) },
  Logger: { log: s => console.log("LOG:", s) },
  ContentService: { createTextOutput: t => ({ text: t, setMimeType() { return this; } }), MimeType: { TEXT: 1 } },
};
const G = { msgs: [], mute: null, muteFails: false };
const fn = new Function(...Object.keys(g), src + "\n;return {checkMeetup, doGet, setup, parseMain, parseMore, testForwarded, isMuted};");
const api = fn(...Object.values(g));

const ok = (c, m) => { console.log((c ? "PASS " : "FAIL ") + m); if (!c) process.exitCode = 1; };
const m1 = mk("a1", '📅 Thursday: Join 13 Members at "Afterwork Indoor Padel at Padelzone Floridsdorf"', mail1, 0.1);
const m2 = mk("a2", '📅 Tuesday: Can you make "Tuesday (bi-weekly) Padel, pre tournament rehearsal "?', mail2, 40);
let ev = api.parseMain(m1);
console.log(JSON.stringify(ev));
ok(ev.date === "2099-02-12" && ev.time === "19:00" && ev.venue === "Padelzone Floridsdorf" && ev.price === "€20.00" && ev.going === "13" && ev.id === "312345678", "mail 1 parsed");
ok(ev.url === "https://www.meetup.com/sports-meetup-vienna/events/312345678/", "tracking removed from link: " + ev.url);
ev = api.parseMain(m2);
console.log(JSON.stringify(ev));
ok(ev.date === "2025-05-20" && ev.time === "18:00" && ev.venue === "Sportcenter Donaucity" && ev.title.startsWith("Tuesday (bi-weekly) Padel"), "mail 2 parsed");
const more = api.parseMore(m2);
console.log(JSON.stringify(more));
ok(more.length === 2 && !more.find(e => e.id === "300111222") && more.find(e => e.id === "300999002").time === "16:00" && more.find(e => e.id === "300999002").going === "9", "more events parsed");
ok(!more.find(e => /RSVP/i.test(e.title)), "RSVP buttons ignored");

G.msgs = [m2, m1];
api.checkMeetup();
console.log(Object.keys(store));
ok(Object.keys(store).includes("ev_312345678") && Object.keys(store).includes("ev_300999002") && !Object.keys(store).includes("ev_300999001"), "padel events stored, volleyball not");
ok(sent.length === 1 && sent[0].includes("Afterwork Indoor Padel"), "only the recent padel mail alerted (old import silent), count=" + sent.length);
console.log(sent[0]);
api.checkMeetup();                    // second run: nothing new
ok(sent.length === 1, "no duplicate alert");
console.log(api.doGet({ parameter: { key: "wrong" } }).text);
ok(api.doGet({ parameter: { key: "wrong" } }).text === "forbidden", "wrong key refused");

// a past event that shows up again in a recent mail must not be stored or announced
const before = sent.length;
G.msgs = [mk("a3", '"Old Padel night"', mail2.replace("Friday, May 16, 2099","Friday, May 16, 2025"), 0.1)];
api.checkMeetup();
ok(sent.length === before && !("ev_300111222" in store), "past event ignored (no alert, not stored)");
const rows = api.doGet({ parameter: { key: store.MEETUP_KEY || "" } });
store.MEETUP_KEY = "k1"; const out = api.doGet({ parameter: { key: "k1" } }).text; console.log(out);
ok(out.startsWith("# v1") && out.includes("2099-02-12|19:00|Afterwork Indoor Padel at Padelzone Floridsdorf|Padelzone Floridsdorf|€20.00|13|https://"), "overview list output");

// forwarded old mail: test function sends it even though the event is long over
G.msgs = [mk("f1", 'Fwd: 📅 Tuesday: Can you make "Tuesday (bi-weekly) Padel, pre tournament rehearsal "?', mail2, 0)];
const n0 = sent.length; api.testForwarded();
ok(sent.length === n0 + 1 && sent[n0].startsWith("<b>🧪 TEST</b>") && sent[n0].includes("Tuesday (bi-weekly) Padel"), "testForwarded sends a TEST message");

// ---- mute from the Telegram menu
G.mute = null; ok(api.isMuted() === false, "no mute.txt (404) = not muted");
G.mute = "sunset 2099-01-01T00:00:00+00:00\n"; ok(api.isMuted() === false, "other watcher muted = Meetup not muted");
G.mute = "meetup 2000-01-01T00:00:00+00:00\n"; ok(api.isMuted() === false, "expired mute = not muted");
G.mute = "meetup 2099-01-01T00:00:00+00:00\n"; ok(api.isMuted() === true, "future mute = muted");
G.muteFails = true; ok(api.isMuted() === false, "network problem = not muted (alerts keep working)"); G.muteFails = false;

const mail3 = mail1.replace("312345678", "355555555").replace("February 12, 2099", "March 5, 2099").replace("Afterwork Indoor Padel at Padelzone Floridsdorf", "Padel Sunday Special");
const m3 = mk("a9", '📅 Sunday: Join "Padel Sunday Special"', mail3, 5);      // arrived 5 days ago, during the mute
G.msgs = [m3];
G.mute = "meetup 2099-01-01T00:00:00+00:00\n";
const n1 = sent.length;
api.checkMeetup();
ok(sent.length === n1 && !("ev_355555555" in store), "muted: nothing sent, mail left alone");
ok(!!store.MUTED_SINCE, "start of the mute remembered");
store.MUTED_SINCE = String(Date.now() - 6 * 86400000);                      // pretend the mute began 6 days ago
G.mute = "meetup 2000-01-01T00:00:00+00:00\n";
api.checkMeetup();
ok(sent.length === n1 + 1 && sent[n1].includes("Padel Sunday Special"), "after the mute: the mail from the mute period is announced (even if older than 2 days)");
ok(!store.MUTED_SINCE, "mute marker cleared");
api.checkMeetup();
ok(sent.length === n1 + 1, "and only once");
