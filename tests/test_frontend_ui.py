import unittest
from pathlib import Path


class FrontendUiTests(unittest.TestCase):
    def setUp(self):
        self.html = Path("app/static/index.html").read_text(encoding="utf-8")

    def test_extract_result_renders_dedicated_download_slot(self):
        self.assertIn("res.download_url", self.html)
        self.assertIn('class="extract-download-slot"', self.html)
        self.assertIn('class="download-btn"', self.html)
        self.assertIn("data-download-slot", self.html)

    def test_embed_section_has_upload_controls(self):
        self.assertIn('id="uploadSubtitleFile"', self.html)
        self.assertIn('id="uploadSubtitleBtn"', self.html)
        self.assertIn("/api/upload-subtitle", self.html)

    def test_render_detail_uses_uploaded_subtitles(self):
        self.assertIn("probe.uploaded_subtitles", self.html)
        self.assertIn("source === 'upload'", self.html)

    def test_extract_log_keeps_text_output(self):
        self.assertIn("已生成: ${res.output}", self.html)
        self.assertIn("${res.cmd}", self.html)


if __name__ == "__main__":
    unittest.main()
