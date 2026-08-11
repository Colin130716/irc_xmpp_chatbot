# IRC + XMPP 双协议聊天机器人

基于 server + client 结构的分布式聊天机器人，同时接入 IRC（ngIRCd）与 XMPP（Snikket）。

## 功能

- TLS 连接（IRC / XMPP / server↔client 三条链路）
- LLM API：OpenAI Chat Completions / OpenAI Responses / Anthropic Messages 三种格式
- 配置文件热重载（除连接参数外）
- IRC 端完整权限体系：botop / oper / shellop / 黑白名单
- 跨 client 远程 shell 执行：`!<bot_name> runcmd <client_name> <cmd>`
- XMPP 端仅 chat（私聊免前缀 / 群聊带前缀）

## 快速开始

```bash
pip install -e ".[dev]"

# 1. 准备证书（server 端 TLS）
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout configs/server.key -out configs/server.crt -days 365 \
  -subj "/CN=localhost"

# 2. 编辑 configs/*.yaml（token 三处一致；填入真实 IRC/XMPP/LLM 参数）

# 3. 启动 server
python -m ircxmppbot server

# 4. 启动 client（可多个，连接不同网络）
python -m ircxmppbot client-irc
python -m ircxmppbot client-xmpp
```

## 命令（IRC 端，唤醒词 `!<bot_name>`）

| 命令 | 权限 | 说明 |
|---|---|---|
| `chat <文本>` | 所有人 | LLM 对话 |
| `help` | 所有人 | 帮助 |
| `ban/unban <nick>` | botop+ | 频道 MODE +b/-b |
| `whitelist/blacklist add/del/list/on/off` | botop+ | 黑白名单管理（互斥） |
| `info` | botop+ | 状态 |
| `botop give/remove <user@host>` | oper | 管理 botop |
| `shellop add <user@host>` | oper | 发起确认流程（候选人须 botop+oper） |
| `shellop remove <user@host>` | oper | 直接移除 |
| `confirm/reject <ID>` | botop+ | shellop 提议投票 |
| `runcmd <client_name> <cmd>` | shellop | 远程 shell（结果私信） |

## 说明

- oper 身份通过 WHOIS 313 实时检测（ngIRCd 不广播 oper 变更）
- 白名单/黑名单禁止同时启用；白名单豁免 = 白名单用户 + oper + botop
- 权限命令生效时原子写回 server.yaml；热重载以文件为准
