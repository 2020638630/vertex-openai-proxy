# vertex-openai-proxy

本地 **OpenAI 兼容网关**：把标准 `/v1/chat/completions` 请求经官方 `google-genai` SDK 转发到 **Vertex AI**（Gemini），让只支持 OpenAI 协议的本地 Agent（如 ZCode）可以直接用 Vertex 额度。

```
ZCode ──OpenAI 协议──▶ 127.0.0.1:4000 ──google-genai──▶ Vertex AI (Gemini)
                          ▲ 脱敏 / 工具映射 / 重试
```

## 为什么需要它

- 客户端只认 OpenAI 协议，而 Vertex 用的是 `generateContent` / `functionCall` 那套结构
- API Key（Express 模式）不能直接打 Bearer 端点（会 401），必须走 `genai.Client(vertexai=True, api_key=...)`
- 顺带做了一层出站脱敏，避免把密钥、口令、隐私发给第三方模型

## 快速开始

```powershell
pip install -r requirements.txt

copy .env.example .env      # 然后填 GEMINI_API_KEY 或配置服务账号
python vertex_openai_proxy.py
```

或双击 `start_proxy.bat`（自带 Key 空值检查与端口占用检查）。自测：`test_proxy.bat`。

客户端侧填：

| 项 | 值 |
| --- | --- |
| Base URL | `http://127.0.0.1:4000/v1` |
| API 格式 | Chat Completions |
| API Key | 随便填（如 `sk-vertex-local`，本代理不校验） |
| 模型 | `gemini-4-argon` / `gemini-3.8-flash` / `gemini-2.5-pro` |

## 鉴权模式

`.env` 里 `VERTEX_AUTH` 控制，默认 `auto`：

| 模式 | 触发条件 | 配额池 |
| --- | --- | --- |
| `api_key` | 只配了 `GEMINI_API_KEY` | Express 共享池（小，易 429） |
| `service_account` | 配了 `VERTEX_SA_JSON` + `GOOGLE_CLOUD_PROJECT` + `GOOGLE_CLOUD_LOCATION` | 项目配额（大） |

服务账号模式需要该账号有 `roles/aiplatform.user`，密钥文件不要入库（已在 `.gitignore`）。

## Claude（AnthropicVertex）

`model` 以 `claude-` 开头时走独立分支，用官方 `anthropic[vertex]` SDK 转发，消息/工具双向转换由 `claude_adapter.py` 负责。

| 项 | 值 |
| --- | --- |
| 区域 | **只能是 `global`**（`us-east5` / `us-central1` / `europe-west1` 实测全部 404） |
| 凭证 | 复用 `VERTEX_SA_JSON`（服务账号 JSON） |
| 已确认存在的模型 ID | `claude-sonnet-4-5@20250929`、`claude-opus-4-5@20251101`、`claude-haiku-4-5@20251001` |
| 本项目不可用 | `claude-3-7-sonnet@20250219`、`claude-3-5-sonnet-v2@20241022`、`claude-3-5-haiku@20241022`（404） |

### 实测记录（2026-10-17）

**结论：代码侧已就绪，当前项目无法调用，卡在配额「0 到 0」锁死。**

1. 三个模型都存在，但配额为 0，调用返回：

   ```
   429 Quota exceeded for aiplatform.googleapis.com/global_online_prediction_requests_per_base_model
   with base model: anthropic-claude-sonnet-4-5.
   Please submit a quota increase request.
   ```

2. 到控制台 `IAM 和管理 → 配额` 尝试自助提额（指标 `online_prediction_requests_per_base_model`，
   base model `anthropic-claude-*`）时，**无法提交**：

   > 请输入 0 到 0 之间的新配额值，根据您的服务使用记录目前不符合条件

   即：新账号没有产生付费使用记录前，自助提额表单被锁死在 0~0。

3. 与 `us-east5` 等区域无关（这些区域本身就是 404），也与服务账号权限无关（Gemini 同凭证正常）。

**后续怎么恢复**：等账号积累了实际计费使用记录后再去同一页面尝试提额；一旦配额 > 0，
`claude-*` 无需改任何代码即可使用。在此之前该分支只会返回带中文提示的 502，不影响 Gemini。

可用环境变量：`CLAUDE_REGION`（默认 `global`）、`CLAUDE_PROJECT`（默认同 `GOOGLE_CLOUD_PROJECT`）。

## 主要能力

- **消息映射**：`system` → `system_instruction`；`assistant` → `model`；连续同角色合并；`function_response` 强制独立成 turn（否则上游 400）
- **工具调用**：`tools` / `tool_choice` 双向映射，支持并行调用；`thought_signature` 按 `tool_call_id` 缓存并在回传时还原（3.8-flash / 4-argon 强制要求）
- **服务端工具代执行**：模型调用 `Google:web_search` 时由网关内部用 `google_search` 执行并把结果回灌，客户端无感知（`SERVER_TOOL_BRIDGE=1`）
- **出站脱敏**：密钥/令牌/私钥、连接串口令、邮箱、手机号、身份证、银行卡（Luhn 校验）→ 占位符，响应自动还原（`sanitize.py`）
- **流式**：SSE 逐帧输出，占位符跨 chunk 也能还原
- **健壮性**：429/5xx 指数退避重试；硬错误以 HTTP 502 + JSON 返回；模型名归一化与别名改写

## 配置

全部在 `.env`，改完下一次请求生效（换 Key 无需重启）。逐项说明见 `.env.example`。

常用：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SANITIZE_ENABLED` | 1 | 脱敏总开关 |
| `SANITIZE_NETWORK` | 0 | 是否连 IP 一起脱（写代码时建议关） |
| `SERVER_TOOL_BRIDGE` | 1 | 服务端工具代执行 |
| `BRIDGE_SEARCH_MODEL` | gemini-2.5-flash | 桥接搜索固定用的快模型 |
| `RETRY_MAX_ATTEMPTS` | 4 | 429 重试次数（间隔 5/10/20s） |
| `THINKING_BUDGET` | 空 | 思考预算；2.5-flash 可设 0，2.5-pro 最小 128 |

## 日志

写入 `logs/proxy.log`（UTF-8），按大小自动切分，**磁盘占用封顶**：

```
LOG_MAX_MB=5      单文件上限
LOG_BACKUPS=5     保留份数
→ proxy.log / proxy.log.1 … .5，总计约 30 MB
```

日志同时打到控制台；启动时会打印实际文件路径。`logs/` 已在 `.gitignore` 中。

字段格式：`时间 级别 模块 内容`，其中模块区分 `vertex-proxy`（业务）、`uvicorn.access`（访问）、`httpx`（上游请求）。

## 测试

```powershell
pip install -r requirements-dev.txt
python -m pytest        # 15 个用例，约 1 秒，零网络、不需要 Key
```

用例覆盖历史踩过的坑：并行工具合并、`function_response` 不合并、`thought_signature` 回传、服务端工具桥接、429 重试、硬错误 HTTP 化、脱敏往返、流式拆包。

## 常见报错对照

| 客户端看到的现象 | 真正原因 | 处理 |
| --- | --- | --- |
| 连接被拒绝 / 无法连接 127.0.0.1:4000 | **本代理没启动** | 双击 `start_proxy.bat`，或 `python vertex_openai_proxy.py` |
| `[网络不可达] Clash/VPN 的代理端口 127.0.0.1:7897 连不上` | Clash / VPN 没开，或换了端口 | 开 Clash；改 `.env` 的 `VERTEX_PROXY` |
| `[网络不可达] 本机连不上 Google` | 没联网，或未配代理且被墙 | 检查网络；配 `VERTEX_PROXY` |
| `本机到 Google 的连接不通或被打断` | 节点抖动、TLS 被重置 | 代理已自动重试过，换节点或稍后重发 |
| `429 RESOURCE_EXHAUSTED` | 配额/速率用尽 | 等 30~60 秒；或改用 flash；Claude 需先申请配额 |
| `missing a thought_signature` | 代理重启导致签名缓存丢失 | 新建对话；或改用 2.5 系列 |
| `404 NOT_FOUND ... publishers/.../models/X` | 模型 ID 不存在或区域不对 | Gemini 用 `location=global`；Claude 也仅 global |

> 网络类错误现在会**先预检再决定要不要重试**：代理端口不通、DNS 失败这类直接返回，
> 不会让你干等 30 秒。链路抖动（超时/连接被重置）才走 5s→10s→20s 重试。

## 已知限制

- 图片仅支持 `data:` base64，远程 URL 未验证
- `response_format` 只支持 `json_object`，不支持严格 `json_schema`
- 代理重启后签名缓存清空，继续旧对话里带 `tool_call` 会 400 —— 新建对话即可
- 单用户设计，没有并发/多租户隔离

## 安全

`.env`、服务账号 JSON、`Temp/` 均已在 `.gitignore` 中排除。提交前请确认：

```powershell
git status --short
```

不要把真实 Key 贴进 issue、聊天或截图。
