# AGENTS.md

IRC + XMPP 双协议聊天机器人（**server 中枢化**架构）。Python 3.14+，`src/ircxmppbot/` 包。

## 环境陷阱（必须先知道）

- **项目目录在 exFAT 文件系统上，无法创建 venv**（`python -m venv .venv` 和 `uv venv .venv` 都会因 symlink 受限失败）。
  - 现成 venv 在 `/tmp/opencode/ircxmppbot-venv`（editable 安装指向本仓库）。
  - 所有测试/运行命令用 `/tmp/opencode/ircxmppbot-venv/bin/pytest` / `.../bin/python`。
  - 若该 venv 缺失：`uv venv /tmp/opencode/ircxmppbot-venv && uv pip install --python /tmp/opencode/ircxmppbot-venv/bin/python -e ".[dev]"`。
- `.gitignore` 已忽略 `configs/*.crt`、`configs/*.key`——server 的 TLS 证书不可入库。

## 常用命令

```bash
PY=/tmp/opencode/ircxmppbot-venv/bin/python
P=/tmp/opencode/ircxmppbot-venv/bin/pytest

$P tests/                          # 全量测试（97 个）
$P tests/test_server.py -v         # 单文件
$P tests/test_server.py::test_cmd_chat_uses_llm -v   # 单个测试
$PY -m ircxmppbot --help           # CLI 两个子命令：server / client
$PY -m ircxmppbot server configs/server.yaml   # 启动 server（自动连 IRC/XMPP，需真实参数）
$PY -m ircxmppbot client configs/client.yaml   # 启动 shell 执行器
```

## 架构

- **server.py（BotServer）**：中枢。持有 botop/shellop/黑白名单、oper 缓存（WHOIS 313 上报）、shellop 确认投票编排、runcmd 跨 client 路由、LLM 调用。权限命令生效时**原子写回 server.yaml**（tmp + `os.replace`），热重载以文件为准。
- server↔client TLS 可选：`tls.enabled` 统一判定（默认启用，`enabled: false` 走明文；server 缺 certfile 启动报错；握手失败日志提示检查两端配置）
- **irc_session.py（IRCSession，pydle）**：server 内嵌的 IRC 连接（ChatTarget 实现）。命令解析转发、oper WHOIS、黑白名单 JOIN 踢人、shellop 投票私信（`dm_voters`）。
- **xmpp_session.py（XMPPSession，slixmpp）**：server 内嵌的 XMPP 连接，仅 chat/help（私聊免前缀/群聊带前缀），不参与权限机制。
- **client.py（ShellClient）**：统一单类型执行器，仅连接 server + 执行 runcmd/getroot。
- **server_link.py（ServerLink）**：client 端连接层——TLS 连接/重连、消息分发、getroot/root 会话（`exec_getroot`/`exec_runcmd`/`root_sessions`）。
- **protocol.py**：TLS TCP + JSON lines，仅 6 种执行类消息（auth/auth_ok/runcmd/runcmd_result/getroot/status）；`make_*` 构造器是唯一消息来源。
- **permissions.py**：`LEVELS = everyone < botop < oper < shellop`；白名单与黑名单**禁止同时启用**（`validate_mutex` 抛 `ConfigError`）。

## 命令与权限

`commands.py::COMMAND_LEVELS` 是命令→最低权限注册表（chat/help=everyone，ban/unban/whitelist/blacklist/info/confirm/reject=botop，botop/shellop=oper，runcmd=shellop）。

- **命令在 server 本地处理**：IRCSession/XMPPSession 收到聊天消息 → `server.handle_chat_command(...)` → 复用 `_cmd_*` 处理器（`ChatTarget` 抽象回复）。无 command 消息上报。
- **ChatTarget**：`_cmd_*` 的第一个参数（原 conn）。`reply(text, channel, private_to)` 发回复，`execute(action, args, channel)` 执行频道动作（ban/unban MODE），`dm_voters(...)` 私信 shellop 投票人。
- **XMPP 限制**：`handle_chat_command` 对 xmpp session 仅放行 chat/help；XMPP 用户 userhost 用 jid 映射，is_oper 恒 False。
- **confirm/reject**：`_cmd_confirm`/`_cmd_reject` 处理器（botop+）调 `_handle_vote`；shellop 投票私信在 IRCSession 的 `dm_voters` 私信中提示。
- **getroot**：`runcmd getroot <client_name> <pw>` 由 server 转发到指定 client 执行器，密码存 client 内存 `root_sessions`（5 分钟 TTL），过期回退普通用户并提示。密码**不可**进日志或 `cmd` 字段；经 `su --pty` 的 stdin 传递（不进 argv）。
- **shellop 确认（M8）**：投票人须可触达——`dm_voters` 私信可达投票人并返回名单，仅发起者可触达则直接通过。

## 库版本特性（易踩坑）

- **pydle 1.1**：JOIN 事件处理器是 `on_join`（不是 `on_user_join`）；未连接时 `self.nickname` 是 `"<unregistered>"`（配置昵称在 `self._nicknames[0]`）；断线自动重连（RECONNECT_ON_ERROR）。
- **slixmpp ≥1.9 已移除 `process()`**——用 `await self.disconnected`；`XMLStream.disconnected` 是文档化的 Future。
- LLM 思考配置（llm.py）：`thinking`（enabled/disabled/adaptive，三格式）、`reasoning_effort`（仅 OpenAI 兼容）、`thinking_config`（anthropic dict 透传）、`anthropic_effort`（→ output_config.effort）；不配置则不发
- **slixmpp `server` property**：slixmpp 有 `server` 属性 setter，自定义属性**不可命名 `server`**——XMPPSession 用 `self.srv` 存 server 引用。
- **su --pty**：getroot 依赖 util-linux 的 `--pty`（macOS 的 su 不支持）；密码走 `create_subprocess_exec` 的 stdin（不进 argv，`ps` 不可见）。

## 测试约定

- `pytest-asyncio` 为 auto 模式（async 测试无需装饰器）；测试不依赖真实 IRC/XMPP/root（mock pydle/slixmpp/subprocess）。
- mock 被 `await` 的方法必须是 **async 函数**；slixmpp 的 `send_message` 是同步调用，mock 须为同步。
- server 测试用 `FakeTarget`（ChatTarget 存根：记录 replied/executed/dm_reachable）+ `FakeConn`（client 连接，记录 sent）。
- client 的 getroot/root 会话逻辑在 `ServerLink` 上（`link.exec_getroot` 等）；测试 mock `asyncio.create_subprocess_exec`（密码走 stdin）与 `create_subprocess_shell`（普通命令）。
- XMPPSession 测试需 `FakeServer` 提供 `llm` 属性（session 构造引用 `server.llm`）。

## 文档与流程

- 设计文档：`docs/superpowers/specs/`；实现计划：`docs/superpowers/plans/`（TDD 逐任务）。
- 项目用 superpowers 流程（brainstorming → writing-plans → executing-plans）；用户偏好**不用子代理**，Inline 执行。
- 提交粒度：每个功能任务一个 commit，消息前缀 `feat:`/`fix:`/`refactor:`/`docs:`/`chore:`。
