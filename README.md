# Sub2API Async Image Sidecar

Sub2API 图片请求的异步任务 Sidecar。它接收图片生成或编辑请求，在后台调用 Sub2API 的 OpenAI 兼容图片接口，并通过任务查询接口返回状态和结果。

## 当前基线

`async_image_sidecar.py` 是从现有 VPS 部署提取的原始基线，尚未进行逻辑修改。导入时的 SHA-256：

```text
c933fd429b4db3ff1e51888c3fdb5ede2eea27ae81e766f81cc0acf06ec3152d
```

主要接口：

- `POST /v1/task/submit`：提交异步图片任务
- `POST /v1/task/upload`：上传任务使用的图片
- `GET /v1/task/{task_id}`：查询任务状态和结果
- `GET /health`：健康检查

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

## 已知问题

当前基线不具备数据保留期限和自动清理机制：

- 上游响应 JSON 可能包含 Base64 图片并被完整写入 SQLite。
- 输出图片和上传图片会持续保存在 `/data`。
- 任务队列没有容量上限。

请求量持续增长时，磁盘和内存都可能被耗尽。后续改造应优先增加 TTL 清理、去除数据库中的 Base64 响应、限制队列长度，并补充资源和并发测试。
