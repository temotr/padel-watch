WhatsApp -> Telegram bridge (runs on Wispbyte, not on GitHub)

Files on the server:
  index.js, core.js, package.json   the program
  secrets.json  (or host settings)  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, API_KEY, PORT
  group_links.json                  {"<group id>@g.us": "https://chat.whatsapp.com/..."}
  settings.json  (optional)         copy settings.example.json; changes apply to the next message, no restart
  auth_info/                        the WhatsApp login (keep it private)
  backup/                           automatic copies of auth_info, one per day, the 3 newest are kept
  active_messages.json              open requests (removed after 48 hours)

settings.json
  quiet_from / quiet_to   Vienna hours. Requests that arrive in between are collected and sent as ONE message
                          at quiet_to. Same value twice = quiet hours off.
  weekdays                ["Mon","Tue","Wed","Thu"]: skip posts whose day (name, "heute", "morgen" or a date
                          like 21.11.) is not in the list. Posts without a day always pass. [] = all days.
  include_words           ["starter","anfänger"]: only posts containing at least one of these. [] = no limit.
  exclude_words           ["profi","expert"]: skip posts containing any of these.
  dedupe_similarity       0.85 = the same post in another group (even slightly reworded) is added to the first
                          Telegram message as "also in: ..." instead of a new message.

If WhatsApp is disconnected for 15 minutes the program ends so the host can start it again.
If WhatsApp logs the device out: delete the auth_info folder, restart, scan the QR code.
