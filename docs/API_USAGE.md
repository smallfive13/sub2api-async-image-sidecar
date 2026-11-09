# 异步图片 API 使用说明

## 服务地址和鉴权

当前线上地址：

```text
http://api.cyrus88.top:8090
```

`8090` 是异步图片 Sidecar；Sub2API 原有的聊天、Responses 等接口仍使用 `8080`。任务提交和查询使用用户自己的 Sub2API API Key：

```http
Authorization: Bearer sk-用户自己的Sub2APIKey
```

主要接口：

- `POST /v1/images/generations`：提交图片生成任务
- `POST /v1/images/edits`：使用 multipart 表单提交图片编辑任务
- `POST /v1/images/responses`：提交 Responses 图片工具任务，支持推理辅助和多轮编辑
- `POST /v1/task/submit`：使用统一 JSON 结构提交生成或编辑任务，可配置回调
- `POST /v1/task/upload`：单独上传任务使用的图片
- `GET /v1/task/{task_id}`：查询任务状态和结果
- `GET /v1/task/images/{filename}`：读取生成的图片
- `GET /health`：健康检查

## 客户端变量和健康检查

以下示例使用 shell 变量，避免把 API Key 直接写入脚本：

```bash
export ASYNC_IMAGE_BASE_URL="http://api.cyrus88.top:8090"
export SUB2API_API_KEY="sk-替换成自己的Sub2APIKey"

curl -sS "$ASYNC_IMAGE_BASE_URL/health"
```

正常返回：

```json
{"status":"ok"}
```

## 模型选择与旧版本兼容

**旧模型仍可显式调用。** 本次升级修改了省略 `model` 时的默认值；显式传入的模型名会保留并发送给 Sub2API。

| 调用方式 | 模型选择规则 |
| --- | --- |
| `/v1/images/generations` 不传 `model` | 默认 `gpt-image-2.6-flare` |
| `/v1/images/edits` 的 multipart 表单不传 `model` | 默认 `gpt-image-2.6-sunburst` |
| 上述接口显式传入 `model` | 按请求值调用，可继续选择 `gpt-image-2.5-flare`、`gpt-image-2.5-sunburst`、`gpt-image-2`、`gpt-image-1.5` 等上游支持的模型 |
| `/v1/task/submit` | 必须在顶层或 `input` 中提供 `model`；顶层值优先 |
| `/v1/images/responses` | 顶层 `model` 是推理模型，图片工具模型在 `tools[].model` 中指定；不会自动套用上述 Images API 默认模型 |

原客户端如果已经显式指定 2.5，继续使用原请求即可。原客户端如果一直省略 `model`，升级后会使用 2.6；需要固定旧行为时补上对应的 2.5 模型名。Sidecar 不会在模型失败后自动把请求改为其他版本，失败原因通过任务的 `error` 返回。

2026-11-09 在升级后的线上服务中，已实测通过 2.5 Flare 生图、2.5 Sunburst multipart 编辑、`gpt-image-2` 生图和 `gpt-image-1.5` 生图，均完成异步任务并成功下载 PNG。测试使用显式模型和 `quality=low`，不代表所有参数组合均已验证。

继续使用 2.5 Flare 生图：

```bash
curl -sS "$ASYNC_IMAGE_BASE_URL/v1/images/generations" \
  -H "Authorization: Bearer $SUB2API_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-image-2.5-flare",
    "prompt": "一个绿色陶瓷杯，白色背景",
    "size": "1024x1024",
    "quality": "high",
    "output_format": "png",
    "n": 1
  }'
```

继续使用 2.5 Sunburst 编辑：

```bash
curl -sS "$ASYNC_IMAGE_BASE_URL/v1/images/edits" \
  -H "Authorization: Bearer $SUB2API_API_KEY" \
  -F "model=gpt-image-2.5-sunburst" \
  -F "prompt=只把杯子改为绿色，保留形状和白色背景" \
  -F "image=@input.png" \
  -F "quality=high" \
  -F "output_format=png"
```

调用旧模型时使用该模型支持的参数；增加 `complexity`、markup 或动画参数前，先确认模型及渠道是否支持。最终可用性还取决于所用 API Key 的分组、模型白名单、账号权限和上游服务。逐项线上验证结果见 [升级验证记录](IMAGE26_VALIDATION.md)。

## 生成图片

省略 `model` 时默认使用 `gpt-image-2.6-flare`。需要最高质量时显式改为 `gpt-image-2.6-sunburst`。

```bash
curl -sS "$ASYNC_IMAGE_BASE_URL/v1/images/generations" \
  -H "Authorization: Bearer $SUB2API_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-image-2.6-flare",
    "prompt": "一张现代办公室里的产品海报，明亮自然光",
    "size": "1024x1024",
    "quality": "max",
    "complexity": "medium",
    "response_format": "b64_json",
    "output_format": "png",
    "n": 1
  }'
```

Sidecar 不会等待图片生成完成，而是立即返回 HTTP `202` 和任务 ID：

```json
{
  "id": "task_xxx",
  "status": "queued",
  "model": "gpt-image-2.6-flare",
  "endpoint": "/v1/images/generations",
  "created_at": 1782020000,
  "updated_at": 1782020000
}
```

保存响应中的 `id`，随后通过任务查询接口轮询状态。

## 编辑图片

省略 `model` 时默认使用更适合精确编辑的 `gpt-image-2.6-sunburst`。`image`、`image[]` 和 `image[0]` 等字段名均可提交一个或多个参考图。

直接使用 `multipart/form-data` 上传本地图片：

```bash
curl -sS "$ASYNC_IMAGE_BASE_URL/v1/images/edits" \
  -H "Authorization: Bearer $SUB2API_API_KEY" \
  -F "model=gpt-image-2.6-sunburst" \
  -F "prompt=保留主体，把背景改成明亮的现代办公室" \
  -F "image=@input.png" \
  -F "size=1024x1024" \
  -F "quality=xhigh" \
  -F "complexity=medium" \
  -F "response_format=b64_json"
```

需要限定编辑区域时增加 mask：

```bash
curl -sS "$ASYNC_IMAGE_BASE_URL/v1/images/edits" \
  -H "Authorization: Bearer $SUB2API_API_KEY" \
  -F "model=gpt-image-2.6-sunburst" \
  -F "prompt=只修改 mask 区域，把背景改成办公室" \
  -F "image=@input.png" \
  -F "mask=@mask.png" \
  -F "size=1024x1024" \
  -F "quality=xhigh" \
  -F "complexity=medium" \
  -F "response_format=b64_json"
```

编辑接口同样返回 HTTP `202` 和任务 ID。单个 multipart 请求默认最大 `50 MiB`，可通过 `ASYNC_IMAGE_MAX_UPLOAD_BYTES` 调整。

`POST /v1/images/edits` 接受 multipart 表单。使用 JSON 组织编辑请求时，改用统一任务入口，并提供显式模型：

```bash
curl -sS "$ASYNC_IMAGE_BASE_URL/v1/task/submit" \
  -H "Authorization: Bearer $SUB2API_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "endpoint": "edits",
    "model": "gpt-image-2.6-sunburst",
    "input": {
      "prompt": "保留主体，把背景改成明亮的现代办公室",
      "images": [{"image_url": "data:image/png;base64,替换为图片Base64"}],
      "quality": "high",
      "complexity": "medium",
      "output_format": "png"
    }
  }'
```

GPT Image 2.6 支持 `low`、`medium`、`high`、`xhigh`、`max` 和 `auto` 质量档位，也支持满足官方约束的自定义 `WIDTHxHEIGHT` 尺寸。

## GPT Image 2.6 新参数

以下功能需要上游 Sub2API 和账号同时支持。Sidecar 的 JSON 请求透传这些字段，multipart 编辑也会保留 `complexity`、`markup`、`frame_count` 和 `frame_duration_ms`。现有部署如果仍显式配置 2.5 默认值，需更新配置并重新部署后才会切换到 2.6。

2026-11-09 线上验证已通过 2.6 默认 PNG 生图和 multipart 编辑。当前订阅渠道的 SVG 请求实际返回 PNG，GIF 请求返回 HTTP 400：`GIF output is not enabled for this request.`；指定 `size=1024x1024` 也出现了其他实际像素尺寸。该渠道当前建议使用 PNG，按 `outputs[].file` 的实际扩展名和文件尺寸处理结果。下方 SVG/GIF 示例用于支持相应能力的上游；详细证据见 [升级验证记录](IMAGE26_VALIDATION.md)。

`complexity` 控制渲染前的规划程度，支持 `low`、`medium`、`high`，省略时上游默认为 `high`；它与渲染质量 `quality` 独立。建议用 `low` 做简单改色和草稿，复杂文字或布局再比较更高档位。

生成 SVG 时，仅使用生成接口，设 `n=1`，不传 `stream`、`partial_images` 或 `output_compression`：

```bash
curl -sS "$ASYNC_IMAGE_BASE_URL/v1/images/generations" \
  -H "Authorization: Bearer $SUB2API_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-image-2.6-flare",
    "prompt": "一个简洁的蓝色咖啡杯矢量图标，使用可编辑路径",
    "quality": "low",
    "complexity": "low",
    "output_format": "svg",
    "n": 1
  }'
```

生成动画 GIF 时，将 `output_format` 设为 `gif`，可通过 `frame_count` 指定 2–16 帧（默认 9），通过 `frame_duration_ms` 指定每帧 20–2000 毫秒（默认 200，上游按 10 毫秒取整）。例如：

```json
{
  "model": "gpt-image-2.6-flare",
  "prompt": "蓝色小球轻轻跳动，首尾衔接的循环动画",
  "output_format": "gif",
  "quality": "low",
  "complexity": "low",
  "size": "auto",
  "frame_count": 4,
  "frame_duration_ms": 200,
  "n": 1
}
```

GIF 可用于生成或编辑接口，不支持 streaming、partial images 或自定义压缩。帧数和每帧尺寸须满足上游的总像素限制，优先从 `size=auto` 开始。SVG/GIF 完成后仍从 `outputs[].url` 下载，文件扩展名分别为 `.svg` 和 `.gif`。

markup 引导编辑使用与第一张图片尺寸一致、包含透明像素的独立 PNG 标注层，不能与 mask 同时使用：

```bash
curl -sS "$ASYNC_IMAGE_BASE_URL/v1/images/edits" \
  -H "Authorization: Bearer $SUB2API_API_KEY" \
  -F "model=gpt-image-2.6-sunburst" \
  -F "prompt=把标注圈出的杯子改为蓝色，保留其他物体" \
  -F "image=@input.png" \
  -F "markup=@overlay.png" \
  -F "complexity=medium" \
  -F "quality=high"
```

使用 `/v1/task/submit` 的 JSON 编辑请求时，也可以在 `input` 内使用 `"markup": {"image_url": "data:image/png;base64,..."}`。尺寸、帧数、格式组合等模型约束由上游校验。官方说明见 [Image generation](https://developers.openai.com/api/docs/guides/image-generation#gpt-image-26)。

## 推理辅助和多轮编辑

OpenAI Image API 没有名为 `thinking` 的参数。需要模型先理解编辑意图、强制编辑上下文中的图片或连续多轮修改时，应使用 Responses API：顶层模型负责推理，`image_generation` 工具通过 `action: "edit"` 执行编辑。Sidecar 将这一流程包装为异步的 `POST /v1/images/responses`。

下例保留此前使用的 2.5 工具模型；2.6 的 complexity、markup、SVG 和 GIF 示例使用上面的 Images API，Responses 对新模型和字段的支持需按上游能力另行验证。

```bash
curl -sS "$ASYNC_IMAGE_BASE_URL/v1/images/responses" \
  -H "Authorization: Bearer $SUB2API_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-6-astra",
    "reasoning": {"effort": "high"},
    "input": [{
      "role": "user",
      "content": [
        {"type": "input_text", "text": "保留人物身份和姿势，只把背景改成夜晚城市"},
        {"type": "input_image", "image_url": "data:image/png;base64,替换为图片Base64"}
      ]
    }],
    "tools": [{
      "type": "image_generation",
      "model": "gpt-image-2.5-sunburst",
      "action": "edit",
      "quality": "xhigh"
    }],
    "tool_choice": {"type": "image_generation"}
  }'
```

该接口同样立即返回任务 ID。完成后 `outputs[].url` 是已落盘图片；原始 Responses `id` 和图片调用 `generation_id` 会保留，Base64 `result` 不会持久化。后续轮次可以再次提交 `previous_response_id`，或者把上一轮图片调用放入新请求上下文。JSON 请求默认最大 `70 MiB`，可通过 `ASYNC_IMAGE_MAX_BODY_BYTES` 调整。

## 查询任务和下载图片

查询时必须使用提交任务时的同一个 API Key：

```bash
curl -sS "$ASYNC_IMAGE_BASE_URL/v1/task/task_xxx" \
  -H "Authorization: Bearer $SUB2API_API_KEY"
```

任务状态：

```text
queued -> in_progress -> completed
```

`partial_completed` 和 `failed` 也是终态。完成响应示例：

```json
{
  "id": "task_xxx",
  "status": "completed",
  "model": "gpt-image-2.6-flare",
  "endpoint": "/v1/images/generations",
  "outputs": [
    {
      "url": "/v1/task/images/task_xxx_0.png",
      "file": "task_xxx_0.png",
      "bytes": "123456"
    }
  ],
  "created_at": 1782020000,
  "updated_at": 1782020030,
  "started_at": 1782020001,
  "completed_at": 1782020030
}
```

当前线上服务返回相对图片 URL，下载时需要拼接 Sidecar 地址：

```bash
curl -L "$ASYNC_IMAGE_BASE_URL/v1/task/images/task_xxx_0.png" \
  -o result.png
```

## 单独上传图片

需要先上传图片再组装任务时，可以使用：

```bash
curl -sS "$ASYNC_IMAGE_BASE_URL/v1/task/upload" \
  -H "Authorization: Bearer $SUB2API_API_KEY" \
  -F "file=@input.png"
```

上传成功返回 HTTP `201`：

```json
{
  "url": "/v1/task/uploads/upload_xxx.png",
  "file": "upload_xxx.png",
  "bytes": 123456,
  "content_type": "image/png"
}
```

返回的上传 URL 同样可能是相对地址。普通图片编辑优先直接使用 `/v1/images/edits`，不需要预上传。

## 统一任务提交和回调

需要统一生成、编辑调用方式或接收完成通知时，可以使用 `POST /v1/task/submit`：

```bash
curl -sS "$ASYNC_IMAGE_BASE_URL/v1/task/submit" \
  -H "Authorization: Bearer $SUB2API_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "endpoint": "generations",
    "model": "gpt-image-2.6-flare",
    "input": {
      "prompt": "一张现代办公室里的产品海报",
      "size": "1024x1024",
      "quality": "high",
      "response_format": "b64_json"
    },
    "callback_url": "https://example.com/hooks/image-completed"
  }'
```

`endpoint` 支持 `generations`、`generate`、`edits`、`edit`、`responses`、`thinking` 及对应的完整路径；`thinking` 是 Sidecar 对 Responses 图片工作流的便捷别名，不是 OpenAI Image API 参数。回调地址默认必须是 HTTPS。任务进入终态后，Sidecar 会向 `callback_url` POST 完整任务 JSON，并携带 `X-Async-Image-Task-Id`。配置了 `ASYNC_IMAGE_CALLBACK_SECRET` 时，还会携带 `X-Async-Image-Signature: sha256=<HMAC>`。

## 状态码和兼容性

| HTTP 状态码 | 含义 |
| --- | --- |
| `201` | 文件上传成功 |
| `202` | 异步任务已接受 |
| `400` | JSON、参数或上传内容不合法 |
| `401` | 未提供有效的 Bearer Token |
| `404` | 任务、图片或路由不存在；使用不同 API Key 查询任务也会返回 404 |
| `503` | 等待队列已满，客户端应稍后重试 |

- `response_format=b64_json` 仍会透传给 Sub2API，但 Sidecar 会将 Base64 解码成图片文件。任务查询结果不再返回或持久化 `b64_json` 或 Responses `result`，客户端应读取 `outputs[].url`。
- 图片和上传文件的 URL 当前不要求鉴权，不应向无关人员公开。
- 终态任务、生成图片和上传图片默认保留 24 小时，客户端需要在过期前下载或转存。
- Windows PowerShell 建议使用 `curl.exe`，避免与 PowerShell 内置 `curl` 别名冲突。
