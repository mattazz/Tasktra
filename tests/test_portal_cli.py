"""The portal CLI binds before opening a browser and closes on interruption."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from tasktra.cli import main


class PortalCliTests(unittest.TestCase):
    def test_launch_reports_actual_port_and_closes_on_interrupt(self):
        server = MagicMock()
        server.__enter__.return_value = server
        server.server_address = ("127.0.0.1", 43210)
        server.serve_forever.side_effect = KeyboardInterrupt
        stdout = io.StringIO()
        with patch("tasktra.portal.make_portal_server", return_value=server) as factory, \
                patch("webbrowser.open", return_value=True) as browser, redirect_stdout(stdout):
            code = main(["portal", "--root", ".", "--port", "0", "--open"])
        self.assertEqual(code, 0)
        factory.assert_called_once_with(Path(".").resolve(), port=0)
        self.assertEqual(json.loads(stdout.getvalue())["url"], "http://127.0.0.1:43210/")
        browser.assert_called_once_with("http://127.0.0.1:43210/")
        server.__exit__.assert_called_once()

    def test_browser_is_opt_in_and_bind_failure_is_actionable(self):
        stderr = io.StringIO()
        with patch("tasktra.portal.make_portal_server", side_effect=OSError("Port is occupied")), \
                patch("webbrowser.open") as browser, redirect_stderr(stderr):
            code = main(["portal"])
        self.assertEqual(code, 2)
        self.assertIn("Port is occupied", json.loads(stderr.getvalue())["error"])
        browser.assert_not_called()

    def test_browser_failure_keeps_the_portal_available(self):
        server = MagicMock()
        server.__enter__.return_value = server
        server.server_address = ("127.0.0.1", 8765)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("tasktra.portal.make_portal_server", return_value=server), \
                patch("webbrowser.open", return_value=False), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(main(["portal", "--open"]), 0)
        server.serve_forever.assert_called_once()
        self.assertIn("http://127.0.0.1:8765/", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
