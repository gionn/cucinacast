"""Serve synthesized TTS announcements over HTTP for the Chromecast to fetch."""

import http.server
import logging
import os
import socket
import threading

from tts import DEFAULT_PATH, synthesize

logger = logging.getLogger(__name__)

_URL_PATH = f"/{DEFAULT_PATH.name}"

_server_lock = threading.Lock()
_server_started = False
_server_port = None


def _announce_port():
    return int(os.environ.get("ANNOUNCE_PORT", "8765"))


class _AnnounceHandler(http.server.BaseHTTPRequestHandler):
    """Serves only the single announcement file — never the whole temp directory."""

    def do_GET(self):
        if self.path != _URL_PATH:
            self.send_error(404)
            return
        try:
            data = DEFAULT_PATH.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        logger.info("%s - %s", self.address_string(), format % args)


def _get_lan_ip():
    if os.environ.get("ANNOUNCE_HOST"):
        return os.environ["ANNOUNCE_HOST"]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError as exc:
        raise RuntimeError(
            "Couldn't auto-detect a LAN IP for announcements (no network route?); "
            "set ANNOUNCE_HOST explicitly"
        ) from exc
    finally:
        s.close()


def _ensure_server():
    global _server_started, _server_port
    with _server_lock:
        if _server_started:
            return
        port = _announce_port()
        httpd = http.server.ThreadingHTTPServer(("0.0.0.0", port), _AnnounceHandler)  # noqa: S104
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        _server_started = True
        _server_port = port
        logger.info("Announcement HTTP server listening on port %s", port)


def synthesize_and_serve(text, lang="en"):
    """Synthesize text to speech, overwrite the shared audio file, ensure the HTTP
    server is running, and return a LAN-reachable URL for the Chromecast to fetch."""
    synthesize(text, lang=lang, path=DEFAULT_PATH)
    _ensure_server()
    return f"http://{_get_lan_ip()}:{_server_port}/{DEFAULT_PATH.name}"
