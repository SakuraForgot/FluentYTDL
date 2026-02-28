"""
Comprehensive tests for DLE (Dynamic Local Extension) integration.

Tests cover:
- LocalCookieServer: start/stop, cookie reception, token auth
- ExtensionGenerator: file generation, manifest, background.js
- DLEProvider: browser resolution, cookies_to_netscape
"""

import json
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

# Add src to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ==================== Server Tests ====================


class TestLocalCookieServer:
    """Tests for LocalCookieServer."""

    def test_start_and_stop(self):
        """Server should start on a random port and stop cleanly."""
        from fluentytdl.auth.server import LocalCookieServer

        server = LocalCookieServer()
        port = server.start()

        assert port > 0, f"Port should be positive, got {port}"
        assert server.port == port
        assert server.auth_token, "Auth token should be generated"
        assert len(server.auth_token) == 32, "Token should be 32 hex chars"

        server.stop()
        print(f"  PASS: Server started on port {port} and stopped cleanly")

    def test_receive_cookies_with_valid_token(self):
        """Server should accept cookies with valid auth token."""
        from fluentytdl.auth.server import LocalCookieServer

        server = LocalCookieServer()
        port = server.start()
        token = server.auth_token

        # Send cookies in a separate thread
        test_cookies = [
            {"domain": ".youtube.com", "name": "LOGIN_INFO", "value": "test123", "path": "/"},
        ]

        def send_cookies():
            time.sleep(0.3)
            data = json.dumps(test_cookies).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/submit_cookies",
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "X-Auth-Token": token,
                },
            )
            urllib.request.urlopen(req)

        t = threading.Thread(target=send_cookies, daemon=True)
        t.start()

        cookies = server.wait_for_cookies(timeout=5.0)
        server.stop()

        assert cookies is not None, "Should have received cookies"
        assert len(cookies) == 1
        assert cookies[0]["name"] == "LOGIN_INFO"
        print(f"  PASS: Received {len(cookies)} cookie(s) with valid token")

    def test_reject_invalid_token(self):
        """Server should reject requests with invalid auth token."""
        from fluentytdl.auth.server import LocalCookieServer

        server = LocalCookieServer()
        port = server.start()

        import urllib.request

        data = json.dumps([{"name": "test"}]).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/submit_cookies",
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-Auth-Token": "wrong_token_000000000000000000",
            },
        )

        import urllib.error

        try:
            urllib.request.urlopen(req)
            server.stop()
            raise AssertionError("Should have raised an error")
        except urllib.error.HTTPError as e:
            assert e.code == 403, f"Expected 403, got {e.code}"
            print(f"  PASS: Rejected invalid token with HTTP {e.code}")
        finally:
            server.stop()

    def test_timeout_returns_none(self):
        """wait_for_cookies should return None on timeout."""
        from fluentytdl.auth.server import LocalCookieServer

        server = LocalCookieServer()
        server.start()

        result = server.wait_for_cookies(timeout=0.5)
        server.stop()

        assert result is None, "Should return None on timeout"
        print("  PASS: Timeout correctly returns None")


# ==================== ExtensionGenerator Tests ====================


class TestExtensionGenerator:
    """Tests for ExtensionGenerator."""

    def test_generate_creates_files(self):
        """generate() should create manifest.json and background.js."""
        from fluentytdl.auth.extension_gen import ExtensionGenerator

        gen = ExtensionGenerator()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "ext"
            gen.generate(output_dir, receiver_port=12345, auth_token="test_token_abc")

            manifest_path = output_dir / "manifest.json"
            bg_path = output_dir / "background.js"

            assert manifest_path.exists(), "manifest.json should be created"
            assert bg_path.exists(), "background.js should be created"
            print("  PASS: Both manifest.json and background.js created")

    def test_manifest_content(self):
        """manifest.json should have correct permissions and structure."""
        from fluentytdl.auth.extension_gen import ExtensionGenerator

        gen = ExtensionGenerator()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "ext"
            gen.generate(output_dir, receiver_port=12345)

            manifest = json.loads((output_dir / "manifest.json").read_text())

            assert manifest["manifest_version"] == 3
            assert "cookies" in manifest["permissions"]
            assert "notifications" in manifest["permissions"]
            assert "*://*.youtube.com/*" in manifest["host_permissions"]
            assert "*://accounts.google.com/*" in manifest["host_permissions"]
            # Ensure overly broad google.com is NOT present
            assert "*://*.google.com/*" not in manifest["host_permissions"]
            print("  PASS: Manifest has correct permissions")

    def test_background_js_contains_port_and_token(self):
        """background.js should contain the injected port and auth token."""
        from fluentytdl.auth.extension_gen import ExtensionGenerator

        gen = ExtensionGenerator()
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "ext"
            gen.generate(output_dir, receiver_port=54321, auth_token="my_secret_token")

            bg_content = (output_dir / "background.js").read_text()

            assert "54321" in bg_content, "Port should be injected"
            assert "my_secret_token" in bg_content, "Token should be injected"
            assert "X-Auth-Token" in bg_content, "Should use X-Auth-Token header"
            assert "LOGIN_INFO" in bg_content, "Should monitor LOGIN_INFO cookie"
            print("  PASS: background.js contains correct port, token, and trigger")


# ==================== DLEProvider Tests ====================


class TestDLEProvider:
    """Tests for DLEProvider."""

    pass

    def test_browser_paths_table_complete(self):
        """BROWSER_PATHS should include all expected browser types."""
        from fluentytdl.auth.providers.dle_provider import DLEProvider

        expected = {
            "edge",
            "chrome",
            "brave",
            "vivaldi",
            "opera",
            "opera_gx",
            "centbrowser",
            "chromium",
        }
        actual = set(DLEProvider.BROWSER_PATHS.keys())

        assert expected == actual, (
            f"Missing browsers: {expected - actual}, extra: {actual - expected}"
        )
        print(f"  PASS: BROWSER_PATHS covers all {len(expected)} browsers")

    def test_browser_resolution_fallback(self):
        """Should find at least one browser on a typical Windows system."""
        from fluentytdl.auth.providers.dle_provider import DLEProvider

        provider = DLEProvider()

        # Try all types
        found_any = False
        for bt in ["edge", "chrome", "brave", "vivaldi", "opera"]:
            result = provider._resolve_browser(bt)
            if result and result.exists():
                print(f"  INFO: Found {bt} at {result}")
                found_any = True

        if found_any:
            print("  PASS: At least one browser found via auto-detection")
        else:
            print("  WARN: No browser found (expected in CI/sandbox)")

    def test_cookies_to_netscape_format(self):
        """cookies_to_netscape should produce valid Netscape format."""
        from fluentytdl.auth.providers.dle_provider import DLEProvider

        cookies = [
            {
                "domain": ".youtube.com",
                "name": "LOGIN_INFO",
                "value": "testvalue123",
                "path": "/",
                "secure": True,
                "expirationDate": 1700000000,
            },
            {
                "domain": "accounts.google.com",
                "name": "SID",
                "value": "sid_value",
                "path": "/",
                "secure": False,
            },
        ]

        result = DLEProvider.cookies_to_netscape(cookies)
        lines = result.strip().split("\n")

        # Header lines
        assert lines[0].startswith("# Netscape"), "Should have Netscape header"

        # Cookie lines
        cookie_lines = [line for line in lines if not line.startswith("#") and line.strip()]
        assert len(cookie_lines) == 2, f"Expected 2 cookie lines, got {len(cookie_lines)}"

        # First cookie
        parts = cookie_lines[0].split("\t")
        assert len(parts) == 7, f"Netscape format needs 7 tab-separated fields, got {len(parts)}"
        assert parts[0] == ".youtube.com"
        assert parts[1] == "TRUE"  # domain starts with .
        assert parts[3] == "TRUE"  # secure
        assert parts[4] == "1700000000"  # expiration
        assert parts[5] == "LOGIN_INFO"
        assert parts[6] == "testvalue123"

        # Second cookie
        parts2 = cookie_lines[1].split("\t")
        assert parts2[0] == "accounts.google.com"
        assert parts2[1] == "FALSE"  # no leading dot
        assert parts2[3] == "FALSE"  # not secure
        assert parts2[4] == "0"  # no expiration provided

        print(f"  PASS: Netscape format output is valid ({len(cookie_lines)} cookies)")

    def test_cookies_to_netscape_empty(self):
        """cookies_to_netscape should handle empty list."""
        from fluentytdl.auth.providers.dle_provider import DLEProvider

        result = DLEProvider.cookies_to_netscape([])
        assert "# Netscape" in result
        # Should only have header lines, no data
        data_lines = [
            line for line in result.strip().split("\n") if not line.startswith("#") and line.strip()
        ]
        assert len(data_lines) == 0
        print("  PASS: Empty cookie list produces valid header-only output")


# ==================== Integration Test ====================


class TestServerExtensionIntegration:
    """Integration test: server + extension generator."""

    def test_server_token_flows_to_extension(self):
        """Auth token generated by server should be injected into extension."""
        from fluentytdl.auth.extension_gen import ExtensionGenerator
        from fluentytdl.auth.server import LocalCookieServer

        server = LocalCookieServer()
        port = server.start()
        token = server.auth_token

        gen = ExtensionGenerator()
        with tempfile.TemporaryDirectory() as tmpdir:
            ext_dir = Path(tmpdir) / "ext"
            gen.generate(ext_dir, port, auth_token=token)

            bg_content = (ext_dir / "background.js").read_text()
            assert token in bg_content, "Server token should be in background.js"
            assert str(port) in bg_content, "Server port should be in background.js"

        server.stop()
        print(f"  PASS: Token '{token[:8]}...' flows from server to extension")


# ==================== Runner ====================


def run_all_tests():
    """Run all test classes and methods."""
    test_classes = [
        TestLocalCookieServer,
        TestExtensionGenerator,
        TestDLEProvider,
        TestServerExtensionIntegration,
    ]

    total = 0
    passed = 0
    failed = 0

    for cls in test_classes:
        print(f"\n{'=' * 60}")
        print(f"  {cls.__name__}")
        print(f"{'=' * 60}")

        instance = cls()
        for name in sorted(dir(instance)):
            if name.startswith("test_"):
                total += 1
                try:
                    getattr(instance, name)()
                    passed += 1
                except Exception as e:
                    failed += 1
                    print(f"  FAIL: {name}: {e}")

    print(f"\n{'=' * 60}")
    print(f"  Results: {passed}/{total} passed, {failed} failed")
    print(f"{'=' * 60}")

    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
