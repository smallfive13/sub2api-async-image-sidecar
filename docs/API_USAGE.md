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

## 生成图片

```bash
curl -sS "$ASYNC_IMAGE_BASE_URL/v1/images/generations" \
  -H "Authorization: Bearer $SUB2API_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-image-2",
    "prompt": "一张现代办公室里的产品海报，明亮自然光",
    "size": "1024x1024",
    "quality": "high",
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
  "model": "gpt-image-2",
  "endpoint": "/v1/images/generations",
  "created_at": 1782020000,
  "updated_at": 1782020000
}
```

保存响应中的 `id`，随后通过任务查询接口轮询状态。

## 编辑图片

直接使用 `multipart/form-data` 上传本地图片：

```bash
curl -sS "$ASYNC_IMAGE_BASE_URL/v1/images/edits" \
  -H "Authorization: Bearer $SUB2API_API_KEY" \
  -F "model=gpt-image-2" \
  -F "prompt=保留主体，把背景改成明亮的现代办公室" \
  -F "image=@input.png" \
  -F "size=1024x1024" \
  -F "quality=high" \
  -F "response_format=b64_json"
```

需要限定编辑区域时增加 mask：

```bash
curl -sS "$ASYNC_IMAGE_BASE_URL/v1/images/edits" \
  -H "Authorization: Bearer $SUB2API_API_KEY" \
  -F "model=gpt-image-2" \
  -F "prompt=只修改 mask 区域，把背景改成办公室" \
  -F "image=@input.png" \
  -F "mask=@mask.png" \
  -F "size=1024x1024" \
  -F "quality=high" \
  -F "response_format=b64_json"
```

编辑接口同样返回 HTTP `202` 和任务 ID。单个 multipart 请求默认最大 `20 MiB`。

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
  "model": "gpt-image-2",
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
    "model": "gpt-image-2",
    "input": {
      "prompt": "一张现代办公室里的产品海报",
      "size": "1024x1024",
      "quality": "high",
      "response_format": "b64_json"
    },
    "callback_url": "https://example.com/hooks/image-completed"
  }'
```

`endpoint` 支持 `generations`、`generate`、`edits`、`edit` 及对应的完整路径。回调地址默认必须是 HTTPS。任务进入终态后，Sidecar 会向 `callback_url` POST 完整任务 JSON，并携带 `X-Async-Image-Task-Id`。配置了 `ASYNC_IMAGE_CALLBACK_SECRET` 时，还会携带 `X-Async-Image-Signature: sha256=<HMAC>`。

## 状态码和兼容性

| HTTP 状态码 | 含义 |
| --- | --- |
| `201` | 文件上传成功 |
| `202` | 异步任务已接受 |
| `400` | JSON、参数或上传内容不合法 |
| `401` | 未提供有效的 Bearer Token |
| `404` | 任务、图片或路由不存在；使用不同 API Key 查询任务也会返回 404 |
| `503` | 等待队列已满，客户端应稍后重试 |

- `response_format=b64_json` 仍会透传给 Sub2API，但 Sidecar 会将 Base64 解码成图片文件。任务查询结果不再返回或持久化 `b64_json`，客户端应读取 `outputs[].url`。
- 图片和上传文件的 URL 当前不要求鉴权，不应向无关人员公开。
- 终态任务、生成图片和上传图片默认保留 24 小时，客户端需要在过期前下载或转存。
- Windows PowerShell 建议使用 `curl.exe`，避免与 PowerShell 内置 `curl` 别名冲突。
