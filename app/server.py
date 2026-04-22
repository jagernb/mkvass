"""字幕提取/封装 Web 服务。

- 浏览挂载目录下的视频与字幕文件
- 通过 ffprobe 查看视频内封字幕流
- 提取指定字幕流到 .srt / .ass
- 将外挂字幕以软字幕（mkv）形式封装进视频
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", "/media")).resolve()
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".ts", ".m4v", ".flv", ".wmv"}
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub"}

app = Flask(__name__, static_folder="static", static_url_path="/static")


def safe_path(rel: str) -> Path:
    """把相对路径解析到 MEDIA_DIR 下，禁止越权访问。"""
    rel = (rel or "").lstrip("/\\")
    target = (MEDIA_DIR / rel).resolve()
    if MEDIA_DIR != target and MEDIA_DIR not in target.parents:
        raise ValueError("path escapes MEDIA_DIR")
    return target


def rel_to_media(p: Path) -> str:
    return str(p.relative_to(MEDIA_DIR)).replace("\\", "/")


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


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
    if not out_name:
        out_name = f"{target.stem}.track{stream_index}{ext}"
    else:
        if Path(out_name).suffix.lower() not in (ext, ".srt", ".ass", ".vtt"):
            out_name = out_name + ext

    out_path = (target.parent / out_name).resolve()
    if MEDIA_DIR not in out_path.parents and out_path != MEDIA_DIR:
        return jsonify(error="output path invalid"), 400

    # 对于位图字幕（pgs/dvd/dvb）只能复制，不能转文本
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
        "cmd": " ".join(shlex.quote(c) for c in cmd),
    })


@app.route("/api/embed", methods=["POST"])
def api_embed():
    """把一个或多个外挂字幕以软字幕形式封装进视频（输出 .mkv）。"""
    body = request.get_json(silent=True) or {}
    video_rel = body.get("video")
    subs = body.get("subtitles") or []  # [{path, language, title, default}]
    out_name = body.get("out_name")
    keep_existing = bool(body.get("keep_existing", True))

    if not video_rel or not subs:
        return jsonify(error="video and subtitles required"), 400

    try:
        video = safe_path(video_rel)
    except ValueError:
        return jsonify(error="invalid video path"), 400
    if not video.is_file():
        return jsonify(error="video not found"), 404

    sub_paths = []
    for s in subs:
        try:
            sp = safe_path(s.get("path", ""))
        except ValueError:
            return jsonify(error=f"invalid subtitle path: {s.get('path')}"), 400
        if not sp.is_file():
            return jsonify(error=f"subtitle not found: {s.get('path')}"), 404
        sub_paths.append((sp, s))

    if not out_name:
        out_name = f"{video.stem}.muxed.mkv"
    if not out_name.lower().endswith(".mkv"):
        out_name += ".mkv"
    out_path = (video.parent / out_name).resolve()
    if MEDIA_DIR not in out_path.parents and out_path != MEDIA_DIR:
        return jsonify(error="output path invalid"), 400

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(video)]
    for sp, _ in sub_paths:
        cmd += ["-i", str(sp)]

    # 映射视频/音频
    cmd += ["-map", "0:v?", "-map", "0:a?"]
    # 保留原有字幕
    if keep_existing:
        cmd += ["-map", "0:s?"]
    # 映射外挂字幕
    for i in range(len(sub_paths)):
        cmd += ["-map", f"{i + 1}:0"]

    # 复制音视频，字幕重新编码成 mkv 支持的格式
    cmd += ["-c:v", "copy", "-c:a", "copy", "-c:s", "srt"]

    # 为每条新加字幕设置元数据
    base_sub_index = 0  # 相对于输出中字幕流的序号
    # 如果保留原字幕，需要先统计原字幕数量
    if keep_existing:
        try:
            probe = subprocess.check_output([
                "ffprobe", "-v", "error", "-select_streams", "s",
                "-show_entries", "stream=index", "-of", "json", str(video),
            ], timeout=60)
            base_sub_index = len(json.loads(probe.decode()).get("streams", []))
        except Exception:
            base_sub_index = 0

    for i, (_, meta) in enumerate(sub_paths):
        out_idx = base_sub_index + i
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
        return jsonify(error="ffmpeg failed", cmd=" ".join(shlex.quote(c) for c in cmd),
                       detail=exc.output.decode(errors="ignore")), 500

    return jsonify({
        "ok": True,
        "output": rel_to_media(out_path),
        "cmd": " ".join(shlex.quote(c) for c in cmd),
    })


if __name__ == "__main__":
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host=HOST, port=PORT, debug=False)
