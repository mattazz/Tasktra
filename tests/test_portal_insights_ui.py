"""Exercise the offline portal attention and diagnostics view model."""
from pathlib import Path
import shutil
import subprocess
import unittest


class PortalInsightsUiTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for portal insight model checks")
    def test_attention_and_diagnostics_model(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "portal_insights.test.cjs")],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("portal insights checks passed", result.stdout)
