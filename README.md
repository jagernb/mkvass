# 字幕提取/封装工具

一个基于 Docker + Flask + ffmpeg 的小工具，提供 Web 界面，支持：

- 浏览挂载目录，列出视频 / 字幕文件
- 查看视频的内封字幕轨道（ffprobe）
- **提取**某条字幕轨道为 `.srt` / `.ass` / `.vtt`（位图字幕导出为 `.sup`，并支持浏览器直接下载）
- **将同目录下的外挂字幕封装为 `.mkv`**（可多轨、可标记默认、可保留原有字幕）
- 可配置默认输出路径，并在封装时选择是否使用
- 浏览器上传的 `.ass/.ssa` 可在封装时转换为 PGS 位图字幕后再封装（镜像内已内置 Linux 可用转换器）

## 目录结构

```text
.
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── app
│   ├── server.py          # Flask 后端
│   └── static/index.html  # 前端界面
└── media/                 # 默认挂载目录（放视频/字幕）
```

## 安装与启动

### 使用 Docker 镜像安装

Docker Hub 镜像地址：

```text
jagernb/mkvass:latest
```

1. 准备一个宿主机目录用于存放视频和字幕，例如：

```bash
mkdir -p /opt/mkvass/media
cd /opt/mkvass
```

1. 如果镜像仓库是私有的，先登录 Docker Hub：

```bash
docker login
```

1. 创建 `docker-compose.yml`：

```yaml
services:
  subtitle-tool:
    image: jagernb/mkvass:${MKVASS_TAG:-latest}
    container_name: subtitle-tool
    ports:
      - "8083:8080"
    volumes:
      - /opt/mkvass/media:/media
    environment:
      - MEDIA_DIR=/media
      - PORT=8080
      - DEFAULT_OUTPUT_DIR=output
      - PGS_CONVERTER_CMD=mkvtool
      - MKVMERGE_CMD=mkvmerge
      - PGS_FONT_DIR=/usr/share/fonts/truetype/dejavu
      - PGS_FRAMERATE=23.976
      - PGS_RESOLUTION=1920*1080
    restart: unless-stopped
```

1. 拉取镜像并启动：

```bash
docker compose pull
docker compose up -d
```

默认会使用 `latest`。如果你想锁定正式版本，可在启动前设置环境变量，例如：

```bash
export MKVASS_TAG=1.1.3
docker compose pull
docker compose up -d
```

你也可以设置默认封装输出目录，例如把所有封装结果统一输出到 `/media/output`：

```bash
export DEFAULT_OUTPUT_DIR=output
docker compose up -d
```

1. 浏览器访问：

```text
http://localhost:8083
```

后续更新时重复执行：

```bash
docker compose pull
docker compose up -d
```

### 本地构建启动

如果你想基于当前仓库源码本地构建，也可以使用：

```bash
# 1. 在项目目录下准备 media/ 并放入你的视频文件
mkdir -p media

# 2. 本地构建镜像并启动
docker build -t jagernb/mkvass:latest .
docker compose up -d

# 3. 浏览器访问
http://localhost:8083
```

## 修改挂载目录

编辑 `docker-compose.yml` 中的 `volumes`：

```yaml
volumes:
  - /your/real/path:/media
```

## 自动发布镜像

仓库新增了 GitHub Actions 工作流 [docker-image.yml](.github/workflows/docker-image.yml)：

- push 到 `main` 时自动构建镜像
- push `v*` Git tag（例如 `v1.1.3`）时自动发布正式版本标签
- 自动推送到 Docker Hub
- 默认发布 `latest`、分支名、commit sha 标签
- Git tag 发布时额外生成 `1.1.3`、`1.1` 这类版本标签
- 镜像会写入 OCI 标签 `org.opencontainers.image.version` 与 `org.opencontainers.image.created`，容器内也可读取 `APP_VERSION` 和 `BUILD_DATE`

首次启用时请确认：

- 在 GitHub 仓库 Secrets 中配置 `DOCKERHUB_USERNAME`
- 在 GitHub 仓库 Secrets 中配置 `DOCKERHUB_TOKEN`
- 如果部署端要匿名拉取，需要把 Docker Hub 仓库设为 public

### 发布正式版本

当你需要发布一个可固定部署、可回退的正式版本时：

```bash
git tag v1.1.3
git push origin v1.1.3
```

随后 GitHub Actions 会自动发布这些镜像标签：

- `jagernb/mkvass:1.1.3`
- `jagernb/mkvass:1.1`

### 回退到旧版本

如果需要回退，只要把部署机上的 `MKVASS_TAG` 改成旧版本号，再重新拉取并启动：

```bash
export MKVASS_TAG=1.1.3
docker compose pull
docker compose up -d
```

## 说明

- **提取**：输出文件会保存到原视频同目录，命名为 `<原名>.track<索引>.<扩展>`，操作完成后可直接在对应字幕轨的“提取”按钮下方点击下载。
- **封装**：默认输出 `<原名>.muxed.mkv`。如果配置了 `DEFAULT_OUTPUT_DIR`，则浏览器封装时可勾选“使用默认输出路径”，把结果统一输出到该目录；未勾选时仍输出到原视频同目录。文本字幕模式下外挂字幕会转为 `srt` 写入 MKV。
- **封装设置区**：视频详情里的“封装设置”会集中显示默认输出目录与 PGS 参数。默认输出目录仅提供“是否使用默认目录”的开关，不支持在页面里临时改成任意别的目录。
- **ASS 转 PGS**：浏览器封装区可选择“ASS 转 PGS 后封装”。该模式仅对 `.ass/.ssa` 生效。Docker 镜像内默认内置 Linux 可用的 `mkvtool` 转换器，因此正常启动后前端选项会直接可用；如果你自行覆盖相关环境变量，也可以改成别的兼容转换器。
- **PGS 尺寸与帧率**：前端默认会以“跟随视频”模式自动读取当前视频分辨率，也可以切换为自定义尺寸；帧率也可按这次封装单独填写。未填写请求级值时，后端会回退到环境变量默认配置。
- **PGS 配置项**：
  - `PGS_CONVERTER_CMD`：PGS 转换器命令，默认 `mkvtool`
  - `PGS_FONT_DIR`：字体目录，默认 `/usr/share/fonts/truetype/dejavu`
  - `PGS_FRAMERATE`：PGS 帧率，默认 `23.976`
  - `PGS_RESOLUTION`：PGS 分辨率，默认 `1920*1080`
  - 为兼容旧配置，后端仍接受 `ASS_TO_PGS_CMD`、`ASS_TO_PGS_FONT_DIR`、`ASS_TO_PGS_FRAMERATE`、`ASS_TO_PGS_RESOLUTION`
- **临时上传字幕**：浏览器上传的 `.srt/.ass/.ssa/.vtt/.sub` 会保存到按视频文件名隔离的隐藏临时目录，只在当前视频详情中显示；封装成功后会清理本次参与封装的临时字幕，封装失败时会保留以便重试。
- 所有 ffmpeg / ffprobe / 转换器命令都会在操作结果中展示，方便你了解实际调用。
- API 只允许访问 `/media` 目录内的文件，防止路径穿越。

## 常用 API

- `GET /api/list?path=` 列目录
- `GET /api/probe?path=<视频>` 查看流信息，并返回当前视频的临时上传字幕
- `GET /api/download?path=<字幕路径>` 下载提取出的字幕文件
- `POST /api/extract` body: `{path, stream_index, codec}`
- `POST /api/upload-subtitle` form-data: `video=<视频路径>`, `file=<字幕文件>`
- `POST /api/embed` body: `{video, subtitles:[{path,language,title,default}], keep_existing, out_name, subtitle_mode, use_default_output_dir, pgs_options}`
