import unittest
from pathlib import Path


class FrontendUiTests(unittest.TestCase):
    def setUp(self):
        self.html = Path("app/static/index.html").read_text(encoding="utf-8")

    def test_internal_subtitle_metadata_fields_are_editable(self):
        self.assertIn('class="track-subtitle-lang"', self.html)
        self.assertIn('class="track-subtitle-title"', self.html)
        self.assertIn("value=\"${escapeHtml(s.language || 'und')}\"", self.html)
        self.assertIn("value=\"${escapeHtml(s.title || '')}\"", self.html)

    def test_embed_request_sends_internal_subtitle_metadata(self):
        self.assertIn("language: row.querySelector('.track-subtitle-lang').value || 'und'", self.html)
        self.assertIn("title: row.querySelector('.track-subtitle-title').value || ''", self.html)
        self.assertIn("order: Number(row.querySelector('.track-subtitle-order').value || 0)", self.html)
        self.assertIn("default: row.querySelector('.track-subtitle-default').checked", self.html)


if __name__ == "__main__":
    unittest.main()
