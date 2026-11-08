# vids2api 对接文档

下游通过 OpenAI 风格的 HTTP 接口调用 vids2api，完成图片、语音、视频三类媒体生成。

## 1. 接入说明

### 1.1 Base URL

```
http://<服务器地址>:8687/v1
```

### 1.2 认证

所有接口使用 `Authorization: Bearer` 头携带全局 API Key：

```
Authorization: Bearer sk-vids2api-xxxxxxxxxxxxxxxx
```

同时兼容两种替代写法（便于对接不支持自定义头的客户端）：

- 请求头 `x-goog-api-key: <API Key>`
- 查询参数 `?key=<API Key>`（不推荐，可能进入访问日志）

API Key 由部署方在管理面板中生成与管理。

### 1.3 幂等键（必填）

所有生成类 POST 请求必须携带 `Idempotency-Key` 请求头（1–200 个字符），缺失返回 `400 idempotency_required`。

- 幂等键决定生成 ID：`vids_<sha256(key)>`。相同幂等键的重复请求**永远不会重复提交付费生成**，而是返回首次结果（收据持久化在磁盘，服务重启后仍可重放）。
- 客户端应为每次新生成使用全局唯一键（如 UUID），重试同一业务操作时复用同一键。
- 收据重放和恢复下载只结算一次。账号停用或登录失效不会阻止读取已保存的收据与产物；
  删除账号后，已使用的键仍保留原账号归属，不会转移到其他账号。
- 注意：OpenAI SDK 不会自动发送该头，直接使用 HTTP 客户端对接最简单。

### 1.4 同步与异步

生成接口的返回分两种：

- **`200`**：生成已完成。图片返回包含 URL 或 Base64 的 JSON，语音直接返回 WAV，视频返回完成收据。
- **`202`**：返回未完成收据，也可能携带下载失败、终态失败或受理状态未知的结果。按第 7 节检查 `status` 与 `error`，选择查询或恢复下载；此状态码不保证有持续执行的后台任务。

视频常见耗时 30 秒左右，通常同步返回；上游繁忙时进入 202 轮询路径。

---

## 2. 模型

`GET /v1/models` 返回当前可用模型及能力声明：

| 模型 ID | 类型 | 端点 | 说明 |
|---|---|---|---|
| `vids-image` | 图片 | `POST /v1/images/generations`、`POST /v1/images/edits` | 文生图 / 图片编辑，单次最多 4 张 |
| `vids-speech` | 语音 | `POST /v1/audio/speech` | 文本转语音，输出 WAV |
| `vids-video` | 视频 | `POST /v1/videos` | 文生视频 / 图生视频 / 视频编辑等 6 种模式 |

`GET /v1/models/{model_id}` 返回单个模型详情（含 capability 枚举）。

---

## 3. 图片

### 3.1 文生图 `POST /v1/images/generations`

请求体（JSON）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `prompt` | string | 是 | 画面描述，非空，≤32000 字符 |
| `n` | int | 否 | 生成张数，1–4，默认 1 |
| `size` | string | 否 | 兼容 OpenAI 取值（自动映射比例）：`1024x1024`/`1:1`、`1792x1024`/`16:9`、`1024x1792`/`9:16`，默认 `1:1` |
| `aspect_ratio` | string | 否 | 直接指定比例：`1:1`、`16:9`、`9:16`（优先于 `size`） |
| `style` | string | 否 | 风格：`auto`（默认）、`photography`、`background`、`vector`、`sketch`、`watercolor`、`cyberpunk` |
| `response_format` | string | 否 | `url`（默认，返回临时下载地址）或 `b64_json` |

关闭媒体缓存时须使用 `b64_json`；`url` 请求返回 `400 cache_disabled`，不会提交生成。

```bash
curl http://localhost:8687/v1/images/generations \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: img-20260101-0001" \
  -d '{"model":"vids-image","prompt":"a red kite over green hills","n":1,"size":"16:9"}'
```

完成响应（`200`）：

```json
{
  "created": 1794018890,
  "data": [
    {"url": "http://<host>:8687/tmp/media/<file_id>.jpg"}
  ]
}
```

`b64_json` 时 `data[].b64_json` 为 Base64 的 JPEG。多张时 `data` 数组按序返回。

### 3.2 图片编辑 `POST /v1/images/edits`

`multipart/form-data`：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `image` | file | 是 | 原始图，PNG/JPEG/WebP，≤10 MiB |
| `prompt` | string | 是 | 编辑指令 |

编辑模式固定返回 1 张，不接受 `aspect_ratio`/`style`。当前编辑接口返回 URL，需开启媒体缓存。

```bash
curl http://localhost:8687/v1/images/edits \
  -H "Authorization: Bearer $KEY" \
  -H "Idempotency-Key: edit-20260101-0001" \
  -F image=@input.png \
  -F prompt="把背景换成雪夜森林"
```

---

## 4. 语音 `POST /v1/audio/speech`

请求体（JSON）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `input` | string | 是 | 合成文本，≤4096 字符（超长会被截断） |
| `voice` | string | 否 | 预设音色 ID，默认 `Puck`，白名单见下 |

**语言限制（上游强制）**：当前仅支持英文输入，中文返回 `400 LANGUAGE_NOT_SUPPORTED`（`upstream_error` 透传）。

完成响应（`200`）：`audio/wav` 二进制流，可直接写文件。

未就绪响应（`202`）：

```json
{
  "error": {
    "message": "语音尚未就绪，请稍后通过收据查询",
    "type": "not_ready_error",
    "code": "speech_not_ready",
    "vids_receipt": { "id": "vids_…", "status": "generating", "…": "…" }
  }
}
```

取 `vids_receipt.id` 后按第 7 节轮询，`completed` 后请求 `GET /v1/vids/generations/{id}/content` 获取 WAV。

预设音色（`voice` ID → 声音名称 / 风格，共 30 个）：

| voice | 名称 | 风格 | voice | 名称 | 风格 |
|---|---|---|---|---|---|
| Achernar | Nyla | 柔和、较高 | Puck | Neo | 欢快、适中 |
| Achird | Elio | 亲切友好、偏低 | Pulcherrima | Vira | 激进、适中 |
| Algenib | Jett | 低沉沙哑、偏低 | Rasalgethi | Iro | 信息丰富、适中 |
| Algieba | Knox | 平稳流畅、偏低 | Sadachbia | Dori | 生动活泼、偏低 |
| Alnilam | Zeno | 坚定、偏低 | Sadaltager | Umi | 知识丰富、适中 |
| Aoede | Tova | 轻松活泼、适中 | Schedar | Jacy | 平稳、偏低 |
| Autonoe | Kaci | 嘹亮、适中 | Sulafat | Fola | 温暖、适中 |
| Callirrhoe | Lani | 轻松、适中 | Umbriel | Baya | 轻松随意、偏低 |
| Charon | Holt | 信息丰富、偏低 | Vindemiatrix | Orla | 柔和、适中 |
| Despina | Lora | 平稳流畅、适中 | Zephyr | Sani | 嘹亮、较高 |
| Enceladus | Paz | 气声、偏低 | Zubenelgenubi | Yori | 闲适、偏低 |
| Erinome | Tyra | 清晰、适中 | Kore | Lito | 坚定、适中 |
| Fenrir | Kero | 激动、偏低 | Iapetus | Nyx | 清晰、偏低 |
| Gacrux | Saro | 成熟、适中 | Laomedeia | Peli | 欢快、较高 |
| Leda | Fira | 青春洋溢、较高 | Orus | Cale | 坚定、偏低 |

完整白名单以 `GET /v1/models/vids-speech` 的 `capabilities.voices` 为准。

```bash
curl http://localhost:8687/v1/audio/speech \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: tts-20260101-0001" \
  -d '{"model":"vids-speech","input":"Hello from the media API.","voice":"Puck"}' \
  -o speech.wav
```

---

## 5. 视频 `POST /v1/videos`

支持 JSON 或 `multipart/form-data` 两种请求体。

### 5.1 参数

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `prompt` | string | 是 | 画面/运镜描述，非空，≤32000 字符 |
| `mode` | string | 否 | 生成模式，默认 `text`。可选：`text`、`animate`、`ingredients`、`edit`、`extend`、`upscale` |
| `seconds` | int | 否 | 时长 3–15 秒（15 秒已实测上游支持），默认 3（`upscale` 固定等于源时长） |
| `size` | string | 否 | `1280x720`（默认）、`720x1280`、`1920x1080`、`1080x1920`；`upscale` 仅 1080p |
| `shot_mode` | string | 否 | 镜头模式：`auto`（默认）、`long_take`、`montage`；仅 `text`/`ingredients`/`edit` 可用 |
| `image_base64` | string | 否 | 参考图 Base64（`animate` 用 1 张，`ingredients` 可多张），PNG/JPEG/WebP ≤10 MiB |
| `images_base64` | string[] | 否 | 多参考图（`ingredients`） |
| `video_base64` | string | 否 | 上传 MP4（≤50 MiB），需 `edit`/`extend`/`upscale` 模式 |
| `source_id` | string | 否 | 引用已完成的 vids 视频 ID（`edit`/`extend`/`upscale`），与 `video_base64` 二选一 |

模式约束：

| 模式 | 输入 | 说明 |
|---|---|---|
| `text` | 仅 prompt | 文生视频 |
| `animate` | `image_base64` ×1 | 单图动起来 |
| `ingredients` | `image_base64` ×1 或 `images_base64` | 多素材合成 |
| `edit` | `video_base64` 或 `source_id` | 指令改写已有视频；源可到 20 秒 |
| `extend` | `video_base64` 或 `source_id` | 续写源视频（源 ≤10 秒） |
| `upscale` | `video_base64` 或 `source_id` | 超分到 1080p；时长与方向锁定为源值 |

### 5.2 示例（文生视频）

```bash
curl http://localhost:8687/v1/videos \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: vid-20260101-0001" \
  -d '{"model":"vids-video","prompt":"a lighthouse beam sweeping across a stormy night sea","seconds":3,"size":"1280x720"}'
```

完成响应（`200`）：

```json
{
  "id": "vids_0022aba61eada3cf22bba5140…",
  "object": "vids.generation",
  "created_at": 1794018890,
  "status": "completed",
  "kind": "video",
  "error": null,
  "metadata": {"width": 1280, "height": 720, "duration": 3, "bytes": 245760},
  "content_url": "/v1/vids/generations/vids_0022…/content"
}
```

`content_url` 为相对路径，拼接 Base URL 后用 API Key 下载 `video/mp4`。

`GET /v1/videos/{id}` 查询响应额外携带规格回显：

```json
{
  "id": "vids_…",
  "object": "video",
  "status": "completed",
  "model": "vids-video",
  "seconds": 15,
  "size": "1280x720",
  "content_url": "/v1/videos/vids_…/content"
}
```

`content_url` 为相对路径，拼接 Base URL 后用 API Key 下载 `video/mp4`。

未完成响应（`202`）：同结构但 `status` 为 `in_progress`（可能伴随 `error.code` 描述中间态），按第 7 节轮询。

### 5.3 视频列表与查询

| 端点 | 说明 |
|---|---|
| `GET /v1/videos?limit=20&after=<id>&order=desc` | 分页列出（`limit` 1–100） |
| `GET /v1/videos/{video_id}` | 单条查询（OpenAI 简化形态） |
| `GET /v1/videos/{video_id}/content` | 下载 MP4 |

---

## 6. 通用生成收据（底层接口）

不依赖 OpenAI 形态的下游可直接使用原始收据接口，图片/语音/视频统一：

| 端点 | 说明 |
|---|---|
| `POST /v1/vids/generations` | 通用生成，请求体必须含 `kind`（`image`/`video`/`speech`）及对应参数，字段与第 3–5 节一致 |
| `GET /v1/vids/generations/{id}` | 查询收据（含 `metadata`、多图 `outputs`） |
| `GET /v1/vids/generations/{id}/content?index=0` | 下载产物；多张图用 `index` 选择 |
| `POST /v1/vids/generations/{id}/download` | **重新下载**：当收据为 `download_failed`（上游媒体下载瞬时失败）时触发恢复，不重复扣费生成 |

收据结构（`GET /v1/vids/generations/{id}`）：

```json
{
  "id": "vids_…",
  "object": "vids.generation",
  "kind": "video",
  "status": "completed",
  "created_at": 1794018890,
  "error": null,
  "content_url": "/v1/vids/generations/vids_…/content",
  "metadata": {"width": 1280, "height": 720, "duration": 3.0, "bytes": 1834582},
  "outputs": [
    {"index": 0, "metadata": {"…": "…"}, "content_url": "/v1/vids/generations/vids_…/content"}
  ]
}
```

图片多张时 `outputs[].index` 对应 `content?index=` 参数。

---

## 7. 状态机与轮询

收据 `status` 取值及建议动作：

| 状态 | 含义 | 下游动作 |
|---|---|---|
| `generating` / `received` / `inserting` | 生成/接收/写入中 | 继续轮询（建议 5–10s 间隔） |
| `download_failed` | 上游已出片但媒体下载瞬时失败 | 调 `POST /v1/vids/generations/{id}/download` 恢复 |
| `completed` | 完成 | 下载 `content_url` |
| `failed` / `no_result` / `specification_mismatch` | 失败（不再变化） | 读取 `error`/`upstream_error`，更换幂等键重试 |
| `unknown` | 无法确认上游是否受理或可靠解析结果，包括 `acceptance_unknown` 与中断恢复情形；付费提交不会自动重发 | 保留原幂等键与收据，先核对上游结果；换用新键会创建新生成，可能再次消耗配额 |

`GET /v1/videos/{id}` 会把内部状态归一为 OpenAI 风格：`in_progress` / `completed` / `failed` / `unknown`；`download_failed` 归一为 `in_progress`（因为可恢复）。

**轮询终止条件**：`completed` 或终态失败。对 `download_failed` 应调用恢复下载接口；对 `unknown` 应核对上游结果。达到客户端等待期限后保留原收据，不要仅因等待超时就将请求作为新生成重新提交。

---

## 8. 错误格式

所有错误统一为 OpenAI 形态：

```json
{
  "error": {
    "message": "描述",
    "type": "invalid_request_error",
    "code": "错误码",
    "param": null
  }
}
```

常见错误码：

| HTTP | code | 说明 |
|---|---|---|
| 400 | `idempotency_required` | 缺少或非法 Idempotency-Key |
| 400 | `invalid_request` | JSON 解析失败 / 字段不支持 |
| 400 | `invalid_mode`、`invalid_voice`、`invalid_style`、`invalid_aspect_ratio` | 参数取值非法 |
| 400 | `invalid_image_count` | 图片张数越界（text 1–4，edit 固定 1） |
| 400 | `invalid_image` / `invalid_video` | 输入媒体格式或大小非法 |
| 400 | `invalid_source` | 模式与输入不匹配（如 text 模式带 source_id） |
| 400 | `unsupported_specification` | seconds/size 组合不支持 |
| 400 | `unsupported_source_specification` | 源视频时长/尺寸超出模式限制 |
| 400 | `speech_not_ready` | 语音未就绪（202，配合 `vids_receipt` 轮询） |
| 404 | `not_found` | 收据不存在 |
| 409 | `no_received_result` | 无可重试的已完成媒体（retry download 时） |
| 410 | `asset_expired` | 产物文件已过期清理 |
| 429 | `busy` | 池内账号全部繁忙，稍后重试 |
| 429 | `quota_exhausted` | 选中账号配额耗尽 |
| 503 | `account_disabled` / `session_unavailable` | 无可用账号/会话，稍后重试 |
| 500 | `internal_error` | 内部错误 |

401 未携带或错误的 API Key 返回 `{"detail": {"error": {"message": "Invalid API key", "type": "auth_error"}}}`。

---

## 9. 调用方注意事项

1. **配额消耗**：图片/视频/语音按上游账号配额计费；`202` 轮询与 `download` 重试不额外消耗生成配额，但上游侧部分失败提交可能已计入消耗，属正常现象。
2. **产物保留**：产物文件保存在服务端直至清理周期（`410 asset_expired` 表示已被清理），下游应及时转存。
3. **幂等键安全**：幂等键即生成身份，请勿使用可预测的自增序号暴露给不可信方。
4. **速率建议**：单账号同一时刻仅处理一个生成；并发请求数请不超过池内可用账号数，超并发会得到 `429 busy`。
