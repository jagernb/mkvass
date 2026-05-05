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

    def test_external_subtitle_candidates_match_video_filename_and_episode(self):
        self.assertIn("function mostlyMatchesVideoSubtitle(videoEntry, subtitleEntry)", self.html)
        self.assertIn("function episodeTokens(name)", self.html)
        self.assertIn(".filter(s => mostlyMatchesVideoSubtitle(entry, s))", self.html)
        self.assertIn("const uploadedSubs = probe.uploaded_subtitles || []", self.html)
        self.assertIn("`s${match[1].padStart(2, '0')}e${match[2].padStart(2, '0')}`", self.html)

    def test_external_subtitle_metadata_presets_keep_text_inputs(self):
        self.assertIn('type="text" list="embedLangPresets"', self.html)
        self.assertIn('type="text" list="embedTitlePresets"', self.html)
        self.assertIn('id="embedLangPresets"', self.html)
        self.assertIn('id="embedTitlePresets"', self.html)
        self.assertIn('value="zhi"', self.html)
        self.assertIn('value="双语简英"', self.html)
        self.assertIn('value="简体中文"', self.html)
        self.assertIn('value=" " label="空白"', self.html)

    def test_embed_request_sends_external_metadata_raw_values(self):
        self.assertIn("language: row.querySelector('.embed-lang').value", self.html)
        self.assertIn("title: row.querySelector('.embed-title').value", self.html)
        self.assertNotIn("language: row.querySelector('.embed-lang').value || 'und'", self.html)


if __name__ == "__main__":
    unittest.main()
