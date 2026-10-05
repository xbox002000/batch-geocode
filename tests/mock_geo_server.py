"""Local provider mock for E2E: GEOCODE_TEST_BASE=http://127.0.0.1:8767

Valid key = MOCK_GEO_KEY (default TEST_KEY) in `key` / `access_token` / `apiKey`.
Address containing 'nowhere' → zero results; 'ratelimit' → 429 / OVER_QUERY_LIMIT.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, os.path.dirname(__file__))
from fixtures import GOOGLE_DENIED, GOOGLE_OK, GOOGLE_OVER, GOOGLE_ZERO, HERE_OK, MAPBOX_EMPTY, MAPBOX_OK, MATRIX_OK  # noqa: E402

VALID = os.getenv("MOCK_GEO_KEY", "TEST_KEY")


class H(BaseHTTPRequestHandler):
    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if code == 429:
            self.send_header("Retry-After", "0")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path
        if path == "/maps/api/geocode/json":
            if q.get("key") != VALID:
                return self._json(200, GOOGLE_DENIED)
            text = (q.get("address") or q.get("latlng") or "").lower()
            if "nowhere" in text:
                return self._json(200, GOOGLE_ZERO)
            if "ratelimit" in text:
                return self._json(200, GOOGLE_OVER)
            return self._json(200, GOOGLE_OK)
        if path == "/maps/api/distancematrix/json":
            if q.get("key") != VALID:
                return self._json(200, {**GOOGLE_DENIED, "rows": []})
            return self._json(200, MATRIX_OK)
        if path.startswith("/geocoding/v5/mapbox.places/"):
            if q.get("access_token") != VALID:
                return self._json(401, {"message": "Not Authorized - Invalid Token"})
            text = unquote(path.rsplit("/", 1)[-1]).lower()
            if "ratelimit" in text:
                return self._json(429, {"message": "Too Many Requests"})
            return self._json(200, MAPBOX_EMPTY if "nowhere" in text else MAPBOX_OK)
        if path in ("/v1/geocode", "/v1/revgeocode"):
            if q.get("apiKey") != VALID:
                return self._json(401, {"error": "Unauthorized", "error_description": "apiKey invalid. apiKey not found."})
            if "nowhere" in (q.get("q") or "").lower():
                return self._json(200, {"items": []})
            return self._json(200, HERE_OK)
        self._json(404, {"error": f"unknown path {path}"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(os.getenv("PORT", "8767"))), H).serve_forever()
