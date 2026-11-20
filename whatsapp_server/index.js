'use strict';
const { default: makeWASocket, useMultiFileAuthState, fetchLatestWaWebVersion, DisconnectReason, Browsers } = require('@whiskeysockets/baileys');
const qrcode = require('qrcode-terminal');
const axios = require('axios');
const fs = require('fs');
const http = require('http');
const path = require('path');
const P = require('pino');
const core = require('./core');

// ---- Secrets: NOT in this file. Set them as settings (environment variables) of the host, or in secrets.json
//      next to this file: {"TELEGRAM_TOKEN": "...", "TELEGRAM_CHAT_ID": "...", "API_KEY": "...", "PORT": 15688}
function readJson(file, fallback) {
    try { return JSON.parse(fs.readFileSync(path.join(__dirname, file), 'utf8')); } catch (e) { return fallback; }
}
const fileSecrets = readJson('secrets.json', {});
const pick = name => process.env[name] || fileSecrets[name];
const TELEGRAM_TOKEN = pick('TELEGRAM_TOKEN');
const TELEGRAM_CHAT_ID = pick('TELEGRAM_CHAT_ID');
const API_KEY = pick('API_KEY');
const PORT = Number(pick('PORT') || 15688);
if (!TELEGRAM_TOKEN || !TELEGRAM_CHAT_ID || !API_KEY || String(API_KEY).length < 24) {
    console.error('Missing settings: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID and API_KEY (at least 24 characters) are required.');
    process.exit(1);
}

// Optional: settings.json (quiet hours, filters). Read again for every message, so edits apply without a restart.
const loadSettings = () => core.normalizeSettings(readJson('settings.json', {}));
// Invite links of your groups (jid -> link) live in group_links.json on the server, not in the code.
const GROUP_LINKS = readJson('group_links.json', {});

const DATA_FILE = path.join(__dirname, 'active_messages.json');
let activeMessages = readJson('active_messages.json', {});
const recent = [];                  // recent posts for duplicate detection
const groupNames = new Map();       // jid -> { name, at }
let waState = 'reconnecting';       // connected | reconnecting | logged_out
let downSince = Date.now();

function saveActive() {
    const tmp = DATA_FILE + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(activeMessages, null, 2));
    fs.renameSync(tmp, DATA_FILE);
}

async function tgRequest(method, data) {
    try {
        const res = await axios.post(`https://api.telegram.org/bot${TELEGRAM_TOKEN}/${method}`, data, { timeout: 15000 });
        return res.data.result;
    } catch (e) {
        console.error(`Telegram ${method} failed:`, e.response && e.response.data ? e.response.data.description : e.message);
        return null;
    }
}
const queuedEntries = () => Object.values(activeMessages).filter(e => e.queued);

// Requests older than 48 hours are removed here and their Telegram messages are deleted.
async function purge() {
    const removed = core.purgeExpired(activeMessages, Math.floor(Date.now() / 1000));
    for (const [, entry] of removed) {
        if (entry.tgMessageId && !core.stillShared(activeMessages, entry.tgMessageId)) {
            await tgRequest('deleteMessage', { chat_id: TELEGRAM_CHAT_ID, message_id: entry.tgMessageId });
        }
    }
    saveActive();
    if (removed.length) console.log(`Removed ${removed.length} request(s) older than 48 hours.`);
}

// After the quiet hours: everything that arrived meanwhile goes out as ONE message.
async function flushQueue() {
    const s = loadSettings();
    if (core.isQuiet(Date.now(), s.quietFrom, s.quietTo)) return;
    const queued = queuedEntries();
    if (!queued.length) return;
    queued.sort((a, b) => a.timestamp - b.timestamp);
    const res = await tgRequest('sendMessage', {
        chat_id: TELEGRAM_CHAT_ID, parse_mode: 'HTML', disable_web_page_preview: true, text: core.buildDigest(queued)
    });
    if (!res) return;                                  // try again next minute
    for (const e of queued) { e.tgMessageId = res.message_id; e.queued = false; e.digest = true; }
    saveActive();
    console.log(`Sent digest with ${queued.length} request(s).`);
}

async function groupName(sock, jid) {
    const cached = groupNames.get(jid);
    if (cached && Date.now() - cached.at < 3600 * 1000) return cached.name;
    try {
        const meta = await sock.groupMetadata(jid);
        const name = meta.subject || 'Unknown Group';
        groupNames.set(jid, { name, at: Date.now() });
        return name;
    } catch (e) {
        return cached ? cached.name : 'Group (No Name)';
    }
}

// ---- Backup of the WhatsApp login (auth_info): once a day, the 3 newest are kept ----
function backupAuth() {
    try {
        if (waState !== 'connected') return;                      // never copy a broken session
        const root = path.join(__dirname, 'backup');
        const name = 'auth_info_' + new Date().toISOString().slice(0, 10).replace(/-/g, '');
        const dest = path.join(root, name);
        if (fs.existsSync(dest)) return;
        fs.mkdirSync(root, { recursive: true });
        fs.cpSync(path.join(__dirname, 'auth_info'), dest, { recursive: true });
        for (const old of core.backupsToDelete(fs.readdirSync(root), 3)) fs.rmSync(path.join(root, old), { recursive: true, force: true });
        console.log('Backup of auth_info saved:', name);
    } catch (e) { console.error('Backup failed:', e.message); }
}

async function startBot() {
    const { version } = await fetchLatestWaWebVersion({});
    const { state, saveCreds } = await useMultiFileAuthState('auth_info');

    const sock = makeWASocket({
        version, auth: state, printQRInTerminal: false, browser: Browsers.ubuntu('Chrome'),
        syncFullHistory: false, defaultQueryTimeoutMs: 60000, logger: P({ level: 'silent' })
    });

    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('connection.update', (update) => {
        const { connection, lastDisconnect, qr } = update;
        if (qr) qrcode.generate(qr, { small: true });
        if (connection === 'close') {
            const loggedOut = lastDisconnect?.error?.output?.statusCode === DisconnectReason.loggedOut;
            if (waState === 'connected') downSince = Date.now();
            waState = loggedOut ? 'logged_out' : 'reconnecting';
            if (loggedOut) console.error('WhatsApp logged this device out. Delete the auth_info folder and restart to scan the QR code again.');
            else setTimeout(() => startBot(), 5000);
        } else if (connection === 'open') {
            waState = 'connected';
            console.log('✅ WhatsApp Bot is successfully connected!');
            backupAuth();
        }
    });

    sock.ev.on('messages.upsert', async (m) => {
        if (m.type !== 'notify') return;                       // only new messages, not history
        for (const msg of m.messages) {                        // every message of the batch, not only the first
            try {
                if (!msg.message || msg.key.fromMe) continue;
                const jid = msg.key.remoteJid || '';
                if (!jid.endsWith('@g.us')) continue;          // groups only: private chats are never read
                const text = msg.message.conversation || msg.message.extendedTextMessage?.text || msg.message.ephemeralMessage?.message?.extendedTextMessage?.text || '';
                const settings = loadSettings();
                const nowMs = Date.now();
                if (!text || !core.passesFilters(text, settings, nowMs)) continue;

                const inviteLink = GROUP_LINKS[jid] || '';
                const gName = await groupName(sock, jid);
                const senderName = msg.pushName || 'Unknown';

                // Same post (or nearly the same) already seen in another group: add the group to the first message.
                const clean = core.cleanText(text);
                const dup = core.findDuplicate(recent, clean, nowMs, settings.similarity);
                if (dup) {
                    const first = activeMessages[dup.id];
                    if (first && first.group !== gName && !(first.also || []).some(g => g.name === gName)) {
                        first.also = (first.also || []).concat([{ name: gName, link: inviteLink }]);
                        if (first.tgMessageId && !first.digest && !first.queued) {
                            await tgRequest('editMessageText', {
                                chat_id: TELEGRAM_CHAT_ID, message_id: first.tgMessageId, parse_mode: 'HTML', disable_web_page_preview: true,
                                text: core.buildTelegramMessage({ senderName: first.sender, groupName: first.group, inviteLink: first.link, text: first.text, also: first.also })
                            });
                        }
                        saveActive();
                    }
                    console.log('🛑 Duplicate: added the group to the first message.');
                    continue;
                }
                recent.push({ at: nowMs, id: msg.key.id, clean, tokens: core.tokens(clean) });

                const entry = { tgMessageId: null, sender: senderName, group: gName, text, link: inviteLink, also: [], timestamp: Math.floor(nowMs / 1000) };
                if (core.isQuiet(nowMs, settings.quietFrom, settings.quietTo)) {
                    entry.queued = true;                       // sent in the morning digest
                    activeMessages[msg.key.id] = entry;
                    saveActive();
                    console.log('🌙 Quiet hours: request saved for the digest.');
                    continue;
                }
                console.log('🚀 Forwarding to Telegram...');
                const tgRes = await tgRequest('sendMessage', {
                    chat_id: TELEGRAM_CHAT_ID, parse_mode: 'HTML', disable_web_page_preview: true,
                    text: core.buildTelegramMessage({ senderName, groupName: gName, inviteLink, text })
                });
                if (tgRes) {
                    entry.tgMessageId = tgRes.message_id;
                    activeMessages[msg.key.id] = entry;
                    saveActive();
                }
            } catch (e) {
                console.error('Could not handle a message:', e.message);
            }
        }
    });

    sock.ev.on('messages.update', async (updates) => {
        for (const update of updates) {
            if (update.update.messageStubType === 1 || update.update.message === null) {
                const entry = activeMessages[update.key.id];
                if (entry) {
                    delete activeMessages[update.key.id];
                    if (entry.tgMessageId && !core.stillShared(activeMessages, entry.tgMessageId)) {
                        await tgRequest('deleteMessage', { chat_id: TELEGRAM_CHAT_ID, message_id: entry.tgMessageId });
                    }
                    saveActive();
                }
            }
        }
    });
}

// The web address read by the Telegram listener: every request must carry a valid signature (see core.js).
const info = () => ({ whatsapp: waState, queued: queuedEntries().length });
http.createServer(core.createHandler(() => API_KEY, () => activeMessages, undefined, info)).listen(PORT, () => {
    console.log(`📡 API Server listening on port ${PORT} (signed requests only)`);
});

// A crash of one handler must not stop the bridge.
process.on('unhandledRejection', e => console.error('Unhandled error:', e && e.message ? e.message : e));
process.on('uncaughtException', e => console.error('Uncaught error:', e && e.message ? e.message : e));

purge();
setInterval(purge, 15 * 60 * 1000);
setInterval(() => { flushQueue().catch(e => console.error('Digest failed:', e.message)); }, 60 * 1000);
setInterval(backupAuth, 6 * 3600 * 1000);
// Connection lost for more than 15 minutes (not logged out): end the program, the host starts it again.
setInterval(() => {
    if (waState === 'reconnecting' && Date.now() - downSince > 15 * 60 * 1000) {
        console.error('No WhatsApp connection for 15 minutes: restarting.');
        process.exit(1);
    }
}, 60 * 1000);
setInterval(() => {
    console.log(`[Watchdog] WhatsApp: ${waState}. Active games: ${Object.keys(activeMessages).length}`);
}, 1000 * 60 * 60);

startBot();
