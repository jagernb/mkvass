"""字幕提取/封装 Web 服务。

- 浏览挂载目录下的视频与字幕文件
- 通过 ffprobe 查看视频内封字幕流
- 提取指定字幕流到 .srt / .ass
- 将外挂字幕以软字幕（mkv）形式封装进视频
"""
from __future__ import annotations

from collections import deque
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote

from flask import Flask, jsonify, request, send_file, send_from_directory
from werkzeug.utils import secure_filename

MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", "/media")).resolve()
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
APP_VERSION = (os.environ.get("APP_VERSION") or "dev").strip() or "dev"
BUILD_DATE = (os.environ.get("BUILD_DATE") or "").strip()
DEFAULT_OUTPUT_DIR = (os.environ.get("DEFAULT_OUTPUT_DIR") or "").strip()
PGS_CONVERTER_CMD = (os.environ.get("PGS_CONVERTER_CMD") or os.environ.get("ASS_TO_PGS_CMD") or "mkvtool").strip()
MKVMERGE_CMD = (os.environ.get("MKVMERGE_CMD") or "mkvmerge").strip()
PGS_FONT_DIR = (os.environ.get("PGS_FONT_DIR") or os.environ.get("ASS_TO_PGS_FONT_DIR") or "/usr/share/fonts/truetype/dejavu").strip()
PGS_FRAMERATE = (os.environ.get("PGS_FRAMERATE") or os.environ.get("ASS_TO_PGS_FRAMERATE") or "23.976").strip() or "23.976"
PGS_RESOLUTION = (os.environ.get("PGS_RESOLUTION") or os.environ.get("ASS_TO_PGS_RESOLUTION") or "1920*1080").strip() or "1920*1080"

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".ts", ".m4v", ".flv", ".wmv"}
TEXT_SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
PGS_SUBTITLE_EXTS = {".pgs", ".sup"}
SUBTITLE_EXTS = TEXT_SUBTITLE_EXTS | PGS_SUBTITLE_EXTS
DOWNLOADABLE_SUBTITLE_EXTS = SUBTITLE_EXTS
TMP_SUBTITLE_ROOT = MEDIA_DIR / ".tmp_subtitles"
TASK_DIAGNOSTIC_ROOT = TMP_SUBTITLE_ROOT / "_diagnostics"
TASKS: dict[str, dict] = {}
TASK_QUEUE: deque[str] = deque()
TASK_LOCK = threading.RLock()
TASK_EVENT = threading.Event()
TASK_WORKER_STARTED = False
FINAL_TASK_STATUSES = {"canceled", "succeeded", "failed"}

app = Flask(__name__, static_folder="static", static_url_path="/static")


class PgsConversionError(RuntimeError):
    def __init__(self, message: str, *, cmd_text: str = "", output: str = ""):
        super().__init__(message)
        self.cmd_text = cmd_text
        self.output = output


def safe_path(rel: str) -> Path:
    rel = (rel or "").lstrip("/\\")
    target = (MEDIA_DIR / rel).resolve()
    if MEDIA_DIR != target and MEDIA_DIR not in target.parents:
        raise ValueError("path escapes MEDIA_DIR")
    return target


def rel_to_media(p: Path) -> str:
    return str(p.relative_to(MEDIA_DIR)).replace("\\", "/")


def _video_temp_dir(video: Path) -> Path:
    rel_parent = video.relative_to(MEDIA_DIR).parent
    safe_name = secure_filename(video.name)
    return (TMP_SUBTITLE_ROOT / rel_parent / safe_name).resolve()


def _validate_output_name(name: str | None, default_name: str) -> str:
    if not name:
        return default_name
    candidate = Path(name)
    if candidate.name != name or candidate.parent != Path("."):
        raise ValueError("invalid output name")
    return name


def _list_uploaded_subtitles(video: Path) -> list[dict]:
    temp_dir = _video_temp_dir(video)
    if not temp_dir.exists() or not temp_dir.is_dir():
        return []

    entries = []
    for child in sorted(temp_dir.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_file() or child.suffix.lower() not in SUBTITLE_EXTS:
            continue
        entries.append({
            "name": child.name,
            "path": rel_to_media(child),
            "kind": "file",
            "role": "subtitle",
            "size": child.stat().st_size,
            "source": "upload",
        })
    return entries


def _is_temp_subtitle(path: Path, video: Path) -> bool:
    temp_dir = _video_temp_dir(video)
    return path == temp_dir or temp_dir in path.parents


def _unique_file_path(parent: Path, filename: str) -> Path:
    candidate = parent / filename
    if not candidate.exists():
        return candidate

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    index = 1
    while True:
        candidate = parent / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def _resolve_pgs_converter_command() -> str | None:
    if not PGS_CONVERTER_CMD:
        return None
    candidate = Path(PGS_CONVERTER_CMD)
    if candidate.is_file():
        return str(candidate.resolve())
    return shutil.which(PGS_CONVERTER_CMD)


def _resolve_mkvmerge_command() -> str | None:
    if not MKVMERGE_CMD:
        return None
    candidate = Path(MKVMERGE_CMD)
    if candidate.is_file():
        return str(candidate.resolve())
    return shutil.which(MKVMERGE_CMD)


def _pgs_converter_status() -> dict:
    tool = _resolve_pgs_converter_command()
    font_dir = Path(PGS_FONT_DIR) if PGS_FONT_DIR else None
    missing = []
    hints = []

    if not tool:
        missing.append("converter")
        configured = PGS_CONVERTER_CMD or "未配置"
        hints.append(f"未找到 PGS 转换器：{configured}")
    if font_dir is None or not font_dir.is_dir():
        missing.append("font_dir")
        configured = PGS_FONT_DIR or "未配置"
        hints.append(f"PGS 字体目录不存在：{configured}")

    return {
        "available": not missing,
        "hint": "；".join(hints),
        "missing": missing,
    }


def _pgs_converter_available() -> bool:
    return bool(_pgs_converter_status()["available"])


def _configured_default_output_dir() -> Path | None:
    if not DEFAULT_OUTPUT_DIR:
        return None
    if Path(DEFAULT_OUTPUT_DIR).is_absolute():
        raise ValueError("invalid default output dir")
    target = safe_path(DEFAULT_OUTPUT_DIR)
    if target.exists() and not target.is_dir():
        raise ValueError("invalid default output dir")
    return target


def _configured_default_output_dir_rel() -> str | None:
    try:
        target = _configured_default_output_dir()
    except ValueError:
        return None
    return rel_to_media(target) if target is not None else None


def _resolve_embed_output_path(video: Path, out_name: str | None, use_default_output_dir: bool) -> Path:
    out_name = _validate_output_name(out_name, f"{video.stem}.muxed.mkv")
    if not out_name.lower().endswith(".mkv"):
        out_name += ".mkv"

    base_dir = video.parent
    if use_default_output_dir:
        default_dir = _configured_default_output_dir()
        if default_dir is None:
            raise ValueError("default output dir not configured")
        default_dir.mkdir(parents=True, exist_ok=True)
        base_dir = default_dir

    out_path = (base_dir / out_name).resolve()
    if MEDIA_DIR not in out_path.parents and out_path != MEDIA_DIR:
        raise ValueError("output path invalid")
    return out_path


def _count_subtitle_streams(video: Path) -> int:
    probe = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "s",
        "-show_entries", "stream=index", "-of", "json", str(video),
    ], timeout=60)
    return len(json.loads(probe.decode()).get("streams", []))


def _probe_streams(video: Path) -> list[dict]:
    probe = subprocess.check_output([
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_streams",
        str(video),
    ], stderr=subprocess.STDOUT, timeout=60)
    return json.loads(probe.decode("utf-8", errors="ignore")).get("streams", [])


def _stream_track_info(stream: dict) -> dict:
    tags = stream.get("tags") or {}
    info = {
        "index": stream.get("index"),
        "codec": stream.get("codec_name"),
        "language": tags.get("language"),
        "title": tags.get("title"),
        "disposition": stream.get("disposition") or {},
    }
    if stream.get("codec_type") == "audio":
        info["channels"] = stream.get("channels")
        info["channel_layout"] = stream.get("channel_layout")
    return info


def _streams_by_type(streams: list[dict], codec_type: str) -> list[dict]:
    return [stream for stream in streams if stream.get("codec_type") == codec_type]


def _stream_index(stream: dict) -> int | None:
    index = stream.get("index")
    return index if isinstance(index, int) else None


def _format_frame_rate_value(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _parse_frame_rate(raw: str | None, source: str) -> dict | None:
    raw = (raw or "").strip()
    if not raw or raw == "0/0":
        return None
    if "/" in raw:
        numerator, denominator = raw.split("/", 1)
        try:
            value = float(numerator) / float(denominator)
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    else:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
    if value <= 0:
        return None
    display_value = _format_frame_rate_value(value)
    return {"raw": raw, "value": value, "display": f"{display_value} fps", "source": source}


def _normalize_pgs_framerate(framerate: str) -> str:
    parsed = _parse_frame_rate(framerate, "pgs_framerate")
    if parsed is None:
        raise ValueError("invalid pgs framerate")
    return _format_frame_rate_value(parsed["value"])


def _video_framerate(streams: list[dict]) -> dict | None:
    for stream in streams:
        if stream.get("codec_type") != "video":
            continue
        return (
            _parse_frame_rate(stream.get("avg_frame_rate"), "avg_frame_rate")
            or _parse_frame_rate(stream.get("r_frame_rate"), "r_frame_rate")
        )
    return None


def _video_dimensions(video: Path) -> tuple[int, int] | None:
    probe = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "json", str(video),
    ], timeout=60)
    streams = json.loads(probe.decode()).get("streams", [])
    if not streams:
        return None
    width = streams[0].get("width")
    height = streams[0].get("height")
    if not isinstance(width, int) or not isinstance(height, int):
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _pgs_defaults() -> dict:
    return {
        "resolution": PGS_RESOLUTION,
        "framerate": PGS_FRAMERATE,
        "font_dir": PGS_FONT_DIR,
        "resolution_mode": "video",
    }


def _normalize_embed_settings(body: dict) -> dict:
    settings = body.get("settings") if isinstance(body.get("settings"), dict) else {}
    output_settings = settings.get("output") if isinstance(settings.get("output"), dict) else {}
    pgs_settings = settings.get("pgs") if isinstance(settings.get("pgs"), dict) else {}
    raw_pgs_options = body.get("pgs_options") if isinstance(body.get("pgs_options"), dict) else {}

    use_default_output_dir = output_settings.get("use_default_output_dir")
    if use_default_output_dir is None:
        use_default_output_dir = body.get("use_default_output_dir", False)

    resolution_mode = pgs_settings.get("resolution_mode") or raw_pgs_options.get("resolution_mode") or "video"
    resolution = pgs_settings.get("resolution") or raw_pgs_options.get("resolution") or PGS_RESOLUTION
    framerate = pgs_settings.get("framerate") or raw_pgs_options.get("framerate") or PGS_FRAMERATE

    return {
        "use_default_output_dir": bool(use_default_output_dir),
        "pgs_options": {
            "resolution_mode": str(resolution_mode).strip() or "video",
            "resolution": str(resolution).strip() or PGS_RESOLUTION,
            "framerate": str(framerate).strip() or PGS_FRAMERATE,
        },
    }


def _validate_pgs_options(pgs_options: dict) -> dict:
    resolution_mode = (pgs_options.get("resolution_mode") or "video").strip().lower()
    if resolution_mode not in {"video", "custom"}:
        raise ValueError("invalid pgs resolution mode")

    resolution = (pgs_options.get("resolution") or PGS_RESOLUTION).strip()
    if resolution_mode == "custom":
        if not re.fullmatch(r"\d+\*\d+", resolution):
            raise ValueError("invalid pgs resolution")
        width, height = (int(part) for part in resolution.split("*", 1))
        if width <= 0 or height <= 0:
            raise ValueError("invalid pgs resolution")

    framerate = (pgs_options.get("framerate") or PGS_FRAMERATE).strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?", framerate):
        raise ValueError("invalid pgs framerate")
    framerate = _normalize_pgs_framerate(framerate)

    return {
        "resolution_mode": resolution_mode,
        "resolution": resolution,
        "framerate": framerate,
    }


def _standard_pgs_canvas(width: int, height: int) -> str:
    if width > 1920 or height > 1080:
        return "3840*2160"
    if width > 1280 or height > 720:
        return "1920*1080"
    return "1280*720"


def _derive_pgs_resolution(video: Path, pgs_options: dict) -> str:
    if pgs_options["resolution_mode"] == "custom":
        return pgs_options["resolution"]

    dims = _video_dimensions(video)
    if dims is None:
        return PGS_RESOLUTION
    width, height = dims
    return _standard_pgs_canvas(width, height)


def _convert_ass_to_pgs(subtitle_path: Path, *, resolution: str | None = None, framerate: str | None = None) -> tuple[Path, str, Path]:
    if subtitle_path.suffix.lower() not in {".ass", ".ssa"}:
        raise ValueError("pgs conversion only supports ass/ssa")

    tool = _resolve_pgs_converter_command()
    if not tool:
        raise RuntimeError("pgs converter not configured")

    font_dir = Path(PGS_FONT_DIR) if PGS_FONT_DIR else None
    if font_dir is None or not font_dir.is_dir():
        raise RuntimeError("pgs font dir not found")

    resolved_resolution = (resolution or PGS_RESOLUTION).strip() or PGS_RESOLUTION
    resolved_framerate = (framerate or PGS_FRAMERATE).strip() or PGS_FRAMERATE

    temp_root = TMP_SUBTITLE_ROOT / "_pgs"
    temp_root.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="pgs-", dir=temp_root))
    input_dir = work_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    input_path = input_dir / subtitle_path.name
    shutil.copy2(subtitle_path, input_path)

    cmd = [
        tool,
        "--enable-pgs-output",
        "--resolution",
        resolved_resolution,
        "--framerate",
        resolved_framerate,
        "subset",
        str(input_path),
        "--font-dir",
        str(font_dir),
        "--output-dir",
        str(work_dir),
    ]
    cmd_text = " ".join(shlex.quote(c) for c in cmd)

    try:
        subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=3600)
    except subprocess.CalledProcessError as exc:
        output = exc.output.decode(errors="ignore") if exc.output else ""
        raise PgsConversionError(output or "pgs conversion failed", cmd_text=cmd_text, output=output) from exc

    candidates = sorted(
        path
        for path in work_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in {".pgs", ".sup"}
    )
    if candidates:
        output_path = candidates[0]
    else:
        produced = sorted(str(path.relative_to(work_dir)) for path in work_dir.rglob("*") if path.is_file())
        detail = f": {', '.join(produced)}" if produced else ""
        raise RuntimeError(f"pgs converter did not produce a new .pgs or .sup file{detail}")

    return output_path, cmd_text, work_dir


def _subtitle_warning_for_existing_pgs(prepared_subs: list[dict]) -> list[str]:
    if any(prepared["path"].suffix.lower() in PGS_SUBTITLE_EXTS for prepared in prepared_subs):
        return ["已有 PGS/SUP 字幕会原样封装；尺寸设置仅用于 ASS/SSA 转 PGS。"]
    return []


def _parse_order(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _one_default(items: list[dict]) -> None:
    found = False
    for item in items:
        if item.get("default") and not found:
            found = True
        else:
            item["default"] = False


def _meta_for_existing_stream(stream: dict, override: dict | None = None) -> dict:
    override = override or {}
    tags = stream.get("tags") or {}
    disposition = stream.get("disposition") or {}
    return {
        "language": override.get("language") or tags.get("language") or "und",
        "title": override.get("title") if override.get("title") is not None else (tags.get("title") or ""),
        "default": bool(override.get("default", disposition.get("default") == 1)),
    }


def _external_track_settings(tracks: dict, fallback_subs: list[dict]) -> dict[str, dict]:
    settings: dict[str, dict] = {}
    for idx, sub in enumerate(fallback_subs):
        path = sub.get("path")
        if path:
            settings[path] = {**sub, "keep": True, "order": _parse_order(sub.get("order"), 1000 + idx)}
    if not isinstance(tracks, dict):
        return settings
    for idx, track in enumerate(tracks.get("subtitle") or []):
        if not isinstance(track, dict) or track.get("source") != "external":
            continue
        path = track.get("path")
        if not path:
            continue
        if not bool(track.get("keep", True)):
            settings.pop(path, None)
            continue
        settings[path] = {**settings.get(path, {}), **track, "keep": True, "order": _parse_order(track.get("order"), 1000 + idx)}
    return settings


def _build_mux_plan(video: Path, prepared_subs: list[dict], body: dict) -> dict:
    tracks = body.get("tracks") if isinstance(body.get("tracks"), dict) else None
    streams = _probe_streams(video)
    audio_streams = _streams_by_type(streams, "audio")
    subtitle_streams = _streams_by_type(streams, "subtitle")
    audio_by_index = {idx: stream for stream in audio_streams if (idx := _stream_index(stream)) is not None}
    subtitle_by_index = {idx: stream for stream in subtitle_streams if (idx := _stream_index(stream)) is not None}

    if tracks:
        audio_tracks = tracks.get("audio") or []
        existing_sub_tracks = [track for track in (tracks.get("subtitle") or []) if isinstance(track, dict) and track.get("source") == "existing"]
    else:
        audio_tracks = [{"stream_index": idx, "keep": True} for idx in audio_by_index]
        existing_sub_tracks = [
            {"stream_index": idx, "keep": bool(body.get("keep_existing", True))}
            for idx in subtitle_by_index
        ]

    audio_items = []
    for pos, track in enumerate(audio_tracks):
        if not isinstance(track, dict) or not bool(track.get("keep", True)):
            continue
        try:
            stream_index = int(track.get("stream_index"))
        except (TypeError, ValueError):
            raise ValueError("invalid audio track index")
        stream = audio_by_index.get(stream_index)
        if stream is None:
            raise ValueError(f"audio track not found: {stream_index}")
        audio_items.append({
            "source": "existing",
            "stream_index": stream_index,
            "order": _parse_order(track.get("order"), pos * 10),
            "meta": _meta_for_existing_stream(stream, track),
            "default": bool(track.get("default", (stream.get("disposition") or {}).get("default") == 1)),
        })

    subtitle_items = []
    for pos, track in enumerate(existing_sub_tracks):
        if not isinstance(track, dict) or not bool(track.get("keep", True)):
            continue
        try:
            stream_index = int(track.get("stream_index"))
        except (TypeError, ValueError):
            raise ValueError("invalid subtitle track index")
        stream = subtitle_by_index.get(stream_index)
        if stream is None:
            raise ValueError(f"subtitle track not found: {stream_index}")
        meta = _meta_for_existing_stream(stream, track)
        subtitle_items.append({
            "source": "existing",
            "stream_index": stream_index,
            "order": _parse_order(track.get("order"), 500 + pos * 10),
            "meta": meta,
            "codec": "copy",
            "default": bool(track.get("default", meta.get("default"))),
        })

    fallback_subs = [prepared["meta"] for prepared in prepared_subs]
    external_settings = _external_track_settings(tracks or {"subtitle": []}, fallback_subs)
    for pos, prepared in enumerate(prepared_subs):
        rel_path = rel_to_media(prepared["path"])
        setting = external_settings.get(rel_path) or external_settings.get(str(prepared["path"]))
        if setting is None:
            continue
        meta = {
            "language": setting.get("language") or "und",
            "title": setting.get("title") or "",
            "default": bool(setting.get("default")),
        }
        subtitle_items.append({
            "source": "external",
            "path": prepared["path"],
            "input_index": pos + 1,
            "order": _parse_order(setting.get("order"), 1000 + pos * 10),
            "meta": meta,
            "codec": prepared["codec"],
            "default": bool(setting.get("default")),
        })

    audio_items.sort(key=lambda item: (item["order"], item["stream_index"]))
    subtitle_items.sort(key=lambda item: (item["order"], item.get("stream_index", 10_000), str(item.get("path", ""))))
    _one_default(audio_items)
    _one_default(subtitle_items)
    for item in audio_items:
        item["meta"]["default"] = item["default"]
    for item in subtitle_items:
        item["meta"]["default"] = item["default"]
    return {"audio": audio_items, "subtitles": subtitle_items}


def _build_legacy_mux_plan(video: Path, prepared_subs: list[dict], keep_existing: bool) -> dict:
    return _build_mux_plan(video, prepared_subs, {"subtitles": [prepared["meta"] for prepared in prepared_subs], "keep_existing": keep_existing})


def _build_ffmpeg_embed_command(video: Path, out_path: Path, prepared_subs: list[dict], keep_existing: bool, mux_plan: dict | None = None) -> list[str]:
    mux_plan = mux_plan or _build_legacy_mux_plan(video, prepared_subs, keep_existing)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(video)]
    for prepared in prepared_subs:
        cmd += ["-i", str(prepared["path"])]

    cmd += ["-map", "0:v?"]
    for item in mux_plan["audio"]:
        cmd += ["-map", f"0:{item['stream_index']}"]
    for item in mux_plan["subtitles"]:
        if item["source"] == "existing":
            cmd += ["-map", f"0:{item['stream_index']}"]
        else:
            cmd += ["-map", f"{item['input_index']}:0"]

    cmd += ["-c:v", "copy", "-c:a", "copy"]

    for idx, item in enumerate(mux_plan["audio"]):
        cmd += [f"-disposition:a:{idx}", "default" if item.get("default") else "0"]

    for idx, item in enumerate(mux_plan["subtitles"]):
        cmd += [f"-c:s:{idx}", item["codec"]]
        meta = item["meta"]
        lang = meta.get("language") or "und"
        title = meta.get("title") or ""
        cmd += [f"-metadata:s:s:{idx}", f"language={lang}"]
        if title:
            cmd += [f"-metadata:s:s:{idx}", f"title={title}"]
        cmd += [f"-disposition:s:{idx}", "default" if item.get("default") else "0"]

    cmd.append(str(out_path))
    return cmd


def _build_mkvmerge_embed_command(video: Path, out_path: Path, prepared_subs: list[dict], keep_existing: bool, mux_plan: dict | None = None) -> list[str]:
    tool = _resolve_mkvmerge_command()
    if not tool:
        raise RuntimeError("mkvmerge not found")

    mux_plan = mux_plan or _build_legacy_mux_plan(video, prepared_subs, keep_existing)
    audio_ids = [str(item["stream_index"]) for item in mux_plan["audio"]]
    existing_subtitle_items = [item for item in mux_plan["subtitles"] if item["source"] == "existing"]
    existing_subtitle_ids = [str(item["stream_index"]) for item in existing_subtitle_items]
    external_items = [item for item in mux_plan["subtitles"] if item["source"] == "external"]
    track_order = [f"0:{item['stream_index']}" for item in mux_plan["audio"]]
    track_order += [f"0:{item['stream_index']}" for item in existing_subtitle_items]
    track_order += [f"{input_index}:0" for input_index, _item in enumerate(external_items, start=1)]

    cmd = [tool, "-o", str(out_path)]
    cmd += ["--audio-tracks", ",".join(audio_ids)] if audio_ids else ["--no-audio"]
    cmd += ["--subtitle-tracks", ",".join(existing_subtitle_ids)] if existing_subtitle_ids else ["--no-subtitles"]
    for item in mux_plan["audio"]:
        cmd += ["--default-track-flag", f"{item['stream_index']}:{'yes' if item.get('default') else 'no'}"]
    for item in existing_subtitle_items:
        cmd += ["--default-track-flag", f"{item['stream_index']}:{'yes' if item.get('default') else 'no'}"]
    cmd.append(str(video))

    for item in external_items:
        meta = item["meta"]
        lang = meta.get("language") or "und"
        title = meta.get("title") or ""
        cmd += ["--language", f"0:{lang}"]
        if title:
            cmd += ["--track-name", f"0:{title}"]
        cmd += ["--default-track-flag", f"0:{'yes' if item.get('default') else 'no'}"]
        cmd.append(str(item["path"]))

    if track_order:
        cmd += ["--track-order", ",".join(track_order)]
    return cmd


def _convert_ass_to_pgs_persistent(subtitle_path: Path, *, resolution: str | None = None, framerate: str | None = None) -> tuple[Path, str]:
    output_path, cmd_text, work_dir = _convert_ass_to_pgs(
        subtitle_path,
        resolution=resolution,
        framerate=framerate,
    )
    try:
        if output_path.parent.resolve() == subtitle_path.parent.resolve():
            return output_path, cmd_text

        target_name = f"{subtitle_path.stem}{output_path.suffix.lower()}"
        target_path = _unique_file_path(subtitle_path.parent, target_name)
        shutil.copy2(output_path, target_path)
        return target_path, cmd_text
    finally:
        _cleanup_dir(work_dir)


def _cleanup_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _now() -> float:
    return time.time()


def _progress(percent: int = 0, label: str = "", estimated: bool = False) -> dict:
    return {"percent": max(0, min(100, int(percent))), "label": label, "estimated": estimated}


def _public_task(task: dict) -> dict:
    return {
        key: value
        for key, value in task.items()
        if key not in {"process", "body", "prepared_subs", "temp_files_to_cleanup"}
    }


def _update_task(task_id: str, **updates) -> dict:
    with TASK_LOCK:
        task = TASKS[task_id]
        task.update(updates)
        task["updated_at"] = _now()
        return task


def _set_task_progress(task_id: str, key: str, percent: int, label: str = "", estimated: bool = False) -> None:
    with TASK_LOCK:
        task = TASKS[task_id]
        task[key] = _progress(percent, label, estimated)
        task["updated_at"] = _now()


def _append_task_log(task_id: str, line: str) -> None:
    with TASK_LOCK:
        task = TASKS[task_id]
        logs = task.setdefault("recent_logs", [])
        logs.append(line)
        del logs[:-50]
        now = _now()
        task["last_output_at"] = now
        task["updated_at"] = now
        if task.get("diagnostic") == "PGS 转换长时间没有新输出，可能卡住。":
            task["diagnostic"] = ""


def _set_task_phase(task_id: str, phase: str, message: str) -> None:
    _update_task(task_id, phase=phase, message=message, phase_started_at=_now(), diagnostic="")


def _task_cancel_requested(task_id: str) -> bool:
    with TASK_LOCK:
        return bool(TASKS[task_id].get("cancel_requested"))


def _task_cmd_text(cmd: list[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)


def _trim_task_output(output: str) -> str:
    return output[-4000:] if len(output) > 4000 else output


def _task_diagnostic_dir(task_id: str) -> Path:
    return TASK_DIAGNOSTIC_ROOT / task_id


def _task_diagnostic_json_path(task_id: str) -> Path:
    return _task_diagnostic_dir(task_id) / "diagnostic.json"


def _task_log_path(task_id: str) -> Path:
    return _task_diagnostic_dir(task_id) / "task.log"


def _safe_rel_or_abs(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return rel_to_media(path)
    except ValueError:
        return str(path)


def _write_task_diagnostic(task_id: str, **extra) -> None:
    with TASK_LOCK:
        task = TASKS.get(task_id)
        if task is None:
            return
        body = task.get("body") or {}
        data = {
            "task_id": task_id,
            "status": task.get("status"),
            "phase": task.get("phase"),
            "error": task.get("error"),
            "message": task.get("message"),
            "video": task.get("video"),
            "subtitle_mode": task.get("subtitle_mode"),
            "cmd": task.get("cmd"),
            "pid": task.get("pid"),
            "created_at": task.get("created_at"),
            "updated_at": task.get("updated_at"),
            "phase_started_at": task.get("phase_started_at"),
            "last_output_at": task.get("last_output_at"),
            "pgs_work_dir": task.get("pgs_work_dir"),
            "pgs_input_copy": task.get("pgs_input_copy"),
            "pgs_converter_version": task.get("pgs_converter_version"),
            "pgs_options": body.get("pgs_options"),
            "recent_logs": task.get("recent_logs", []),
        }
        data.update(extra)
    diagnostic_dir = _task_diagnostic_dir(task_id)
    diagnostic_dir.mkdir(parents=True, exist_ok=True)
    _task_diagnostic_json_path(task_id).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _pgs_converter_version(tool: str) -> str:
    try:
        output = subprocess.check_output([tool, "--version"], stderr=subprocess.STDOUT, timeout=10)
    except Exception:
        return "version unavailable"
    return output.decode(errors="ignore").strip() or "version unavailable"


def _video_duration_seconds(video: Path) -> float | None:
    try:
        probe = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video),
        ], stderr=subprocess.STDOUT, timeout=60)
    except Exception:
        return None
    try:
        duration = float(json.loads(probe.decode()).get("format", {}).get("duration") or 0)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return duration if duration > 0 else None


def _parse_ffmpeg_progress(line: str, duration: float | None) -> int | None:
    if not duration:
        return None
    if line.startswith("out_time_ms="):
        try:
            seconds = int(line.split("=", 1)[1]) / 1_000_000
        except ValueError:
            return None
        return min(99, int(seconds / duration * 100))
    if line.startswith("out_time="):
        raw = line.split("=", 1)[1].strip()
        parts = raw.split(":")
        if len(parts) != 3:
            return None
        try:
            seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        except ValueError:
            return None
        return min(99, int(seconds / duration * 100))
    return None


def _parse_mkvmerge_progress(line: str) -> int | None:
    patterns = [r"Progress:\s*(\d+)%", r"#GUI#progress\s+(\d+)"]
    for pattern in patterns:
        match = re.search(pattern, line)
        if match:
            return min(99, int(match.group(1)))
    return None


def _parse_percent(line: str) -> int | None:
    match = re.search(r"(?<!\d)(\d{1,3})%", line)
    if not match:
        return None
    return min(99, int(match.group(1)))


def _enqueue_process_output(process: subprocess.Popen, output_queue: queue.Queue) -> None:
    if process.stdout is None:
        return
    for line in process.stdout:
        output_queue.put(line)


def _terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _run_task_process(
    task_id: str,
    cmd: list[str],
    *,
    phase: str,
    progress_key: str,
    progress_parser=None,
    log_path: Path | None = None,
    timeout: int = 3600,
    complete_percent: int = 100,
) -> str:
    cmd_text = _task_cmd_text(cmd)
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"$ {cmd_text}\n", encoding="utf-8")
    _update_task(task_id, cmd=cmd_text, message=f"正在执行: {phase}", phase_started_at=_now(), diagnostic="")
    output_lines = []
    started = _now()
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="ignore")
    output_queue: queue.Queue[str] = queue.Queue()
    threading.Thread(target=_enqueue_process_output, args=(process, output_queue), daemon=True).start()
    with TASK_LOCK:
        TASKS[task_id]["process"] = process
        TASKS[task_id]["pid"] = process.pid
    try:
        while True:
            if _task_cancel_requested(task_id):
                _terminate_process(process)
                raise RuntimeError("task canceled")
            if timeout and _now() - started > timeout:
                _terminate_process(process)
                raise RuntimeError("task timed out")
            try:
                line = output_queue.get(timeout=0.2)
            except queue.Empty:
                if process.poll() is not None:
                    break
                with TASK_LOCK:
                    task = TASKS[task_id]
                    last_output_at = task.get("last_output_at") or task.get("phase_started_at") or started
                    if task.get("phase") == "pgs" and _now() - last_output_at > 300 and not task.get("diagnostic"):
                        task["diagnostic"] = "PGS 转换长时间没有新输出，可能卡住。"
                        task["updated_at"] = _now()
                continue
            line = line.rstrip()
            output_lines.append(line)
            if log_path is not None:
                with log_path.open("a", encoding="utf-8") as log_file:
                    log_file.write(line + "\n")
            _append_task_log(task_id, line)
            _update_task(task_id, message=line or f"正在执行: {phase}")
            if progress_parser:
                percent = progress_parser(line)
                if percent is not None:
                    _set_task_progress(task_id, progress_key, percent, line, False)

        while True:
            try:
                line = output_queue.get_nowait().rstrip()
            except queue.Empty:
                break
            output_lines.append(line)
        output = _trim_task_output("\n".join(output_lines))
        if process.returncode != 0:
            raise RuntimeError(output or f"{phase} failed")
        _set_task_progress(task_id, progress_key, complete_percent, f"{phase}完成", False)
        return output
    finally:
        with TASK_LOCK:
            if TASKS.get(task_id, {}).get("process") is process:
                TASKS[task_id]["process"] = None


def _convert_ass_to_pgs_for_task(
    task_id: str,
    subtitle_path: Path,
    *,
    resolution: str,
    framerate: str,
    progress_offset: int = 0,
    progress_span: int = 100,
) -> tuple[Path, str]:
    if subtitle_path.suffix.lower() not in {".ass", ".ssa"}:
        raise ValueError("pgs conversion only supports ass/ssa")

    tool = _resolve_pgs_converter_command()
    if not tool:
        raise RuntimeError("pgs converter not configured")

    font_dir = Path(PGS_FONT_DIR) if PGS_FONT_DIR else None
    if font_dir is None or not font_dir.is_dir():
        raise RuntimeError("pgs font dir not found")

    temp_root = TMP_SUBTITLE_ROOT / "_pgs"
    temp_root.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="pgs-", dir=temp_root))
    input_dir = work_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    input_path = input_dir / subtitle_path.name
    shutil.copy2(subtitle_path, input_path)

    diagnostic_dir = _task_diagnostic_dir(task_id)
    diagnostic_dir.mkdir(parents=True, exist_ok=True)
    log_path = _task_log_path(task_id)
    converter_version = _pgs_converter_version(tool)
    _update_task(
        task_id,
        diagnostic_dir=_safe_rel_or_abs(diagnostic_dir),
        full_log_path=_safe_rel_or_abs(log_path),
        diagnostic_download_url=f"/api/tasks/{task_id}/diagnostic",
        pgs_work_dir=_safe_rel_or_abs(work_dir),
        pgs_input_copy=_safe_rel_or_abs(input_path),
        pgs_converter_version=converter_version,
    )

    cmd = [
        tool,
        "--enable-pgs-output",
        "--resolution",
        resolution,
        "--framerate",
        framerate,
        "subset",
        str(input_path),
        "--font-dir",
        str(font_dir),
        "--output-dir",
        str(work_dir),
    ]
    cmd_text = _task_cmd_text(cmd)
    _write_task_diagnostic(
        task_id,
        subtitle=rel_to_media(subtitle_path),
        command=cmd_text,
        converter_version=converter_version,
        resolution=resolution,
        framerate=framerate,
        font_dir=str(font_dir),
    )
    success = False
    try:
        def parse_pgs_progress(line: str) -> int | None:
            percent = _parse_percent(line)
            if percent is None:
                return None
            return min(99, progress_offset + int(percent * progress_span / 100))

        _run_task_process(
            task_id,
            cmd,
            phase="PGS 转换",
            progress_key="pgs_progress",
            progress_parser=parse_pgs_progress,
            log_path=log_path,
            complete_percent=min(100, progress_offset + progress_span),
        )
        candidates = sorted(
            path
            for path in work_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in {".pgs", ".sup"}
        )
        if not candidates:
            produced = sorted(str(path.relative_to(work_dir)) for path in work_dir.rglob("*") if path.is_file())
            detail = f": {', '.join(produced)}" if produced else ""
            raise RuntimeError(f"pgs converter did not produce a new .pgs or .sup file{detail}")
        output_path = candidates[0]
        target_name = f"{subtitle_path.stem}{output_path.suffix.lower()}"
        target_path = _unique_file_path(subtitle_path.parent, target_name)
        shutil.copy2(output_path, target_path)
        success = True
        _write_task_diagnostic(task_id, output=rel_to_media(target_path), conversion_success=True)
        return target_path, cmd_text
    finally:
        if success:
            _cleanup_dir(work_dir)
        else:
            _write_task_diagnostic(task_id, conversion_success=False)


def _build_ffmpeg_progress_command(cmd: list[str]) -> list[str]:
    return cmd[:-1] + ["-progress", "pipe:1", "-nostats", cmd[-1]]


def _build_mkvmerge_progress_command(cmd: list[str]) -> list[str]:
    if "--gui-mode" in cmd:
        return cmd
    return [cmd[0], "--gui-mode"] + cmd[1:]


def _prepare_embed_task(body: dict) -> tuple[Path, list[tuple[Path, dict]], Path, dict, bool, str]:
    video_rel = body.get("video")
    subs = body.get("subtitles") or []
    out_name = body.get("out_name")
    subtitle_mode = (body.get("subtitle_mode") or "text").strip() or "text"
    tracks = body.get("tracks") if isinstance(body.get("tracks"), dict) else None
    if not video_rel:
        raise ValueError("video required")
    if not subs and not tracks:
        raise ValueError("video and subtitles required")
    if subtitle_mode not in {"text", "pgs_auto"}:
        raise ValueError("invalid subtitle mode")

    embed_settings = _normalize_embed_settings(body)
    pgs_options = _validate_pgs_options(embed_settings["pgs_options"])
    use_default_output_dir = embed_settings["use_default_output_dir"]

    video = safe_path(video_rel)
    if not video.is_file():
        raise FileNotFoundError("video not found")
    if video.suffix.lower() not in VIDEO_EXTS:
        raise ValueError("unsupported video type")

    sub_paths = []
    for sub in subs:
        sp = safe_path(sub.get("path", ""))
        if not sp.is_file():
            raise FileNotFoundError(f"subtitle not found: {sub.get('path')}")
        if sp.suffix.lower() not in SUBTITLE_EXTS:
            raise ValueError(f"unsupported subtitle type: {sub.get('path')}")
        sub_paths.append((sp, sub))

    out_path = _resolve_embed_output_path(video, out_name, use_default_output_dir)
    return video, sub_paths, out_path, pgs_options, use_default_output_dir, subtitle_mode


def _prepare_pgs_only_task(body: dict) -> tuple[Path, dict]:
    subtitle_rel = body.get("subtitle")
    if not subtitle_rel:
        raise ValueError("subtitle required")

    embed_settings = _normalize_embed_settings(body)
    pgs_options = _validate_pgs_options(embed_settings["pgs_options"])

    subtitle = safe_path(subtitle_rel)
    if not subtitle.is_file():
        raise FileNotFoundError("subtitle not found")
    if subtitle.suffix.lower() not in {".ass", ".ssa"}:
        raise ValueError("pgs conversion only supports ass/ssa")
    return subtitle, pgs_options


def _derive_standalone_pgs_resolution(pgs_options: dict) -> str:
    if pgs_options["resolution_mode"] == "custom":
        return pgs_options["resolution"]
    return PGS_RESOLUTION


def _run_embed_task(task_id: str) -> None:
    with TASK_LOCK:
        body = TASKS[task_id]["body"]
    keep_existing = bool(body.get("keep_existing", True))
    video = None
    out_path = None
    prepared_subs = []
    warnings = []
    cmd_text = ""

    try:
        video, sub_paths, out_path, pgs_options, use_default_output_dir, subtitle_mode = _prepare_embed_task(body)
        temp_files_to_cleanup = [sp for sp, _meta in sub_paths if _is_temp_subtitle(sp, video)]
        resolved_pgs_resolution = _derive_pgs_resolution(video, pgs_options)
        _set_task_progress(task_id, "pgs_progress", 100, "PGS 转换任务已分离", False)
        for sp, meta in sub_paths:
            codec = "copy" if sp.suffix.lower() in PGS_SUBTITLE_EXTS else "srt"
            prepared_subs.append({"path": sp, "meta": meta, "codec": codec})

        has_pgs_subtitle = any(prepared["path"].suffix.lower() in PGS_SUBTITLE_EXTS for prepared in prepared_subs)
        muxer = "mkvmerge" if has_pgs_subtitle else "ffmpeg"
        mux_plan = _build_mux_plan(video, prepared_subs, body)
        warnings = _subtitle_warning_for_existing_pgs(prepared_subs)
        _set_task_phase(task_id, "embed", "开始封装")
        _update_task(task_id, muxer=muxer, warnings=warnings)
        if muxer == "mkvmerge":
            cmd = _build_mkvmerge_progress_command(_build_mkvmerge_embed_command(video, out_path, prepared_subs, keep_existing, mux_plan))
            progress_parser = _parse_mkvmerge_progress
        else:
            cmd = _build_ffmpeg_progress_command(_build_ffmpeg_embed_command(video, out_path, prepared_subs, keep_existing, mux_plan))
            duration = _video_duration_seconds(video)
            progress_parser = lambda line: _parse_ffmpeg_progress(line, duration)
        cmd_text = _task_cmd_text(cmd)
        _run_task_process(task_id, cmd, phase="封装", progress_key="embed_progress", progress_parser=progress_parser)

        for temp_file in temp_files_to_cleanup:
            if temp_file.exists():
                temp_file.unlink()
        temp_dir = _video_temp_dir(video)
        if temp_dir.exists() and temp_dir.is_dir() and not any(temp_dir.iterdir()):
            temp_dir.rmdir()

        output_dir = rel_to_media(out_path.parent) if out_path.parent != MEDIA_DIR else ""
        _update_task(
            task_id,
            status="succeeded",
            phase="done",
            output=rel_to_media(out_path),
            output_dir=output_dir,
            subtitle_mode=subtitle_mode,
            used_default_output_dir=use_default_output_dir,
            muxer=muxer,
            warnings=warnings,
            pgs_settings={
                "resolution_mode": pgs_options["resolution_mode"],
                "resolution": resolved_pgs_resolution,
                "framerate": pgs_options["framerate"],
                "applies_to": "ass_to_pgs_conversion_only",
                "applied_to_existing_pgs": False,
            },
            cmd=cmd_text,
            message="任务完成",
        )
    except Exception as exc:
        if _task_cancel_requested(task_id) or str(exc) == "task canceled":
            if out_path is not None and out_path.exists():
                out_path.unlink()
            _update_task(task_id, status="canceled", phase="canceled", message="任务已取消", error="")
            _write_task_diagnostic(task_id, canceled=True)
        else:
            _update_task(task_id, status="failed", phase="error", message="任务失败", error=str(exc), cmd=cmd_text, warnings=warnings)
            _write_task_diagnostic(task_id, failed=True)


def _replace_converted_subtitle_paths(body: dict, converted: list[dict]) -> dict:
    next_body = json.loads(json.dumps(body, ensure_ascii=False))
    replacements = {item["source_path"]: item["path"] for item in converted}
    for sub in next_body.get("subtitles") or []:
        path = sub.get("path")
        if path in replacements:
            sub["converted_from"] = path
            sub["path"] = replacements[path]
    tracks = next_body.get("tracks") if isinstance(next_body.get("tracks"), dict) else None
    if tracks:
        for track in tracks.get("subtitle") or []:
            if not isinstance(track, dict) or track.get("source") != "external":
                continue
            path = track.get("path")
            if path in replacements:
                track["converted_from"] = path
                track["path"] = replacements[path]
    next_body["subtitle_mode"] = "text"
    return next_body


def _find_dependent_embed_task(task_id: str) -> str | None:
    with TASK_LOCK:
        direct_id = TASKS.get(task_id, {}).get("dependent_task_id")
        if direct_id and direct_id in TASKS:
            return direct_id
        for candidate_id, task in TASKS.items():
            if task.get("depends_on") == task_id and task.get("type") == "embed":
                return candidate_id
    return None


def _finish_dependent_embed_task(task_id: str, *, status: str, message: str, error: str = "") -> None:
    dependent_id = _find_dependent_embed_task(task_id)
    if dependent_id:
        _update_task(dependent_id, status=status, phase="canceled", message=message, error=error)


def _enqueue_dependent_embed_task(task_id: str, body: dict) -> None:
    dependent_id = _find_dependent_embed_task(task_id)
    if not dependent_id:
        return
    with TASK_LOCK:
        task = TASKS[dependent_id]
        task["body"] = body
        task["subtitle_mode"] = body.get("subtitle_mode") or "text"
        task["message"] = "等待封装"
        task["phase"] = "queued"
        task["updated_at"] = _now()
        if task["status"] == "pending" and dependent_id not in TASK_QUEUE:
            TASK_QUEUE.append(dependent_id)
            TASK_EVENT.set()


def _run_pgs_task(task_id: str) -> None:
    with TASK_LOCK:
        body = TASKS[task_id]["body"]
    converted = []
    cmd_text = ""
    standalone = bool(body.get("subtitle")) and not body.get("video")
    try:
        if standalone:
            subtitle, pgs_options = _prepare_pgs_only_task(body)
            resolved_pgs_resolution = _derive_standalone_pgs_resolution(pgs_options)
            source_path = rel_to_media(subtitle)
            _set_task_phase(task_id, "pgs", "开始 PGS 转换")
            _set_task_progress(task_id, "pgs_progress", 0, f"正在转换: {source_path}", True)
            converted_path, cmd_text = _convert_ass_to_pgs_for_task(
                task_id,
                subtitle,
                resolution=resolved_pgs_resolution,
                framerate=pgs_options["framerate"],
            )
            converted_rel = rel_to_media(converted_path)
            converted = [{"source_path": source_path, "path": converted_rel}]
            output_dir = rel_to_media(converted_path.parent) if converted_path.parent != MEDIA_DIR else ""
            _set_task_progress(task_id, "pgs_progress", 100, f"已转换: {converted_rel}", False)
            _update_task(
                task_id,
                status="succeeded",
                phase="done",
                output=converted_rel,
                output_dir=output_dir,
                subtitle_mode="pgs_auto",
                converted_subtitles=converted,
                pgs_settings={
                    "resolution_mode": pgs_options["resolution_mode"],
                    "resolution": resolved_pgs_resolution,
                    "framerate": pgs_options["framerate"],
                    "applies_to": "standalone_ass_to_pgs_conversion",
                    "applied_to_existing_pgs": False,
                },
                cmd=cmd_text,
                message="PGS 转换完成",
            )
            return

        video, sub_paths, _out_path, pgs_options, use_default_output_dir, _subtitle_mode = _prepare_embed_task(body)
        resolved_pgs_resolution = _derive_pgs_resolution(video, pgs_options)
        ass_subtitles = [(sp, meta) for sp, meta in sub_paths if sp.suffix.lower() in {".ass", ".ssa"}]
        total = len(ass_subtitles)
        if not total:
            raise RuntimeError("no ass/ssa subtitles to convert")
        _set_task_phase(task_id, "pgs", "开始 PGS 转换")
        done = 0
        for sp, _meta in ass_subtitles:
            if _task_cancel_requested(task_id):
                raise RuntimeError("task canceled")
            source_path = rel_to_media(sp)
            base_percent = int(done / total * 100)
            _set_task_progress(task_id, "pgs_progress", base_percent, f"正在转换: {source_path}", True)
            converted_path, cmd_text = _convert_ass_to_pgs_for_task(
                task_id,
                sp,
                resolution=resolved_pgs_resolution,
                framerate=pgs_options["framerate"],
                progress_offset=base_percent,
                progress_span=int(100 / total),
            )
            done += 1
            converted_rel = rel_to_media(converted_path)
            converted.append({"source_path": source_path, "path": converted_rel})
            _set_task_progress(task_id, "pgs_progress", int(done / total * 100), f"已转换: {converted_rel}", True)

        next_body = _replace_converted_subtitle_paths(body, converted)
        _enqueue_dependent_embed_task(task_id, next_body)
        output = converted[0]["path"] if len(converted) == 1 else f"已转换 {len(converted)} 个字幕"
        _update_task(
            task_id,
            status="succeeded",
            phase="done",
            output=output,
            output_dir="",
            subtitle_mode="pgs_auto",
            used_default_output_dir=use_default_output_dir,
            converted_subtitles=converted,
            pgs_settings={
                "resolution_mode": pgs_options["resolution_mode"],
                "resolution": resolved_pgs_resolution,
                "framerate": pgs_options["framerate"],
                "applies_to": "ass_to_pgs_conversion_only",
                "applied_to_existing_pgs": False,
            },
            cmd=cmd_text,
            message="PGS 转换完成，已加入封装任务",
        )
    except Exception as exc:
        if _task_cancel_requested(task_id) or str(exc) == "task canceled":
            _update_task(task_id, status="canceled", phase="canceled", message="PGS 转换已取消", error="")
            if not standalone:
                _finish_dependent_embed_task(task_id, status="canceled", message="PGS 转换已取消，已跳过封装")
            _write_task_diagnostic(task_id, canceled=True)
        else:
            _update_task(task_id, status="failed", phase="error", message="PGS 转换失败", error=str(exc), cmd=cmd_text)
            if not standalone:
                _finish_dependent_embed_task(task_id, status="canceled", message="PGS 转换失败，已跳过封装", error=str(exc))
            _write_task_diagnostic(task_id, failed=True)


def _task_worker() -> None:
    while True:
        TASK_EVENT.wait()
        while True:
            with TASK_LOCK:
                while TASK_QUEUE and TASKS.get(TASK_QUEUE[0], {}).get("status") != "pending":
                    TASK_QUEUE.popleft()
                if not TASK_QUEUE:
                    TASK_EVENT.clear()
                    break
                task_id = TASK_QUEUE.popleft()
                task = TASKS[task_id]
                task["status"] = "running"
                task["phase"] = "queued"
                task["updated_at"] = _now()
                task_type = task.get("type")
            if task_type == "pgs":
                _run_pgs_task(task_id)
            else:
                _run_embed_task(task_id)


def _ensure_task_worker() -> None:
    global TASK_WORKER_STARTED
    with TASK_LOCK:
        if TASK_WORKER_STARTED:
            return
        TASK_WORKER_STARTED = True
    threading.Thread(target=_task_worker, daemon=True, name="subtitle-task-worker").start()


def _base_task(body: dict, task_type: str, *, phase: str = "queued", message: str = "等待执行", depends_on: str = "") -> dict:
    task_id = uuid.uuid4().hex
    now = _now()
    return {
        "id": task_id,
        "type": task_type,
        "depends_on": depends_on,
        "created_at": now,
        "updated_at": now,
        "status": "pending",
        "phase": phase,
        "video": body.get("video") or "",
        "subtitle": body.get("subtitle") or "",
        "out_name": body.get("out_name") or "",
        "output": "",
        "output_dir": "",
        "subtitle_mode": body.get("subtitle_mode") or "text",
        "pgs_progress": _progress(0, "等待 PGS 转换", False),
        "embed_progress": _progress(0, "等待封装", False),
        "message": message,
        "cmd": "",
        "error": "",
        "warnings": [],
        "muxer": "",
        "pgs_settings": None,
        "converted_subtitles": [],
        "used_default_output_dir": False,
        "cancel_requested": False,
        "process": None,
        "pid": None,
        "phase_started_at": now,
        "last_output_at": None,
        "diagnostic": "",
        "recent_logs": [],
        "body": body,
    }


def _body_needs_pgs_task(body: dict) -> bool:
    if (body.get("subtitle_mode") or "text") != "pgs_auto":
        return False
    for sub in body.get("subtitles") or []:
        try:
            path = safe_path(sub.get("path", ""))
        except ValueError:
            continue
        if path.suffix.lower() in {".ass", ".ssa"}:
            return True
    return False


def _create_embed_task(body: dict) -> dict:
    task = _base_task(body, "embed")
    with TASK_LOCK:
        TASKS[task["id"]] = task
        TASK_QUEUE.append(task["id"])
        TASK_EVENT.set()
    _ensure_task_worker()
    return task


def _create_embed_task_chain(body: dict) -> list[dict]:
    if not _body_needs_pgs_task(body):
        return [_create_embed_task(body)]

    pgs_task = _base_task(body, "pgs", message="等待 PGS 转换")
    embed_task = _base_task(body, "embed", phase="waiting_pgs", message="等待 PGS 转换完成", depends_on=pgs_task["id"])
    pgs_task["dependent_task_id"] = embed_task["id"]
    with TASK_LOCK:
        TASKS[pgs_task["id"]] = pgs_task
        TASKS[embed_task["id"]] = embed_task
        TASK_QUEUE.append(pgs_task["id"])
        TASK_EVENT.set()
    _ensure_task_worker()
    return [pgs_task, embed_task]


def _create_pgs_only_task(body: dict) -> dict:
    task = _base_task(body, "pgs", message="等待 PGS 转换")
    with TASK_LOCK:
        TASKS[task["id"]] = task
        TASK_QUEUE.append(task["id"])
        TASK_EVENT.set()
    _ensure_task_worker()
    return task


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/clear-temp-subtitles", methods=["POST"])
def api_clear_temp_subtitles():
    temp_root = TMP_SUBTITLE_ROOT.resolve()
    if MEDIA_DIR != temp_root and MEDIA_DIR not in temp_root.parents:
        return jsonify(error="invalid temp directory"), 400

    if not temp_root.exists():
        return jsonify({"ok": True, "cleared": False})
    if not temp_root.is_dir():
        return jsonify(error="invalid temp directory"), 400

    _cleanup_dir(temp_root)
    return jsonify({"ok": True, "cleared": True})


@app.route("/api/download")
def api_download():
    rel = request.args.get("path", "")
    try:
        target = safe_path(rel)
    except ValueError:
        return jsonify(error="invalid path"), 400
    if not target.is_file():
        return jsonify(error="not a file"), 404
    if target.suffix.lower() not in DOWNLOADABLE_SUBTITLE_EXTS:
        return jsonify(error="unsupported file type"), 400

    return send_file(target, as_attachment=True, download_name=target.name)


@app.route("/api/version")
def api_version():
    return jsonify({"version": APP_VERSION, "build_date": BUILD_DATE})


@app.route("/api/list")
def api_list():
    rel = request.args.get("path", "")
    try:
        target = safe_path(rel)
    except ValueError:
        return jsonify(error="invalid path"), 400
    if not target.exists() or not target.is_dir():
        return jsonify(error="not a directory"), 404

    entries = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if child.name.startswith("."):
            continue
        kind = "dir" if child.is_dir() else "file"
        ext = child.suffix.lower()
        role = "other"
        if kind == "file":
            if ext in VIDEO_EXTS:
                role = "video"
            elif ext in SUBTITLE_EXTS:
                role = "subtitle"
        entries.append({
            "name": child.name,
            "path": rel_to_media(child),
            "kind": kind,
            "role": role,
            "size": child.stat().st_size if kind == "file" else None,
        })

    parent = None
    if target != MEDIA_DIR:
        parent = rel_to_media(target.parent) if target.parent != MEDIA_DIR else ""

    return jsonify({
        "cwd": rel_to_media(target) if target != MEDIA_DIR else "",
        "parent": parent,
        "entries": entries,
    })


@app.route("/api/probe")
def api_probe():
    rel = request.args.get("path", "")
    try:
        target = safe_path(rel)
    except ValueError:
        return jsonify(error="invalid path"), 400
    if not target.is_file():
        return jsonify(error="not a file"), 404

    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_streams", "-show_format",
        str(target),
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=60)
    except subprocess.CalledProcessError as exc:
        return jsonify(error="ffprobe failed", detail=exc.output.decode(errors="ignore")), 500

    data = json.loads(out.decode("utf-8", errors="ignore"))
    streams = data.get("streams", [])
    audios = [_stream_track_info(s) for s in streams if s.get("codec_type") == "audio"]
    subtitles = [_stream_track_info(s) for s in streams if s.get("codec_type") == "subtitle"]

    pgs_status = _pgs_converter_status()

    return jsonify({
        "path": rel_to_media(target),
        "format": data.get("format", {}),
        "streams": streams,
        "audios": audios,
        "subtitles": subtitles,
        "uploaded_subtitles": _list_uploaded_subtitles(target),
        "default_output_dir": _configured_default_output_dir_rel(),
        "pgs_defaults": _pgs_defaults(),
        "video_dimensions": _video_dimensions(target),
        "video_framerate": _video_framerate(streams),
        "pgs_mode_available": pgs_status["available"],
        "pgs_mode_hint": pgs_status["hint"],
        "pgs_mode_missing": pgs_status["missing"],
    })


def _codec_to_ext(codec: str | None) -> str:
    codec = (codec or "").lower()
    if codec in {"ass", "ssa"}:
        return ".ass"
    if codec == "webvtt":
        return ".vtt"
    if codec in {"subrip", "srt"}:
        return ".srt"
    if codec in {"mov_text"}:
        return ".srt"
    if codec in {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"}:
        return ".sup"
    return ".srt"


@app.route("/api/extract", methods=["POST"])
def api_extract():
    body = request.get_json(silent=True) or {}
    rel = body.get("path", "")
    stream_index = body.get("stream_index")
    codec = body.get("codec")
    out_name = body.get("out_name")

    if stream_index is None:
        return jsonify(error="stream_index required"), 400
    try:
        target = safe_path(rel)
    except ValueError:
        return jsonify(error="invalid path"), 400
    if not target.is_file():
        return jsonify(error="not a file"), 404

    ext = _codec_to_ext(codec)
    try:
        out_name = _validate_output_name(out_name, f"{target.stem}.track{stream_index}{ext}")
    except ValueError:
        return jsonify(error="invalid output name"), 400
    if Path(out_name).suffix.lower() not in (ext, ".srt", ".ass", ".vtt"):
        out_name = out_name + ext

    out_path = (target.parent / out_name).resolve()
    if MEDIA_DIR not in out_path.parents and out_path != MEDIA_DIR:
        return jsonify(error="output path invalid"), 400

    bitmap = (codec or "").lower() in {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"}
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(target),
        "-map", f"0:{stream_index}",
    ]
    if bitmap:
        cmd += ["-c:s", "copy"]
    else:
        if ext == ".srt":
            cmd += ["-c:s", "subrip"]
        elif ext == ".ass":
            cmd += ["-c:s", "ass"]
        elif ext == ".vtt":
            cmd += ["-c:s", "webvtt"]
    cmd.append(str(out_path))

    try:
        subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=600)
    except subprocess.CalledProcessError as exc:
        return jsonify(error="ffmpeg failed", cmd=" ".join(shlex.quote(c) for c in cmd),
                       detail=exc.output.decode(errors="ignore")), 500

    return jsonify({
        "ok": True,
        "output": rel_to_media(out_path),
        "download_url": f"/api/download?path={quote(rel_to_media(out_path), safe='')}",
        "cmd": " ".join(shlex.quote(c) for c in cmd),
    })


@app.route("/api/upload-subtitle", methods=["POST"])
def api_upload_subtitle():
    video_rel = request.form.get("video", "")
    file = request.files.get("file")

    if not video_rel or file is None:
        return jsonify(error="video and file required"), 400

    try:
        video = safe_path(video_rel)
    except ValueError:
        return jsonify(error="invalid video path"), 400
    if not video.is_file():
        return jsonify(error="video not found"), 404
    if video.suffix.lower() not in VIDEO_EXTS:
        return jsonify(error="unsupported video type"), 400

    filename = secure_filename(file.filename or "")
    ext = Path(filename).suffix.lower()
    if not filename or ext not in SUBTITLE_EXTS:
        return jsonify(error="unsupported subtitle type"), 400

    temp_dir = _video_temp_dir(video)
    temp_dir.mkdir(parents=True, exist_ok=True)
    out_path = _unique_file_path(temp_dir, filename)
    file.save(out_path)

    return jsonify({
        "ok": True,
        "path": rel_to_media(out_path),
        "name": out_path.name,
        "size": out_path.stat().st_size,
    })


@app.route("/api/convert-ass-to-pgs", methods=["POST"])
def api_convert_ass_to_pgs():
    body = request.get_json(silent=True) or {}
    video_rel = body.get("video", "")
    subtitle_rel = body.get("subtitle", "")

    try:
        pgs_options = _validate_pgs_options(_normalize_embed_settings(body)["pgs_options"])
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    try:
        video = safe_path(video_rel)
    except ValueError:
        return jsonify(error="invalid video path"), 400
    if not video.is_file():
        return jsonify(error="video not found"), 404
    if video.suffix.lower() not in VIDEO_EXTS:
        return jsonify(error="unsupported video type"), 400

    try:
        subtitle = safe_path(subtitle_rel)
    except ValueError:
        return jsonify(error="invalid subtitle path"), 400
    if not subtitle.is_file():
        return jsonify(error="subtitle not found"), 404
    if subtitle.suffix.lower() not in {".ass", ".ssa"}:
        return jsonify(error="pgs conversion only supports ass/ssa"), 400

    resolved_pgs_resolution = _derive_pgs_resolution(video, pgs_options)
    try:
        output_path, conversion_cmd = _convert_ass_to_pgs_persistent(
            subtitle,
            resolution=resolved_pgs_resolution,
            framerate=pgs_options["framerate"],
        )
    except PgsConversionError as exc:
        return jsonify(error=str(exc), detail=exc.output, stdout=exc.output, cmd=exc.cmd_text), 400
    except (RuntimeError, ValueError) as exc:
        return jsonify(error=str(exc)), 400

    output_dir = rel_to_media(output_path.parent) if output_path.parent != MEDIA_DIR else ""
    return jsonify({
        "ok": True,
        "input": rel_to_media(subtitle),
        "output": rel_to_media(output_path),
        "output_dir": output_dir,
        "pgs_settings": {
            "resolution_mode": pgs_options["resolution_mode"],
            "resolution": resolved_pgs_resolution,
            "framerate": pgs_options["framerate"],
        },
        "cmd": conversion_cmd,
    })


@app.route("/api/tasks/embed", methods=["POST"])
def api_create_embed_task():
    body = request.get_json(silent=True) or {}
    try:
        _prepare_embed_task(body)
    except FileNotFoundError as exc:
        return jsonify(error=str(exc)), 404
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    tasks = _create_embed_task_chain(body)
    public_tasks = [_public_task(task) for task in tasks]
    return jsonify({"ok": True, "task": public_tasks[0], "tasks": public_tasks}), 202


@app.route("/api/tasks/pgs", methods=["POST"])
def api_create_pgs_task():
    body = request.get_json(silent=True) or {}
    try:
        _prepare_pgs_only_task(body)
    except FileNotFoundError as exc:
        return jsonify(error=str(exc)), 404
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    task = _create_pgs_only_task(body)
    return jsonify({"ok": True, "task": _public_task(task)}), 202


@app.route("/api/tasks")
def api_tasks():
    with TASK_LOCK:
        tasks = sorted((_public_task(task) for task in TASKS.values()), key=lambda task: task["created_at"])
    return jsonify({"tasks": tasks})


@app.route("/api/tasks/<task_id>")
def api_task(task_id: str):
    with TASK_LOCK:
        task = TASKS.get(task_id)
        if task is None:
            return jsonify(error="task not found"), 404
        data = _public_task(task)
    return jsonify(data)


@app.route("/api/tasks/<task_id>", methods=["DELETE"])
def api_delete_task(task_id: str):
    with TASK_LOCK:
        task = TASKS.get(task_id)
        if task is None:
            return jsonify(error="task not found"), 404
        if task.get("status") not in FINAL_TASK_STATUSES:
            return jsonify(error="task is not finished"), 400
        TASKS.pop(task_id, None)
        try:
            TASK_QUEUE.remove(task_id)
        except ValueError:
            pass
    return jsonify({"ok": True})


@app.route("/api/tasks/<task_id>/diagnostic")
def api_task_diagnostic(task_id: str):
    with TASK_LOCK:
        if task_id not in TASKS:
            return jsonify(error="task not found"), 404
    diagnostic_dir = _task_diagnostic_dir(task_id).resolve()
    diagnostic_root = TASK_DIAGNOSTIC_ROOT.resolve()
    if diagnostic_root != diagnostic_dir and diagnostic_root not in diagnostic_dir.parents:
        return jsonify(error="invalid diagnostic path"), 400
    if not diagnostic_dir.is_dir():
        return jsonify(error="diagnostic not found"), 404

    zip_path = diagnostic_dir / f"{task_id}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ["diagnostic.json", "task.log"]:
            path = diagnostic_dir / name
            if path.is_file():
                archive.write(path, name)
        with TASK_LOCK:
            input_copy = TASKS.get(task_id, {}).get("pgs_input_copy") or ""
        input_path = safe_path(input_copy) if input_copy and not Path(input_copy).is_absolute() else Path(input_copy)
        if input_path.is_file():
            archive.write(input_path, f"input/{input_path.name}")
    return send_file(zip_path, as_attachment=True, download_name=f"{task_id}-diagnostic.zip")


@app.route("/api/tasks/<task_id>/cancel", methods=["POST"])
def api_cancel_task(task_id: str):
    with TASK_LOCK:
        task = TASKS.get(task_id)
        if task is None:
            return jsonify(error="task not found"), 404
        if task["status"] in FINAL_TASK_STATUSES:
            return jsonify({"ok": True, "task": _public_task(task)})
        task["cancel_requested"] = True
        task["updated_at"] = _now()
        if task["status"] == "pending":
            task["status"] = "canceled"
            task["phase"] = "canceled"
            task["message"] = "任务已取消"
            return jsonify({"ok": True, "task": _public_task(task)})
        task["status"] = "canceling"
        task["phase"] = "canceled"
        task["message"] = "正在取消任务"
        process = task.get("process")
        if process is not None and process.poll() is None:
            process.terminate()
        data = _public_task(task)
    return jsonify({"ok": True, "task": data})


@app.route("/api/embed", methods=["POST"])
def api_embed():
    body = request.get_json(silent=True) or {}
    video_rel = body.get("video")
    subs = body.get("subtitles") or []
    out_name = body.get("out_name")
    keep_existing = bool(body.get("keep_existing", True))
    subtitle_mode = (body.get("subtitle_mode") or "text").strip() or "text"

    try:
        embed_settings = _normalize_embed_settings(body)
        pgs_options = _validate_pgs_options(embed_settings["pgs_options"])
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    use_default_output_dir = embed_settings["use_default_output_dir"]

    tracks = body.get("tracks") if isinstance(body.get("tracks"), dict) else None
    if not video_rel:
        return jsonify(error="video required"), 400
    if not subs and not tracks:
        return jsonify(error="video and subtitles required"), 400
    if subtitle_mode not in {"text", "pgs_auto"}:
        return jsonify(error="invalid subtitle mode"), 400

    try:
        video = safe_path(video_rel)
    except ValueError:
        return jsonify(error="invalid video path"), 400
    if not video.is_file():
        return jsonify(error="video not found"), 404
    if video.suffix.lower() not in VIDEO_EXTS:
        return jsonify(error="unsupported video type"), 400

    sub_paths = []
    temp_files_to_cleanup = []
    for s in subs:
        try:
            sp = safe_path(s.get("path", ""))
        except ValueError:
            return jsonify(error=f"invalid subtitle path: {s.get('path')}"), 400
        if not sp.is_file():
            return jsonify(error=f"subtitle not found: {s.get('path')}"), 404
        if sp.suffix.lower() not in SUBTITLE_EXTS:
            return jsonify(error=f"unsupported subtitle type: {s.get('path')}"), 400
        if _is_temp_subtitle(sp, video):
            temp_files_to_cleanup.append(sp)
        sub_paths.append((sp, s))

    try:
        out_path = _resolve_embed_output_path(video, out_name, use_default_output_dir)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    prepared_subs = []
    success = False
    resolved_pgs_resolution = _derive_pgs_resolution(video, pgs_options)

    try:
        for sp, meta in sub_paths:
            codec = "copy" if sp.suffix.lower() in PGS_SUBTITLE_EXTS else "srt"
            prepared_subs.append({"path": sp, "meta": meta, "codec": codec})

        has_pgs_subtitle = any(prepared["path"].suffix.lower() in PGS_SUBTITLE_EXTS for prepared in prepared_subs)
        muxer = "mkvmerge" if has_pgs_subtitle else "ffmpeg"
        warnings = _subtitle_warning_for_existing_pgs(prepared_subs)
        try:
            mux_plan = _build_mux_plan(video, prepared_subs, body)
            if muxer == "mkvmerge":
                cmd = _build_mkvmerge_embed_command(video, out_path, prepared_subs, keep_existing, mux_plan)
            else:
                cmd = _build_ffmpeg_embed_command(video, out_path, prepared_subs, keep_existing, mux_plan)
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 500
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

        try:
            subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=3600)
        except subprocess.CalledProcessError as exc:
            return jsonify(error=f"{muxer} failed", cmd=" ".join(shlex.quote(c) for c in cmd),
                           detail=exc.output.decode(errors="ignore")), 500

        success = True
        output_dir = rel_to_media(out_path.parent) if out_path.parent != MEDIA_DIR else ""
        return jsonify({
            "ok": True,
            "output": rel_to_media(out_path),
            "output_dir": output_dir,
            "subtitle_mode": subtitle_mode,
            "used_default_output_dir": use_default_output_dir,
            "muxer": muxer,
            "warnings": warnings,
            "pgs_settings": {
                "resolution_mode": pgs_options["resolution_mode"],
                "resolution": resolved_pgs_resolution,
                "framerate": pgs_options["framerate"],
                "applies_to": "ass_to_pgs_conversion_only",
                "applied_to_existing_pgs": False,
            },
            "cmd": " ".join(shlex.quote(c) for c in cmd),
        })
    finally:
        if success:
            for temp_file in temp_files_to_cleanup:
                if temp_file.exists():
                    temp_file.unlink()
            temp_dir = _video_temp_dir(video)
            if temp_dir.exists() and temp_dir.is_dir() and not any(temp_dir.iterdir()):
                temp_dir.rmdir()


if __name__ == "__main__":
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host=HOST, port=PORT, debug=False)
