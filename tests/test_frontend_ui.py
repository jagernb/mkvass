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

    def test_embed_section_has_subtitle_mode_default_output_and_pgs_controls(self):
        self.assertIn('id="subtitleMode"', self.html)
        self.assertIn('id="useDefaultOutputDir"', self.html)
        self.assertIn('id="pgsResolutionMode"', self.html)
        self.assertIn('id="pgsResolution"', self.html)
        self.assertIn('id="pgsFramerate"', self.html)
        self.assertIn("probe.default_output_dir", self.html)
        self.assertIn("probe.pgs_mode_available", self.html)
        self.assertIn("probe.pgs_defaults", self.html)
        self.assertIn("probe.video_dimensions", self.html)

    def test_embed_request_sends_subtitle_mode_output_dir_flag_and_pgs_options(self):
        self.assertIn("subtitle_mode: document.getElementById('subtitleMode').value", self.html)
        self.assertIn("use_default_output_dir: document.getElementById('useDefaultOutputDir').checked", self.html)
        self.assertIn("pgs_options:", self.html)
        self.assertIn("resolution_mode: document.getElementById('pgsResolutionMode').value", self.html)
        self.assertIn("resolution: document.getElementById('pgsResolution').value", self.html)
        self.assertIn("framerate: document.getElementById('pgsFramerate').value", self.html)

    def test_embed_settings_state_updates_with_mode_changes(self):
        self.assertIn("function updateEmbedSettingsState()", self.html)
        self.assertIn("pgsSettingsPanel.classList.toggle('hidden', !pgsEnabled)", self.html)
        self.assertIn("pgsResolution.disabled = !pgsEnabled || pgsResolutionMode.value !== 'custom'", self.html)
        self.assertIn("document.getElementById('subtitleMode').onchange = updateEmbedSettingsState", self.html)
        self.assertIn("document.getElementById('pgsResolutionMode').onchange = updateEmbedSettingsState", self.html)


if __name__ == "__main__":
    unittest.main()
