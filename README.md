# vertex-openai-proxy

[![CI](https://github.com/2020638630/vertex-openai-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/2020638630/vertex-openai-proxy/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

本地 **OpenAI 兼容网关**：把标准 `/v1/chat/completions` 请求经官方 `google-genai` SDK 转发到 **Vertex AI**（Gemini），让只支持 OpenAI 协议的本地 Agent（如 ZCode）可以直接用 Vertex 额度。

> **定位**：个人自用项目，围绕「本地可控 + 成本可观测」打磨 —— 密钥不出本机、出站脱敏、
> 每次调用花了多少可查。欢迎参考和借鉴，但不承诺生产可用性。

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

## 多 provider：通用 OpenAI 兼容通道

除了 Vertex Gemini，还能把请求转发到**任何 OpenAI 兼容服务**：DeepSeek / 智谱 GLM / xAI Grok /
OpenAI / 本地 Ollama / 自建中转站。一个实现覆盖全部，配置驱动，新增一家不用改代码。

配置文件 `providers.json`（改完无需重启）：

```json
{
  "ollama":   { "base_url": "http://127.0.0.1:11434/v1", "api_key_env": "", "models": ["qwen2.5:7b"] },
  "deepseek": { "base_url": "https://api.deepseek.com/v1", "api_key_env": "DEEPSEEK_API_KEY",
                "models": ["deepseek-chat", "deepseek-reasoner"] }
}
```

### 预置的 provider

| provider | base_url | Key 环境变量 | 备注 |
| --- | --- | --- | --- |
| `ollama` | `http://127.0.0.1:11434/v1` | （无需） | 本地，成本 0 |
| `deepseek` | `https://api.deepseek.com/v1` | `DEEPSEEK_API_KEY` | 已实测 |
| `glm` | `https://open.bigmodel.cn/api/paas/v4` | `GLM_API_KEY` | 智谱，有免费档 |
| `siliconflow` | `https://api.siliconflow.cn/v1` | `SILICONFLOW_API_KEY` | 注册送额度 |
| `groq` | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` | 免费额度 |
| `grok` | `https://api.x.ai/v1` | `XAI_API_KEY` | 需充值 |
| `openai` | `https://api.openai.com/v1` | `OPENAI_API_KEY` | 需充值 |
| `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` | 一个 key 多家，有 `:free` |
| `relay` | 自填 | `RELAY_API_KEY` | 自建中转，空则跳过 |

只填你有 key 的那几个，其余留空 —— `base_url` 或 key 为空的 provider 不会被使用。

### 路由规则（混合，先精确后兜底）

| 写法 | 结果 |
| --- | --- |
| `ollama/qwen2.5:7b` | 前缀命中 → provider=ollama，上游只收 `qwen2.5:7b` |
| `deepseek-chat` | 前缀未命中 → 查各 provider 的 `models` 列表 → deepseek |
| `gemini-3.8-flash` | 都没命中 → 走 Vertex Gemini 通道 |
| `relay/任意名字` | 中转站，模型名原样透传 |

`base_url` 为空 = 该 provider 未启用，不会被劫持。

### 本地优先：Ollama

```powershell
ollama serve                        # 默认 11434
curl http://127.0.0.1:4000/v1/models  # 会列出 ollama/qwen2.5:7b 等
```

`/v1/models` 对**核实过**的模型还会附带 `context_window` / `max_output_tokens`，
客户端可用来预填上下文长度；未核实的模型（如 `gemini-4-argon`）不编造这两个字段。

本地模型**不走代理**（代码里对 `127.0.0.1` 自动跳过 `VERTEX_PROXY`），成本为 0
（`prices.json` 里配成 0，`/stats` 会显示 $0）。

实测（qwen2.5:7b，本机 CPU）：非流式 9.9s、流式 3.3s、工具调用 2.1s。

### usage 字段归一

各家命名不同，统一映射后才算得准账：

| 来源 | 输入 | 输出 | 缓存命中 |
| --- | --- | --- | --- |
| OpenAI 风格 | `prompt_tokens` | `completion_tokens` | `prompt_tokens_details.cached_tokens` |
| DeepSeek | `prompt_tokens` | `completion_tokens` | `prompt_cache_hit_tokens` |
| Anthropic 风格 | `input_tokens` | `output_tokens` | `cache_read_input_tokens` |

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

## 用量与限额

每次请求都会记一行到 `data/usage/YYYY-MM-DD.jsonl`（保留 14 天，`data/` 已忽略）：

```json
{"ts":"22:04:46","model":"gemini-3.8-flash","provider":"gemini","in":7,"out":2,"ms":4196,"ok":true,"tools":0}
```

- 记账在**响应完成后**写入，重启会从当天文件重建计数，不会清零
- 启动时读回当天账本，所以重启不影响当日累计

### 限额与「软着陆」

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `RPM_LIMIT` | 30 | 60 秒滑动窗口，超了**立即** 429（60 秒后恢复） |
| `DAILY_UNCACHED_LIMIT` | 5,000,000 | **主口径**：只算未命中缓存的输入 token（约 $25/天） |
| `DAILY_TOKEN_LIMIT` | 10,000,000 | 兜底：in + out 总 token |
| `DAILY_REQ_LIMIT` | 500 | 每日请求数 |
| `LIMIT_HARD_MULTIPLIER` | 3 | 总量超上限 N 倍时强制熔断；`0` = 关闭 |
| `LIMIT_MODE` | `block` | `warn` 只记日志不拦；`block` 启用软着陆 |

**为什么按"未命中 token"记账**：缓存命中部分官方只收 1 折（见下节），
按总 token 算会把额度算虚高 —— 实测同一天两轮，命中率 0% 和 81% 的消耗差了 2 倍。

**软着陆**：额度用完后**不会掐断正在跑的会话**，只写 `[额度预警]`；
等到**下一段会话的第一条消息**（按首条 user 消息指纹识别）才返回 429。
例外：总量超过 `LIMIT_HARD_MULTIPLIER` 倍时强制熔断，防 Agent 死循环一夜烧穿。

### 估算花费

单价放在根目录 `prices.json`（美元 / 百万 token），**改完不用重启**，`/stats` 每次现读：

```json
{ "gemini-4-argon": { "in": 4.0, "out": 20.0 },
  "gemini-3.8-flash": { "in": 0.75, "out": 3.75 } }
```

默认值取自作者本机结算账号的价目表导出（**Default 计费模式**，不含 1/3 年节省计划折扣）。
各家官方报价币种不同：智谱、硅基流动用人民币，用 `"currency": "CNY"` 标注，
加载时按 `_fx` 里的汇率统一折算成美元再计算；OpenRouter 的 `:free` 模型按后缀自动记为 0，无需逐条登记。
注意两点：**长上下文档**（如 2.5 Pro >200k token）单价更高，这里只取标准档；
`Claude` 不在该价目表内，故未配置，用到时会出现在 `missing_prices` 里。

#### 缓存折扣（为什么账单比"标价×token"低很多）

Gemini 的**隐式上下文缓存默认开启**，命中部分按输入价的 **10%** 计费（`_cache_discount: 0.9`）。
Agent 场景每轮都会重发几乎相同的前缀（system prompt + 工具定义 + 已读文件），命中率往往很高。

- 响应 `usage_metadata` 里的 `cachedContentTokenCount` 就是命中量，本代理已记录为账本里的 `cache` 字段
- `/stats` 同时给出「缓存命中率」，费用按 `未命中×全价 + 命中×一折 + 输出×全价` 计算
- 门槛：Gemini 4 / 3 系列 4,096 token，**3.8 Flash 6,144**，2 系列 2,048

实测（同一段 9,307 token 前缀连发两次）：第二次 `cache=4081`，命中 44%。
Agent 真实负载因前缀更稳定，命中率通常更高。

- `_note` 会原样显示在 `/stats` 页面底部，用来提醒"这是示例价"
- 没配价格的模型不会编造金额，会列在 `/stats` 的 `missing_prices` 里
- 价格**只用于估算展示，不参与限额拦截**（避免厂商调价导致误拦）

### 计数口径（与客户端统计不一致是正常的）

客户端（ZCode 等）统计的是"它发出和收到的内容"，我们统计的是"**真正打到 Google 的请求**"。三处会放大：

| 场景 | 客户端计 | 我们的账本 |
| --- | --- | --- |
| 429 自动重试 | 1 次 | 2~4 次上游调用 |
| 服务端搜索桥接 | 1 次 | 3 次（决策 + 搜索 + 作答） |
| 调用失败但已消耗输入 | 1 次失败 | 仍计入输入 token |

另外 `completion` 已**包含思考 token**（Gemini 的 `candidates_token_count` 不含思考，但计费含），
`total = prompt + completion` 与出账口径一致。

### 查看用量

```
GET /stats                 今日 + 近 7 日，JSON
GET /stats?format=html     一屏页面（浏览器直接打开）
GET /healthz               存活探针 + 当前限额模式
```

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
