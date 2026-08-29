"""Serve synthesized TTS announcements over HTTP for the Chromecast to fetch."""
import http.server
import logging
import os
import socket
import tempfile
import threading
from functools import partial

from tts import DEFAULT_PATH, synthesize

logger = logging.getLogger(__name__)

ANNOUNCE_PORT = int(os.environ.get("ANNOUNCE_PORT", "8765"))

_server_lock = threading.Lock()
_server_started = False


def _get_lan_ip():
    if os.environ.get("ANNOUNCE_HOST"):
        return os.environ["ANNOUNCE_HOST"]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def _ensure_server():
    global _server_started
    with _server_lock:
        if _server_started:
            return
        handler = partial(http.server.SimpleHTTPRequestHandler, directory=tempfile.gettempdir())
        httpd = http.server.ThreadingHTTPServer(("0.0.0.0", ANNOUNCE_PORT), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        _server_started = True
        logger.info("Announcement HTTP server listening on port %s", ANNOUNCE_PORT)


def synthesize_and_serve(text, lang="en"):
    """Synthesize text to speech, overwrite the shared audio file, ensure the HTTP
    server is running, and return a LAN-reachable URL for the Chromecast to fetch."""
    synthesize(text, lang=lang, path=DEFAULT_PATH)
    _ensure_server()
    return f"http://{_get_lan_ip()}:{ANNOUNCE_PORT}/{DEFAULT_PATH.name}"
