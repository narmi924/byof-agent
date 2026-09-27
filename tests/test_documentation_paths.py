"""Documentation links resolve and the published documents stay in English."""

import re
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = [
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "database/README.md",
    ROOT / "database/data_dictionary.md",
    ROOT / "database/er_diagram.md",
    ROOT / "data/development/assembly-branch.md",
    *sorted((ROOT / "docs").rglob("*.md")),
]
# Chinese ideographs; seed data may keep them, published documents may not.
CJK = "[" + chr(0x4E00) + "-" + chr(0x9FFF) + "]"


class DocumentationPathTests(unittest.TestCase):
    def test_markdown_local_targets_exist(self):
        for document in DOCUMENTS:
            text = document.read_text(encoding="utf-8")
            for link in re.findall(r"\]\(([^)]+)\)|src=\"([^\"]+)\"", text):
                target = next(part for part in link if part)
                url = urlsplit(target)
                if url.scheme or url.netloc or not url.path:
                    continue
                with self.subTest(document=str(document.relative_to(ROOT)), link=target):
                    self.assertTrue((document.parent / unquote(url.path)).exists())

    def test_documents_are_english(self):
        for document in DOCUMENTS:
            with self.subTest(document=str(document.relative_to(ROOT))):
                self.assertIsNone(re.search(CJK, document.read_text(encoding="utf-8")))
