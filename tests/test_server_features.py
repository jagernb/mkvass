import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class SubtitleToolServerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.media_dir = Path(self.tempdir.name)
        self.video_rel = "movies/demo.mkv"
        self.video_path = self.media_dir / self.video_rel
        self.video_path.parent.mkdir(parents=True, exist_ok=True)
        self.video_path.write_bytes(b"video")

        import app.server as server

        self.server = server
        self.original_media_dir = server.MEDIA_DIR
        self.original_tmp_root = getattr(server, "TMP_SUBTITLE_ROOT", None)
        self.original_default_output_dir = getattr(server, "DEFAULT_OUTPUT_DIR", "")
        self.original_ass_to_pgs_cmd = getattr(server, "ASS_TO_PGS_CMD", "")
        self.original_ass_to_pgs_font_dir = getattr(server, "ASS_TO_PGS_FONT_DIR", "")
        self.original_ass_to_pgs_framerate = getattr(server, "ASS_TO_PGS_FRAMERATE", "")
        self.original_ass_to_pgs_resolution = getattr(server, "ASS_TO_PGS_RESOLUTION", "")
        server.MEDIA_DIR = self.media_dir.resolve()
        server.TMP_SUBTITLE_ROOT = server.MEDIA_DIR / ".tmp_subtitles"
        server.DEFAULT_OUTPUT_DIR = ""
        server.ASS_TO_PGS_CMD = ""
        server.ASS_TO_PGS_FONT_DIR = str(server.MEDIA_DIR / "fonts")
        server.ASS_TO_PGS_FRAMERATE = "23.976"
        server.ASS_TO_PGS_RESOLUTION = "1080p"
        Path(server.ASS_TO_PGS_FONT_DIR).mkdir(parents=True, exist_ok=True)
        server.app.config["TESTING"] = True
        self.client = server.app.test_client()

    def tearDown(self):
        self.server.MEDIA_DIR = self.original_media_dir
        if self.original_tmp_root is not None:
            self.server.TMP_SUBTITLE_ROOT = self.original_tmp_root
        self.server.DEFAULT_OUTPUT_DIR = self.original_default_output_dir
        self.server.ASS_TO_PGS_CMD = self.original_ass_to_pgs_cmd
        self.server.ASS_TO_PGS_FONT_DIR = self.original_ass_to_pgs_font_dir
        self.server.ASS_TO_PGS_FRAMERATE = self.original_ass_to_pgs_framerate
        self.server.ASS_TO_PGS_RESOLUTION = self.original_ass_to_pgs_resolution
        self.tempdir.cleanup()

    def test_download_returns_attachment_for_extracted_subtitle(self):
        subtitle_rel = "movies/demo.track2.srt"
        subtitle_path = self.media_dir / subtitle_rel
        subtitle_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")

        response = self.client.get(f"/api/download?path={subtitle_rel}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, subtitle_path.read_bytes())
        self.assertIn("attachment", response.headers.get("Content-Disposition", ""))
        self.assertIn("demo.track2.srt", response.headers.get("Content-Disposition", ""))

    def test_probe_includes_uploaded_subtitles_and_embed_capabilities(self):
        uploaded_dir = self.media_dir / ".tmp_subtitles" / "movies" / "demo.mkv"
        uploaded_dir.mkdir(parents=True, exist_ok=True)
        uploaded_file = uploaded_dir / "demo-upload.srt"
        uploaded_file.write_text("uploaded", encoding="utf-8")
        self.server.DEFAULT_OUTPUT_DIR = "output"
        self.server.ASS_TO_PGS_CMD = "missing-tool"

        with patch.object(self.server.subprocess, "check_output", return_value=b'{"streams": [], "format": {}}'):
            response = self.client.get(f"/api/probe?path={self.video_rel}")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        uploaded = payload.get("uploaded_subtitles")
        self.assertIsInstance(uploaded, list)
        self.assertEqual(len(uploaded), 1)
        self.assertEqual(uploaded[0]["name"], "demo-upload.srt")
        self.assertEqual(uploaded[0]["role"], "subtitle")
        self.assertEqual(uploaded[0]["path"], ".tmp_subtitles/movies/demo.mkv/demo-upload.srt")
        self.assertEqual(payload["default_output_dir"], "output")
        self.assertFalse(payload["pgs_mode_available"])

    def test_extract_returns_download_url(self):
        with patch.object(self.server.subprocess, "check_output", return_value=b""):
            response = self.client.post(
                "/api/extract",
                json={"path": self.video_rel, "stream_index": 2, "codec": "subrip"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["output"], "movies/demo.track2.srt")
        self.assertEqual(payload["download_url"], "/api/download?path=movies%2Fdemo.track2.srt")

    def test_upload_subtitle_saves_file_in_video_temp_directory(self):
        response = self.client.post(
            "/api/upload-subtitle",
            data={
                "video": self.video_rel,
                "file": (io.BytesIO(b"1\n00:00:00,000 --> 00:00:01,000\nHi\n"), "caption.zh.srt"),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        saved_path = self.media_dir / payload["path"]
        self.assertTrue(saved_path.is_file())
        self.assertEqual(saved_path.read_text(encoding="utf-8"), "1\n00:00:00,000 --> 00:00:01,000\nHi\n")
        self.assertEqual(payload["path"], ".tmp_subtitles/movies/demo.mkv/caption.zh.srt")

    def test_uploads_are_isolated_by_full_video_filename(self):
        other_video_rel = "movies/demo.mp4"
        other_video_path = self.media_dir / other_video_rel
        other_video_path.write_bytes(b"other-video")

        first = self.client.post(
            "/api/upload-subtitle",
            data={
                "video": self.video_rel,
                "file": (io.BytesIO(b"first"), "caption.srt"),
            },
            content_type="multipart/form-data",
        )
        second = self.client.post(
            "/api/upload-subtitle",
            data={
                "video": other_video_rel,
                "file": (io.BytesIO(b"second"), "caption.srt"),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        first_path = first.get_json()["path"]
        second_path = second.get_json()["path"]
        self.assertNotEqual(first_path, second_path)
        self.assertIn("demo.mkv", first_path)
        self.assertIn("demo.mp4", second_path)

    def test_extract_rejects_out_name_with_parent_directory(self):
        response = self.client.post(
            "/api/extract",
            json={
                "path": self.video_rel,
                "stream_index": 2,
                "codec": "subrip",
                "out_name": "../other-dir/out.srt",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "invalid output name")

    def test_embed_removes_uploaded_temp_subtitles_after_success(self):
        uploaded_dir = self.media_dir / ".tmp_subtitles" / "movies" / "demo.mkv"
        uploaded_dir.mkdir(parents=True, exist_ok=True)
        uploaded_file = uploaded_dir / "caption.zh.srt"
        uploaded_file.write_text("uploaded", encoding="utf-8")

        def fake_check_output(cmd, stderr=None, timeout=None):
            if cmd[:2] == ["ffprobe", "-v"]:
                return b'{"streams": []}'
            return b""

        with patch.object(self.server.subprocess, "check_output", side_effect=fake_check_output):
            response = self.client.post(
                "/api/embed",
                json={
                    "video": self.video_rel,
                    "subtitles": [
                        {
                            "path": ".tmp_subtitles/movies/demo.mkv/caption.zh.srt",
                            "language": "chi",
                            "title": "Chinese",
                            "default": True,
                        }
                    ],
                    "keep_existing": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(uploaded_file.exists())

    def test_embed_rejects_invalid_output_name(self):
        subtitle_rel = "movies/demo.zh.srt"
        subtitle_path = self.media_dir / subtitle_rel
        subtitle_path.write_text("subtitle", encoding="utf-8")

        response = self.client.post(
            "/api/embed",
            json={
                "video": self.video_rel,
                "subtitles": [
                    {"path": subtitle_rel, "language": "chi", "title": "Chinese", "default": False}
                ],
                "out_name": "../other-dir/out.mkv",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "invalid output name")

    def test_embed_uses_default_output_dir_when_enabled(self):
        subtitle_rel = "movies/demo.zh.srt"
        subtitle_path = self.media_dir / subtitle_rel
        subtitle_path.write_text("subtitle", encoding="utf-8")
        self.server.DEFAULT_OUTPUT_DIR = "output/final"

        captured = {}

        def fake_check_output(cmd, stderr=None, timeout=None):
            if cmd[:2] == ["ffprobe", "-v"]:
                return b'{"streams": []}'
            captured["cmd"] = cmd
            return b""

        with patch.object(self.server.subprocess, "check_output", side_effect=fake_check_output):
            response = self.client.post(
                "/api/embed",
                json={
                    "video": self.video_rel,
                    "subtitles": [
                        {"path": subtitle_rel, "language": "chi", "title": "Chinese", "default": False}
                    ],
                    "use_default_output_dir": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["output"], "output/final/demo.muxed.mkv")
        self.assertEqual(payload["output_dir"], "output/final")
        self.assertTrue(payload["used_default_output_dir"])
        self.assertEqual(captured["cmd"][-1], str((self.media_dir / "output/final/demo.muxed.mkv").resolve()))

    def test_embed_rejects_default_output_dir_when_not_configured(self):
        subtitle_rel = "movies/demo.zh.srt"
        subtitle_path = self.media_dir / subtitle_rel
        subtitle_path.write_text("subtitle", encoding="utf-8")

        response = self.client.post(
            "/api/embed",
            json={
                "video": self.video_rel,
                "subtitles": [
                    {"path": subtitle_rel, "language": "chi", "title": "Chinese", "default": False}
                ],
                "use_default_output_dir": True,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "default output dir not configured")

    def test_embed_rejects_pgs_mode_when_tool_not_configured(self):
        subtitle_rel = "movies/demo.ass"
        subtitle_path = self.media_dir / subtitle_rel
        subtitle_path.write_text("ass subtitle", encoding="utf-8")

        response = self.client.post(
            "/api/embed",
            json={
                "video": self.video_rel,
                "subtitles": [
                    {"path": subtitle_rel, "language": "chi", "title": "Chinese", "default": False}
                ],
                "subtitle_mode": "pgs_auto",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "ass_to_pgs tool not configured")


if __name__ == "__main__":
    unittest.main()
