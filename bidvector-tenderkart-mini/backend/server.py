"""
A very small HTTP framework on top of http.server.

Written by hand because this project has a hard zero-dependency rule: it has
to run on a fresh machine with nothing but Python 3.9+, no pip install, no
network. That constraint costs about 150 lines and buys a demo that never
fails on somebody else's laptop.

What it gives you: path routing with typed parameters (/api/tenders/<int:id>),
JSON in and out, query-string parsing, a bearer-token hook, and static file
serving for the frontend.
"""

import json
import re
import os
import traceback
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "frontend")

ROUTES = []


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def route(method, pattern):
    """
    @route("GET", "/api/tenders/<int:tender_id>")
    def handler(req, tender_id): ...
    """
    regex = _compile(pattern)

    def decorator(fn):
        ROUTES.append((method.upper(), regex, fn))
        return fn
    return decorator


def _compile(pattern):
    out, i = "", 0
    for m in re.finditer(r"<(?:(int|str):)?([a-zA-Z_][a-zA-Z0-9_]*)>", pattern):
        out += re.escape(pattern[i:m.start()])
        kind, name = m.group(1) or "str", m.group(2)
        out += f"(?P<{name}>[0-9]+)" if kind == "int" else f"(?P<{name}>[^/]+)"
        i = m.end()
    out += re.escape(pattern[i:])
    return re.compile("^" + out + "$")


class Request:
    def __init__(self, handler, body, query):
        self.handler = handler
        self.body = body
        self.query = query
        self.headers = handler.headers
        self.user = None            # filled by the auth middleware

    def arg(self, name, default=None, cast=None):
        v = self.query.get(name, [None])[0]
        if v is None or v == "":
            return default
        if cast:
            try:
                return cast(v)
            except (ValueError, TypeError):
                return default
        return v

    def json(self, name, default=None, cast=None):
        v = (self.body or {}).get(name, default)
        if v is None:
            return default
        if cast:
            try:
                return cast(v)
            except (ValueError, TypeError):
                return default
        return v

    def require(self, name, cast=None):
        v = self.json(name, None, cast)
        if v is None:
            raise HttpError(400, f"Missing required field: {name}")
        return v

    def bearer(self):
        h = self.headers.get("Authorization", "")
        return h[7:].strip() if h.lower().startswith("bearer ") else None


class Handler(BaseHTTPRequestHandler):
    server_version = "BidVector/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("BIDVECTOR_QUIET"):
            return
        print(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

    # -------------------------------------------------------------- verbs
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")

    # ----------------------------------------------------------- dispatch
    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        for m, regex, fn in ROUTES:
            if m != method:
                continue
            match = regex.match(path)
            if not match:
                continue
            try:
                body = self._read_body()
                req = Request(self, body, query)
                result = fn(req, **{k: (int(v) if v.isdigit() else v)
                                    for k, v in match.groupdict().items()})
                status = 200
                if isinstance(result, tuple):
                    result, status = result
                self._json(status, result)
            except HttpError as e:
                self._json(e.status, {"error": e.message})
            except Exception as e:                      # noqa: BLE001
                traceback.print_exc()
                self._json(500, {"error": f"{type(e).__name__}: {e}"})
            return

        if method == "GET":
            self._static(path)
        else:
            self._json(404, {"error": "No such endpoint"})

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise HttpError(400, "Body is not valid JSON")

    # ------------------------------------------------------------ output
    def _json(self, status, payload):
        data = json.dumps(payload, default=_encode).encode("utf-8")
        self._send(status, data, "application/json; charset=utf-8")

    def _send(self, status, data, ctype, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            # Single-page app: unknown non-asset paths fall back to index.
            if "." not in os.path.basename(rel):
                full = os.path.join(STATIC_DIR, "index.html")
            else:
                self._json(404, {"error": "Not found"})
                return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as fh:
            self._send(200, fh.read(), ctype)


def _encode(o):
    if isinstance(o, tuple):
        return list(o)
    if hasattr(o, "isoformat"):
        return o.isoformat()
    return str(o)


def serve(host="127.0.0.1", port=8000):
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd
