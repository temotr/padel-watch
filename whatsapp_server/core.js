'use strict';
// Logic of the WhatsApp -> Telegram bridge that does not need WhatsApp itself (so it can be tested alone).
const crypto = require('crypto');

const MAX_AGE_SEC = 48 * 3600;     // a request disappears 48 hours after it arrived
const MAX_SKEW_SEC = 300;          // a signed request is only valid for 5 minutes
const TZ = 'Europe/Vienna';

function escapeHtml(s) {
    return String(s === undefined || s === null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ---------- settings.json (all optional; the defaults change nothing) ----------
const DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
function normalizeSettings(raw) {
    raw = raw && typeof raw === 'object' ? raw : {};
    const hour = (v, d) => (Number.isInteger(v) && v >= 0 && v <= 23 ? v : d);
    const words = v => (Array.isArray(v) ? v.map(x => String(x).toLowerCase().trim()).filter(Boolean) : []);
    const days = (Array.isArray(raw.weekdays) ? raw.weekdays : []).map(d => DAY_NAMES.findIndex(n => n.toLowerCase() === String(d).slice(0, 3).toLowerCase())).filter(i => i >= 0);
    const sim = typeof raw.dedupe_similarity === 'number' && raw.dedupe_similarity > 0.5 && raw.dedupe_similarity <= 1 ? raw.dedupe_similarity : 0.85;
    return { quietFrom: hour(raw.quiet_from, 23), quietTo: hour(raw.quiet_to, 7), weekdays: new Set(days),
             includeWords: words(raw.include_words), excludeWords: words(raw.exclude_words), similarity: sim };
}

// ---------- time in Vienna ----------
function viennaParts(ms) {
    const f = new Intl.DateTimeFormat('en-GB', { timeZone: TZ, hour12: false, year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric' });
    const o = {};
    for (const p of f.formatToParts(new Date(ms))) o[p.type] = Number(p.value);
    if (o.hour === 24) o.hour = 0;
    return o;
}
function isQuiet(ms, from, to) {
    if (from === to) return false;                 // same hour twice = quiet hours switched off
    const h = viennaParts(ms).hour;
    return from < to ? (h >= from && h < to) : (h >= from || h < to);
}

// ---------- which days does a post talk about? ----------
const DAY_WORDS = [
    [1, /\b(montag|monday|mo\.)/i], [2, /\b(dienstag|tuesday|di\.)/i], [3, /\b(mittwoch|wednesday|mi\.)/i],
    [4, /\b(donnerstag|thursday|do\.)/i], [5, /\b(freitag|friday|fr\.)/i], [6, /\b(samstag|saturday|sa\.|sam\.)/i],
    [0, /\b(sonntag|sunday|so\.|son\.)/i]
];
function mentionedDays(text, nowMs) {
    const days = new Set();
    const t = String(text);
    for (const [d, re] of DAY_WORDS) if (re.test(t)) days.add(d);
    if (/\bwochenende\b|\bweekend\b/i.test(t)) { days.add(6); days.add(0); }
    const now = viennaParts(nowMs);
    const todayIdx = new Date(Date.UTC(now.year, now.month - 1, now.day)).getUTCDay();
    if (/\b(heute|today)\b/i.test(t)) days.add(todayIdx);
    if (/(?<!guten )\b(morgen|tomorrow)\b/i.test(t)) days.add((todayIdx + 1) % 7);
    const re = /\b(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})?(?!\d)/g;
    let m;
    while ((m = re.exec(t))) {
        const day = Number(m[1]), mon = Number(m[2]);
        if (day < 1 || day > 31 || mon < 1 || mon > 12) continue;
        let year = m[3] ? Number(m[3]) : now.year;
        if (year < 100) year += 2000;
        if (!m[3] && Date.UTC(year, mon - 1, day) < Date.UTC(now.year, now.month - 1, now.day) - 5 * 86400000) year += 1;
        const d = new Date(Date.UTC(year, mon - 1, day));
        if (d.getUTCMonth() === mon - 1) days.add(d.getUTCDay());
    }
    return days;
}

// Is this message a request for players? (the original rules)
function isValidRequest(text) {
    const t = text.toLowerCase();
    const intentWords = ['suche', 'suchen', 'gesucht', 'spieler', 'spielerin', 'brauche', 'ersatz', 'abgesagt', 'frei', 'reservierung', 'reserviert', 'platz', 'spontan', 'finden', 'team'];
    if (!intentWords.some(word => t.includes(word))) return false;
    const isWeekend = /(samstag|sonntag|sam\.|son\.|sa\.|so\.|wochenende|saturday|sunday|weekend)/i.test(t);
    if (isWeekend) return true;
    const timeRegex = /\b(18|19|20)(?:[:.]?(?:00|30))?\s*(?:uhr)?\b/i;
    return timeRegex.test(t);
}

// Original rules + your optional filters from settings.json.
function passesFilters(text, settings, nowMs) {
    if (!isValidRequest(text)) return false;
    const t = text.toLowerCase();
    if (settings.excludeWords.some(w => t.includes(w))) return false;
    if (settings.includeWords.length && !settings.includeWords.some(w => t.includes(w))) return false;
    if (settings.weekdays.size) {
        const days = mentionedDays(text, nowMs);
        if (days.size && ![...days].some(d => settings.weekdays.has(d))) return false;
    }
    return true;
}

// ---------- duplicates: same post in several groups, even if slightly reworded ----------
function cleanText(text) {
    return String(text).toLowerCase().replace(/[^\p{L}\p{N}\s]/gu, '').replace(/\s+/g, ' ').trim();
}
function tokens(clean) { return new Set(clean.split(' ').filter(w => w.length > 1)); }
function similarity(a, b) {
    if (!a.size || !b.size) return 0;
    let inter = 0;
    for (const w of a) if (b.has(w)) inter++;
    return inter / (a.size + b.size - inter);
}
// recent: array of {at, id, clean, tokens}. Drops entries older than 2 h; returns the matching entry or null.
function findDuplicate(recent, clean, nowMs, threshold) {
    for (let i = recent.length - 1; i >= 0; i--) if (nowMs - recent[i].at > 2 * 3600 * 1000) recent.splice(i, 1);
    const tk = tokens(clean);
    return recent.find(r => r.clean === clean || similarity(r.tokens, tk) >= threshold) || null;
}

// ---------- Telegram texts: everything from WhatsApp is escaped ----------
const LINK_RE = /^https:\/\/chat\.whatsapp\.com\/[A-Za-z0-9]+$/;
function groupHtml(name, link) {
    return typeof link === 'string' && LINK_RE.test(link) ? `<a href="${link}">${escapeHtml(name)}</a>` : escapeHtml(name);
}
function buildTelegramMessage({ senderName, groupName, inviteLink, text, also }) {
    let groups = groupHtml(groupName, inviteLink);
    if (also && also.length) groups += ` (also in: ${also.map(g => groupHtml(g.name, g.link)).join(', ')})`;
    return `🎾 <b>New Padel Request from WhatsApp</b>\n👤 <b>From:</b> ${escapeHtml(senderName)}\n👥 <b>Group:</b> ${groups}\n\n<i>${escapeHtml(text)}</i>`;
}
// One message for everything that arrived during the quiet hours (Telegram allows 4096 characters).
function buildDigest(entries) {
    const head = `🌙 <b>Padel requests since last evening (${entries.length})</b>`;
    const parts = [];
    let used = head.length;
    let shown = 0;
    for (const e of entries) {
        const groups = groupHtml(e.group, e.link) + (e.also && e.also.length ? ` (+ ${e.also.map(g => groupHtml(g.name, g.link)).join(', ')})` : '');
        const block = `<b>${shown + 1}. ${escapeHtml(e.sender)}</b> · ${groups}\n<i>${escapeHtml(e.text)}</i>`;
        if (used + block.length + 2 > 3700) break;
        parts.push(block); used += block.length + 2; shown++;
    }
    const more = entries.length > shown ? `\n\n…and ${entries.length - shown} more (button "WhatsApp Summary")` : '';
    return `${head}\n\n${parts.join('\n\n')}${more}`;
}

// ---------- request signature (key never travels) ----------
function signature(key, ts, path) {
    return crypto.createHmac('sha256', key).update(`${ts}\n${path}`).digest('hex');
}
function verifyRequest(key, headers, path, nowMs) {
    if (!key) return false;
    const ts = String(headers['x-ts'] || '');
    const sig = String(headers['x-sig'] || '');
    if (!/^\d{9,12}$/.test(ts) || !/^[0-9a-f]{64}$/.test(sig)) return false;
    const now = (nowMs === undefined ? Date.now() : nowMs) / 1000;
    if (Math.abs(now - Number(ts)) > MAX_SKEW_SEC) return false;
    const expected = Buffer.from(signature(key, ts, path), 'hex');
    return crypto.timingSafeEqual(expected, Buffer.from(sig, 'hex'));
}

// ---------- housekeeping ----------
// Removes requests older than 48 hours. Entries without a timestamp get one now.
function purgeExpired(active, nowSec) {
    const removed = [];
    for (const [id, entry] of Object.entries(active)) {
        if (!entry.timestamp) { entry.timestamp = nowSec; continue; }
        if (nowSec - entry.timestamp > MAX_AGE_SEC) { removed.push([id, entry]); delete active[id]; }
    }
    return removed;
}
// A digest message is shared by several requests: delete it only when none of them is left.
function stillShared(active, tgMessageId) {
    return !!tgMessageId && Object.values(active).some(e => e.tgMessageId === tgMessageId);
}
function backupsToDelete(names, keep) {
    return names.filter(n => /^auth_info_\d{8}$/.test(n)).sort().slice(0, Math.max(0, names.filter(n => /^auth_info_\d{8}$/.test(n)).length - keep));
}

// The web address the listener reads. Every path needs a valid signature; there is no open endpoint.
function createHandler(getKey, getActive, nowFn, getInfo) {
    return (req, res) => {
        const path = String(req.url || '').split('?')[0];
        const now = nowFn ? nowFn() : Date.now();
        if (req.method !== 'GET' || !verifyRequest(getKey(), req.headers, path, now)) {
            res.writeHead(401, { 'Content-Type': 'text/plain' });
            return res.end('unauthorized');
        }
        const active = getActive();
        if (path === '/summary') {
            res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
            return res.end(JSON.stringify(active));
        }
        if (path === '/status') {
            const info = getInfo ? getInfo() : {};
            res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
            return res.end(JSON.stringify({ status: 'online', activeGames: Object.keys(active).length, whatsapp: info.whatsapp || 'unknown', queued: info.queued || 0 }));
        }
        res.writeHead(404, { 'Content-Type': 'text/plain' });
        res.end('Not found');
    };
}

module.exports = { MAX_AGE_SEC, MAX_SKEW_SEC, escapeHtml, normalizeSettings, viennaParts, isQuiet, mentionedDays, isValidRequest, passesFilters,
    cleanText, tokens, similarity, findDuplicate, buildTelegramMessage, buildDigest, signature, verifyRequest, purgeExpired, stillShared, backupsToDelete, createHandler };
