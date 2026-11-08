# Vids2API

[项目仓库](https://github.com/TheSmallHanCat/vids2api) · [API 文档](docs/API.md) · [MIT 许可证](LICENSE)

将 Google Vids 的图片、视频和文字转语音能力封装为统一的媒体生成 API，提供账号池、配额调度、会话恢复和 Web 管理界面。

接口采用 OpenAI 风格的媒体端点，支持通过 HTTP 客户端接入，并以持久化收据追踪每次生成。**当前生成链路无需配置打码服务。**

## 核心特性

- **图片生成与编辑**：文生图、上传图片编辑、一次生成多张图片，以及比例与风格选择。
- **视频生成与处理**：文生视频、图生视频、多素材生成、视频编辑、延长和放大。
- **文字转语音**：提供 30 种预设声线，完成后返回 WAV 音频。
- **账号池与配额调度**：按剩余配额加权选择空闲账号，或按账号顺序轮询；分别控制图片、视频和语音能力。
- **会话保活与恢复**：自动探活、保存轮换后的 Cookie；支持重新同步凭据、浏览器 Profile 恢复和自动重登。
- **幂等收据**：生成请求绑定原账号，同键重放复用已有收据；下载失败可单独恢复，付费提交不自动重发。
- **代理支持**：支持 HTTP / HTTPS / SOCKS5 / SOCKS5H，账号专属代理优先于全局代理。
- **媒体托管**：支持图片 URL 或 Base64 响应，以及视频、语音产物下载；图片 URL 缓存可设置过期时间。
- **管理与诊断**：账号管理、配额查看、生成统计、请求日志、系统设置和测试工作台，支持中文 / English 界面。
- **浏览器扩展**：Chrome / Edge 扩展可将当前 Google 登录状态推送到账号池。

## 支持的模型与能力

以下为本服务对外提供的模型 ID。实际可用能力和配额以账号对应的 Google Vids 状态为准。

| 模型 ID | 能力 | 主要参数与输出 |
| --- | --- | --- |
| `vids-image` | 文生图、图片编辑 | 文生图支持 `n=1–4`；比例 `1:1`、`16:9`、`9:16`；URL 或 Base64 JPEG |
| `vids-video` | 生成、编辑、延长、放大视频 | 生成时长 `3–15` 秒；720p / 1080p、横屏 / 竖屏；MP4 |
| `vids-speech` | 文字转语音 | `Puck` 等 30 种预设声线；输入最多 4096 字符；WAV |

视频模式：

| `mode` | 用途 | 输入 |
| --- | --- | --- |
| `text` | 文生视频，默认模式 | 提示词 |
| `animate` | 图生视频 | 一张参考图 |
| `ingredients` | 多素材生成 | 一张或多张参考图 |
| `edit` | 编辑已有视频 | 上传视频或已完成视频的 `source_id` |
| `extend` | 延长已有视频 | 上传视频或已完成视频的 `source_id` |
| `upscale` | 放大至 1080p | 上传视频或已完成视频的 `source_id` |

视频的源时长、输出规格及模式组合存在约束；语音也受上游语言支持范围限制。完整参数、声线和响应格式见 [API 对接文档](docs/API.md)。

## 快速开始

### 前置要求

- 一个能够正常进入 Google Vids 编辑器、且具有相应生成配额的 Google 账号。
- 本地运行建议使用 **Python 3.12**，与项目 Docker 镜像的运行版本保持一致。
- 容器运行需要 Docker；镜像通过项目自带的 Dockerfile 在本地构建。
- 运行机器能够访问 Google Vids 及其媒体服务。需要代理时，可在管理台配置。

### 方式一：本地运行

先获取源码：

```bash
git clone https://github.com/TheSmallHanCat/vids2api.git
cd vids2api
```

以下命令均在项目根目录执行。

**Windows PowerShell：**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

**Linux / macOS：**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
```

通过扩展或 Cookie 导入账号时，生成请求使用 HTTP 会话。若要使用服务器浏览器登录、Profile 恢复或自动重登，还需安装 Chromium：

```bash
python -m patchright install chromium
```

Windows 可使用 `.\.venv\Scripts\python.exe -m patchright install chromium`。Linux 首次安装浏览器及系统依赖时，可使用 `python -m patchright install --with-deps chromium`；打开有头登录窗口还需要可用的桌面或显示环境。

### 方式二：Docker 运行

在项目根目录构建镜像，再创建持久化数据卷并启动：

```bash
docker build -t vids2api:local .
docker volume create vids2api-data
docker volume create vids2api-cache
docker run -d --name vids2api --restart unless-stopped -p 8687:8687 -v vids2api-data:/app/data -v vids2api-cache:/app/tmp vids2api:local
docker logs -f vids2api
```

上述命令使用 Docker 命名卷，可在 Bash 和 PowerShell 中执行。`vids2api-data` 保存数据库、加密密钥、账号会话、收据与原始产物；`vids2api-cache` 保存 URL 缓存。

镜像包含 Chromium、Xvfb 和 noVNC。需要通过网页操作容器内的登录浏览器时，可改用以下启动命令：

```bash
docker run -d --name vids2api --restart unless-stopped -p 8687:8687 -p 127.0.0.1:8690:8690 -v vids2api-data:/app/data -v vids2api-cache:/app/tmp vids2api:local
```

两条 `docker run` 命令按是否需要 noVNC 选择一条执行。noVNC 地址为 `http://localhost:8690/vnc.html`，连接密码由管理台的服务器登录窗口提供。远程服务器采用上述本机绑定时，可通过 SSH 转发访问：

```bash
ssh -L 8690:127.0.0.1:8690 user@your-server
```

Docker 中已包含浏览器依赖，无需再次手动安装。容器重建时沿用相同数据卷，即可保留账号和历史结果。

### 首次访问

服务默认监听 `0.0.0.0:8687`。

| 页面 | 本地地址 | 用途 |
| --- | --- | --- |
| 管理台 | `http://localhost:8687/` | 登录、账号管理、系统设置 |
| 测试工作台 | `http://localhost:8687/test` | 登录管理台后测试媒体生成 |
| API 指南 | `http://localhost:8687/api-guide` | 查看接口与调用方式 |
| 健康快照 | `http://localhost:8687/health` | 查看本地账号数量与就绪状态 |

默认管理员为 `admin / admin`，默认 API Key 为 `sk-vids2api-default-key`。首次登录后，先在「系统配置」中修改管理凭据和 API Key，再接入账号。

修改管理凭据需要验证旧密码并确认新密码，成功后所有管理会话失效，需要重新登录。

## 接入账号

### 浏览器扩展

1. 在管理台「系统配置 → 扩展推送」中启用推送，复制推送令牌。
2. Chrome 打开 `chrome://extensions`，Edge 打开 `edge://extensions`；启用开发者模式，选择「加载已解压的扩展程序」，加载项目的 [`extension/`](extension/) 目录。
3. 在已登录 Google 的浏览器中打开扩展，填写服务器根地址，例如 `http://localhost:8687`，再填入推送令牌。
4. 点击「推送当前登录」。扩展会定位现有 Vids 文档；未找到时尝试通过 `vids.new` 创建文档，再将会话推送入池。

管理台显示的完整推送地址用于核对连接路径；扩展的服务器地址字段填写根地址，扩展会自行追加 `/api/extension/push`。

扩展会读取当前浏览器中的 Google 登录 Cookie，并发送到你填写的服务器。请仅连接自己控制的服务；远程连接使用 HTTPS，并妥善保管推送令牌。

同一账号再次推送会更新凭据。启用刷新恢复策略后，仅恢复自动封禁的账号，手动停用的账号保持停用。推送接口使用独立令牌 `X-Push-Token` 鉴权，限流为每分钟 10 次；重置令牌后，需要同步更新扩展配置。

### 手动导入 Cookie

1. 使用目标账号打开 Google Vids，确认能够正常进入文档编辑器。
2. 在管理台账号池中选择接入账号，填写编辑器 URL、同一账号的 Cookie，以及可选的备注和账号代理。
3. 提交后，服务会初始化编辑器会话、校验身份并读取配额。

Cookie 支持 JSON 数组或 `name=value; name2=value2` 请求头格式。图片编辑需要文档中存在已保存的场景；可先在 Vids 编辑器中创建并保存场景，再更新会话。

### 服务器浏览器登录

管理台支持打开服务器端浏览器完成 Google 登录。保存登录凭据后，可由自动重登功能恢复会话；密码、TOTP 与 Cookie 快照采用加密存储，浏览器 Profile 保存在 `data/` 下。

本地有桌面环境时使用登录窗口；Docker 中需要手动操作登录浏览器时，通过前述 noVNC 地址进入。登录完成后会校验账号身份，再创建或刷新账号池中的会话。

遇到 Google 的额外登录验证时，通过本地登录窗口或 noVNC 完成验证后继续。

## 系统设置

| 分组 | 设置项目 |
| --- | --- |
| 生成配置 | 图片超时、语音超时、视频超时、最大重试次数 |
| 池子配置 | 配额加权 / 顺序轮询、连续错误禁用阈值、配额预检、会话保活及间隔 |
| 登录与恢复 | 自动重登开关、重登冷却时间、登录等待超时 |
| 代理配置 | 全局代理、连通测试；账号可另设专属代理 |
| 缓存配置 | 缓存开关、过期时间、外链基地址、生效访问前缀 |
| 扩展推送 | 推送开关、连接地址、令牌设置与重置、凭据刷新恢复策略 |
| 调试日志 | 请求 / 响应摘要开关、严格脱敏 |
| 管理员与 API Key | 修改管理凭据、更新 API Key |

- **配置来源**：有 `config/setting.toml` 时优先读取该文件，否则使用 [`config/setting_example.toml`](config/setting_example.toml)。管理配置在首次启动时写入数据库，之后以管理台保存的数据库配置为准；服务器监听地址和端口仍由 TOML 的 `[server]` 段决定。
- **超时与重试**：图片、语音超时范围为 `30–3600` 秒，视频为 `60–7200` 秒。`max_retries` 为免费请求与媒体下载的额外重试次数，范围 `0–10`；付费生成提交始终不自动重发。
- **调度与保活**：每个账号同一时刻处理一个在途操作，多个账号分担并发。启动时先建立收据索引，再在后台恢复会话；恢复过程中管理台可以访问。
- **代理生效**：账号代理优先于全局代理。修改后，会在下次上游操作前验证并切换；验证失败会保留原会话，并拒绝本次上游操作。
- **缓存行为**：`cache_timeout=0` 表示不自动过期。关闭缓存后，文生图需使用 `b64_json`；图片编辑接口当前返回 URL，需要开启缓存。
- **日志内容**：调试日志写入 `logs.txt`，记录请求、响应的结构摘要及受限异常信息；凭据、正文文本和媒体内容始终隐藏。

## API 使用

生成接口采用全局 API Key 鉴权；管理台登录会话和扩展推送令牌各自独立。

```http
Authorization: Bearer YOUR_API_KEY
Idempotency-Key: unique-request-key
```

**每个新生成请求使用一个唯一的 `Idempotency-Key`；重试同一请求时保留原键和原参数。**

以下 cURL 示例使用 Bash 语法，可在 Linux、macOS 或 Git Bash 中运行。先将 `YOUR_API_KEY` 替换为管理台设置的密钥：

```bash
export VIDS_API_BASE="http://localhost:8687"
export VIDS_API_KEY="YOUR_API_KEY"
```

### 文生图

```bash
curl -sS "$VIDS_API_BASE/v1/images/generations" \
  -H "Authorization: Bearer $VIDS_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: image-request-001" \
  -d '{"model":"vids-image","prompt":"晨光中的山间湖泊，远处薄雾，写实摄影","n":1,"aspect_ratio":"16:9","response_format":"url"}'
```

完成后从 `data[].url` 读取图片地址。将 `response_format` 改为 `b64_json`，可在 `data[].b64_json` 中取得 Base64 JPEG。

### 图片编辑

将原图保存为 `input.png`，开启媒体缓存后调用：

```bash
curl -sS "$VIDS_API_BASE/v1/images/edits" \
  -H "Authorization: Bearer $VIDS_API_KEY" \
  -H "Idempotency-Key: image-edit-001" \
  -F "image=@input.png" \
  -F "prompt=将背景改为雪后的森林，保留主体外观"
```

输入支持 PNG / JPEG / WebP，大小不超过 10 MiB。编辑固定返回一张图片，不接受比例或风格选项。

### 文字转语音

```bash
curl -sS "$VIDS_API_BASE/v1/audio/speech" \
  -H "Authorization: Bearer $VIDS_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: speech-request-001" \
  -d '{"model":"vids-speech","input":"Welcome to the mountain observatory.","voice":"Puck"}' \
  -D speech.headers -o speech.wav
```

返回 `200` 时，响应体为 WAV。若返回 `202`，响应体是含收据的 JSON，应先查询收据或恢复下载；此时保存的文件不能当作音频使用。

### 文生视频

```bash
curl -sS "$VIDS_API_BASE/v1/videos" \
  -H "Authorization: Bearer $VIDS_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: video-request-001" \
  -d '{"model":"vids-video","prompt":"A slow cinematic aerial shot above a mountain lake at sunrise","mode":"text","seconds":3,"size":"1280x720"}'
```

创建接口返回生成收据，读取其中的 `id`、`status` 和 `content_url`。保存返回的完整 ID，再查询视频状态或下载产物：

```bash
export VIDEO_ID="REPLACE_WITH_RETURNED_ID"
curl -sS "$VIDS_API_BASE/v1/videos/$VIDEO_ID" \
  -H "Authorization: Bearer $VIDS_API_KEY"
curl -sS "$VIDS_API_BASE/v1/videos/$VIDEO_ID/content" \
  -H "Authorization: Bearer $VIDS_API_KEY" \
  -o output.mp4
```

仅在状态为 `completed` 时下载文件。客户端及反向代理的等待时间应覆盖服务端生成超时；连接中断后，保留原幂等键并查询原收据。

### Windows PowerShell 示例

```powershell
$headers = @{
    Authorization = "Bearer YOUR_API_KEY"
    "Idempotency-Key" = "image-request-002"
}
$body = @{
    model = "vids-image"
    prompt = "A mountain lake at sunrise"
    response_format = "b64_json"
} | ConvertTo-Json
Invoke-RestMethod -Uri "http://localhost:8687/v1/images/generations" -Method Post -Headers $headers -ContentType "application/json" -Body $body
```

### 端点速查

| 方法与路径 | 用途 |
| --- | --- |
| `GET /v1/models` | 查看模型与能力 |
| `POST /v1/images/generations` | 文生图 |
| `POST /v1/images/edits` | 图片编辑 |
| `POST /v1/audio/speech` | 文字转语音 |
| `POST /v1/videos` | 视频生成与处理 |
| `GET /v1/videos` | 视频列表 |
| `GET /v1/videos/{id}` | 视频状态 |
| `GET /v1/videos/{id}/content` | 下载视频 |
| `POST /v1/vids/generations` | 统一原始生成端点 |
| `GET /v1/vids/generations/{id}` | 原始收据 |
| `POST /v1/vids/generations/{id}/download` | 恢复已接收结果的下载 |
| `GET /v1/vids/generations/{id}/content` | 下载原始产物，图片可通过 `index` 选择输出 |

完整请求字段、错误码和声线列表见 [API 对接文档](docs/API.md)。

## 幂等、状态与恢复

生成请求会先绑定账号并保存收据，再执行付费提交。相同幂等键与相同参数复用已有收据；参数不一致会返回 `409 idempotency_conflict`。收据重放和恢复下载不会重复累计生成统计。

| 收据状态 | 建议操作 |
| --- | --- |
| `completed` | 读取结果或下载产物 |
| `generating` / `received` / `inserting` | 查询原收据，设置合理的等待期限 |
| `download_failed` | 调用恢复下载接口，复用已接收的结果 |
| `failed` / `no_result` / `specification_mismatch` | 查看错误与请求日志；确认原因后，再决定是否发起新的生成 |
| `unknown` | 无法确认上游是否受理，保留原键并核对上游结果 |

图片、视频的 JSON 收据直接包含 `id`；语音返回 `202` 时，从 `error.vids_receipt.id` 取得 ID。将其填入下面的变量，可统一查询三类媒体的原始收据：

```bash
export GENERATION_ID="REPLACE_WITH_RETURNED_ID"
curl -sS "$VIDS_API_BASE/v1/vids/generations/$GENERATION_ID" \
  -H "Authorization: Bearer $VIDS_API_KEY"
```

对 `received` 或 `download_failed` 状态，可调用恢复下载接口：

```bash
curl -sS -X POST "$VIDS_API_BASE/v1/vids/generations/$GENERATION_ID/download" \
  -H "Authorization: Bearer $VIDS_API_KEY"
```

恢复为 `completed` 后，通过收据中的 `content_url` 下载产物。该操作只恢复已接收结果的下载，不重新提交生成。

`202` 本身不代表后台任务一定会继续执行，应以收据的 `status` 和 `error` 判断下一步。查询接口不会重新提交付费生成。

**`unknown` 状态下换用新键，会创建新的生成并可能再次消耗配额。** 停用账号或登录失效后，已保存的收据与产物仍可读取；恢复下载涉及上游请求，需要账号会话可用。

## 健康检查与界面语言

```bash
curl http://localhost:8687/health
```

`/health` 无需鉴权，返回本地账号总数、启用数量和就绪数量，不会主动请求 Google。启动恢复期间，就绪数量可能暂时为 `0`。

管理台、登录页和测试工作台支持中文 / English，语言选择会保存在当前浏览器中。界面语言与上游语音生成的输入语言支持范围分别处理。

## 数据目录与升级备份

```text
config/setting_example.toml  首次启动默认配置
config/setting.toml          可选的本地配置
data/vids2api.db             账号、幂等归属、结算状态、统计、请求日志与配置
data/vids2api.key            会话及登录凭据的加密密钥
data/accounts/{id}/          每账号的 JSON 收据与原始媒体产物
data/browser_profiles/      服务器端浏览器 Profile
data/vnc_secret             Docker noVNC 连接密码
tmp/media/                  图片 URL 缓存，对应 /tmp/media/
extension/                  浏览器扩展
static/                     管理台、测试工作台和 API 指南
docs/API.md                 完整 API 对接说明
logs.txt                    调试日志
```

- **持久化**：本地运行保留 `data/`；Docker 运行保留对应数据卷。需要跨容器重建保留 URL 缓存时，同时保留 `tmp/` 对应数据卷。
- **备份**：停止服务后备份整个 `data/`，使数据库、密钥、收据和 Profile 保持一致。密钥必须与数据库一起保存。
- **升级**：更新源码并重启，服务会自动补齐数据库结构。Docker 升级时重新构建镜像，并沿用原数据卷。
- **清理范围**：缓存过期只清理 `tmp/media/` 中的副本；账号目录中的原始产物独立保留。删除账号会清理其凭据、Profile、收据和产物，但保留已使用幂等键的归属记录，防止旧请求重新提交。
- **运行进程**：同一数据目录由一个服务进程持有，请使用默认的单进程启动方式。

## 常见问题

**管理台能访问，但生成返回 `503`？**

检查是否已接入账号、账号是否启用、对应图片 / 视频 / 语音能力是否开启，以及会话是否仍在恢复。代理修改后验证失败也会阻止本次上游操作，可结合账号状态和请求日志定位。

**返回 `429 busy` 或 `quota_exhausted`？**

`busy` 表示可用账号正在处理其他操作；按 `Retry-After` 稍后重试原请求。`quota_exhausted` 表示配额预检发现余额不足，可刷新配额或检查其他账号的可用状态。

**Cookie 失效后如何恢复？**

通过扩展重新推送，或在管理台更换 Cookie。已保存 Profile 与登录凭据时，可以使用会话恢复和自动重登；手动停用的账号仍需手动启用。

**图片链接打不开或已经过期？**

检查外链基地址和 `/tmp/media/` 路径是否能够从客户端访问。缓存过期后，可读取鉴权的原始产物端点，或重放原请求重新取得缓存链接。关闭缓存时，文生图使用 `b64_json`；图片编辑需要开启缓存。

**为什么不能把所有 `202` 都当成处理中？**

`202` 可能携带下载失败、终态失败或 `unknown` 收据。请根据具体状态处理；发生网络中断时保留原幂等键，避免将已受理的任务当作新生成再次提交。

## 源码检查

安装项目依赖后，可先检查 Python 源码语法：

```bash
python -m compileall -q src main.py
```

配置模板可以随源码分发；正式部署的配置、数据库、密钥、Cookie、浏览器 Profile、运行日志及生成产物应保存在本地或受控的数据卷中。

## 许可证

本项目采用 [MIT 许可证](LICENSE)。

## 相关项目

[Flow2API](https://github.com/TheSmallHanCat/flow2api/) 是同一作者维护的关联项目。
