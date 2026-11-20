'use strict';
const assert = require('assert');
const http = require('http');
const core = require('./core');
const KEY = 'k'.repeat(32);
const NOW = 1_800_000_000_000;
const ts = String(NOW / 1000);
const sign = (p, k = KEY, t = ts) => ({ 'x-ts': t, 'x-sig': core.signature(k, t, p) });

// escaping
assert.strictEqual(core.escapeHtml('<b>&'), '&lt;b&gt;&amp;');
const msg = core.buildTelegramMessage({ senderName: '<x>', groupName: 'A&B', inviteLink: 'https://chat.whatsapp.com/Abc123', text: '<script>' });
assert(!msg.includes('<x>') && !msg.includes('<script>') && msg.includes('A&amp;B'));
assert(core.buildTelegramMessage({ senderName: 'a', groupName: 'g', inviteLink: 'javascript:alert(1)', text: 't' }).indexOf('<a ') === -1);

// request rules
assert(core.isValidRequest('Suche 2 Spieler Samstag'));
assert(!core.isValidRequest('Guten Morgen'));

// signature
assert(core.verifyRequest(KEY, sign('/summary'), '/summary', NOW));
assert(!core.verifyRequest(KEY, sign('/summary'), '/status', NOW), 'path is signed');
assert(!core.verifyRequest(KEY, sign('/summary', 'x'.repeat(32)), '/summary', NOW), 'wrong key');
assert(!core.verifyRequest(KEY, sign('/summary'), '/summary', NOW + 301000), 'old request');
assert(!core.verifyRequest(KEY, {}, '/summary', NOW), 'no headers');
assert(!core.verifyRequest('', sign('/summary', ''), '/summary', NOW), 'no key set');

// purge
const a = { old: { timestamp: NOW / 1000 - 49 * 3600, tgMessageId: 1 }, fresh: { timestamp: NOW / 1000 - 3600 }, legacy: { tgMessageId: 2 } };
const removed = core.purgeExpired(a, NOW / 1000);
assert.deepStrictEqual(removed.map(r => r[0]), ['old']);
assert(a.fresh && a.legacy && a.legacy.timestamp && !a.old);

// settings
const def = core.normalizeSettings({});
assert.strictEqual(def.quietFrom, 23); assert.strictEqual(def.quietTo, 7); assert(def.weekdays.size === 0);
const bad = core.normalizeSettings({ quiet_from: 99, weekdays: ['xyz', 'Mon'], dedupe_similarity: 5, include_words: 'no' });
assert.strictEqual(bad.quietFrom, 23); assert(bad.weekdays.has(1) && bad.weekdays.size === 1); assert.strictEqual(bad.similarity, 0.85); assert.strictEqual(bad.includeWords.length, 0);

// quiet hours (Vienna, winter = UTC+1): 22:30 UTC = 23:30 Vienna, 05:59 UTC = 06:59, 06:00 UTC = 07:00
const utc = (h, m) => Date.UTC(2026, 10, 20, h, m);
assert(core.isQuiet(utc(22, 30), 23, 7)); assert(core.isQuiet(utc(5, 59), 23, 7));
assert(!core.isQuiet(utc(6, 0), 23, 7)); assert(!core.isQuiet(utc(12, 0), 23, 7));
assert(!core.isQuiet(utc(2, 0), 5, 5), 'same hour = off');
assert(core.isQuiet(utc(12, 0), 11, 14) && !core.isQuiet(utc(15, 0), 11, 14));

// days mentioned (Fri 20.11.2026 noon Vienna)
const NOON = utc(11, 0);
const dn = t => [...core.mentionedDays(t, NOON)].sort().join(',');
assert.strictEqual(dn('Samstag 21.11. 10:00'), '6');
assert.strictEqual(dn('Wochenende'), '0,6');
assert.strictEqual(dn('heute 20-22'), '5');
assert.strictEqual(dn('morgen Abend'), '6');
assert.strictEqual(dn('Guten Morgen, suche Spieler'), '');
assert.strictEqual(dn('am 24.11. Dienstag'), '2');
assert.strictEqual(dn('13 bis 15 Uhr, 12.30-13.30'), '', 'times are not dates');
assert.strictEqual(dn('Sonntag, 22.11., 12.30'), '0');

// filters
const only = core.normalizeSettings({ weekdays: ['Mon', 'Tue', 'Wed', 'Thu'], exclude_words: ['Profi'], include_words: [] });
assert(!core.passesFilters('Suche 2 Spieler Samstag 21.11.', only, NOON), 'Saturday not wanted');
assert(core.passesFilters('Suche 2 Spieler Dienstag 24.11. 19 Uhr', only, NOON));
assert(core.passesFilters('Suche Spieler 19 Uhr', only, NOON), 'no day named: passes');
assert(!core.passesFilters('Suche Profi Spieler Dienstag 19 Uhr', only, NOON), 'excluded word');
const inc = core.normalizeSettings({ include_words: ['starter', 'anfänger'] });
assert(core.passesFilters('Suche Spieler Samstag, Starter', inc, NOON) && !core.passesFilters('Suche Spieler Samstag, Advanced', inc, NOON));
assert(core.passesFilters('Suche Spieler Samstag', def, NOON), 'defaults change nothing');

// duplicates
const c1 = core.cleanText('🎾 Suche 2 Spieler!\nSamstag 21.11. 13 bis 15 Uhr – Europahalle, 20 Euro');
const rec = [{ at: NOW, id: 'a', clean: c1, tokens: core.tokens(c1) }];
assert(core.findDuplicate(rec, core.cleanText('Suche 2 Spieler Samstag 21.11 13 bis 15 Uhr Europahalle 20 Euro PN'), NOW, 0.85), 'slightly reworded');
assert(!core.findDuplicate(rec, core.cleanText('Suche 3 Spieler Sonntag Padeldome 10 Uhr'), NOW, 0.85));
assert(!core.findDuplicate(rec, c1, NOW + 3 * 3600 * 1000, 0.85) && rec.length === 0, 'expires after 2 h');

// message with several groups, digest
const m2 = core.buildTelegramMessage({ senderName: 'a', groupName: 'G1', inviteLink: 'https://chat.whatsapp.com/Abc1', text: 't', also: [{ name: 'G<2>', link: '' }] });
assert(m2.includes('also in: G&lt;2&gt;'));
const digest = core.buildDigest([{ sender: '<s>', group: 'G', link: '', text: '<t>', also: [] }]);
assert(digest.includes('(1)') && !digest.includes('<s>') && !digest.includes('<t>'));
const many = Array.from({ length: 60 }, (_, i) => ({ sender: 's' + i, group: 'G', link: '', text: 'x'.repeat(200), also: [] }));
const big = core.buildDigest(many);
assert(big.length < 4096 && big.includes('more'));

// shared digest message / backups
const act = { a: { tgMessageId: 9 }, b: { tgMessageId: 9 }, c: { tgMessageId: 5 } };
assert(core.stillShared(act, 9) && !core.stillShared(act, 7));
assert.deepStrictEqual(core.backupsToDelete(['auth_info_20261118', 'auth_info_20261120', 'auth_info_20261119', 'auth_info_20261117', 'x'], 3), ['auth_info_20261117']);

// http
const server = http.createServer(core.createHandler(() => KEY, () => a, () => NOW)).listen(0, async () => {
    const port = server.address().port;
    const get = (p, h) => new Promise(r => http.get({ port, path: p, headers: h }, res => { let b = ''; res.on('data', d => b += d); res.on('end', () => r([res.statusCode, b, res.headers])); }));
    try {
        assert.strictEqual((await get('/summary'))[0], 401);
        assert.strictEqual((await get('/status'))[0], 401);
        const [c, b, h] = await get('/summary', sign('/summary'));
        assert.strictEqual(c, 200); assert(Object.keys(JSON.parse(b)).length === 2); assert(!h['access-control-allow-origin']);
        const st = JSON.parse((await get('/status', sign('/status')))[1]); assert.strictEqual(st.activeGames, 2); assert.strictEqual(st.whatsapp, 'unknown');
        assert.strictEqual((await get('/other', sign('/other')))[0], 404);
        console.log('test_wa_server OK');
    } catch (e) { console.error(e); process.exitCode = 1; }
    server.close();
});
