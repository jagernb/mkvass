# 字幕提取/封装工具

一个基于 Docker + Flask + ffmpeg 的小工具，提供 Web 界面，支持：

- 浏览挂载目录，列出视频 / 字幕文件
- 查看视频的内封字幕轨道（ffprobe）
- **提取**某条字幕轨道为 `.srt` / `.ass` / `.vtt`（位图字幕导出为 `.sup`）
- 将同目录下的外挂字幕**封装**为软字幕写入 `.mkv`（可多轨、可标记默认、可保留原有字幕）

## 目录结构

```
.
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── app
│   ├── server.py          # Flask 后端
│   └── static/index.html  # 前端界面
└── media/                 # 默认挂载目录（放视频/字幕）
```

## 启动

```bash
# 1. 在项目目录下准备 media/ 并放入你的视频文件
mkdir -p media

# 2. 构建并启动
docker compose up -d --build

# 3. 浏览器访问
http://localhost:8083
```

## 修改挂载目录

编辑 `docker-compose.yml` 中的 `volumes`：

```yaml
volumes:
  - /your/real/path:/media
```

## 说明

- **提取**：输出文件会保存到原视频同目录，命名为 `<原名>.track<索引>.<扩展>`。
- **封装**：默认输出 `<原名>.muxed.mkv`。音视频流直接 copy，不重新编码，速度很快；字幕统一转为 `srt` 写入 MKV。位图字幕（PGS/DVD/DVB）在 MKV 中同样支持但只能 copy，此工具的封装流程默认转为 srt，如需保留位图请在封装前手动处理。
- 所有 ffmpeg / ffprobe 命令都会在操作结果中展示，方便你了解实际调用。
- API 只允许访问 `/media` 目录内的文件，防止路径穿越。

## 常用 API

- `GET /api/list?path=` 列目录
- `GET /api/probe?path=<视频>` 查看流信息
- `POST /api/extract` body: `{path, stream_index, codec}`
- `POST /api/embed` body: `{video, subtitles:[{path,language,title,default}], keep_existing, out_name}`
