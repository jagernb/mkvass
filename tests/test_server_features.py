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
        server.MEDIA_DIR = self.media_dir.resolve()
        server.TMP_SUBTITLE_ROOT = server.MEDIA_DIR / ".tmp_subtitles"
        server.DEFAULT_OUTPUT_DIR = ""
        server.app.config["TESTING"] = True
        self.client = server.app.test_client()

    def tearDown(self):
        self.server.MEDIA_DIR = self.original_media_dir
        if self.original_tmp_root is not None:
            self.server.TMP_SUBTITLE_ROOT = self.original_tmp_root
        self.server.DEFAULT_OUTPUT_DIR = self.original_default_output_dir
        self.tempdir.cleanup()

    def test_embed_sets_existing_subtitle_metadata_with_ffmpeg(self):
        subtitle_rel = "movies/demo.zh.srt"
        (self.media_dir / subtitle_rel).write_text("subtitle", encoding="utf-8")
        captured = {}

        def fake_check_output(cmd, stderr=None, timeout=None):
            if cmd[:2] == ["ffprobe", "-v"]:
                return b'{"streams": [{"index": 3, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng", "title": "Old"}}]}'
            captured["cmd"] = cmd
            return b""

        with patch.object(self.server.subprocess, "check_output", side_effect=fake_check_output):
            response = self.client.post(
                "/api/embed",
                json={
                    "video": self.video_rel,
                    "subtitles": [
                        {"path": subtitle_rel, "language": "chi", "title": "Chinese", "default": True, "order": 30}
                    ],
                    "tracks": {
                        "subtitle": [
                            {"source": "existing", "stream_index": 3, "keep": True, "language": "jpn", "title": "Japanese Internal", "order": 10, "default": False},
                            {"source": "external", "path": subtitle_rel, "keep": True, "language": "chi", "title": "Chinese", "order": 30, "default": True},
                        ],
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        cmd = captured["cmd"]
        self.assertIn("0:3", cmd)
        self.assertIn("language=jpn", cmd)
        self.assertIn("title=Japanese Internal", cmd)
        self.assertEqual(cmd[cmd.index("-disposition:s:0") + 1], "0")
        self.assertEqual(cmd[cmd.index("-disposition:s:1") + 1], "default")

    def test_embed_sets_existing_subtitle_metadata_with_mkvmerge(self):
        pgs_rel = "movies/demo.pgs"
        (self.media_dir / pgs_rel).write_bytes(b"pgs")
        captured = {}

        def fake_check_output(cmd, stderr=None, timeout=None):
            if cmd[:2] == ["ffprobe", "-v"]:
                return b'{"streams": [{"index": 5, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "eng", "title": "Old"}}]}'
            captured["cmd"] = cmd
            return b""

        with patch.object(self.server.subprocess, "check_output", side_effect=fake_check_output):
            with patch.object(self.server, "_resolve_mkvmerge_command", return_value="/usr/bin/mkvmerge"):
                response = self.client.post(
                    "/api/embed",
                    json={
                        "video": self.video_rel,
                        "subtitles": [
                            {"path": pgs_rel, "language": "chi", "title": "Chinese PGS", "default": False, "order": 30}
                        ],
                        "tracks": {
                            "subtitle": [
                                {"source": "existing", "stream_index": 5, "keep": True, "language": "jpn", "title": "Japanese PGS", "order": 10, "default": True},
                                {"source": "external", "path": pgs_rel, "keep": True, "language": "chi", "title": "Chinese PGS", "order": 30, "default": False},
                            ],
                        },
                    },
                )

        self.assertEqual(response.status_code, 200)
        cmd = captured["cmd"]
        self.assertIn("--language", cmd)
        self.assertIn("5:jpn", cmd)
        self.assertIn("--track-name", cmd)
        self.assertIn("5:Japanese PGS", cmd)
        self.assertIn("5:yes", cmd)
        self.assertIn("0:no", cmd)
        self.assertEqual(cmd[cmd.index("--track-order") + 1], "0:5,1:0")


if __name__ == "__main__":
    unittest.main()
