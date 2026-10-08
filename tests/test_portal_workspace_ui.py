from pathlib import Path
import shutil
import subprocess
import unittest


class PortalWorkspaceUiTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for workspace model checks")
    def test_workspace_model(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([shutil.which("node"), str(root / "tests" / "portal_workspace.test.cjs")], cwd=root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("portal workspace checks passed", result.stdout)
