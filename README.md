# IRC + XMPP 双协议聊天机器人

基于 **server 中枢化** 架构：server 直接连接 IRC（ngIRCd）与 XMPP（Snikket）并处理全部命令；client 是分布式的纯 shell 执行器。

## 功能

- TLS 连接：IRC/XMPP 各自配置；server↔client 可选（`tls.enabled` 控制，默认启用，`enabled: false` 走明文）
- LLM API：OpenAI Chat Completions / OpenAI Responses / Anthropic Messages 三种格式
- LLM 思考控制：thinking 开关（三格式）+ DeepSeek reasoning_effort + Anthropic output_config.effort
- 配置文件热重载（server.yaml 权限/LLM 段）
- IRC 端完整权限体系：botop / oper / shellop / 黑白名单
- 跨 client 远程 shell 执行：`!<bot_name> runcmd <client_name> <cmd>`
- getroot 提权：`!<bot_name> runcmd getroot <root密码>`（5 分钟会话，su 提权）
- XMPP 端仅 chat（私聊免前缀 / 群聊带前缀）

## 快速开始

```bash
pip install -e ".[dev]"

# 1. 准备证书（server 端 TLS）
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout configs/server.key -out configs/server.crt -days 365 \
  -subj "/CN=localhost"

# 2. 从示例复制并编辑配置（IRC/XMPP/LLM/权限 全部集中于 server.yaml）
cp configs/server.example.yaml configs/server.yaml
cp configs/client.example.yaml configs/client.yaml
#    编辑 configs/server.yaml（server 端配置）
#    编辑 configs/client.yaml（shell 执行器连接参数）

# 3. 启动 server（自动连接配置中的 IRC/XMPP 段）
python -m ircxmppbot server

# 4. 启动 client 执行器（可多个，分布式部署）
python -m ircxmppbot client
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
| `shellop add <user@host>` | oper | 发起确认流程（候选人须 botop+oper，投票人须可触达） |
| `shellop remove <user@host>` | oper | 直接移除 |
| `confirm/reject <ID>` | botop+ | shellop 提议投票 |
| `runcmd <client_name> <cmd>` | shellop | 远程 shell（结果私信） |
| `runcmd getroot <client_root_password>` | shellop | 本 client root 提权（5 分钟有效） |

## 架构

```
┌────────────── server.yaml（唯一配置）──────────────┐
│ server: 监听端口/token                              │
│ irc: host/port/channels/bot_name      ← 可选段      │
│ xmpp: jid/password/host/mucs          ← 可选段      │
│ llm: 三种格式配置                                    │
│ permissions: botop/shellop/黑白名单                  │
└──────────────┬──────────────────────────┬───────────┘
               │ pydle IRCSession / slixmpp XMPPSession（server 内）
               │ 命令处理 / LLM / WHOIS / 踢人 / 投票
               │
               │ TLS + JSON lines（机器人私有网络）
      ┌────────┴─────────┐   ┌────────┴─────────┐
      │ client（执行器）  │   │ client（执行器）  │
      │ client.yaml      │   │ 仅 name+server 连接│
      │ runcmd shell     │   │ runcmd + getroot  │
      └──────────────────┘   └──────────────────┘
```

## 说明

- oper 身份通过 WHOIS 313 实时检测（ngIRCd 不广播 oper 变更）
- 白名单/黑名单禁止同时启用；白名单豁免 = 白名单用户 + oper + botop
- 权限命令生效时原子写回 server.yaml；热重载以文件为准
- runcmd getroot 使用 su 提权，会话绑定发起用户，仅所在 client 生效，5 分钟过期回退普通用户
- shellop 投票人须可触达（server 内嵌 IRC 会话私信判断），仅发起者可触达时直接通过
