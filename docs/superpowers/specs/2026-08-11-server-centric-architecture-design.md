# Server 中枢化架构调整设计 — IRC/XMPP 连接与命令处理上移

- 日期：2026-08-11
- 状态：待用户审阅
- 基础：`docs/superpowers/specs/2026-08-11-irc-xmpp-chatbot-design.md`（已实现，本设计为架构演进）

## 1. 目标

将架构从「Server=权限中枢 + Client 各自连 IM」调整为「**Server 连 IM 并处理一切，Client 仅执行 shell**」：

- Server 内嵌 IRC（pydle）与 XMPP（slixmpp）连接，处理全部聊天命令、LLM 调用、oper WHOIS、黑白名单踢人、shellop 投票
- Client 退化为**统一单类型**的纯 shell 执行器（仅 runcmd 执行 + getroot 提权），不再区分 IRC/XMPP
- **配置集中**：server.yaml 是唯一配置（含 `irc:`/`xmpp:`/`llm:`/`permissions:`），client.yaml 只剩 server 连接参数
- 协议精简：删除命令上报类消息，仅保留执行类消息

## 2. 已确认决策

| 决策点 | 结论 |
|---|---|
| 架构方向 | Server 连 IM + Client 仅执行 shell |
| client 形态 | **统一单类型 client**（不再分 client-irc/client-xmpp 子命令） |
| IM 组合 | server.yaml 中 `irc:` 与 `xmpp:` 段**可选**，配置了哪个就连哪个 |
| IM 地址归属 | 全部集中 server.yaml |
| LLM 配置 | 移入 server.yaml（server 处理 chat 时调用） |
| runcmd 权限判定 | 仍在 server（shellop 校验后转发 client），逻辑不变 |
| getroot 归属 | 仍在 client 端（密码验证 su --pty），不迁移 |
| 黑白名单踢人 | 逻辑上移：从 irc_client 迁至 server 内嵌 IRCSession |

## 3. 目标架构

```
┌────────────── server.yaml（唯一配置）────────────────┐
│ server: 监听端口/token/oper_cache_ttl/确认超时        │
│ irc: host/port/tls/channels/realname/bot_name ← 可选  │
│ xmpp: jid/password/host/mucs/bot_name        ← 可选  │
│ llm: format/base_url/api_key/model/...       ← 移入  │
│ permissions: botop/shellop/whitelist/blacklist       │
└──────────────┬──────────────────────────┬────────────┘
               │ pydle IRCSession（irc 段存在时）
               │ slixmpp XMPPSession（xmpp 段存在时）
               │   - 命令解析与执行 / LLM / WHOIS / 踢人 / 投票
               │
               │ TLS + JSON lines（机器人私有网络）
      ┌────────┴──────────┐   ┌─────────┴─────────┐
      │ client（执行器）   │   │ client（执行器）   │
      │ client.yaml:      │   │ name+token+server │
      │ name + server 连接 │   │ 连接              │
      │ runcmd shell +    │   │ runcmd + getroot  │
      │ getroot           │   │                  │
      └───────────────────┘   └───────────────────┘
```

## 4. 模块重组

### 4.1 server.py（扩展为核心）

新增/迁移能力：

| 能力 | 来源 | 说明 |
|---|---|---|
| `IRCSession` 内部类 | 迁移自 irc_client.py | pydle Client 子类，含 on_channel_message/on_message/on_join/whois/踢人/投票私信 |
| `XMPPSession` 内部类 | 迁移自 xmpp_client.py | slixmpp ClientXMPP 子类，仅 chat（私聊免前缀/群聊带前缀）+ 转发命令 |
| 命令本地执行 | 迁移自 irc_client `_maybe_command` | 解析 `!<bot_name> <cmd>` → 直接调 `_cmd_*`（不再发 `command` 消息） |
| LLM 调用 | 迁移自 irc/xmpp client | `chat` 命令在 server 调 `LLMClient`（配置读 server.yaml `llm:`） |
| 广播对象 | 原 `ClientConnection`（client 连接） | 保留，用于 runcmd 路由 |

关键交互：
- IRCSession/XMPPSession 持有对 BotServer 的引用（回调 `server._handle_chat_command(cmd, args, userhost, is_oper, channel, session)`）
- `_handle_chat_command` 复用现有 `_cmd_*` 处理器（botop/shellop/whitelist/blacklist/ban/unban/info/runcmd），输出经 session 发送（`session.message(...)` 回频道 / 私信）
- **新增 `_cmd_chat` 处理器**：server 直接调 `self.llm.chat(...)`（LLM 配置在 server.yaml），回复经 session 分片发送
- **XMPP 会话命令集限制**：XMPP 无 oper/botop 机制，`session_type == "xmpp"` 时 `handle_chat_command` 仅放行 `chat`/`help`（其余返回「XMPP 端仅支持 chat 命令」）；XMPP 用户 userhost 用 `jid` 映射（如 `user@snikket.example`），`is_oper` 恒 False
- ban/unban 的 `action` 现在由 session 直接执行 MODE（不再走 command_result）

### 4.2 删除/退化模块

| 模块 | 处理 |
|---|---|
| `irc_client.py` | **删除**（功能并入 server.py IRCSession） |
| `xmpp_client.py` | **删除**（功能并入 server.py XMPPSession） |
| `client_irc.yaml` / `client_xmpp.yaml` | 替换为单个 `client.yaml` |
| `__main__.py` | 子命令：`server` + `client`（`client-irc`/`client-xmpp` 删除） |

### 4.3 server_link.py（保留，client 端使用）

- 保留 `ServerLink`：TLS 连接/重连、auth、`exec_runcmd`、`exec_getroot`、`root_sessions`
- 删除 handler 回调机制（client 不再有 client 特有消息）
- `_on_server_msg` 只处理 `runcmd`/`getroot`/`auth_ok`

### 4.4 protocol.py（精简）

**保留消息类型**：

| type | 方向 | 用途 |
|---|---|---|
| `auth` | C→S | client 注册 |
| `auth_ok` | S→C | 认证结果 |
| `runcmd` | S→C | server 转发 shell 命令（含 caller_userhost） |
| `runcmd_result` | C→S | client 回传执行结果（含 as_root/root_expired） |
| `getroot` | S→C | server 下发 getroot 验证请求 |
| `status` | C→S | 心跳 |

**删除消息类型**：`command`、`command_result`、`permission_update`、`shellop_proposal`、`vote`、`reachability`

**保留构造器**：`make_auth`、`make_runcmd`、`make_runcmd_result`、`make_getroot`
**删除构造器**：`make_command`、`make_command_result`、`make_permission_update`、`make_shellop_proposal`、`make_vote`、`make_reachability`

## 5. 配置结构

### 5.1 server.yaml（唯一配置）

```yaml
server:
  listen_host: 0.0.0.0
  listen_port: 8443
  tls:
    certfile: server.crt
    keyfile: server.key
  token: "change-me-shared-secret"
  oper_cache_ttl: 60
  shellop_confirm_timeout: 60

irc:                       # ← 可选：不配则不连 IRC
  host: irc.example.com
  port: 6697
  tls: true
  tls_verify: true
  bot_name: qsdwindows_bot  # 唤醒词前缀 + IRC nickname
  realname: "My IRC Bot"
  channels:
    - "#chan1"

xmpp:                      # ← 可选：不配则不连 XMPP
  jid: "bot@snikket.example"
  password: "change-me"
  host: "snikket.example"
  port: 5222
  tls: true
  bot_name: qsdwindows_bot
  mucs:
    - "room@conference.snikket.example"

llm:                       # ← 移入：server 处理 chat 用
  format: openai_chat
  base_url: "https://api.openai.com/v1"
  api_key: "sk-xxx"
  model: "gpt-4o-mini"
  system_prompt: "You are a helpful bot."
  temperature: 0.7
  max_tokens: 1000
  timeout: 60
  max_reply_chars: 1500

permissions:
  botop: ["user1@host.example"]
  shellop: []
  whitelist: {enabled: false, entries: []}
  blacklist: {enabled: false, entries: []}
```

### 5.2 client.yaml（统一单类型）

```yaml
client:
  name: shell_node_1
  server:
    host: 127.0.0.1
    port: 8443
    token: "change-me-shared-secret"
    tls:
      verify: false
root_session_ttl: 300
```

### 5.3 热重载

- **server.yaml**：`permissions.*`、`llm.*`、`shellop_confirm_timeout`、`oper_cache_ttl` 热重载（`irc:`/`xmpp:` 连接参数与监听参数需重启）
- **client.yaml**：`root_session_ttl` 热重载；连接参数需重启

## 6. 命令处理流程（迁移后）

### 6.1 IRC 命令

```
用户在 #chan1 发 "!qsdwindows_bot ban baduser"
  → IRCSession.on_channel_message → server._handle_chat_command("ban", ["baduser"], "u@h", is_oper, "#chan1", session)
  → 权限校验（COMMAND_LEVELS + oper cache）
  → _cmd_ban：session.rawmsg("MODE", "#chan1", "+b", mask)
  → session.message("#chan1", "已批准封禁 baduser")
```

### 6.2 XMPP chat

```
私聊 "hello"（免前缀）→ XMPPSession._on_message → server._handle_chat_command("chat", ["hello"], jid_as_userhost, is_oper=False, None, session)
  → _cmd_chat（新增）：server.llm.chat(...) → session.send_message 回
```

### 6.3 runcmd（跨 client）

```
IRC 用户 "!qsdwindows_bot runcmd shell_node_1 ls -la"
  → server._cmd_runcmd：校验 shellop → 向 client shell_node_1 发 runcmd
  → client 执行 shell → runcmd_result → server 私信发起者（经原 session）
```

## 7. 消息回调接口（IRCSession/XMPPSession → BotServer）

```python
class BotServer:
    async def handle_chat_command(self, cmd, args, caller_userhost, is_oper, channel, session) -> None:
        # 复用现有 _cmd_* 处理器；回复经 session
```

IRCSession/XMPPSession 构造：`IRCSession(server, irc_cfg)` / `XMPPSession(server, xmpp_cfg)`

## 8. 测试策略

- **协议**：保留 auth/runcmd/runcmd_result/getroot 构造器测试；删除 command 等已删构造器测试
- **server**：新增 IRCSession 测试（mock pydle）、XMPPSession 测试（mock slixmpp）；`handle_chat_command` 权限与分发测试（沿用 FakeConn 思路）
- **server_link**：不变（exec_runcmd/exec_getroot/root_sessions 测试保留）
- **client**：`client` 单类型测试（auth + runcmd + getroot，mock subprocess）
- **迁移验证**：原 irc_client/xmpp_client 测试删除（功能上移），新增对应 server 端测试

## 9. 明确不做（YAGNI）

- 不做 client 的 LLM/命令处理（完全退化为执行器）
- 不做 server 到多个 IM 网络的冗余连接
- 不做 client 状态上报（status 保留但暂不实现具体载荷）
- 不保留 irc_client/xmpp_client 模块（删除而非保留兼容层）

## 10. 兼容性影响

- CLI：`python -m ircxmppbot client-irc/client-xmpp` 删除 → 改用 `python -m ircxmppbot client`
- 配置：`configs/client_irc.yaml`/`client_xmpp.yaml` 删除 → 用 `configs/client.yaml`
- 测试：125 个现有测试大改（删除 irc/xmpp client 测试，新增 server 端 session 测试）
- 文档：README / AGENTS.md / 既有 spec 需同步更新
