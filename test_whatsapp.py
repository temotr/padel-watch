"""WhatsApp bridge client: signed requests, escaping, not-connected state."""
import http.server, json, os, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x"); os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
import hashlib, hmac
import tg_listen as T

KEY = "k" * 32
DATA = {"1": {"sender": "<b>Eve</b>", "group": "A&B", "text": "<script>x</script>", "link": "https://chat.whatsapp.com/Abc1"},
        "2": {"sender": "Bob", "group": "G", "text": "hi", "link": "javascript:alert(1)"}}

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        ts, sig = self.headers.get("x-ts", ""), self.headers.get("x-sig", "")
        want = hmac.new(KEY.encode(), f"{ts}\n{self.path}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, want) or abs(time.time() - int(ts or 0)) > 300:
            self.send_response(401); self.end_headers(); return
        body = json.dumps(DATA if self.path == "/summary" else {"status": "online", "activeGames": 2}).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(body)

srv = http.server.HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
T.WISPBYTE_URL = f"http://127.0.0.1:{srv.server_port}"

T.WISPBYTE_KEY = ""
assert not T.wa_configured()
T.WISPBYTE_KEY = KEY
assert T.wa_configured()
assert T.wa_get("/status")["activeGames"] == 2
txt = T.wa_summary_text(T.wa_get("/summary"))
assert "<script>" not in txt and "<b>Eve</b>" not in txt and "A&amp;B" in txt
assert '<a href="https://chat.whatsapp.com/Abc1">' in txt and "javascript:" not in txt
T.WISPBYTE_KEY = "w" * 32
try:
    T.wa_get("/status"); raise SystemExit("wrong key must fail")
except Exception as e:
    assert "401" in str(e)
assert not hasattr(T, "sync_deletions")
srv.shutdown()
print("test_whatsapp OK")
