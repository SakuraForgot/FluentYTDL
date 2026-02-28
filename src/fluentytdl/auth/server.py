import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any


class CookieHTTPServer(HTTPServer):
    """HTTPServer subclass with explicit cookie-related attributes."""

    received_cookies: list[dict[str, Any]]
    shutdown_event: threading.Event
    auth_token: str

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.received_cookies = []
        self.shutdown_event = threading.Event()
        self.auth_token = ""


class CookieReceiverHandler(BaseHTTPRequestHandler):
    """Handles POST requests containing cookie data with token authentication."""

    server: CookieHTTPServer  # type: ignore[assignment]

    def do_POST(self):
        if self.path == "/submit_cookies":
            try:
                # Validate auth token
                expected_token = self.server.auth_token
                if expected_token:
                    auth_header = self.headers.get("X-Auth-Token", "")
                    if auth_header != expected_token:
                        self.send_error(403, "Invalid authentication token")
                        return

                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length)
                data = json.loads(body)

                if isinstance(data, list):
                    self.server.received_cookies = data

                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(
                        b'{"status": "ok", "message": "Cookies received successfully"}'
                    )

                    # Signal that we are done
                    self.server.shutdown_event.set()

                else:
                    self.send_error(400, "Invalid payload format: Expected list of cookies")

            except Exception as e:
                self.send_error(500, f"Server Error: {str(e)}")
        else:
            self.send_error(404, "Not Found")

    def do_OPTIONS(self):
        """Handle CORS preflight requests."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token")
        self.end_headers()

    def log_message(self, format, *args):
        # Suppress default logging to keep console clean
        pass


class LocalCookieServer:
    """Manages the lifecycle of the local HTTP server with token authentication."""

    def __init__(self):
        self._server: CookieHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._shutdown_event = threading.Event()
        self._port: int = 0
        self._auth_token: str = ""
        self._cookies: list[dict[str, Any]] = []

    def start(self, port: int = 0) -> int:
        """
        Starts the server on a random port (or specified port).

        Returns the port number.

        Raises:
            RuntimeError: If server is already running.
            OSError: If the port is unavailable.
        """
        if self._server:
            raise RuntimeError("Server is already running")

        # Generate random auth token for this session
        self._auth_token = secrets.token_hex(16)

        # Create server with random port (port 0 lets OS pick)
        self._server = CookieHTTPServer(("127.0.0.1", port), CookieReceiverHandler)
        self._port = self._server.server_port

        # Configure server attributes
        self._server.received_cookies = []
        self._server.shutdown_event = self._shutdown_event
        self._server.auth_token = self._auth_token
        self._shutdown_event.clear()

        # Start server in a separate thread
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

        return self._port

    def wait_for_cookies(self, timeout: float = 300.0) -> list[dict[str, Any]] | None:
        """
        Blocks until cookies are received or timeout occurs.
        Returns the list of cookies if successful, None otherwise.
        """
        if not self._server:
            raise RuntimeError("Server not started")

        is_set = self._shutdown_event.wait(timeout)

        if is_set:
            self._cookies = getattr(self._server, "received_cookies", [])
            return self._cookies

        return None

    def stop(self):
        """Stops the server and cleans up resources."""
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None

    @property
    def port(self) -> int:
        return self._port

    @property
    def auth_token(self) -> str:
        """The random auth token for this session."""
        return self._auth_token
