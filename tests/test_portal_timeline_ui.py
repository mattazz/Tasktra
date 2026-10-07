"""Exercise the offline portal execution timeline view model."""
from pathlib import Path
import shutil
import subprocess
import unittest


class PortalTimelineUiTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for portal timeline model checks")
    def test_execution_timeline_model(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which("node"), str(root / "tests" / "portal_timeline.test.cjs")], cwd=root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("portal timeline checks passed", result.stdout)
