import re
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")


class DocumentationTests(unittest.TestCase):
    def test_local_markdown_links_resolve(self):
        failures = []
        documents = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
        for document in documents:
            for target in LINK.findall(document.read_text(encoding="utf-8")):
                if "://" in target or target.startswith("#"):
                    continue
                destination = target.split("#", 1)[0]
                if destination and not (document.parent / destination).resolve().exists():
                    failures.append(f"{document.relative_to(ROOT)} -> {target}")
        self.assertEqual(failures, [])
