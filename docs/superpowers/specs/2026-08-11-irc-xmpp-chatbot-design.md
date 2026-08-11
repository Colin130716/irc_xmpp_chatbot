# IRC + XMPP 双协议聊天机器人 — 设计文档

- 日期：2026-08-11
- 状态：已获用户批准
- 环境：Python 3.14（本地 3.14.6），Linux

## 1. 目标与范围

构建一个能够**同时**接入 IRC 服务器（ngIRCd）和 XMPP 服务器（Snikket）的聊天机器人：

- 支持 TLS 连接（IRC / XMPP / server↔client 三条链路全部支持）
- 可自定义 LLM API，支持三种格式：OpenAI Chat Completions、OpenAI Responses、Anthropic Messages
- 配置文件热重载（除连接参数外全部可热重载）
- IRC 端完整权限体系 + 管理命令；XMPP 端仅 `chat` 命令
- server + client 分布式结构：一个 server 中枢 + 多个 client（IRC/XMPP），支持跨 client 远程命令执行（`runcmd`）

**明确不做（YAGNI）**：
- 不做 Web 管理界面
- 不做多 server 联邦
- 不做 XMPP 端权限管理（XMPP 仅 chat）
- 不做消息持久化/历史记录

## 2. 架构

```
┌─────────────────────────────────────────────────────────┐
│  Server（中枢，一个）                                      │
│  • 监听 TLS TCP 端口，接受 Client 连接（共享 token 认证）    │
│  • 权威权限存储：botop / shellop / whitelist / blacklist   │
│  • shellop 添加确认流程编排（提议→全员投票→生效/否决/超时）    │
│  • runcmd 跨 client 转发 + 结果回传                       │
│  • server.yaml 热重载（除监听参数外）                      │
└──────────────┬──────────────────────────┬────────────────┘
               │ TLS TCP + JSON lines      │ TLS TCP + JSON lines
   ┌───────────▼─────────────┐  ┌──────────▼─────────────┐
   │ Client (IRC)             │  │ Client (XMPP)           │
   │ • pydle 连接 ngIRCd      │  │ • slixmpp 连接 Snikket  │
   │ • 完整命令集              │  │ • 仅 chat 命令           │
   │ • oper WHOIS(313) 检测   │  │ • 私聊免前缀 / 群聊带前缀 │
   │ • 黑白名单踢人 / KICK     │  │                         │
   │ • 本地 LLM 调用           │  │ • 本地 LLM 调用          │
   └──────────────────────────┘  └────────────────────────┘
```

### 2.1 组件职责

| 组件 | 职责 | 不做什么 |
|---|---|---|
| **Server** | 权限权威判定、shellop 确认编排、runcmd 路由、权限快照推送、配置热重载 | 不连接任何 IM 网络，不做 LLM 调用 |
| **IRC Client** | 连接 ngIRCd、收发消息、执行 IRC 命令（KICK/MODE/WHOIS）、oper 检测、黑白名单即时踢人、LLM 调用 | 不持有权威权限数据（本地仅缓存快照） |
| **XMPP Client** | 连接 Snikket、收发消息、仅执行 chat（私聊免前缀/群聊带前缀）、LLM 调用 | 不参与权限/管理命令 |

### 2.2 技术选型

| 用途 | 选型 | 理由 |
|---|---|---|
| XMPP | `slixmpp>=1.12` | 官方支持 Python 3.14（1.12.0 起修复 Py3.14 兼容）；异步；成熟 |
| IRC | `pydle>=1.1` | 异步、IRCv3、TLS、自动跟踪频道成员/模式（支撑黑白名单踢人）、`on_raw_*` 可处理 313 等数字回复 |
| 配置 | `PyYAML` | YAML 可读性/注释支持；用户已确认 |
| HTTP(LLM) | `httpx` | 异步客户端，三种 LLM 格式统一封装 |
| 协议 | 标准库 `asyncio` + `ssl` | server↔client 自定义协议无需第三方框架 |

依赖清单：`slixmpp>=1.12`、`pydle>=1.1`、`PyYAML>=6`、`httpx>=0.27`

## 3. 权限模型（仅 IRC 端）

### 3.1 权限层级

| 级别 | 获取方式 | 标识 |
|---|---|---|
| **所有用户** | 默认 | — |
| **botop** | `botop give/remove`（仅 oper 可调） | `user@host` 精确匹配（忽略 nick） |
| **oper** | 用户在 IRC 端 `/oper <name> <pass>` 成功后，由机器人实时 WHOIS 检测 313 | `user@host` |
| **shellop** | `shellop add`（仅 oper 发起）+ 全员确认；候选人**必须已是 botop 且是 oper** | `user@host` |

### 3.2 命令权限矩阵

| 命令 | 所有用户 | botop | oper | shellop |
|---|---|---|---|---|
| `chat <文本>` | ✅ | ✅ | ✅ | ✅ |
| `help` | ✅ | ✅ | ✅ | ✅ |
| `ban <nick>` / `unban <nick>` | — | ✅ | ✅ | ✅ |
| `whitelist add/del/list` | — | ✅ | ✅ | ✅ |
| `blacklist add/del/list` | — | ✅ | ✅ | ✅ |
| `info` / `status` | — | ✅ | ✅ | ✅ |
| `botop give/remove <user@host>` | — | — | ✅ | ✅ |
| `shellop add <user@host>`（发起） | — | — | ✅ | ✅ |
| `shellop remove <user@host>` | — | — | ✅ | ✅ |
| `confirm <提议ID>` / `reject <提议ID>` | — | ✅ | ✅ | ✅ |
| `runcmd <client_name> <cmd>` | — | — | — | ✅ |

### 3.3 oper 检测机制（ngIRCd 特定）

- ngIRCd 在 `/oper` 成功后仅向 oper 本人发送 `381 RPL_YOUREOPER`，**不向其他用户广播模式变更**
- 因此机器人对**命令调用者实时执行 WHOIS**，解析 `313 RPL_WHOISOPERATOR`（"is an IRC operator"）判定 oper 身份
- WHOIS 结果按 `user@host` 缓存于 server（带 TTL，如 60 秒），缓存有效期内免重复 WHOIS
- 权限判定发生在 server：client 上报调用者 `user@host`，server 查 botop 表 + oper 缓存 + shellop 表

### 3.4 黑白名单（全局 + 可指定频道）

- 每一条目格式：`mask`（用户掩码）+ 可选 `channel`（不填=全局生效）
- **白名单**与**黑名单不能同时启用**（配置校验 + 运行时互斥，违反时命令拒绝并提示）
- **白名单启用时**：除「白名单条目匹配的用户 + oper + botop」外，所有频道用户被 kick；JOIN 事件即时检测，新加入者不满足即踢
- **黑名单启用时**：黑名单条目匹配的用户被 kick（JOIN 时即时检测）
- 踢人依赖机器人频道 `+o`（用户在 ngIRCd 配置服务器自动 op，机器人加入频道即为频道管理员）
- 权限数据权威在 server；变更后 server 推送新快照给所有 client，client 本地即时执行踢人

### 3.5 shellop 添加确认流程

1. oper 发送 `!<bot_name> shellop add <user@host>`
2. server 校验候选人：必须**同时**在 botop 表且 oper 缓存中，否则拒绝
3. server 生成提议 ID，向所有**在线**的 botop + oper 发送私信：「提议 #ID：将 `<user@host>` 设为 shellop，请回复 `confirm #ID` 或 `reject #ID`」（私信经调用者所在 client 转发）
4. 投票规则：**全员同意**才生效；发起者自动计同意票；任一 reject 即否决；超时（默认 60 秒，可配置）作废
5. 生效后 server 更新 shellop 表并推送权限快照；否决/超时均向发起者私信说明结果
6. `shellop remove` 无需确认，仅 oper 直接执行

## 4. 命令参考（IRC 端）

唤醒词默认 `!<bot_name> <cmd>`，`bot_name` 在 client 配置中设置；IRC nickname 同样使用 `bot_name`；realname 在 client 配置的 IRC 连接段单独设置。

```
!<bot_name> chat <文本>                  # LLM 对话（所有人）
!<bot_name> help                         # 命令帮助（所有人）
!<bot_name> ban <nick>                   # 频道 MODE +b 封禁（botop+）
!<bot_name> unban <nick>                 # 频道 MODE -b 解封（botop+）
!<bot_name> whitelist add <mask> [channel]
!<bot_name> whitelist del <mask> [channel]
!<bot_name> whitelist list
!<bot_name> blacklist add <mask> [channel]
!<bot_name> blacklist del <mask> [channel]
!<bot_name> blacklist list
!<bot_name> botop give <user@host>
!<bot_name> botop remove <user@host>
!<bot_name> shellop add <user@host>      # 进入全员确认流程
!<bot_name> shellop remove <user@host>
!<bot_name> confirm <提议ID>              # 投票同意
!<bot_name> reject <提议ID>               # 投票否决
!<bot_name> runcmd <client_name> <cmd>    # 远程 shell（结果私信）
!<bot_name> info                          # 机器人状态（版本/连接/权限概况）
```

- 命令不匹配时提示 `未知命令，输入 help 查看帮助`
- 权限不足时私信提示 `权限不足`

## 5. Server ↔ Client 协议

- **传输**：TLS TCP（自签证书或系统 CA），JSON lines（每行一个 JSON 对象）
- **认证**：连接后首条消息为 `auth`，携带共享 token；失败则断开
- **client_name**：client 在自身配置中定义（如 `irc_main`、`xmpp_main`），注册时上报 server

消息类型（type 字段）：

| type | 方向 | 载荷 |
|---|---|---|
| `auth` | C→S | `{token, client_name, client_type, bot_name}` |
| `command` | C→S | `{cmd, args, caller_userhost, channel, proposal_id?}` |
| `command_result` | S→C | `{ok, reply, target, target_type}` |
| `permission_update` | S→C | 全量权限快照 `{botop, shellop, whitelist, blacklist, oper_cache_ttl}` |
| `shellop_proposal` | S→C | `{proposal_id, candidate, deadline}`（client 转私信给本网络在线 botop+oper） |
| `vote` | C→S | `{proposal_id, vote}`（confirm/reject 上报） |
| `runcmd` | S→C | `{task_id, cmd}`（路由到目标 client） |
| `runcmd_result` | C→S | `{task_id, ok, output}`（目标 client 执行后回传） |
| `status` | C→S | 心跳/在线状态 |

数据流示例（runcmd）：
1. 用户在 IRC 频道发 `!<bot_name> runcmd xmpp_main ls -la`
2. IRC client 解析 → 上报 `command` 到 server（含调用者 user@host）
3. server 校验 shellop 权限（查 shellop 表）→ 通过后向名为 `xmpp_main` 的 client 发 `runcmd`
4. XMPP client 在本地用 `subprocess` 执行 shell 命令（不截断、不超时）→ 回传 `runcmd_result`
5. server 路由回发起者所在 client → client **私信**调用者完整输出（输出按 `max_reply_chars` 分片发送，IRC 512 字节/行限制内；输出为空时提示「命令执行成功，无输出」）

## 6. 配置文件（YAML）

### 6.1 server.yaml

```yaml
server:
  listen_host: 0.0.0.0
  listen_port: 8443
  tls:
    certfile: server.crt
    keyfile: server.key
  token: "change-me-shared-secret"      # client 连接认证
  oper_cache_ttl: 60                    # WHOIS 结果缓存秒数
  shellop_confirm_timeout: 60           # 确认超时秒数

permissions:
  botop:
    - "user1@host.example"
  shellop:
    - "user2@host.example"
  whitelist:
    enabled: false                      # 与 blacklist 互斥
    entries:
      - mask: "user1!*@*.example"
        channel: "#chan1"               # 可选，不填=全局
  blacklist:
    enabled: false
    entries:
      - mask: "bad!*@*"
```

### 6.2 client_irc.yaml

```yaml
client:
  name: irc_main
  type: irc
  bot_name: qsdwindows_bot              # 唤醒词前缀 + IRC nickname
  server:
    host: 127.0.0.1
    port: 8443
    token: "change-me-shared-secret"
    tls:
      verify: false                     # 或 ca_cert: server.crt

irc:
  host: irc.example.com
  port: 6697
  tls: true
  realname: "My IRC Bot"                # realname 在 IRC 段单独配置
  channels:
    - "#chan1"
    - "#chan2"

llm:
  format: openai_chat                  # openai_chat | openai_responses | anthropic
  base_url: "https://api.openai.com/v1"
  api_key: "sk-xxx"
  model: "gpt-4o-mini"
  system_prompt: "You are a helpful IRC bot."
  temperature: 0.7
  max_tokens: 1000
  timeout: 60
  max_reply_chars: 1500                # 频道回复分片长度（IRC 512 字节限制内）
```

### 6.3 client_xmpp.yaml

```yaml
client:
  name: xmpp_main
  type: xmpp
  bot_name: qsdwindows_bot
  server:
    host: 127.0.0.1
    port: 8443
    token: "change-me-shared-secret"
    tls:
      verify: false

xmpp:
  jid: "bot@snikket.example"
  password: "xxx"
  host: "snikket.example"              # 可选，覆盖 DNS SRV
  port: 5222
  tls: true
  mucs:                                # 群聊（MUC）
    - "room@conference.snikket.example"

llm:
  # 同 client_irc.yaml 的 llm 段
```

### 6.4 热重载规则

- **Server**：轮询 server.yaml mtime（2 秒间隔），变更即重载 `permissions.*` 与 `shellop_confirm_timeout`、`oper_cache_ttl`；`server.listen_*` / `tls` / `token` 变更需重启（记录日志提示）
- **Client**：轮询自身 yaml mtime，变更即重载 `llm.*`、`bot_name`（广播改名需重新注册到 server）、`irc.channels`（自动加入新增频道）、黑白名单本地镜像（以 server 推送为准）；`client.server.*` 与 `irc.host/port/tls`、`xmpp.jid/password` 等连接参数变更需重启
- **权限持久化策略**：server.yaml 的 `permissions.*` 是唯一权威持久化存储。权限命令（botop/shellop/黑白名单增删）生效时**原子写回** server.yaml；热重载重读文件时以文件内容为准替换内存。管理员手动编辑文件 + 热重载与命令修改效果一致，二者无冲突

## 7. LLM 集成

统一封装 `LLMClient`，三种 format：

| format | 端点 | 认证 | 请求体要点 |
|---|---|---|---|
| `openai_chat` | `{base_url}/chat/completions` | `Authorization: Bearer <key>` | `{model, messages:[{role,content}], temperature, max_tokens}` |
| `openai_responses` | `{base_url}/responses` | 同上 | `{model, input:"..." , temperature, max_tokens}`（OpenAI Responses API，input 可为字符串或消息数组） |
| `anthropic` | `{base_url}/messages` | `x-api-key: <key>` + `anthropic-version: 2023-06-01` | `{model, system, messages:[{role,content}], max_tokens, temperature}` |

- 使用 httpx AsyncClient，单次请求超时（可配置）
- 对话模式：仅单轮（不维护多轮上下文，YAGNI；如需可后续扩展 `history` 段）
- 回复长度超限时按 `max_reply_chars` 分片发送（IRC 512 字节/行限制；XMPP 无严格限制但同样分片）
- LLM 错误（超时/HTTP 错误/格式错误）→ 私信或频道友好报错

## 8. 可靠性

- **断线重连**：IRC / XMPP / server 连接均指数退避重连（1s → 2s → 4s … 上限 60s）
- **权限缓存**：server 的 oper 缓存 TTL 60s；botop/shellop 为权威数据无 TTL
- **并发**：单事件循环（asyncio），各 client 独立任务；命令处理加锁防止并发竞态（如投票计数）
- **日志**：`logging`，INFO 级别常规、DEBUG 级别协议帧；日志到 stdout + 可选文件
- **失败处理**：LLM 失败不影响命令处理；runcmd 目标 client 不在线 → 明确报错；投票超时 → 清理提议状态

## 9. 测试策略

- 单元测试（pytest + pytest-asyncio）：
  - 命令解析器（各命令参数校验、错误输入）
  - 权限判定（botop/oper/shellop/黑白名单豁免矩阵）
  - shellop 确认状态机（全员同意/任一否决/超时作废）
  - LLM 请求构造（三种 format 的请求体/头断言，mock httpx）
  - 协议编解码（JSON lines 封包解析）
- 集成测试（可选，mock IM 传输层）：
  - 消息流转：chat / runcmd / 黑白名单踢人 全链路
- 不依赖真实 IRC/XMPP 服务器（测试用 mock transport 注入）

## 10. 目录结构（规划）

```
irc_xmpp_chatbot/
├── README.md
├── pyproject.toml                 # 依赖 + pytest 配置
├── configs/
│   ├── server.yaml
│   ├── client_irc.yaml
│   └── client_xmpp.yaml
├── src/ircxmppbot/
│   ├── __init__.py
│   ├── __main__.py                # python -m ircxmppbot server|client-irc|client-xmpp
│   ├── config.py                  # YAML 加载 + 热重载（mtime 轮询）
│   ├── protocol.py                # JSON lines 编解码 + 消息类型定义
│   ├── permissions.py             # 权限模型/判定（botop/shellop/黑白名单）
│   ├── server.py                  # server 主逻辑（监听/认证/路由/确认编排）
│   ├── llm.py                     # LLMClient（三种 format）
│   ├── irc_client.py              # pydle 封装 + 命令分发 + oper WHOIS
│   ├── xmpp_client.py             # slixmpp 封装 + chat 命令
│   ├── commands.py                # 命令解析与权限门控（IRC 端）
│   └── util.py                    # 日志/重连/分片等工具
└── tests/
    ├── test_commands.py
    ├── test_permissions.py
    ├── test_shellop_flow.py
    ├── test_llm.py
    └── test_protocol.py
```

## 11. 已确认的决策记录

1. runcmd `<cmd>` = **shell 命令执行**（subprocess），仅 shellop 可调，结果私信调用者，不截断不超时
2. 黑白名单：**全局 + 可指定频道**；白名单与黑名单互斥；白名单豁免 = 白名单用户 + oper + botop
3. botop 匹配字段 = **user@host**（IRC）
4. 配置格式 = **YAML**
5. LLM 配置 = **每 client 独立**
6. 权限数据**集中存 server**，client 持快照；XMPP 不参与权限机制
7. shellop：候选人必须**同时是 botop 且 oper**；`shellop add` 仅 oper 发起；确认需**全体在线 botop+oper 同意**，发起者自动同意，任一 reject 否决，超时（60s）作废；`shellop remove` 仅 oper 直行
8. 唤醒词默认 `!<bot_name> <cmd>`，bot_name 在 client 配置定义并用作 IRC nickname；realname 在 IRC 段单独配置
9. 热重载范围：除连接参数外全热重载
10. XMPP：私聊免前缀直接 chat，群聊需前缀
11. IRC 服务器为 **ngIRCd**；oper 检测 = 实时 WHOIS 查 313；踢人权限 = 服务器自动 op
