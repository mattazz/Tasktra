"""Exercise the portal graph's relationship and bounding rules without a browser."""
from pathlib import Path
import shutil
import subprocess
import unittest


class RelationshipMapModelTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is needed only for the portal graph regression checks")
    def test_recorded_relationships_and_display_bounds(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "portal_relationship_map.test.cjs")],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("relationship map checks passed", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed only for the portal graph regression checks")
    def test_offline_graph_engine_assets(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [shutil.which("node"), str(root / "tests" / "portal_graph_vendor.test.cjs")],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("vendored graph engine checks passed", result.stdout)
