import unittest
from pathlib import Path


class FrontendUiTests(unittest.TestCase):
    def setUp(self):
        self.html = Path("app/static/index.html").read_text(encoding="utf-8")

    def test_extract_result_uses_download_url(self):
        self.assertIn("res.download_url", self.html)
        self.assertIn("下载字幕", self.html)

    def test_embed_section_has_upload_controls(self):
        self.assertIn('id="uploadSubtitleFile"', self.html)
        self.assertIn('id="uploadSubtitleBtn"', self.html)
        self.assertIn("/api/upload-subtitle", self.html)

    def test_render_detail_uses_uploaded_subtitles(self):
        self.assertIn("probe.uploaded_subtitles", self.html)
        self.assertIn("source === 'upload'", self.html)


if __name__ == "__main__":
    unittest.main()
