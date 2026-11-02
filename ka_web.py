"""Übersichtskarte: kleiner HTTP-Server nur für Heimnetz / VPN."""
import ipaddress
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

MAP_HTML = Path(__file__).with_name("map.html")
FAVICON = Path(__file__).with_name("favicon.svg")
MAP_JS = Path(__file__).with_name("map.js")
CGNAT = ipaddress.ip_network("100.64.0.0/10")  # Tailscale


def client_allowed(addr):
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip in CGNAT


def make_handler(store, site):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not client_allowed(self.client_address[0]):
                return self._send(403, b"forbidden", "text/plain")
            path = self.path.split("?", 1)[0]
            if path == "/":
                return self._send(200, MAP_HTML.read_bytes(), "text/html; charset=utf-8")
            if path == "/favicon.svg":
                return self._send(200, FAVICON.read_bytes(), "image/svg+xml")
            if path == "/map.js":
                return self._send(200, MAP_JS.read_bytes(), "text/javascript; charset=utf-8")
            if path == "/config.js":
                body = f"window.KA_SITE = {json.dumps(site, ensure_ascii=False)};".encode()
                return self._send(200, body, "text/javascript; charset=utf-8")
            if path == "/ads.json":
                body = json.dumps(store.map_ads(), ensure_ascii=False).encode()
                return self._send(200, body, "application/json; charset=utf-8")
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if not client_allowed(self.client_address[0]):
                return self._send(403, b"forbidden", "text/plain")
            if self.path != "/feedback":
                return self._send(404, b"not found", "text/plain")
            # JSON + eigener Header verhindern Schreibzugriffe durch fremde Webseiten.
            # Der Reverse Proxy reicht den ursprünglichen Host weiter; kein CORS.
            origin = self.headers.get("Origin")
            if (self.headers.get("X-KA-Feedback") != "1"
                    or self.headers.get("Sec-Fetch-Site") == "cross-site"):
                return self._send(403, b"forbidden", "text/plain")
            if origin:
                try:
                    parsed = urlsplit(origin)
                    same_origin = (parsed.scheme in ("http", "https")
                                   and parsed.netloc.lower() == self.headers.get("Host", "").lower())
                except ValueError:
                    same_origin = False
                if not same_origin:
                    return self._send(403, b"forbidden", "text/plain")
            if self.headers.get_content_type() != "application/json":
                return self._send(415, b"expected application/json", "text/plain")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024:
                    return self._send(413, b"invalid body size", "text/plain")
                self.connection.settimeout(5)
                data = json.loads(self.rfile.read(length))
            except (ValueError, OSError):
                return self._send(400, b"invalid JSON", "text/plain")
            if not isinstance(data, dict):
                return self._send(400, b"expected object", "text/plain")
            ad_id, vote = data.get("id"), data.get("vote")
            if (not isinstance(ad_id, str) or not 0 < len(ad_id) <= 128
                    or type(vote) is not int or vote not in (-1, 0, 1)):
                return self._send(400, b"invalid feedback", "text/plain")
            if not store.has(ad_id):
                return self._send(404, b"unknown listing", "text/plain")
            if vote == 0:
                store.clear_vote(ad_id)
            else:
                store.vote(ad_id, vote)
            body = json.dumps({"id": ad_id, "vote": vote or None}).encode()
            return self._send(200, body, "application/json; charset=utf-8")

        def log_message(self, *args):
            pass

    return Handler


def start(store, host, port, site=None):
    """site: {"label", "radius_km", "center": [lon, lat] oder None} für Titel und Karte."""
    server = ThreadingHTTPServer((host, port), make_handler(store, site or {}))
    threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
    return server
