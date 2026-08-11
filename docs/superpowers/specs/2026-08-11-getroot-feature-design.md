# getroot 功能设计 — IRC/XMPP 聊天机器人 runcmd root 提权

- 日期：2026-08-11
- 状态：待用户审阅
- 基础：`docs/superpowers/specs/2026-08-11-irc-xmpp-chatbot-design.md`（已实现）

## 1. 目标

为 `runcmd` 增加 root 提权能力：

- 命令形态：`!<bot_name> runcmd getroot <client_root_password>`（无 client_name）
- 使用 `su` 提权；提权成功后，该用户对该 client 的 `runcmd` 命令以 root 身份执行
- 超时 5 分钟：过期后回退普通用户执行并提示，需重新 `getroot`

## 2. 已确认决策

| 决策点 | 结论 |
|---|---|
| getroot 目标 client | **调用者所在 client**（发起 command 消息的 conn 自身） |
| root 会话绑定 | **绑定发起用户（user@host）**，其他用户不受影响 |
| 过期行为 | **回退普通用户执行 + 私信提示**「root 会话已过期，请重新 getroot」 |
| root 作用域 | **仅所在 client 生效**：跨 client 的 runcmd 仍普通用户 |
| 提权实现 | **`su --pty`**（util-linux 伪终端，`printf '%s\n' <pw> | su --pty root -c <cmd>` 从管道读密码可靠） |
| 调用权限 | **仅 shellop**（getroot 是 runcmd 子形态，继承其权限检查） |
| 会话 TTL | 300 秒（可配置 `root_session_ttl`，默认 300） |

## 3. 数据流

### 3.1 getroot 流程

```
IRC 用户（shellop）:
  !qsdwindows_bot runcmd getroot <pw>
    │
    ▼
IRC client: _maybe_command → _server_command
  command {cmd: "runcmd", args: ["getroot", "<pw>"], caller_userhost: "u@h", ...}
    │
    ▼
server: _cmd_runcmd 检测 args[0]=="getroot"
  - 权限检查已通过（runcmd 需 shellop）
  - 目标 client = 发起 conn（调用者所在 client）
  - 发送 getroot 消息 {type: "getroot", task_id, password, caller_userhost}
    │
    ▼
目标 client: _on_server_msg → _exec_getroot
  - 验证：printf '%s\n' <pw> | su --pty root -c 'id -u'，输出 "0" 即成功
  - 成功 → 缓存 self.root_sessions[userhost] = (password, now + ttl)
  - 回传 runcmd_result {task_id, ok, output}
    │
    ▼
server: _handle_runcmd_result → 私信发起者（成功/失败）
```

### 3.2 root 化 runcmd 流程

```
IRC 用户（shellop）:
  !qsdwindows_bot runcmd irc_main id
    │
    ▼
IRC client → server: command {cmd: "runcmd", args: ["irc_main", "id"], caller_userhost: "u@h"}
    │
    ▼
server: _cmd_runcmd（args[0] != "getroot"）
  - 目标 client = "irc_main"
  - 发送 runcmd {task_id, cmd: "id", caller_userhost: "u@h"}   ← 新增 caller 字段
    │
    ▼
目标 client: _exec_runcmd
  - caller 在 root_sessions 且未过期 → printf pw | su --pty root -c 'id'
    → runcmd_result {task_id, ok, output, as_root: True}
  - caller 有会话但已过期 → 普通用户执行
    → runcmd_result {task_id, ok, output, as_root: False, root_expired: True}
  - caller 无会话 → 普通用户执行 → runcmd_result {task_id, ok, output, as_root: False}
    │
    ▼
server: _handle_runcmd_result
  - root_expired=True → 私信发起者时附加提示「[root 会话已过期，已按普通用户执行，请重新 getroot]」
  - 否则原样回传
```

## 4. 协议变更

| 变更 | 内容 |
|---|---|
| `make_runcmd(task_id, cmd, caller_userhost="")` | 新增 `caller_userhost` 字段（client 判断 root 会话用） |
| 新增 `make_getroot(task_id, password, caller_userhost)` | 返回 `{type: "getroot", task_id, password, caller_userhost}`；`VALID_TYPES` 增加 `"getroot"` |
| `make_runcmd_result(task_id, ok, output, as_root=False, root_expired=False)` | 新增 `as_root`/`root_expired` 布尔字段 |

**兼容性影响**：`make_runcmd` 与 `make_runcmd_result` 签名变更会使现有 `tests/test_protocol.py` 中 `test_make_vote_and_runcmd_shape` 与 `test_make_command_result_shape` 断言失败——实现时须同步更新这两个测试的期望值（新增字段默认值）。

## 5. 组件变更

### 5.1 server.py

- `_cmd_runcmd`：检测 `args[0] == "getroot"`：
  - 参数不足（无密码）→ 私信「用法: runcmd getroot <client_root_password>」
  - 否则向**发起 conn** 发送 `make_getroot(...)`（目标 client = 调用者所在 client）
  - 同样登记 `runcmd_pending`（复用 runcmd 结果路由）
- `_cmd_runcmd` 普通路径：`make_runcmd(task_id, cmd, caller_userhost=caller)`
- `_handle_runcmd_result`：`root_expired` 为 True 时在私信文本前附加过期提示

### 5.2 irc_client.py / xmpp_client.py

- `_on_server_msg` 增加 `"getroot"` 分支 → `_exec_getroot(msg)`
- 新增 `self.root_sessions: dict[str, tuple[str, float]]`（userhost → (password, expiry_monotonic)）
- 新增 `_exec_getroot(msg)`：
  ```python
  proc = await asyncio.create_subprocess_shell(
      f"printf '%s\\n' {shlex.quote(password)} | su --pty root -c 'id -u'",
      stdout=PIPE, stderr=STDOUT)
  output, _ = await proc.communicate()
  ok = proc.returncode == 0 and output.strip() == b"0"
  if ok: self.root_sessions[userhost] = (password, time.monotonic() + ttl)
  await self._server_send(make_runcmd_result(task_id, ok, 成功/失败消息, as_root=ok))
  ```
- 修改 `_exec_runcmd(msg)`：
  - 取 `caller = msg.get("caller_userhost", "")`，查 `root_sessions`
  - 有效会话 → `su --pty` 包装执行；过期 → 普通执行 + `root_expired=True`；无会话 → 普通执行
- TTL 来源：`cfg.get("root_session_ttl", 300)`（新增配置项，两个 client 配置文件示例）

## 6. 配置变更

```yaml
# client_irc.yaml / client_xmpp.yaml 新增（可选，默认 300）
root_session_ttl: 300   # getroot 会话有效期（秒）
```

## 7. 安全边界

- root 密码**仅存 client 内存**（`root_sessions` dict），不落盘、不写日志、不进协议日志
- `runcmd` 消息的 `cmd` 字段不含密码（getroot 密码走独立 `getroot` 消息的 `password` 字段）
- getroot 仅 shellop 可调；会话绑定 user@host，其他用户无法借用
- 密码经 `shlex.quote` 转义后注入命令，防 shell 注入
- 过期会话不删除密码（保留用于过期提示），仅标记过期——**注意**：过期后密码仍在内存，重新 getroot 覆盖；进程重启清空

## 8. 测试策略

- server：`_cmd_runcmd` getroot 分支（发 `getroot` 消息到发起 conn、参数校验）、`_handle_runcmd_result` 过期提示
- client（IRC/XMPP 均测，mock subprocess）：
  - `_exec_getroot` 成功 → 缓存会话 + 回传 as_root=True
  - `_exec_getroot` 密码错误 → 不缓存 + 回传错误
  - `_exec_runcmd` 有效会话 → 命令被 su 包装（断言 subprocess 命令含 `su --pty root -c`）
  - `_exec_runcmd` 过期会话 → 普通执行 + root_expired=True
  - `_exec_runcmd` 无会话 → 普通执行
- protocol：`make_runcmd`/`make_getroot`/`make_runcmd_result` 新字段 shape 断言

## 9. 明确不做（YAGNI）

- 不做 getroot 的 client_name 参数（远程提权需后续扩展）
- 不做 root 会话的 server 端持久化（client 内存即可）
- 不做密码掩码/轮换
- 不做 pexpect 依赖（用 su --pty）
