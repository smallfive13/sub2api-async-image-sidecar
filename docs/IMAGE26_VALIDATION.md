# GPT Image 2.6 升级验证记录

验证日期：2026-11-09，Asia/Singapore。通过阿里云 Workbench 在现有服务器执行真实请求。

## 当前结论

PNG 生图与编辑升级完成并通过真实验证。默认生图为 `gpt-image-2.6-flare`，默认编辑为 `gpt-image-2.6-sunburst`。任务提交、轮询、输出落盘和文件下载均已验证；编辑样例把蓝色杯子改为红色，并保留杯形、视角和白色背景。

当前订阅渠道的限制也已经实测：请求 SVG 实际返回 PNG；GIF 返回 HTTP 400，提示该请求未启用 GIF；`size` 也没有严格决定最终像素尺寸。现阶段建议使用 PNG，并读取返回文件的实际格式与尺寸。

## 部署结果

- 用户通过后台更新了 Sub2API，已读取实际二进制版本 `0.2.20`，commit `dab829a4e9b7d1cc307189aea8b0319eb29e29d0`。在线 `/v1/models` 已包含两个 2.6 模型。
- 后台更新替换了容器中的程序，容器原始镜像标签仍为 `weishaw/sub2api:0.1.177`。已将 `/opt/sub2api/docker-compose.yml` 的镜像配置同步为 `weishaw/sub2api:0.2.20`，并用 `docker compose config --images` 验证目标镜像。没有重建或重启网关；下一次 Compose 重建需要取得该版本镜像。
- 已将本仓库 Sidecar 脚本补丁应用到 `/opt/sub2api/async-image-mvp/async_image_sidecar.py`，编译检查后重启 `sub2api-async-image-mvp`。检查时没有排队或执行中的图片任务。
- Sidecar 重启后健康检查返回 HTTP 200，运行模块默认模型为 2.6 Flare/Sunburst。省略 model 的真实请求也确认了这两个默认值。
- 最终检查：8080 和 8090 的健康接口均返回 HTTP 200，网关和 Sidecar 容器均为 healthy，图片任务队列没有未完成任务。
- Sidecar 的原部署方式仍为宿主机脚本 bind mount，加上 `python:3.12-alpine`；数据卷和现有 Compose 服务结构保留。

服务器本地备份：

```text
/opt/sub2api/async-image-mvp/async_image_sidecar.py.before-image26-20261109-104607
/opt/sub2api/docker-compose.yml.before-image26-20261109-105243
```

本次没有由助手执行数据库恢复、DDL 或网关镜像切换。后台升级所触发的迁移由 Sub2API 自身处理，本记录不声称独立验收了其全部迁移。

## 升级后的真实请求

| 请求 | 结果 | 证据 |
| --- | --- | --- |
| 显式 2.6 Flare，low complexity、PNG | 完成，约 14 秒 | `task_KIzRECGGTbh1rXKvTWARTaz2`；下载 HTTP 200，`image/png`，1,419,773 bytes；请求 1024×1024，实际 1536×1024 |
| 默认生图，不传 model，medium complexity、PNG | 完成，约 13 秒，使用 2.6 Flare | `task_S9xEq8pO0uZ8CAio4GsfGr9`；下载 HTTP 200，1,237,354 bytes；请求 1024×1024，实际 1254×1254 |
| 默认 multipart 编辑，不传 model，low complexity | 完成，约 16 秒，使用 2.6 Sunburst | `task_qDEDYKSqgum85OF8Itl9RQ7`；蓝杯改红杯；下载 HTTP 200，1,591,505 bytes，1536×1024 |
| 请求 SVG 输出 | 未通过 SVG 验收，实际为 PNG | `task_MJg4wceQCQpSq5XDQ3gLoP7`；任务终态 completed，但实际文件为 PNG，下载 MIME 为 `image/png`，1,033,631 bytes |
| 请求 GIF，4 帧、每帧 200 ms | 失败，上游 HTTP 400 | `task_qavA4hLB48sf1jVo9JY3Fdfo`；`GIF output is not enabled for this request.` |

任务提交的 HTTP 202 只表示入队。成功判断同时检查终态、文件下载和实际文件类型；SVG 的 completed 没有被视为矢量输出通过。

耗时按任务的 `completed_at - started_at` 计算。它是本次简单提示词、low quality 的实测值，不是接口时延承诺。

## 升级后的旧模型兼容性

2026-11-09 在 Sub2API 0.2.20 和新版 Sidecar 上，显式指定旧模型，使用各模型原有参数（未添加 complexity 等新字段）完成以下测试。每项都轮询到 completed，并下载、检查了 PNG 文件头和实际尺寸。

| 显式模型 | 场景 | 耗时 | 任务与下载证据 |
| --- | --- | --- | --- |
| `gpt-image-2.5-flare` | 生图，low quality | 11 秒 | `task_dAtTkKF0uTIP4J3ZkQ4GnM7`；HTTP 200，1,344,753 bytes，1536×1024 |
| `gpt-image-2.5-sunburst` | multipart 编辑，low quality | 17 秒 | `task_qxT2Psazz8HZmwOz2tynZrsq`；HTTP 200，1,332,586 bytes，1536×1024 |
| `gpt-image-2` | 生图，low quality | 13 秒 | `task_A2tOKynTCK63SCCeVFuxldlY`；HTTP 200，1,462,899 bytes，1536×1024 |
| `gpt-image-1.5` | 生图，low quality | 12 秒 | `task_up01MonjPDf18zHXF6LxKIYs`；HTTP 200，1,358,873 bytes，1536×1024 |

结论：省略模型才使用新的 2.6 默认值，显式旧模型调用保持兼容。实际权限仍取决于 API Key 分组和账号配置，测试不覆盖所有模型、接口及参数组合。

接口边界：直接 `/v1/images/edits` 使用 multipart，向该入口发送 JSON 会返回本地 HTTP 400；JSON 编辑使用 `/v1/task/submit`。模型兼容性按符合各入口协议的请求判断。

## 升级前的定位证据

旧版 `0.1.177` 的模型列表没有 2.6，2.6 请求回退到 Responses 图片桥接。真实请求显式传入 `complexity=low` 后仍返回 medium 不支持的错误。正式标签 `v0.2.20` 将两个 2.6 模型加入原生图片端点，并通过 `requiresCodexDirectImages` 禁止回退到不能保留所请求模型的 Responses 桥接。

| 升级前请求 | 结果 |
| --- | --- |
| 2.5 Flare 对照 | `task_s05veBKO0GF6gFtKdfrZbvSt`；约 13 秒完成，PNG 下载 HTTP 200，1,528,067 bytes |
| 2.6 Flare | `task_Wp6j1Zhhk3uYaFDRytEPYUUZ`；HTTP 400，`Complexity 'medium' is not supported for this image model.` |
| 2.6 Sunburst | `task_BTsYISt4fN8FDrciriBVeC0b`；同样的 HTTP 400 |
| Responses：GPT-6 Astra + 2.6 Flare 工具 | `task_KqtyegjJySl56y4K5BZUUq65`；HTTP 502；没有把该工作流当作 2.6 的可用路径 |

从服务器直接用通用 HTTP 客户端请求 ChatGPT 原生端点的额外诊断返回 403 HTML。它没有使用网关的出站传输实现，不能据此推断账号或模型权限。

## 本地代码验证和剩余边界

20 项自动化测试全部通过，使用模拟上游，覆盖默认模型、2.5 显式兼容、multipart 新参数与 markup、SVG/GIF 输出保存及下载 MIME，以及异步队列、清理和 Responses 原有流程。真实测试覆盖表中所列范围；没有对 markup、动画输出或所有 complexity/quality/size 组合作通过声明。

接口文档同时记录新默认值、显式旧模型的兼容规则、multipart/统一 JSON 编辑入口及当前渠道的格式限制。代码、部署配置模板和验证记录随本次仓库更新一并维护。

参考：

- [v0.2.20 发布说明](https://github.com/Wei-Shaw/sub2api/releases/tag/v0.2.20)
- [v0.2.20 原生图片实现](https://github.com/Wei-Shaw/sub2api/blob/v0.2.20/backend/internal/service/openai_images_direct.go)
- [OpenAI 图片生成指南](https://developers.openai.com/api/docs/guides/image-generation)
