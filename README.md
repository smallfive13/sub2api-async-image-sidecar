# Sub2API Async Image Sidecar

Sub2API 图片请求的异步任务 Sidecar。它接收图片生成、编辑或 Responses 图片工具请求，在后台调用 Sub2API，并通过任务查询接口返回状态和结果。默认使用 GPT Image 2.5 Flare 生成、Sunburst 编辑；调用方仍可显式指定其他兼容模型。

## 来源

初始版本从现有 VPS 部署提取。未修改基线的 SHA-256：

```text
c933fd429b4db3ff1e51888c3fdb5ede2eea27ae81e766f81cc0acf06ec3152d
```

## 接口概览

- `POST /v1/images/generations`：提交图片生成任务
- `POST /v1/images/edits`：提交图片编辑任务
- `POST /v1/images/responses`：提交 Responses 图片工具任务，支持推理辅助和多轮编辑
- `POST /v1/task/submit`：统一提交生成或编辑任务，可配置回调
- `GET /v1/task/{task_id}`：查询任务状态和结果

服务器地址、鉴权、完整 curl 示例、图片下载和回调说明见 [API 使用说明](docs/API_USAGE.md)。

## 安全边界

仓库不包含生产环境的 `.env`、SQLite 数据库、图片、上传文件或 API Key。不要提交以下内容：

- `.env`
- `*.sqlite3*` 或 `*.db*`
- `data/`、`images/`、`uploads/`
- `docker compose config` 的完整输出，因为它会展开环境变量中的秘密

首次部署前生成独立的 Token Secret：

```bash
openssl rand -hex 32
```

把生成结果写入服务器本地 `.env` 的 `ASYNC_IMAGE_TOKEN_SECRET`，不要写入 Git。

## 本地构建

```bash
cp .env.example .env
docker compose build
docker compose up -d
docker compose ps
```

Sidecar 默认只监听宿主机 `127.0.0.1:8090`，并连接已经存在的 Sub2API Docker 网络。若网络名称不同，修改 `.env` 中的 `SUB2API_DOCKER_NETWORK`。

## 数据生命周期和过载保护

默认配置：

- 终态任务、输出图片和上传图片保留 `86400` 秒（24 小时）。
- 清理线程每 `600` 秒执行一次。
- 等待队列最多容纳 `100` 个任务；队列满时返回 HTTP `503 server_overloaded`。
- 任务完成或失败后，立即清除原始请求和 API Key 密文。
- SQLite 中的响应和输出不会持久化 `b64_json` 或 Responses 图片调用的 `result`；Base64 图片成功落盘后只返回文件 URL。
- 启动时会压缩旧版本留下的终态记录，并回收其 SQLite 空间。

通过以下环境变量调整：

```text
ASYNC_IMAGE_TASK_RETENTION_SECONDS=86400
ASYNC_IMAGE_CLEANUP_INTERVAL_SECONDS=600
ASYNC_IMAGE_MAX_QUEUE_SIZE=100
ASYNC_IMAGE_DEFAULT_GENERATION_MODEL=gpt-image-2.5-flare
ASYNC_IMAGE_DEFAULT_EDIT_MODEL=gpt-image-2.5-sunburst
```

将 `ASYNC_IMAGE_TASK_RETENTION_SECONDS` 设为 `0` 可以禁用自动过期。文件 URL 在对应文件过期后会返回 404。

## 测试

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
```

测试覆盖终态数据压缩、TTL 清理、队列满载 503、积压任务恢复、GPT Image 2.5 参数透传，以及 Image API/Responses API 从提交任务到读取图片的完整 HTTP 流程。

## 剩余边界

上游响应仍会先完整读入内存再解析，JSON 和 multipart 上传也会在各自限制内完整读入内存。因此当前实现控制了长期持久化和队列增长，但不能消除单个大请求的瞬时内存峰值。上传默认上限为 `50 MiB`；JSON 默认上限为 `70 MiB`，用于容纳约 `50 MiB` 图片的 Base64 膨胀。部署方可按内存预算调低。

代码仍使用 Python 3.12 的 `cgi` 模块处理 multipart；该模块在 Python 3.13 已移除，升级基础镜像前需要替换 multipart 解析实现。
