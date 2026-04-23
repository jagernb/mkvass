"""字幕提取/封装 Web 服务。

- 浏览挂载目录下的视频与字幕文件
- 通过 ffprobe 查看视频内封字幕流
- 提取指定字幕流到 .srt / .ass
- 将外挂字幕以软字幕（mkv）形式封装进视频
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote

from flask import Flask, jsonify, request, send_file, send_from_directory
from werkzeug.utils import secure_filename

MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", "/media")).resolve()
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
DEFAULT_OUTPUT_DIR = (os.environ.get("DEFAULT_OUTPUT_DIR") or "").strip()
PGS_CONVERTER_CMD = (os.environ.get("PGS_CONVERTER_CMD") or os.environ.get("ASS_TO_PGS_CMD") or "mkvtool").strip()
PGS_FONT_DIR = (os.environ.get("PGS_FONT_DIR") or os.environ.get("ASS_TO_PGS_FONT_DIR") or "/usr/share/fonts/truetype/dejavu").strip()
PGS_FRAMERATE = (os.environ.get("PGS_FRAMERATE") or os.environ.get("ASS_TO_PGS_FRAMERATE") or "23.976").strip() or "23.976"
PGS_RESOLUTION = (os.environ.get("PGS_RESOLUTION") or os.environ.get("ASS_TO_PGS_RESOLUTION") or "1920*1080").strip() or "1920*1080"

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".ts", ".m4v", ".flv", ".wmv"}
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
DOWNLOADABLE_SUBTITLE_EXTS = SUBTITLE_EXTS | {".sup"}
TMP_SUBTITLE_ROOT = MEDIA_DIR / ".tmp_subtitles"

app = Flask(__name__, static_folder="static", static_url_path="/static")


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


def _pgs_converter_available() -> bool:
    tool = _resolve_pgs_converter_command()
    font_dir = Path(PGS_FONT_DIR) if PGS_FONT_DIR else None
    return bool(tool and font_dir and font_dir.is_dir())


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

    return {
        "resolution_mode": resolution_mode,
        "resolution": resolution,
        "framerate": framerate,
    }


def _derive_pgs_resolution(video: Path, pgs_options: dict) -> str:
    if pgs_options["resolution_mode"] == "custom":
        return pgs_options["resolution"]

    dims = _video_dimensions(video)
    if dims is None:
        return PGS_RESOLUTION
    width, height = dims
    return f"{width}*{height}"


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
    subset_dir = work_dir / "subsetted"
    output_path = subset_dir / f"{subtitle_path.name}.pgs"

    cmd = [
        tool,
        "--enable-pgs-output",
        "--resolution",
        resolved_resolution,
        "--framerate",
        resolved_framerate,
        "subset",
        str(subtitle_path),
        "--font-dir",
        str(font_dir),
        "--output-dir",
        str(work_dir),
    ]

    try:
        subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=3600)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.output.decode(errors="ignore") or "pgs conversion failed") from exc

    if not output_path.exists():
        candidates = sorted(work_dir.rglob("*.pgs"))
        if candidates:
            output_path = candidates[0]
        else:
            raise RuntimeError("pgs converter did not produce a .pgs file")

    return output_path, " ".join(shlex.quote(c) for c in cmd), work_dir


def _cleanup_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


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
    subtitles = []
    for s in streams:
        if s.get("codec_type") != "subtitle":
            continue
        tags = s.get("tags") or {}
        subtitles.append({
            "index": s.get("index"),
            "codec": s.get("codec_name"),
            "language": tags.get("language"),
            "title": tags.get("title"),
            "disposition": s.get("disposition") or {},
        })

    return jsonify({
        "path": rel_to_media(target),
        "format": data.get("format", {}),
        "streams": streams,
        "subtitles": subtitles,
        "uploaded_subtitles": _list_uploaded_subtitles(target),
        "default_output_dir": _configured_default_output_dir_rel(),
        "pgs_defaults": _pgs_defaults(),
        "video_dimensions": _video_dimensions(target),
        "pgs_mode_available": _pgs_converter_available(),
        "pgs_mode_hint": "需要可用的 PGS 转换器和字体目录" if not _pgs_converter_available() else "",
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

    if not video_rel or not subs:
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
    conversion_cmds = []
    conversion_workdirs = []
    success = False
    resolved_pgs_resolution = _derive_pgs_resolution(video, pgs_options)

    try:
        for sp, meta in sub_paths:
            if subtitle_mode == "pgs_auto" and sp.suffix.lower() in {".ass", ".ssa"}:
                try:
                    converted_path, conversion_cmd, work_dir = _convert_ass_to_pgs(
                        sp,
                        resolution=resolved_pgs_resolution,
                        framerate=pgs_options["framerate"],
                    )
                except (RuntimeError, ValueError) as exc:
                    return jsonify(error=str(exc)), 400
                prepared_subs.append({"path": converted_path, "meta": meta, "codec": "copy"})
                conversion_cmds.append(conversion_cmd)
                conversion_workdirs.append(work_dir)
            else:
                prepared_subs.append({"path": sp, "meta": meta, "codec": "srt"})

        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(video)]
        for prepared in prepared_subs:
            cmd += ["-i", str(prepared["path"])]

        cmd += ["-map", "0:v?", "-map", "0:a?"]
        if keep_existing:
            cmd += ["-map", "0:s?"]
        for i in range(len(prepared_subs)):
            cmd += ["-map", f"{i + 1}:0"]

        cmd += ["-c:v", "copy", "-c:a", "copy"]

        base_sub_index = 0
        if keep_existing:
            try:
                base_sub_index = _count_subtitle_streams(video)
            except Exception:
                base_sub_index = 0
            for idx in range(base_sub_index):
                cmd += [f"-c:s:{idx}", "copy"]

        for i, prepared in enumerate(prepared_subs):
            out_idx = base_sub_index + i
            cmd += [f"-c:s:{out_idx}", prepared["codec"]]
            meta = prepared["meta"]
            lang = meta.get("language") or "und"
            title = meta.get("title") or ""
            cmd += [f"-metadata:s:s:{out_idx}", f"language={lang}"]
            if title:
                cmd += [f"-metadata:s:s:{out_idx}", f"title={title}"]
            if meta.get("default"):
                cmd += [f"-disposition:s:{out_idx}", "default"]

        cmd.append(str(out_path))

        try:
            subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=3600)
        except subprocess.CalledProcessError as exc:
            return jsonify(error="ffmpeg failed", cmd="\n".join(conversion_cmds + [" ".join(shlex.quote(c) for c in cmd)]),
                           detail=exc.output.decode(errors="ignore")), 500

        success = True
        output_dir = rel_to_media(out_path.parent) if out_path.parent != MEDIA_DIR else ""
        return jsonify({
            "ok": True,
            "output": rel_to_media(out_path),
            "output_dir": output_dir,
            "subtitle_mode": subtitle_mode,
            "used_default_output_dir": use_default_output_dir,
            "pgs_settings": {
                "resolution_mode": pgs_options["resolution_mode"],
                "resolution": resolved_pgs_resolution,
                "framerate": pgs_options["framerate"],
            },
            "cmd": "\n".join(conversion_cmds + [" ".join(shlex.quote(c) for c in cmd)]),
        })
    finally:
        for work_dir in conversion_workdirs:
            _cleanup_dir(work_dir)
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
