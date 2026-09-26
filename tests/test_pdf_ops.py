from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pymupdf

from mcp4chatgpt import pdf_ops


class PdfOpsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = SimpleNamespace(allowed_roots=[self.root])
        self.source = self.root / "source.pdf"
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Secret SECRET Straße", fontsize=11)
        doc.save(self.source)
        doc.close()

    def tearDown(self):
        self.temp.cleanup()

    def _text(self, path):
        with pymupdf.open(path) as doc:
            return " ".join(page.get_text() for page in doc)

    def test_redaction_respects_case_and_maps_unicode_casefold(self):
        original_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = pdf_ops.redact_text(self.config, str(self.source), "Secret")
        text = self._text(result["output_path"])
        self.assertNotIn("Secret", text)
        self.assertIn("SECRET", text)
        self.assertIn("Straße", text)
        folded = pdf_ops.redact_text(
            self.config, str(self.source), "STRASSE", case_sensitive=False,
            output_path=str(self.root / "folded.pdf"),
        )
        self.assertNotIn("Straße", self._text(folded["output_path"]))
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), original_hash)

    def test_replacement_overflow_and_no_match_publish_nothing(self):
        with self.assertRaisesRegex(ValueError, "does not fit"):
            pdf_ops.redact_text(
                self.config, str(self.source), "Secret",
                replacement="THIS REPLACEMENT IS MUCH TOO LONG FOR THE ORIGINAL AREA",
                output_path=str(self.root / "overflow.pdf"),
            )
        self.assertFalse((self.root / "overflow.pdf").exists())
        with self.assertRaisesRegex(ValueError, "No matches"):
            pdf_ops.redact_text(self.config, str(self.source), "absent", output_path=str(self.root / "none.pdf"))
        self.assertFalse((self.root / "none.pdf").exists())

    def test_output_conflict_preserves_existing_file(self):
        destination = self.root / "existing.pdf"
        destination.write_bytes(b"keep")
        with self.assertRaisesRegex(ValueError, "already exists"):
            pdf_ops.insert_text(self.config, str(self.source), 1, 72, 100, "new", output_path=str(destination))
        self.assertEqual(destination.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
