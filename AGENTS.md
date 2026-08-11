# AGENTS.md

IRC + XMPP 双协议聊天机器人（server + client 分布式架构）。Python 3.14+，`src/ircxmppbot/` 包。

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

$P tests/                          # 全量测试（103 个）
$P tests/test_server.py -v         # 单文件
$P tests/test_server.py::test_runcmd_getroot_routes_to_self -v  # 单个测试
$PY -m ircxmppbot --help           # CLI 三个子命令：server / client-irc / client-xmpp
$PY -m ircxmppbot server configs/server.yaml   # 启动（需真实证书/服务器参数）
```

## 架构

- **server.py（BotServer）**：权威权限中枢。持有 botop/shellop/黑白名单、oper 缓存（WHOIS 313 上报）、shellop 确认投票编排、runcmd 跨 client 路由。权限命令生效时**原子写回 server.yaml**（tmp + `os.replace`），热重载以文件为准。
- **irc_client.py（IRCBot，pydle）**：完整命令集、oper WHOIS、黑白名单 JOIN 踢人、`_exec_getroot`/`_exec_runcmd`。
- **xmpp_client.py（XMPPBot，slixmpp）**：仅 chat（私聊免前缀/群聊带前缀）+ runcmd 执行目标，**不参与权限机制**。
- **protocol.py**：TLS TCP + JSON lines，每个消息含 `type` 字段；`make_*` 构造器是唯一消息来源。新增消息类型须同时更新 `VALID_TYPES`。
- **permissions.py**：`LEVELS = everyone < botop < oper < shellop`；白名单与黑名单**禁止同时启用**（`validate_mutex` 抛 `ConfigError`）。

## 命令与权限

`commands.py::COMMAND_LEVELS` 是命令→最低权限注册表（chat/help=everyone，ban/unban/whitelist/blacklist/info/confirm/reject=botop，botop/shellop=oper，runcmd=shellop）。

- **confirm/reject 走 `vote` 消息**（server 无 `_cmd_confirm/_cmd_reject` 处理器）——irc_client `_maybe_command` 特判路由到 `_server_vote`。
- **getroot**：`runcmd getroot <pw>` 是 server 特判子命令（目标=调用者所在 client），密码存 client 内存 `root_sessions`，5 分钟 TTL（`root_session_ttl` 配置），过期回退普通用户并提示。密码**不可**进日志或 `cmd` 字段。
- 权限判定在 server 端：client 上报 `command`（含 caller_userhost + caller_is_oper），server 查权限表后返回 `command_result`。

## 库版本特性（易踩坑）

- **pydle 1.1**：JOIN 事件处理器是 `on_join`（不是 `on_user_join`）；未连接时 `self.nickname` 是 `"<unregistered>"`（配置昵称在 `self._nicknames[0]`）；断线自动重连（RECONNECT_ON_ERROR）。
- **slixmpp ≥1.9 已移除 `process()`**——用 `await self.disconnected`；`XMLStream.disconnected` 是文档化的 Future。
- **su --pty**：getroot 依赖 util-linux 的 `--pty`（macOS 的 su 不支持）；`printf '%s\n' pw | su --pty root -c cmd` 是唯一可靠的管道密码注入方式。

## 测试约定

- `pytest-asyncio` 为 auto 模式（async 测试无需装饰器）；测试不依赖真实 IRC/XMPP/root（mock transport / mock `asyncio.create_subprocess_shell`）。
- mock 被 `await` 的方法（如 `_server_send`、`message`、`kick`、`_whois_user`）必须是 **async 函数**；slixmpp 的 `send_message` 是同步调用，mock 须为同步。
- 服务器测试用 `FakeConn`（记录 `sent` 消息）；需验证广播（shellop 提议/权限快照）到达时必须先把 conn 注册进 `srv.conns`。
- shellop 流程测试会创建 proposal timer 任务，`_finalize_proposal` 已 await 取消，测试结束无悬挂任务。

## 文档与流程

- 设计文档：`docs/superpowers/specs/`；实现计划：`docs/superpowers/plans/`（TDD 逐任务，每任务含失败测试→实现→回归→提交）。
- 项目用 superpowers 流程（brainstorming → writing-plans → executing-plans）；用户偏好**不用子代理**，Inline 执行。
- 提交粒度：每个功能任务一个 commit，消息前缀 `feat:`/`docs:`/`chore:`。
