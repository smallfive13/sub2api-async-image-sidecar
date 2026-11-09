# Sub2API Async Image Sidecar

图片请求的异步任务 Sidecar。它接收图片生成、编辑或 Responses 图片工具请求，在后台按 provider 调用 Sub2API 或原生 OpenAI，并通过任务查询接口返回状态和结果。默认使用 GPT Image 2.6 Flare 生成、Sunburst 编辑；调用方仍可显式指定其他兼容模型。

支持 `provider=auto|sub2api|openai` 选路：常规图片默认走 Sub2API，SVG/GIF 自动选择原生 OpenAI；调用方可显式固定渠道，失败不会自动换渠道或模型。官方路由需要服务器 API Key、调用者允许名单及有效的 Sub2API 鉴权。Grok、Gemini 后续可使用相同字段接入，当前尚未实现。详见 [Provider 路由](docs/API_USAGE.md#provider-路由)。

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

## 模型选择与费用

GPT Image 2.6 的 Flare 和 Sunburst 都支持图片生成和编辑。Flare 适合快速迭代，Sunburst 适合精细构图和编辑。2.6 通过 Images API 新增 `complexity`、markup 引导编辑、SVG 和动画 GIF 输出。

| 模型 | 定位 | 支持场景 | 质量档位 |
| --- | --- | --- | --- |
| `gpt-image-2.6-flare` | 默认生图模型，速度优先 | 日常生图、快速编辑、SVG 和动画 GIF | `low`、`medium`、`high`、`xhigh`、`max`、`auto` |
| `gpt-image-2.6-sunburst` | 默认编辑模型，精细视觉工作 | 复杂布局、文字、精确编辑、SVG 和动画 GIF | `low`、`medium`、`high`、`xhigh`、`max`、`auto` |
| `gpt-image-2.5-flare` | GPT Image 2.5 中速度优先 | 日常生图、快速编辑、批量草稿 | `low`、`medium`、`high`、`xhigh`、`max`、`auto` |
| `gpt-image-2.5-sunburst` | GPT Image 2.5 中质量优先 | 高质量生图、精确编辑、主体和布局保持 | `low`、`medium`、`high`、`xhigh`、`max`、`auto` |
| `gpt-image-2` | 上一代基线 | 常规生图和编辑 | `low`、`medium`、`high`、`auto` |
| `gpt-image-1.5` | 更早的兼容模型 | 常规生图和编辑，取决于上游权限 | `low`、`medium`、`high`、`auto` |

Sidecar 默认使用 2.6 Flare 生成、2.6 Sunburst 编辑；请求显式传入 `model` 时始终以请求值为准，因此仍可用 Sunburst 生图、Flare 编辑或继续调用 2.5 和 `gpt-image-2`。

旧客户端已指定 `model` 时无需改动。旧客户端省略 `model` 时会切换到 2.6；要保留旧模型行为，应显式填写 `gpt-image-2.5-flare` 或 `gpt-image-2.5-sunburst`。各模型使用自身支持的参数，调用仍受账号和分组模型权限约束。旧版调用示例和各接口选模规则见 [模型选择与旧版本兼容](docs/API_USAGE.md#模型选择与旧版本兼容)。

2026-11-09 升级后的线上兼容性验证已通过：2.5 Flare 生图、2.5 Sunburst 编辑、`gpt-image-2` 和 `gpt-image-1.5` 生图，均完成任务并成功下载 PNG。

2026-11-09 核对的 GPT Image 2.6 标准 API 单价如下；2.5 和 GPT Image 2 的数值保留自 2026-09-10 的记录。单位均为美元/100 万 tokens：

| 模型 | 文本输入 | 图片输入 | 图片输出 |
| --- | ---: | ---: | ---: |
| `gpt-image-2.6-flare` | `$5.00` | `$8.00` | `$30.00` |
| `gpt-image-2.6-sunburst` | `$5.00` | `$8.00` | `$30.00` |
| `gpt-image-2.5-flare` | `$5.00` | `$8.00` | `$30.00` |
| `gpt-image-2.5-sunburst` | `$5.00` | `$8.00` | `$30.00` |
| `gpt-image-2` | `$2.50` | `$4.00` | `$15.00` |

缓存输入价格分别为：GPT Image 2.6 和 2.5 文本 `$1.25`、图片 `$2.00`；GPT Image 2 文本 `$0.625`、图片 `$1.00`。每张图不是固定价，最终费用由实际输入、尺寸、质量和输出 token 数决定。编辑任务还会计入输入图片 token；通过 Responses API 进行推理辅助或多轮编辑时，还会叠加顶层推理模型的 token 费用。SVG 还包含将参考图转换成矢量的模型费用。`complexity` 没有单独收费。Sub2API 的最终扣费可能继续受到分组倍率和渠道定价影响。

官方参考：[图片生成指南](https://developers.openai.com/api/docs/guides/image-generation)、[GPT Image 2.6 Flare](https://developers.openai.com/api/docs/models/gpt-image-2.6-flare)、[GPT Image 2.6 Sunburst](https://developers.openai.com/api/docs/models/gpt-image-2.6-sunburst)、[OpenAI Pricing](https://developers.openai.com/api/docs/pricing)。

2026-11-09 现有订阅渠道实测：默认 2.6 Flare PNG 生图和 2.6 Sunburst multipart 编辑已通过；请求 SVG 实际返回 PNG，GIF 返回“未启用”，指定 size 与实际像素也存在差异。建议该渠道使用 PNG，并读取实际文件格式、尺寸。完整结果见 [升级验证记录](docs/IMAGE26_VALIDATION.md)。

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

使用原生 OpenAI 时，在服务器 Shell 设置环境变量后启动容器。`OPENAI_API_KEY` 是官方项目 Key，`ASYNC_IMAGE_OPENAI_ALLOWED_API_KEYS` 是允许使用该官方额度的调用者 Sub2API Key，可用逗号分隔多个。以下命令通过隐藏输入配置，不把 Key 写入本地配置文件或命令历史：

例如允许名单只有你自己的一个 Sub2API Key 时，你仍用该 Key 请求 Sidecar；官方渠道由服务器代为调用，官方 Key 不返回给客户端。其他调用者仍可使用 Sub2API，但不能使用这把服务器官方 Key。这里配置的是 Sidecar 的调用权限，不会修改 OpenAI 组织或 API Key 的权限设置。

```bash
read -r -s -p "OpenAI server API key: " OPENAI_API_KEY
printf '\n'
export OPENAI_API_KEY
read -r -s -p "Allowed caller Sub2API keys (comma separated): " ASYNC_IMAGE_OPENAI_ALLOWED_API_KEYS
printf '\n'
export ASYNC_IMAGE_OPENAI_ALLOWED_API_KEYS
docker compose up -d --build async-image-sidecar
```

Compose 模板会把变量传入 Sidecar；单独在宿主机 `export` 不会修改已经运行的容器。原 VPS 的 `sub2api-async-image-mvp` 使用不同的 Compose 文件，需要在其服务环境中加入同名变量并重新创建该服务，不能直接套用上面的服务名。官方 Key 和允许名单为空时，Sub2API 常规请求仍可用，但 SVG/GIF 的自动路由会返回配置或授权错误。

官方调用在提交和执行前各做一次 Sub2API Key 校验；不会通过 Sub2API 代理生成请求或记录该用量的扣费。官方 `usage`、实际 provider、选路原因和 request ID 保存在任务结果中。服务器官方 Key 不写入 SQLite，`Config` 的打印内容也不会包含它或允许名单。

升级现有部署时，同时检查服务器 `.env` 中的 `ASYNC_IMAGE_DEFAULT_GENERATION_MODEL` 和 `ASYNC_IMAGE_DEFAULT_EDIT_MODEL`；显式设置的旧值会覆盖代码和 Compose 的新默认值。上游 Sub2API 也必须支持两个 2.6 模型及所需字段，账号模型映射和分组白名单需要允许这些模型。仅更新 Sidecar 不能保证上游可用或定价正确，应先显式指定模型完成真实生图和编辑验证，再切换线上默认值。

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
ASYNC_IMAGE_DEFAULT_GENERATION_MODEL=gpt-image-2.6-flare
ASYNC_IMAGE_DEFAULT_EDIT_MODEL=gpt-image-2.6-sunburst
```

将 `ASYNC_IMAGE_TASK_RETENTION_SECONDS` 设为 `0` 可以禁用自动过期。文件 URL 在对应文件过期后会返回 404。

## 测试

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
```

测试使用本地模拟的 Sub2API 与官方上游，覆盖 provider 自动/显式选路、服务端凭据隔离、调用者授权与撤销、无跨渠道重试、历史数据库升级、2.6 参数和 markup 透传、SVG/GIF 格式及下载 MIME、显式旧模型兼容，以及原有异步队列和清理流程。真实模型和线上部署需要单独验证。

## 剩余边界

上游响应仍会先完整读入内存再解析，JSON 和 multipart 上传也会在各自限制内完整读入内存。因此当前实现控制了长期持久化和队列增长，但不能消除单个大请求的瞬时内存峰值。上传默认上限为 `50 MiB`；JSON 默认上限为 `70 MiB`，用于容纳约 `50 MiB` 图片的 Base64 膨胀。部署方可按内存预算调低。

代码仍使用 Python 3.12 的 `cgi` 模块处理 multipart；该模块在 Python 3.13 已移除，升级基础镜像前需要替换 multipart 解析实现。
