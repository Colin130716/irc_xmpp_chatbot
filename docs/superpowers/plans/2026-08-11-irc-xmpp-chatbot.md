# IRC + XMPP 双协议聊天机器人实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现一个 server(中枢)+client(IRC/XMPP) 分布式聊天机器人，支持 TLS、三种 LLM API 格式、配置热重载、IRC 端完整权限体系（botop/oper/shellop/黑白名单）与跨 client 远程 shell 执行（runcmd）。

**Architecture:** 一个 asyncio Server 持有权威权限数据并编排 shellop 确认流程与 runcmd 路由；IRC Client（pydle）与 XMPP Client（slixmpp）通过 TLS+JSON lines 协议注册到 Server，本地执行 LLM 调用与频道动作。权限快照由 Server 推送，Client 本地缓存用于即时踢人。

**Tech Stack:** Python 3.14、asyncio、pydle>=1.1（IRC）、slixmpp>=1.12（XMPP）、PyYAML>=6（配置）、httpx>=0.27（LLM）、pytest+pytest-asyncio（测试）

## Global Constraints

- 所有代码位于 `src/ircxmppbot/` 包内；测试位于 `tests/`
- Python >= 3.14（本机 3.14.6）；类型标注必须完整；禁止 `# type: ignore`
- 依赖版本下限：`slixmpp>=1.12`、`pydle>=1.1`、`PyYAML>=6`、`httpx>=0.27`；开发依赖 `pytest>=8`、`pytest-asyncio>=0.24`
- 配置格式 YAML；唤醒词默认 `!<bot_name> <cmd>`，`bot_name` 在 client 配置顶层，IRC nickname 使用 bot_name，realname 在 `irc:` 段单独配置
- 白名单与黑名单**禁止同时启用**（`Permissions.validate_mutex` 强制）
- 权限数据权威在 server；权限命令生效时**原子写回** server.yaml（tmp 文件 + `os.replace`）
- shellop 候选人必须**同时是 botop 且 oper**（oper 状态来自 server 端 oper 缓存，缓存由 client 的 WHOIS 上报填充）
- 协议：TLS TCP + JSON lines，每行一个含 `type` 字段的 JSON 对象
- IRC 消息单行正文长度硬上限 400 字符（IRC 512 字节/行限制），LLM 回复按此分片；XMPP 分片 1500 字符
- 断线重连指数退避：1s→2s→4s…上限 60s（`util.backoff_delay`）
- 项目当前**不是 git 仓库**：Task 1 执行 `git init`；若用户不需要版本控制，可跳过全部 commit 步骤
- 测试不依赖真实 IRC/XMPP 服务器（mock 传输层）

---

### Task 1: 项目脚手架（pyproject / 包结构 / pytest 配置）

**Files:**
- Create: `pyproject.toml`
- Create: `src/ircxmppbot/__init__.py`
- Create: `tests/__init__.py`（空文件）
- Create: `.gitignore`

**Interfaces:**
- Produces: 可安装的 `ircxmppbot` 包；`pytest` 可运行；后续任务直接在 `src/ircxmppbot/` 下添加模块

- [ ] **Step 1: 创建 pyproject.toml**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "ircxmppbot"
version = "0.1.0"
description = "IRC + XMPP dual-protocol chatbot with server/client architecture"
requires-python = ">=3.14"
dependencies = [
    "slixmpp>=1.12",
    "pydle>=1.1",
    "PyYAML>=6",
    "httpx>=0.27",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.24"]

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: 创建包与测试目录**

```bash
mkdir -p src/ircxmppbot tests configs
touch src/ircxmppbot/__init__.py tests/__init__.py
```

`src/ircxmppbot/__init__.py` 内容：

```python
"""IRC + XMPP 双协议聊天机器人。"""

__version__ = "0.1.0"
```

- [ ] **Step 3: 创建 .gitignore**

```
__pycache__/
*.pyc
*.egg-info/
.pytest_cache/
.venv/
configs/*.crt
configs/*.key
```

- [ ] **Step 4: 初始化 git（可选）并提交**

```bash
git init
git add pyproject.toml .gitignore src tests
git commit -m "chore: 项目脚手架（pyproject/包结构/pytest 配置）"
```

- [ ] **Step 5: 验证安装**

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -c "import ircxmppbot; print(ircxmppbot.__version__)"
```

Expected: `0.1.0`

---

### Task 2: util.py（分片 / 掩码解析 / 退避延时）

**Files:**
- Create: `src/ircxmppbot/util.py`
- Test: `tests/test_util.py`

**Interfaces:**
- Produces:
  - `def split_text(text: str, limit: int) -> list[str]` — 按 limit 分片，空文本返回 `[]`
  - `def parse_userhost(hostmask: str) -> str` — 输入 `nick!user@host` 或 `user@host`，返回规范化小写 `user@host`
  - `def match_mask(hostmask: str, mask: str) -> bool` — fnmatch 通配匹配（大小写不敏感）
  - `def backoff_delay(attempt: int, cap: float = 60.0) -> float` — `min(2 ** attempt, cap)`，attempt 从 0 开始（1,2,4…）

- [ ] **Step 1: 写失败测试**

`tests/test_util.py`:

```python
from ircxmppbot.util import backoff_delay, match_mask, parse_userhost, split_text


def test_split_text_short_unchanged():
    assert split_text("hello", 10) == ["hello"]


def test_split_text_empty():
    assert split_text("", 10) == []


def test_split_text_chunks():
    assert split_text("abcdef", 2) == ["ab", "cd", "ef"]


def test_split_text_exact_multiple():
    assert split_text("abcd", 4) == ["abcd"]


def test_parse_userhost_full_mask():
    assert parse_userhost("Nick!user@host.example") == "user@host.example"


def test_parse_userhost_bare():
    assert parse_userhost("user@host.example") == "user@host.example"


def test_parse_userhost_lowercases():
    assert parse_userhost("Nick!User@Host.Example") == "user@host.example"


def test_match_mask_wildcards():
    # mask "user!*@*.example" 匹配 nick 为 user 的 hostmask
    assert match_mask("user!alice@host.example", "user!*@*.example")
    assert not match_mask("nick!user@host.example", "user!*@*.example")


def test_backoff_delay_sequence():
    assert backoff_delay(0) == 1.0
    assert backoff_delay(1) == 2.0
    assert backoff_delay(5) == 32.0
    assert backoff_delay(6) == 60.0  # 2**6=64 超过默认 cap 60，被截断


def test_backoff_delay_cap():
    assert backoff_delay(10, cap=60.0) == 60.0
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_util.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'ircxmppbot.util'`）

- [ ] **Step 3: 实现 util.py**

```python
"""通用工具：文本分片、IRC 掩码解析、退避延时。"""

from __future__ import annotations

import fnmatch


def split_text(text: str, limit: int) -> list[str]:
    """将文本按 limit 分片；空文本返回空列表。"""
    if not text or limit <= 0:
        return []
    if len(text) <= limit:
        return [text]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def parse_userhost(hostmask: str) -> str:
    """从 nick!user@host 或 user@host 提取规范化的 user@host（小写）。"""
    hostmask = hostmask.strip()
    if "!" in hostmask:
        hostmask = hostmask.split("!", 1)[1]
    return hostmask.lower()


def match_mask(hostmask: str, mask: str) -> bool:
    """fnmatch 通配匹配（大小写不敏感）。"""
    return fnmatch.fnmatchcase(hostmask.lower(), mask.lower())


def backoff_delay(attempt: int, cap: float = 60.0) -> float:
    """指数退避：2**attempt，封顶 cap 秒。attempt 从 0 开始。"""
    return min(2**attempt, cap)
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_util.py -v`
Expected: PASS（10 passed）

- [ ] **Step 5: 提交（可选）**

```bash
git add src/ircxmppbot/util.py tests/test_util.py
git commit -m "feat: util 工具（分片/掩码/退避）"
```

---

### Task 3: config.py（YAML 加载 + 热重载监视器）

**Files:**
- Create: `src/ircxmppbot/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: 无（仅标准库 + PyYAML）
- Produces:
  - `class ConfigError(Exception)`
  - `def load_yaml(path: Path) -> dict` — 读取并解析 YAML；文件不存在/解析失败/根节点非 dict 抛 `ConfigError`
  - `class ConfigWatcher` — `__init__(self, path: Path, on_change: Callable[[dict], Awaitable[None]], poll_interval: float = 2.0)`；`async def run(self)` 轮询 mtime，变更时重载并 `await on_change(cfg)`；`def stop(self)`

- [ ] **Step 1: 写失败测试**

`tests/test_config.py`:

```python
import asyncio
from pathlib import Path

import pytest

from ircxmppbot.config import ConfigError, ConfigWatcher, load_yaml


def test_load_yaml_basic(tmp_path: Path):
    p = tmp_path / "cfg.yaml"
    p.write_text("a: 1\nb:\n  c: 2\n", encoding="utf-8")
    assert load_yaml(p) == {"a": 1, "b": {"c": 2}}


def test_load_yaml_missing(tmp_path: Path):
    with pytest.raises(ConfigError):
        load_yaml(tmp_path / "nope.yaml")


def test_load_yaml_invalid(tmp_path: Path):
    p = tmp_path / "bad.yaml"
    p.write_text(": : :\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_yaml(p)


def test_load_yaml_not_mapping(tmp_path: Path):
    p = tmp_path / "list.yaml"
    p.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_yaml(p)


async def test_config_watcher_reloads_on_change(tmp_path: Path):
    p = tmp_path / "watch.yaml"
    p.write_text("value: 1\n", encoding="utf-8")
    seen = []
    watcher = ConfigWatcher(p, lambda cfg: _record(seen, cfg), poll_interval=0.05)

    task = asyncio.create_task(watcher.run())
    await asyncio.sleep(0.12)  # 首轮加载
    p.write_text("value: 2\n", encoding="utf-8")
    await asyncio.sleep(0.12)  # 等待变更检测
    watcher.stop()
    await task

    assert seen, "应该至少回调一次"
    assert seen[-1]["value"] == 2


async def _record(seen, cfg):
    seen.append(cfg)
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_config.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 config.py**

```python
"""YAML 配置加载与热重载。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable

import yaml

log = logging.getLogger(__name__)


class ConfigError(Exception):
    """配置加载/校验错误。"""


def load_yaml(path: Path) -> dict:
    """读取并解析 YAML 配置。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError as e:
        raise ConfigError(f"配置文件不存在: {path}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML 解析失败 {path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"配置根节点必须是映射: {path}")
    return data


class ConfigWatcher:
    """轮询配置文件 mtime，变更时重载并触发回调。"""

    def __init__(
        self,
        path: Path,
        on_change: Callable[[dict], Awaitable[None]],
        poll_interval: float = 2.0,
    ) -> None:
        self.path = Path(path)
        self.on_change = on_change
        self.poll_interval = poll_interval
        self._mtime: float | None = None
        self._stop = asyncio.Event()

    async def run(self) -> None:
        log.info("配置监视启动: %s (间隔 %.1fs)", self.path, self.poll_interval)
        while not self._stop.is_set():
            try:
                mtime = self.path.stat().st_mtime
            except FileNotFoundError:
                mtime = None
            if self._mtime is not None and mtime != self._mtime:
                try:
                    cfg = load_yaml(self.path)
                    log.info("配置文件变更，热重载: %s", self.path)
                    await self.on_change(cfg)
                except ConfigError as e:
                    log.error("热重载失败: %s", e)
            self._mtime = mtime
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_config.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交（可选）**

```bash
git add src/ircxmppbot/config.py tests/test_config.py
git commit -m "feat: config 加载与热重载监视"
```

---

### Task 4: protocol.py（JSON lines 编解码 + 消息构造器）

**Files:**
- Create: `src/ircxmppbot/protocol.py`
- Test: `tests/test_protocol.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `class ProtocolError(Exception)`
  - `VALID_TYPES: frozenset[str]` = 10 种消息类型的集合（见下方代码）
  - `def encode_msg(msg: dict) -> bytes` — 校验 type 后序列化为 `json.dumps(ensure_ascii=False, separators) + "\n"` 的 UTF-8 bytes
  - `def decode_msg(line: str) -> dict` — 解析单行 JSON 并校验 type，失败抛 `ProtocolError`
  - `def make_auth(token, client_name, client_type, bot_name) -> dict`
  - `def make_command(cmd, args, caller_userhost, caller_is_oper, channel=None) -> dict`
  - `def make_command_result(ok, reply, target_type, target, action="none", action_args=None) -> dict`
  - `def make_permission_update(snapshot: dict, oper_cache: dict) -> dict`
  - `def make_shellop_proposal(proposal_id, candidate, deadline_ts, voters) -> dict`
  - `def make_vote(proposal_id, vote, voter_userhost) -> dict`
  - `def make_runcmd(task_id, cmd) -> dict`
  - `def make_runcmd_result(task_id, ok, output) -> dict`

- [ ] **Step 1: 写失败测试**

`tests/test_protocol.py`:

```python
import pytest

from ircxmppbot.protocol import (
    ProtocolError,
    decode_msg,
    encode_msg,
    make_auth,
    make_command,
    make_command_result,
    make_permission_update,
    make_runcmd,
    make_runcmd_result,
    make_shellop_proposal,
    make_vote,
)


def test_encode_decode_roundtrip_unicode():
    msg = {"type": "auth", "token": "密钥🔑", "client_name": "irc_main"}
    line = encode_msg(msg).decode("utf-8")
    assert line.endswith("\n")
    assert decode_msg(line) == msg


def test_encode_missing_type():
    with pytest.raises(ProtocolError):
        encode_msg({"token": "x"})


def test_encode_unknown_type():
    with pytest.raises(ProtocolError):
        encode_msg({"type": "nope"})


def test_decode_empty_line():
    with pytest.raises(ProtocolError):
        decode_msg("   \n")


def test_decode_bad_json():
    with pytest.raises(ProtocolError):
        decode_msg("{not json")


def test_decode_unknown_type():
    with pytest.raises(ProtocolError):
        decode_msg('{"type": "wat"}')


def test_make_auth_shape():
    assert make_auth("t", "n", "irc", "bot") == {
        "type": "auth", "token": "t", "client_name": "n",
        "client_type": "irc", "bot_name": "bot",
    }


def test_make_command_shape():
    assert make_command("ban", ["nick"], "u@h", True, "#chan") == {
        "type": "command", "cmd": "ban", "args": ["nick"],
        "caller_userhost": "u@h", "caller_is_oper": True, "channel": "#chan",
    }


def test_make_command_result_shape():
    assert make_command_result(True, "ok", "channel", "#chan", "ban", ["nick"]) == {
        "type": "command_result", "ok": True, "reply": "ok",
        "target_type": "channel", "target": "#chan",
        "action": "ban", "action_args": ["nick"],
    }


def test_make_shellop_proposal_shape():
    assert make_shellop_proposal("ab12", "u@h", 123.0, ["a@x", "b@y"]) == {
        "type": "shellop_proposal", "proposal_id": "ab12", "candidate": "u@h",
        "deadline_ts": 123.0, "voters": ["a@x", "b@y"],
    }


def test_make_permission_update_shape():
    assert make_permission_update({"botop": []}, {"u@h": True}) == {
        "type": "permission_update", "botop": [], "oper_cache": {"u@h": True},
    }


def test_make_vote_and_runcmd_shape():
    assert make_vote("ab12", "confirm", "u@h") == {
        "type": "vote", "proposal_id": "ab12", "vote": "confirm", "voter_userhost": "u@h",
    }
    assert make_runcmd("t1", "ls -la") == {"type": "runcmd", "task_id": "t1", "cmd": "ls -la"}
    assert make_runcmd_result("t1", True, "out") == {
        "type": "runcmd_result", "task_id": "t1", "ok": True, "output": "out",
    }
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_protocol.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 protocol.py**

```python
"""server↔client 协议：JSON lines 编解码与消息构造器。"""

from __future__ import annotations

import json

VALID_TYPES = frozenset(
    {
        "auth",
        "auth_ok",
        "command",
        "command_result",
        "permission_update",
        "shellop_proposal",
        "vote",
        "runcmd",
        "runcmd_result",
        "status",
    }
)


class ProtocolError(Exception):
    """协议层错误。"""


def _validate(msg: dict) -> None:
    if not isinstance(msg, dict) or "type" not in msg:
        raise ProtocolError(f"消息必须为含 type 的 dict: {msg!r}")
    if msg["type"] not in VALID_TYPES:
        raise ProtocolError(f"未知消息类型: {msg['type']!r}")


def encode_msg(msg: dict) -> bytes:
    """将消息编码为 JSON line（UTF-8，以 \\n 结尾）。"""
    _validate(msg)
    return (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode_msg(line: str) -> dict:
    """解析单行 JSON 消息。"""
    line = line.strip()
    if not line:
        raise ProtocolError("空行")
    try:
        msg = json.loads(line)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"JSON 解析失败: {e}") from e
    _validate(msg)
    return msg


def make_auth(token: str, client_name: str, client_type: str, bot_name: str) -> dict:
    return {
        "type": "auth",
        "token": token,
        "client_name": client_name,
        "client_type": client_type,
        "bot_name": bot_name,
    }


def make_command(
    cmd: str,
    args: list[str],
    caller_userhost: str,
    caller_is_oper: bool,
    channel: str | None = None,
) -> dict:
    return {
        "type": "command",
        "cmd": cmd,
        "args": args,
        "caller_userhost": caller_userhost,
        "caller_is_oper": caller_is_oper,
        "channel": channel,
    }


def make_command_result(
    ok: bool,
    reply: str,
    target_type: str,
    target: str,
    action: str = "none",
    action_args: list[str] | None = None,
) -> dict:
    return {
        "type": "command_result",
        "ok": ok,
        "reply": reply,
        "target_type": target_type,
        "target": target,
        "action": action,
        "action_args": action_args or [],
    }


def make_permission_update(snapshot: dict, oper_cache: dict) -> dict:
    return {"type": "permission_update", **snapshot, "oper_cache": oper_cache}


def make_shellop_proposal(
    proposal_id: str, candidate: str, deadline_ts: float, voters: list[str]
) -> dict:
    return {
        "type": "shellop_proposal",
        "proposal_id": proposal_id,
        "candidate": candidate,
        "deadline_ts": deadline_ts,
        "voters": voters,
    }


def make_vote(proposal_id: str, vote: str, voter_userhost: str) -> dict:
    return {
        "type": "vote",
        "proposal_id": proposal_id,
        "vote": vote,
        "voter_userhost": voter_userhost,
    }


def make_runcmd(task_id: str, cmd: str) -> dict:
    return {"type": "runcmd", "task_id": task_id, "cmd": cmd}


def make_runcmd_result(task_id: str, ok: bool, output: str) -> dict:
    return {"type": "runcmd_result", "task_id": task_id, "ok": ok, "output": output}
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_protocol.py -v`
Expected: PASS（12 passed）

- [ ] **Step 5: 提交（可选）**

```bash
git add src/ircxmppbot/protocol.py tests/test_protocol.py
git commit -m "feat: 协议层 JSON lines 编解码"
```

---

### Task 5: permissions.py（权限模型：botop/shellop/黑白名单）

**Files:**
- Create: `src/ircxmppbot/permissions.py`
- Test: `tests/test_permissions.py`

**Interfaces:**
- Consumes: `ircxmppbot.util.parse_userhost`、`ircxmppbot.util.match_mask`、`ircxmppbot.config.ConfigError`
- Produces:
  - `LEVELS: dict[str, int]` = `{"everyone": 0, "botop": 1, "oper": 2, "shellop": 3}`
  - `class Permissions`:
    - 属性：`botop: set[str]`、`shellop: set[str]`、`whitelist_enabled: bool`、`whitelist: list[dict]`（`{mask, channel?}`）、`blacklist_enabled: bool`、`blacklist: list[dict]`
    - `classmethod from_config(cfg: dict) -> Permissions` — 读 `cfg["permissions"]`；启用冲突抛 `ConfigError`
    - `def validate_mutex(self) -> None`
    - `def is_botop(self, userhost) -> bool` / `def is_shellop(self, userhost) -> bool`
    - `def level_of(self, userhost, is_oper=False) -> str`
    - `def has_level(self, userhost, required, is_oper=False) -> bool`
    - `def whitelist_allows(self, hostmask, channel) -> bool` — 禁用时恒 True
    - `def blacklist_blocks(self, hostmask, channel) -> bool` — 禁用时恒 False
    - `def to_config_dict(self) -> dict` — 可写回 YAML（与 from_config 输入对称）
    - `def snapshot(self) -> dict` — 同 to_config_dict
    - `def apply_snapshot(self, snap: dict) -> None` — 从 wire 快照恢复

- [ ] **Step 1: 写失败测试**

`tests/test_permissions.py`:

```python
import pytest

from ircxmppbot.config import ConfigError
from ircxmppbot.permissions import LEVELS, Permissions


def _cfg(**overrides):
    base = {
        "permissions": {
            "botop": ["op1@host.example", "op2@host.example"],
            "shellop": ["op1@host.example"],
            "whitelist": {"enabled": False, "entries": []},
            "blacklist": {"enabled": False, "entries": []},
        }
    }
    base["permissions"].update(overrides)
    return base


def test_from_config_basic():
    p = Permissions.from_config(_cfg())
    assert p.is_botop("op1@host.example")
    assert not p.is_botop("nobody@host.example")
    assert p.is_shellop("OP1@HOST.EXAMPLE")  # 大小写不敏感


def test_mutex_both_enabled_raises():
    with pytest.raises(ConfigError):
        Permissions.from_config(
            _cfg(whitelist={"enabled": True, "entries": []},
                 blacklist={"enabled": True, "entries": []})
        )


def test_level_ordering():
    p = Permissions.from_config(_cfg())
    assert LEVELS["everyone"] < LEVELS["botop"] < LEVELS["oper"] < LEVELS["shellop"]
    assert p.level_of("op1@host.example") == "shellop"
    assert p.level_of("op2@host.example") == "botop"
    assert p.level_of("unknown@host.example") == "everyone"
    assert p.level_of("op2@host.example", is_oper=True) == "oper"


def test_has_level():
    p = Permissions.from_config(_cfg())
    assert p.has_level("op1@host.example", "botop")
    assert p.has_level("op1@host.example", "shellop")
    assert not p.has_level("op2@host.example", "shellop")
    assert p.has_level("op2@host.example", "oper", is_oper=True)
    assert not p.has_level("everyone@host.example", "botop")


def test_whitelist_disabled_allows_all():
    p = Permissions.from_config(_cfg())
    assert p.whitelist_allows("any!u@h", "#chan")


def test_whitelist_channel_scoped():
    p = Permissions.from_config(
        _cfg(whitelist={"enabled": True, "entries": [
            {"mask": "good!*@*", "channel": "#chan1"},
        ]})
    )
    assert p.whitelist_allows("good!u@h", "#chan1")
    assert not p.whitelist_allows("good!u@h", "#chan2")  # 频道不符
    assert not p.whitelist_allows("bad!u@h", "#chan1")   # 掩码不符


def test_whitelist_global_entry():
    p = Permissions.from_config(
        _cfg(whitelist={"enabled": True, "entries": [{"mask": "good!*@*"}]})
    )
    assert p.whitelist_allows("good!u@h", "#any")


def test_blacklist_blocks():
    p = Permissions.from_config(
        _cfg(blacklist={"enabled": True, "entries": [{"mask": "bad!*@*"}]})
    )
    assert p.blacklist_blocks("bad!u@h", "#chan")
    assert not p.blacklist_blocks("good!u@h", "#chan")
    p2 = Permissions.from_config(_cfg())
    assert not p2.blacklist_blocks("bad!u@h", "#chan")  # 禁用时恒 False


def test_config_dict_roundtrip():
    p = Permissions.from_config(_cfg())
    p.botop.add("new@host.example")
    p2 = Permissions.from_config({"permissions": p.to_config_dict()})
    assert p2.is_botop("new@host.example")


def test_apply_snapshot():
    p = Permissions()
    p.apply_snapshot({
        "botop": ["a@h"], "shellop": [],
        "whitelist": {"enabled": True, "entries": [{"mask": "m!*@*"}]},
        "blacklist": {"enabled": False, "entries": []},
    })
    assert p.is_botop("a@h")
    assert p.whitelist_enabled
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_permissions.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 permissions.py**

```python
"""权限模型：botop / shellop / 黑白名单（仅 IRC 端使用）。"""

from __future__ import annotations

from .config import ConfigError
from .util import match_mask, parse_userhost

LEVELS = {"everyone": 0, "botop": 1, "oper": 2, "shellop": 3}


class Permissions:
    """权威权限数据。Server 持有，Client 通过快照镜像。"""

    def __init__(self) -> None:
        self.botop: set[str] = set()
        self.shellop: set[str] = set()
        self.whitelist_enabled = False
        self.whitelist: list[dict] = []
        self.blacklist_enabled = False
        self.blacklist: list[dict] = []

    @classmethod
    def from_config(cls, cfg: dict) -> "Permissions":
        p = cls()
        perms = cfg.get("permissions", {}) or {}
        p.botop = {parse_userhost(u) for u in (perms.get("botop", []) or [])}
        p.shellop = {parse_userhost(u) for u in (perms.get("shellop", []) or [])}
        wl = perms.get("whitelist", {}) or {}
        bl = perms.get("blacklist", {}) or {}
        p.whitelist_enabled = bool(wl.get("enabled", False))
        p.blacklist_enabled = bool(bl.get("enabled", False))
        p.whitelist = list(wl.get("entries", []) or [])
        p.blacklist = list(bl.get("entries", []) or [])
        p.validate_mutex()
        return p

    def validate_mutex(self) -> None:
        """白名单与黑名单不能同时启用。"""
        if self.whitelist_enabled and self.blacklist_enabled:
            raise ConfigError("whitelist 与 blacklist 不能同时启用")

    def is_botop(self, userhost: str) -> bool:
        return parse_userhost(userhost) in self.botop

    def is_shellop(self, userhost: str) -> bool:
        return parse_userhost(userhost) in self.shellop

    def level_of(self, userhost: str, is_oper: bool = False) -> str:
        uh = parse_userhost(userhost)
        if uh in self.shellop:
            return "shellop"
        if is_oper:
            return "oper"
        if uh in self.botop:
            return "botop"
        return "everyone"

    def has_level(self, userhost: str, required: str, is_oper: bool = False) -> bool:
        return LEVELS[self.level_of(userhost, is_oper)] >= LEVELS[required]

    def whitelist_allows(self, hostmask: str, channel: str | None) -> bool:
        if not self.whitelist_enabled:
            return True
        return self._entry_matches(hostmask, channel, self.whitelist)

    def blacklist_blocks(self, hostmask: str, channel: str | None) -> bool:
        if not self.blacklist_enabled:
            return False
        return self._entry_matches(hostmask, channel, self.blacklist)

    def _entry_matches(self, hostmask: str, channel: str | None, entries: list[dict]) -> bool:
        for e in entries:
            if "mask" not in e:
                continue
            if e.get("channel") and e["channel"] != channel:
                continue
            if match_mask(hostmask, e["mask"]):
                return True
        return False

    def to_config_dict(self) -> dict:
        """生成与 from_config 输入对称的结构，用于写回 YAML。"""
        return {
            "botop": sorted(self.botop),
            "shellop": sorted(self.shellop),
            "whitelist": {"enabled": self.whitelist_enabled, "entries": self.whitelist},
            "blacklist": {"enabled": self.blacklist_enabled, "entries": self.blacklist},
        }

    def snapshot(self) -> dict:
        return self.to_config_dict()

    def apply_snapshot(self, snap: dict) -> None:
        self.botop = {parse_userhost(u) for u in (snap.get("botop", []) or [])}
        self.shellop = {parse_userhost(u) for u in (snap.get("shellop", []) or [])}
        wl = snap.get("whitelist", {}) or {}
        bl = snap.get("blacklist", {}) or {}
        self.whitelist_enabled = bool(wl.get("enabled", False))
        self.whitelist = list(wl.get("entries", []) or [])
        self.blacklist_enabled = bool(bl.get("enabled", False))
        self.blacklist = list(bl.get("entries", []) or [])
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_permissions.py -v`
Expected: PASS（10 passed）

- [ ] **Step 5: 提交（可选）**

```bash
git add src/ircxmppbot/permissions.py tests/test_permissions.py
git commit -m "feat: 权限模型（botop/shellop/黑白名单）"
```

---

### Task 6: commands.py（IRC 命令解析 + 权限注册表）

**Files:**
- Create: `src/ircxmppbot/commands.py`
- Test: `tests/test_commands.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `COMMAND_LEVELS: dict[str, str]` — 命令 → 所需最低级别（chat/help=everyone，ban/unban/whitelist/blacklist/info/confirm/reject=botop，botop/shellop=oper，runcmd=shellop）
  - `@dataclass(frozen=True) class ParsedCommand` — 字段 `cmd: str`、`args: list[str]`
  - `def parse_command(text: str, bot_name: str) -> ParsedCommand | None` — 解析 `!<bot_name> <cmd> <args...>`；不匹配返回 None；命令名转小写

- [ ] **Step 1: 写失败测试**

`tests/test_commands.py`:

```python
from ircxmppbot.commands import COMMAND_LEVELS, parse_command


def test_parse_basic():
    p = parse_command("!qsdwindows_bot ban nick", "qsdwindows_bot")
    assert p is not None
    assert p.cmd == "ban"
    assert p.args == ["nick"]


def test_parse_case_insensitive_cmd():
    p = parse_command("!qsdwindows_bot BAN nick", "qsdwindows_bot")
    assert p is not None
    assert p.cmd == "ban"


def test_parse_no_args():
    p = parse_command("!qsdwindows_bot help", "qsdwindows_bot")
    assert p is not None
    assert p.cmd == "help"
    assert p.args == []


def test_parse_unknown_bot_name():
    assert parse_command("!other_bot help", "qsdwindows_bot") is None


def test_parse_prefix_only():
    assert parse_command("!qsdwindows_bot", "qsdwindows_bot") is None


def test_parse_not_command():
    assert parse_command("hello world", "qsdwindows_bot") is None


def test_parse_extra_whitespace():
    p = parse_command("  !qsdwindows_bot  chat  hello  world  ", "qsdwindows_bot")
    assert p is not None
    assert p.cmd == "chat"
    assert p.args == ["hello", "world"]


def test_command_levels_cover_all():
    assert COMMAND_LEVELS["runcmd"] == "shellop"
    assert COMMAND_LEVELS["botop"] == "oper"
    assert COMMAND_LEVELS["shellop"] == "oper"
    assert COMMAND_LEVELS["ban"] == "botop"
    assert COMMAND_LEVELS["chat"] == "everyone"
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_commands.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 commands.py**

```python
"""IRC 端命令解析与权限注册表。"""

from __future__ import annotations

from dataclasses import dataclass

# 命令 → 所需最低权限级别（chat/help 之外全部经 server 权限判定）
COMMAND_LEVELS: dict[str, str] = {
    "chat": "everyone",
    "help": "everyone",
    "ban": "botop",
    "unban": "botop",
    "whitelist": "botop",
    "blacklist": "botop",
    "info": "botop",
    "botop": "oper",
    "shellop": "oper",
    "confirm": "botop",
    "reject": "botop",
    "runcmd": "shellop",
}


@dataclass(frozen=True)
class ParsedCommand:
    cmd: str
    args: list[str]


def parse_command(text: str, bot_name: str) -> ParsedCommand | None:
    """解析 !<bot_name> <cmd> <args...>；不匹配返回 None。"""
    prefix = f"!{bot_name}"
    stripped = text.strip()
    if not stripped.startswith(prefix):
        return None
    rest = stripped[len(prefix):].strip()
    if not rest:
        return None
    parts = rest.split()
    return ParsedCommand(cmd=parts[0].lower(), args=parts[1:])
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_commands.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: 提交（可选）**

```bash
git add src/ircxmppbot/commands.py tests/test_commands.py
git commit -m "feat: 命令解析与权限注册表"
```

---

### Task 7: llm.py（LLM 客户端：三种格式）

**Files:**
- Create: `src/ircxmppbot/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `httpx`
- Produces:
  - `class LLMError(Exception)`
  - `class LLMClient`:
    - `__init__(self, cfg: dict)` — 字段：`format`（openai_chat/openai_responses/anthropic）、`base_url`、`api_key`、`model`、`system_prompt`、`temperature`、`max_tokens`、`timeout`；非法 format 或缺 base_url/api_key/model 抛 `LLMError`
    - `def build_request(self, user_text: str) -> tuple[str, dict, dict]` — 返回 `(url, headers, body)`，纯函数便于测试
    - `@staticmethod def extract_reply(fmt: str, data: dict) -> str` — 从响应 JSON 提取文本
    - `async def chat(self, user_text: str) -> str` — 调用 API 并返回回复文本；失败抛 `LLMError`

- [ ] **Step 1: 写失败测试**

`tests/test_llm.py`:

```python
import pytest

from ircxmppbot.llm import LLMClient, LLMError

CFG = {
    "format": "openai_chat",
    "base_url": "https://api.openai.com/v1",
    "api_key": "sk-test",
    "model": "gpt-4o-mini",
    "system_prompt": "You are a bot.",
    "temperature": 0.5,
    "max_tokens": 100,
    "timeout": 30,
}


def test_build_openai_chat():
    client = LLMClient(CFG)
    url, headers, body = client.build_request("hello")
    assert url == "https://api.openai.com/v1/chat/completions"
    assert headers == {"Authorization": "Bearer sk-test"}
    assert body["model"] == "gpt-4o-mini"
    assert body["messages"][0] == {"role": "system", "content": "You are a bot."}
    assert body["messages"][1] == {"role": "user", "content": "hello"}
    assert body["temperature"] == 0.5 and body["max_tokens"] == 100


def test_build_openai_responses():
    client = LLMClient({**CFG, "format": "openai_responses"})
    url, headers, body = client.build_request("hello")
    assert url == "https://api.openai.com/v1/responses"
    assert body["input"] == "hello"
    assert "messages" not in body


def test_build_anthropic():
    client = LLMClient({**CFG, "format": "anthropic"})
    url, headers, body = client.build_request("hello")
    assert url == "https://api.openai.com/v1/messages"
    assert headers["x-api-key"] == "sk-test"
    assert headers["anthropic-version"] == "2023-06-01"
    assert body["system"] == "You are a bot."
    assert body["messages"] == [{"role": "user", "content": "hello"}]


def test_invalid_format():
    with pytest.raises(LLMError):
        LLMClient({**CFG, "format": "nope"})


def test_missing_api_key():
    with pytest.raises(LLMError):
        LLMClient({**CFG, "api_key": ""})


def test_extract_openai_chat():
    data = {"choices": [{"message": {"content": "hi"}}]}
    assert LLMClient.extract_reply("openai_chat", data) == "hi"


def test_extract_openai_responses():
    data = {"output": [
        {"type": "message", "content": [{"type": "output_text", "text": "a"}]},
        {"type": "message", "content": [{"type": "output_text", "text": "b"}]},
    ]}
    assert LLMClient.extract_reply("openai_responses", data) == "a\nb"


def test_extract_anthropic():
    data = {"content": [{"type": "text", "text": "ok"}]}
    assert LLMClient.extract_reply("anthropic", data) == "ok"


async def test_chat_http_error(monkeypatch):
    client = LLMClient(CFG)

    class FakeResp:
        status_code = 500
        text = "boom"

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return FakeResp()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)
    with pytest.raises(LLMError):
        await client.chat("hello")


async def test_chat_ok(monkeypatch):
    client = LLMClient(CFG)

    class FakeResp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "hi"}}]}

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return FakeResp()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)
    assert await client.chat("hello") == "hi"
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_llm.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 llm.py**

```python
"""LLM 客户端：OpenAI Chat Completions / OpenAI Responses / Anthropic Messages。"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"


class LLMError(Exception):
    """LLM 调用失败。"""


class LLMClient:
    """统一封装三种 LLM API 格式。"""

    def __init__(self, cfg: dict) -> None:
        self.format = cfg.get("format", "openai_chat")
        self.base_url = cfg.get("base_url", "").rstrip("/")
        self.api_key = cfg.get("api_key", "")
        self.model = cfg.get("model", "")
        self.system_prompt = cfg.get("system_prompt", "")
        self.temperature = cfg.get("temperature", 0.7)
        self.max_tokens = cfg.get("max_tokens", 1000)
        self.timeout = cfg.get("timeout", 60)
        if self.format not in ("openai_chat", "openai_responses", "anthropic"):
            raise LLMError(f"未知 LLM format: {self.format}")
        if not self.api_key or not self.model or not self.base_url:
            raise LLMError("LLM 配置必须包含 base_url/api_key/model")

    def build_request(self, user_text: str) -> tuple[str, dict, dict]:
        """构造 (url, headers, body)。"""
        if self.format == "openai_chat":
            url = f"{self.base_url}/chat/completions"
            headers = {"Authorization": f"Bearer {self.api_key}"}
            messages = [{"role": "user", "content": user_text}]
            if self.system_prompt:
                messages.insert(0, {"role": "system", "content": self.system_prompt})
            body = {
                "model": self.model,
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }
        elif self.format == "openai_responses":
            url = f"{self.base_url}/responses"
            headers = {"Authorization": f"Bearer {self.api_key}"}
            body = {
                "model": self.model,
                "input": user_text,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }
        else:  # anthropic
            url = f"{self.base_url}/messages"
            headers = {
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            }
            messages = [{"role": "user", "content": user_text}]
            body = {
                "model": self.model,
                "messages": messages,
                "system": self.system_prompt,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
            }
        return url, headers, body

    @staticmethod
    def extract_reply(fmt: str, data: dict) -> str:
        """从响应 JSON 提取回复文本。"""
        if fmt == "openai_chat":
            return str(data["choices"][0]["message"]["content"])
        if fmt == "openai_responses":
            parts = []
            for item in data.get("output", []):
                if item.get("type") == "message":
                    for c in item.get("content", []):
                        if c.get("type") == "output_text":
                            parts.append(str(c.get("text", "")))
            return "\n".join(parts)
        # anthropic
        parts = []
        for c in data.get("content", []):
            if c.get("type") == "text":
                parts.append(str(c.get("text", "")))
        return "\n".join(parts)

    async def chat(self, user_text: str) -> str:
        url, headers, body = self.build_request(user_text)
        log.debug("LLM 请求 %s %s", self.format, url)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as e:
            raise LLMError(f"LLM 请求失败: {e}") from e
        if resp.status_code != 200:
            raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
        except ValueError as e:
            raise LLMError(f"LLM 响应非 JSON: {e}") from e
        try:
            return self.extract_reply(self.format, data)
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"LLM 响应格式异常: {e}") from e
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_llm.py -v`
Expected: PASS（10 passed）

- [ ] **Step 5: 提交（可选）**

```bash
git add src/ircxmppbot/llm.py tests/test_llm.py
git commit -m "feat: LLM 客户端（三种 API 格式）"
```

---

### Task 8: server.py（中枢：监听/认证/路由/shellop 编排/runcmd/持久化）

**Files:**
- Create: `src/ircxmppbot/server.py`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `ircxmppbot.protocol.*`、`ircxmppbot.permissions.Permissions`、`ircxmppbot.commands.COMMAND_LEVELS`、`ircxmppbot.config.{load_yaml, ConfigWatcher, ConfigError}`、`ircxmppbot.util.parse_userhost`
- Produces:
  - `class ClientConnection` — `__init__(reader, writer, client_name, client_type, bot_name)`；`async def send(msg: dict)`；`def close()`
  - `class BotServer`:
    - `__init__(self, config_path: Path)`
    - `def reload(self, cfg: dict)` — 校验 server/permissions 段，重建 `self.permissions`，广播快照；`ConfigError` 可抛
    - `def oper_cache_ttl(self) -> float` / `def confirm_timeout(self) -> float`
    - `async def run(self)` — TLS 监听 + ConfigWatcher
    - `async def _handle_client(self, reader, writer)` — 10s 内首消息必须为 auth（token 匹配），注册 conn 到 `self.conns[client_name]`，发 `auth_ok` + `permission_update`，循环读消息分发
    - `async def _dispatch(self, conn, msg)` — 按 type 路由：command/vote/runcmd_result/status
    - `async def _handle_command(self, conn, msg)` — 更新 oper 缓存 → 权限判定（`COMMAND_LEVELS`）→ 调用 `_cmd_<name>`
    - `_cmd_info / _cmd_botop / _cmd_shellop / _cmd_whitelist / _cmd_blacklist / _cmd_ban / _cmd_unban / _cmd_runcmd`
    - `async def _handle_vote(self, conn, msg)` / `async def _finalize_proposal(self, pid, success, reason)`
    - `async def _persist_permissions(self)` — 原子写回 YAML + 广播
    - `def _oper_cache_snapshot(self) -> dict` / `async def _broadcast_permissions(self)`
    - `def _collect_voters(self) -> set[str]`

- [ ] **Step 1: 写失败测试**

`tests/test_server.py`:

```python
import asyncio
import time
from pathlib import Path

import pytest

from ircxmppbot.config import load_yaml
from ircxmppbot.protocol import (
    make_command,
    make_runcmd_result,
    make_vote,
)
from ircxmppbot.server import BotServer


class FakeConn:
    """记录 send 消息的假连接。"""

    def __init__(self, name="irc_main"):
        self.client_name = name
        self.client_type = "irc"
        self.bot_name = "qsdwindows_bot"
        self.sent: list[dict] = []

    async def send(self, msg: dict) -> None:
        self.sent.append(msg)


def _write_cfg(tmp_path: Path, **perms) -> Path:
    cfg = {
        "server": {
            "listen_host": "127.0.0.1",
            "listen_port": 8443,
            "token": "test-token",
            "oper_cache_ttl": 60,
            "shellop_confirm_timeout": 30,
        },
        "permissions": {
            "botop": ["op1@host.example", "op2@host.example"],
            "shellop": [],
            "whitelist": {"enabled": False, "entries": []},
            "blacklist": {"enabled": False, "entries": []},
            **perms,
        },
    }
    p = tmp_path / "server.yaml"
    p.write_text(_yaml(cfg), encoding="utf-8")
    return p


def _yaml(cfg: dict) -> str:
    import yaml
    return yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)


@pytest.fixture
async def srv(tmp_path: Path) -> BotServer:
    server = BotServer(_write_cfg(tmp_path))
    server.reload(load_yaml(server.config_path))
    return server


async def _cmd(srv: BotServer, conn: FakeConn, cmd: str, args: list[str],
               caller: str, is_oper: bool = False, channel: str = "#chan") -> None:
    await srv._handle_command(conn, make_command(cmd, args, caller, is_oper, channel))


def _last(conn: FakeConn) -> dict:
    return conn.sent[-1]


# ---------- 权限判定 ----------

async def test_command_permission_denied(srv, tmp_path):
    conn = FakeConn()
    await _cmd(srv, conn, "botop", ["give", "x@y"], "nobody@host.example")
    assert not _last(conn)["ok"]
    assert "权限不足" in _last(conn)["reply"]


async def test_botop_give_persists(srv, tmp_path):
    conn = FakeConn()
    srv.conns = {"irc_main": conn}  # 注册以便快照广播到达
    await _cmd(srv, conn, "botop", ["give", "op3@host.example"], "op1@host.example", is_oper=True)
    assert _last(conn)["ok"]
    # 已写回配置文件
    cfg = load_yaml(srv.config_path)
    assert "op3@host.example" in cfg["permissions"]["botop"]
    # 快照已推送
    assert any(m["type"] == "permission_update" for m in conn.sent)


async def test_botop_give_rejects_non_oper(srv):
    conn = FakeConn()
    await _cmd(srv, conn, "botop", ["give", "op3@host.example"], "op2@host.example")  # botop 非 oper
    assert not _last(conn)["ok"]


# ---------- 黑白名单互斥 ----------

async def test_whitelist_blacklist_mutex(srv):
    conn = FakeConn()
    await _cmd(srv, conn, "whitelist", ["on"], "op1@host.example", is_oper=True)
    assert _last(conn)["ok"]
    await _cmd(srv, conn, "blacklist", ["on"], "op1@host.example", is_oper=True)
    assert not _last(conn)["ok"]
    assert "不能同时启用" in _last(conn)["reply"]


async def test_whitelist_add_entry(srv):
    conn = FakeConn()
    await _cmd(srv, conn, "whitelist", ["add", "good!*@*", "#chan1"], "op1@host.example", is_oper=True)
    assert _last(conn)["ok"]
    assert srv.permissions.whitelist[0]["mask"] == "good!*@*"
    assert srv.permissions.whitelist[0]["channel"] == "#chan1"


# ---------- ban/unban 授权 + action ----------

async def test_ban_approves_with_action(srv):
    conn = FakeConn()
    await _cmd(srv, conn, "ban", ["baduser"], "op2@host.example", is_oper=True)
    msg = _last(conn)
    assert msg["ok"] and msg["action"] == "ban" and msg["action_args"] == ["baduser"]


# ---------- shellop 确认流程 ----------

async def test_shellop_add_requires_botop_and_oper(srv):
    conn = FakeConn()
    # 候选人在 botop 但 oper 缓存无记录 → 拒绝
    srv.oper_cache["op2@host.example"] = (False, time.monotonic())
    await _cmd(srv, conn, "shellop", ["add", "op2@host.example"], "op1@host.example", is_oper=True)
    assert not _last(conn)["ok"]
    assert "oper" in _last(conn)["reply"]

    # 候选人不在 botop → 拒绝
    await _cmd(srv, conn, "shellop", ["add", "nobody@host.example"], "op1@host.example", is_oper=True)
    assert not _last(conn)["ok"]
    assert "botop" in _last(conn)["reply"]


async def test_shellop_full_confirm_flow(srv):
    conn = FakeConn()
    srv.conns = {"irc_main": conn}  # 注册以便广播到达
    srv.oper_cache["op2@host.example"] = (True, time.monotonic())
    await _cmd(srv, conn, "shellop", ["add", "op2@host.example"], "op1@host.example", is_oper=True)
    # 发起成功，提议已广播给 client
    proposal = next(m for m in conn.sent if m["type"] == "shellop_proposal")
    pid = proposal["proposal_id"]
    assert "op1@host.example" in proposal["voters"]
    assert "op2@host.example" in proposal["voters"]

    # 另一个投票人 op2 投 confirm → 全员同意（op1 自动同意 + op2）
    vote_conn = FakeConn("irc_other")
    await srv._handle_vote(vote_conn, make_vote(pid, "confirm", "op2@host.example"))
    assert "op2@host.example" in srv.permissions.shellop
    # 结果已私信发起者
    result = next(m for m in conn.sent if m["type"] == "command_result" and "通过" in m["reply"])
    assert result["target_type"] == "private"


async def test_shellop_reject_flow(srv):
    conn = FakeConn()
    srv.conns = {"irc_main": conn}  # 注册以便广播到达
    srv.oper_cache["op2@host.example"] = (True, time.monotonic())
    await _cmd(srv, conn, "shellop", ["add", "op2@host.example"], "op1@host.example", is_oper=True)
    pid = next(m for m in conn.sent if m["type"] == "shellop_proposal")["proposal_id"]

    vote_conn = FakeConn("irc_other")
    await srv._handle_vote(vote_conn, make_vote(pid, "reject", "op2@host.example"))
    assert "op2@host.example" not in srv.permissions.shellop
    result = next(m for m in conn.sent if m["type"] == "command_result" and "否决" in m["reply"])
    assert not result["ok"]


async def test_shellop_remove_direct(srv):
    srv.permissions.shellop.add("op1@host.example")
    conn = FakeConn()
    await _cmd(srv, conn, "shellop", ["remove", "op1@host.example"], "op1@host.example", is_oper=True)
    assert _last(conn)["ok"]
    assert "op1@host.example" not in srv.permissions.shellop


# ---------- runcmd 路由 ----------

async def test_runcmd_routes_to_target(srv):
    origin = FakeConn("irc_main")
    target = FakeConn("xmpp_main")
    srv.conns = {"irc_main": origin, "xmpp_main": target}
    srv.permissions.shellop.add("boss@host.example")

    task = asyncio.create_task(
        srv._handle_command(
            origin,
            make_command("runcmd", ["xmpp_main", "ls", "-la"], "boss@host.example", False, "#chan"),
        )
    )
    # 等待 runcmd 消息到达 target
    for _ in range(100):
        if any(m["type"] == "runcmd" for m in target.sent):
            break
        await asyncio.sleep(0.01)
    runcmd = next(m for m in target.sent if m["type"] == "runcmd")
    assert runcmd["cmd"] == "ls -la"

    await srv._handle_runcmd_result(make_runcmd_result(runcmd["task_id"], True, "total 8"))
    await task
    result = next(m for m in origin.sent if m["type"] == "command_result" and m["target_type"] == "private")
    assert result["reply"] == "total 8"


async def test_runcmd_target_offline(srv):
    conn = FakeConn()
    srv.permissions.shellop.add("boss@host.example")
    await _cmd(srv, conn, "runcmd", ["ghost", "ls"], "boss@host.example")
    assert not _last(conn)["ok"]
    assert "不在线" in _last(conn)["reply"]
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_server.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 server.py**

```python
"""server 中枢：监听/认证/命令路由/shellop 确认编排/runcmd 转发/权限持久化。"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
import time
import uuid
from pathlib import Path

import yaml

from .commands import COMMAND_LEVELS
from .config import ConfigError, ConfigWatcher, load_yaml
from .permissions import Permissions
from .protocol import (
    decode_msg,
    encode_msg,
    make_command_result,
    make_permission_update,
    make_runcmd,
    make_shellop_proposal,
)
from .util import parse_userhost

log = logging.getLogger(__name__)

AUTH_TIMEOUT = 10.0
RUNDMD_TIMEOUT = 300.0


class ClientConnection:
    """一条已认证的 client 连接。"""

    def __init__(self, reader, writer, client_name: str, client_type: str, bot_name: str) -> None:
        self.reader = reader
        self.writer = writer
        self.client_name = client_name
        self.client_type = client_type
        self.bot_name = bot_name

    async def send(self, msg: dict) -> None:
        self.writer.write(encode_msg(msg))
        await self.writer.drain()


class BotServer:
    """权威权限中枢。"""

    def __init__(self, config_path: Path) -> None:
        self.config_path = Path(config_path)
        self.cfg: dict = {}
        self.permissions = Permissions()
        self.conns: dict[str, ClientConnection] = {}
        # userhost -> (is_oper, monotonic_ts)
        self.oper_cache: dict[str, tuple[bool, float]] = {}
        self.proposals: dict[str, dict] = {}
        # task_id -> (future, origin_conn, caller)
        self.runcmd_pending: dict[str, tuple[asyncio.Future, ClientConnection, str]] = {}
        self._persist_lock = asyncio.Lock()

    # ---------- 配置 ----------

    def reload(self, cfg: dict) -> None:
        if "server" not in cfg or "permissions" not in cfg:
            raise ConfigError("server.yaml 必须包含 server 与 permissions 段")
        self.permissions = Permissions.from_config(cfg)
        self.cfg = cfg
        asyncio.create_task(self._broadcast_permissions())

    def oper_cache_ttl(self) -> float:
        return float(self.cfg.get("server", {}).get("oper_cache_ttl", 60))

    def confirm_timeout(self) -> float:
        return float(self.cfg.get("server", {}).get("shellop_confirm_timeout", 60))

    # ---------- 主循环 ----------

    async def run(self) -> None:
        srv = self.cfg["server"]
        host = srv.get("listen_host", "0.0.0.0")
        port = int(srv.get("listen_port", 8443))
        tls_cfg = srv.get("tls", {})
        ssl_ctx = None
        if tls_cfg and tls_cfg.get("certfile"):
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.load_cert_chain(tls_cfg["certfile"], tls_cfg.get("keyfile"))
        server = await asyncio.start_server(self._handle_client, host, port, ssl=ssl_ctx)
        log.info("server 监听 %s:%s (TLS=%s)", host, port, ssl_ctx is not None)
        watcher = ConfigWatcher(self.config_path, self._on_config_change)
        try:
            await asyncio.gather(server.serve_forever(), watcher.run())
        finally:
            watcher.stop()

    async def _on_config_change(self, cfg: dict) -> None:
        try:
            self.reload(cfg)
        except ConfigError as e:
            log.error("热重载失败: %s", e)

    # ---------- 连接处理 ----------

    async def _handle_client(self, reader, writer) -> None:
        conn: ClientConnection | None = None
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=AUTH_TIMEOUT)
            if not line:
                return
            msg = decode_msg(line.decode("utf-8"))
            if msg.get("type") != "auth":
                log.warning("首个消息非 auth")
                return
            token = self.cfg.get("server", {}).get("token", "")
            if msg.get("token") != token:
                log.warning("认证失败: %s", msg.get("client_name"))
                writer.write(encode_msg({"type": "auth_ok", "ok": False}))
                await writer.drain()
                return
            conn = ClientConnection(
                reader, writer,
                msg["client_name"], msg.get("client_type", ""), msg.get("bot_name", ""),
            )
            self.conns[conn.client_name] = conn
            log.info("client 注册: %s (%s)", conn.client_name, conn.client_type)
            await conn.send({"type": "auth_ok", "ok": True})
            await conn.send(self._permission_update_msg())

            async for raw in reader:
                line = raw.decode("utf-8")
                try:
                    msg = decode_msg(line)
                except Exception as e:  # noqa: BLE001 - 协议错误不致命
                    log.warning("协议错误: %s", e)
                    continue
                await self._dispatch(conn, msg)
        except (ConnectionError, asyncio.IncompleteReadError, TimeoutError, OSError):
            pass
        finally:
            if conn is not None:
                self.conns.pop(conn.client_name, None)
                log.info("client 断开: %s", conn.client_name)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    # ---------- 分发 ----------

    async def _dispatch(self, conn: ClientConnection, msg: dict) -> None:
        t = msg.get("type")
        if t == "command":
            await self._handle_command(conn, msg)
        elif t == "vote":
            await self._handle_vote(conn, msg)
        elif t == "runcmd_result":
            await self._handle_runcmd_result(msg)
        elif t == "status":
            pass
        else:
            log.warning("未知消息类型: %s", t)

    async def _handle_command(self, conn: ClientConnection, msg: dict) -> None:
        cmd = msg.get("cmd", "")
        args = msg.get("args", [])
        caller = msg.get("caller_userhost", "")
        is_oper = bool(msg.get("caller_is_oper", False))
        channel = msg.get("channel")
        uh = parse_userhost(caller)
        if uh:
            self.oper_cache[uh] = (is_oper, time.monotonic())
        required = COMMAND_LEVELS.get(cmd)
        if required is None:
            await self._reply(conn, False, f"未知命令: {cmd}", channel, caller)
            return
        if not self.permissions.has_level(uh, required, is_oper=is_oper):
            await self._reply(conn, False, "权限不足", channel, caller)
            return
        handler = getattr(self, f"_cmd_{cmd}", None)
        if handler is None:
            await self._reply(conn, False, f"未知命令: {cmd}", channel, caller)
            return
        await handler(conn, args, uh, is_oper, channel)

    async def _reply(
        self,
        conn: ClientConnection,
        ok: bool,
        reply: str,
        channel: str | None,
        caller: str,
        action: str = "none",
        action_args: list[str] | None = None,
    ) -> None:
        target_type = "channel" if channel else "private"
        target = channel or caller
        await conn.send(make_command_result(ok, reply, target_type, target, action, action_args))

    # ---------- 命令处理器 ----------

    async def _cmd_info(self, conn, args, caller, is_oper, channel) -> None:
        clients = ", ".join(f"{n}({c.client_type})" for n, c in self.conns.items()) or "无"
        reply = (
            f"在线 client: {clients}\n"
            f"botop: {len(self.permissions.botop)} | shellop: {len(self.permissions.shellop)}\n"
            f"whitelist: {'开' if self.permissions.whitelist_enabled else '关'} "
            f"({len(self.permissions.whitelist)}条) | "
            f"blacklist: {'开' if self.permissions.blacklist_enabled else '关'} "
            f"({len(self.permissions.blacklist)}条)"
        )
        await self._reply(conn, True, reply, channel, caller)

    async def _cmd_botop(self, conn, args, caller, is_oper, channel) -> None:
        if len(args) != 2 or args[0] not in ("give", "remove"):
            await self._reply(conn, False, "用法: botop give/remove <user@host>", channel, caller)
            return
        target = parse_userhost(args[1])
        if args[0] == "give":
            self.permissions.botop.add(target)
            reply = f"已授予 botop: {target}"
        else:
            self.permissions.botop.discard(target)
            reply = f"已移除 botop: {target}"
        await self._persist_permissions()
        await self._reply(conn, True, reply, channel, caller)

    async def _cmd_shellop(self, conn, args, caller, is_oper, channel) -> None:
        if len(args) != 2 or args[0] not in ("add", "remove"):
            await self._reply(conn, False, "用法: shellop add/remove <user@host>", channel, caller)
            return
        target = parse_userhost(args[1])
        if args[0] == "remove":
            self.permissions.shellop.discard(target)
            await self._persist_permissions()
            await self._reply(conn, True, f"已移除 shellop: {target}", channel, caller)
            return
        # add → 确认流程
        if target not in self.permissions.botop:
            await self._reply(conn, False, f"候选人 {target} 不是 botop，无法提议为 shellop", channel, caller)
            return
        oper_info = self.oper_cache.get(target)
        if not oper_info or not oper_info[0]:
            await self._reply(conn, False, f"候选人 {target} 未验证为 oper（需其 /oper 后触发机器人 WHOIS），无法提议", channel, caller)
            return
        if target in self.permissions.shellop:
            await self._reply(conn, False, f"{target} 已是 shellop", channel, caller)
            return
        proposal_id = uuid.uuid4().hex[:8]
        voters = self._collect_voters()
        voters.add(caller)  # 发起者必投
        deadline_ts = time.time() + self.confirm_timeout()
        prop = {
            "proposal_id": proposal_id,
            "candidate": target,
            "deadline": time.monotonic() + self.confirm_timeout(),
            "voters": voters,
            "votes": {caller: True},  # 发起者自动同意
            "origin_conn": conn,
            "origin_caller": caller,
            "origin_channel": channel,
            "task": None,
        }
        self.proposals[proposal_id] = prop
        prop["task"] = asyncio.create_task(self._proposal_timer(proposal_id))
        await self._broadcast_proposal(proposal_id, target, deadline_ts, sorted(voters))
        await self._reply(conn, True, f"shellop 提议 #{proposal_id} 已发送给 {len(voters)} 位在线 botop+oper 确认", channel, caller)

    def _collect_voters(self) -> set[str]:
        voters = set(self.permissions.botop)
        ttl = self.oper_cache_ttl()
        now = time.monotonic()
        for uh, (is_oper, ts) in self.oper_cache.items():
            if is_oper and now - ts <= ttl:
                voters.add(uh)
        return voters

    async def _broadcast_proposal(self, proposal_id, candidate, deadline_ts, voters) -> None:
        msg = make_shellop_proposal(proposal_id, candidate, deadline_ts, voters)
        for conn in list(self.conns.values()):
            try:
                await conn.send(msg)
            except (ConnectionError, OSError):
                pass

    async def _proposal_timer(self, proposal_id: str) -> None:
        prop = self.proposals.get(proposal_id)
        if prop is None:
            return
        wait = max(0.0, prop["deadline"] - time.monotonic())
        await asyncio.sleep(wait)
        if proposal_id in self.proposals:
            await self._finalize_proposal(proposal_id, success=False, reason="超时作废")

    async def _handle_vote(self, conn: ClientConnection, msg: dict) -> None:
        pid = msg.get("proposal_id", "")
        vote = msg.get("vote", "")
        voter = parse_userhost(msg.get("voter_userhost", ""))
        prop = self.proposals.get(pid)
        if prop is None:
            await self._reply(conn, False, f"提议 {pid} 不存在或已结束", None, voter)
            return
        if voter not in prop["voters"]:
            await self._reply(conn, False, "你不是该提议的投票人", None, voter)
            return
        if voter in prop["votes"]:
            await self._reply(conn, False, "你已投过票", None, voter)
            return
        if time.monotonic() > prop["deadline"]:
            await self._finalize_proposal(pid, success=False, reason="超时作废")
            await self._reply(conn, False, "提议已超时作废", None, voter)
            return
        if vote == "reject":
            prop["votes"][voter] = False
            await self._finalize_proposal(pid, success=False, reason=f"{voter} 否决")
            return
        if vote == "confirm":
            prop["votes"][voter] = True
            if len(prop["votes"]) >= len(prop["voters"]):
                await self._finalize_proposal(pid, success=True, reason="全员同意")
                return
            await self._reply(conn, True, f"已记录同意 ({len(prop['votes'])}/{len(prop['voters'])})", None, voter)
            return
        await self._reply(conn, False, "投票值必须是 confirm/reject", None, voter)

    async def _finalize_proposal(self, pid: str, success: bool, reason: str) -> None:
        prop = self.proposals.pop(pid, None)
        if prop is None:
            return
        if prop.get("task"):
            prop["task"].cancel()
            try:
                await prop["task"]
            except asyncio.CancelledError:
                pass
        if success:
            self.permissions.shellop.add(prop["candidate"])
            await self._persist_permissions()
            reply = f"提议 #{pid} 通过：{prop['candidate']} 已成为 shellop"
        else:
            reply = f"提议 #{pid} 未通过：{reason}"
        try:
            await self._reply(prop["origin_conn"], success, reply, None, prop["origin_caller"])
        except (ConnectionError, OSError):
            pass

    async def _cmd_whitelist(self, conn, args, caller, is_oper, channel) -> None:
        await self._manage_list(conn, args, caller, channel, "whitelist")

    async def _cmd_blacklist(self, conn, args, caller, is_oper, channel) -> None:
        await self._manage_list(conn, args, caller, channel, "blacklist")

    async def _manage_list(self, conn, args, caller, channel, which: str) -> None:
        entries = self.permissions.whitelist if which == "whitelist" else self.permissions.blacklist
        other = "blacklist" if which == "whitelist" else "whitelist"
        if not args:
            await self._reply(conn, False, f"用法: {which} add/del/list/on/off [mask] [channel]", channel, caller)
            return
        sub = args[0]
        if sub == "list":
            if entries:
                lines = "\n".join(f"- {e.get('mask')} {e.get('channel', '(全局)')}" for e in entries)
                await self._reply(conn, True, f"{which} 条目:\n{lines}", channel, caller)
            else:
                await self._reply(conn, True, f"{which} 为空", channel, caller)
            return
        if sub == "on":
            if getattr(self.permissions, f"{other}_enabled"):
                await self._reply(conn, False, f"{other} 已启用，{which} 与 {other} 不能同时启用", channel, caller)
                return
            setattr(self.permissions, f"{which}_enabled", True)
            await self._persist_permissions()
            await self._reply(conn, True, f"{which} 已启用", channel, caller)
            return
        if sub == "off":
            setattr(self.permissions, f"{which}_enabled", False)
            await self._persist_permissions()
            await self._reply(conn, True, f"{which} 已禁用", channel, caller)
            return
        if sub in ("add", "del"):
            if len(args) < 2:
                await self._reply(conn, False, f"用法: {which} {sub} <mask> [channel]", channel, caller)
                return
            mask = args[1]
            ch = args[2] if len(args) > 2 else None
            if sub == "add":
                entry = {"mask": mask}
                if ch:
                    entry["channel"] = ch
                entries.append(entry)
                await self._persist_permissions()
                await self._reply(conn, True, f"已添加 {which} 条目: {mask} {ch or '(全局)'}", channel, caller)
            else:
                for i, e in enumerate(entries):
                    if e.get("mask") == mask and e.get("channel") == ch:
                        del entries[i]
                        await self._persist_permissions()
                        await self._reply(conn, True, f"已删除 {which} 条目: {mask}", channel, caller)
                        return
                await self._reply(conn, False, f"未找到条目: {mask}", channel, caller)
            return
        await self._reply(conn, False, f"未知子命令: {sub}", channel, caller)

    async def _cmd_ban(self, conn, args, caller, is_oper, channel) -> None:
        if not args:
            await self._reply(conn, False, "用法: ban <nick>", channel, caller)
            return
        await self._reply(conn, True, f"已批准封禁 {args[0]}", channel, caller, action="ban", action_args=[args[0]])

    async def _cmd_unban(self, conn, args, caller, is_oper, channel) -> None:
        if not args:
            await self._reply(conn, False, "用法: unban <nick>", channel, caller)
            return
        await self._reply(conn, True, f"已批准解封 {args[0]}", channel, caller, action="unban", action_args=[args[0]])

    async def _cmd_runcmd(self, conn, args, caller, is_oper, channel) -> None:
        if len(args) < 2:
            await self._reply(conn, False, "用法: runcmd <client_name> <cmd...>", channel, caller)
            return
        target_name = args[0]
        cmd = " ".join(args[1:])
        target = self.conns.get(target_name)
        if target is None:
            await self._reply(conn, False, f"client {target_name} 不在线", channel, caller)
            return
        task_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.runcmd_pending[task_id] = (fut, conn, caller)
        try:
            await target.send(make_runcmd(task_id, cmd))
        except (ConnectionError, OSError):
            self.runcmd_pending.pop(task_id, None)
            await self._reply(conn, False, f"client {target_name} 连接异常", channel, caller)
            return
        await self._reply(conn, True, f"runcmd 已发送到 {target_name}", channel, caller)
        try:
            ok, output = await asyncio.wait_for(fut, timeout=RUNDMD_TIMEOUT)
        except asyncio.TimeoutError:
            await self._reply(conn, False, f"runcmd 在 {target_name} 超时（{int(RUNDMD_TIMEOUT)}s）", None, caller)
            return
        if not ok:
            text = f"命令执行失败:\n{output}" if output else "命令执行失败"
        else:
            text = output if output else "命令执行成功，无输出"
        await conn.send(make_command_result(ok, text, "private", caller))

    async def _handle_runcmd_result(self, msg: dict) -> None:
        tid = msg.get("task_id", "")
        entry = self.runcmd_pending.pop(tid, None)
        if entry is None:
            log.warning("未知 runcmd task: %s", tid)
            return
        fut, origin_conn, caller = entry
        if not fut.done():
            fut.set_result((bool(msg.get("ok")), msg.get("output", "")))

    # ---------- 持久化与广播 ----------

    async def _persist_permissions(self) -> None:
        async with self._persist_lock:
            self.cfg["permissions"] = self.permissions.to_config_dict()
            tmp = self.config_path.with_suffix(".yaml.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.safe_dump(self.cfg, f, allow_unicode=True, sort_keys=False)
            os.replace(tmp, self.config_path)
        await self._broadcast_permissions()

    def _oper_cache_snapshot(self) -> dict:
        ttl = self.oper_cache_ttl()
        now = time.monotonic()
        return {uh: is_oper for uh, (is_oper, ts) in self.oper_cache.items() if now - ts <= ttl}

    def _permission_update_msg(self) -> dict:
        return make_permission_update(self.permissions.snapshot(), self._oper_cache_snapshot())

    async def _broadcast_permissions(self) -> None:
        msg = self._permission_update_msg()
        for conn in list(self.conns.values()):
            try:
                await conn.send(msg)
            except (ConnectionError, OSError):
                pass
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_server.py -v`
Expected: PASS（12 passed）

- [ ] **Step 5: 提交（可选）**

```bash
git add src/ircxmppbot/server.py tests/test_server.py
git commit -m "feat: server 中枢（认证/路由/shellop 编排/runcmd/持久化）"
```

---

### Task 9: irc_client.py（IRC 客户端：pydle 封装 + 命令分发 + oper WHOIS + 踢人）

**Files:**
- Create: `src/ircxmppbot/irc_client.py`
- Test: `tests/test_irc_client.py`

**Interfaces:**
- Consumes: `ircxmppbot.commands.parse_command`、`ircxmppbot.llm.{LLMClient, LLMError}`、`ircxmppbot.permissions.Permissions`、`ircxmppbot.protocol.*`、`ircxmppbot.util.{backoff_delay, split_text}`、`ircxmppbot.config.load_yaml`
- Produces:
  - `IRC_LINE_LIMIT: int = 400`（IRC 512 字节/行安全上限）
  - `class IRCBot(pydle.Client)`:
    - `__init__(self, config_path: Path)` — 读取配置；nickname=bot_name；realname 来自 `irc.realname`；初始化 `self.llm`、`self.permissions`、`self.oper_cache: dict[str, bool]`
    - `async def on_connect()` — join 频道，启动 `_server_loop()` 任务
    - `async def on_channel_message(target, source, message)` / `async def on_message(target, source, message)` — 都转发到 `_maybe_command`
    - `async def on_join(channel, user)` — 调用 `_enforce_lists(channel, user)`
    - `async def _maybe_command(text, source, target, is_channel)` — chat/help 本地处理；confirm/reject 走 `_server_vote`；其余上报 server
    - `async def _server_vote(vote, args, source)` — WHOIS 调用者后发送 `make_vote` 消息（confirm/reject 专用）
    - `async def _server_loop()` / `async def _server_connect(srv_cfg)` / `async def _server_send(msg)` — TLS 连接 + 指数退避重连
    - `async def _on_server_msg(msg)` — 处理 permission_update / shellop_proposal / runcmd / command_result
    - `async def _whois_user(nick) -> dict | None` — `await self.whois(nick)` 带 8s 超时
    - `async def _enforce_lists(channel, nick)` / `async def _enforce_lists_all_channels()`
    - `def _nick_for_userhost(userhost) -> str | None`、`def _ban_mask(nick) -> str`
    - `async def run()` — 连接并保持事件循环运行（pydle 内部自动重连）

- [ ] **Step 1: 写失败测试**

`tests/test_irc_client.py`:

```python
from pathlib import Path

import pytest

from ircxmppbot.config import load_yaml
from ircxmppbot.irc_client import IRCBot, IRC_LINE_LIMIT


def _write_cfg(tmp_path: Path, **llm_overrides) -> Path:
    cfg = {
        "client": {
            "name": "irc_main",
            "type": "irc",
            "bot_name": "qsdwindows_bot",
            "server": {
                "host": "127.0.0.1",
                "port": 8443,
                "token": "test-token",
                "tls": {"verify": False},
            },
        },
        "irc": {
            "host": "irc.example.com",
            "port": 6697,
            "tls": True,
            "realname": "My IRC Bot",
            "channels": ["#chan1"],
        },
        "llm": {
            "format": "openai_chat",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test",
            "model": "gpt-4o-mini",
            "max_reply_chars": 1500,
            **llm_overrides,
        },
    }
    p = tmp_path / "client_irc.yaml"
    import yaml
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p


@pytest.fixture
def bot(tmp_path: Path) -> IRCBot:
    b = IRCBot(_write_cfg(tmp_path))
    b.users = {
        "op1": {"nickname": "op1", "username": "op1", "hostname": "host.example"},
        "op2": {"nickname": "op2", "username": "op2", "hostname": "host.example"},
    }
    b.channels = {"#chan1": {"users": {"op1", "op2"}}}
    return b


def test_line_limit_constant():
    assert IRC_LINE_LIMIT == 400


def test_bot_name_from_config(bot):
    assert bot.bot_name == "qsdwindows_bot"
    # pydle 未连接时 self.nickname 为 "<unregistered>"，配置的昵称存于 _nicknames[0]
    assert bot._nicknames[0] == "qsdwindows_bot"


def test_nick_for_userhost(bot):
    assert bot._nick_for_userhost("op1@host.example") == "op1"
    assert bot._nick_for_userhost("ghost@host.example") is None


def test_ban_mask_known_user(bot):
    assert bot._ban_mask("op1") == "op1@host.example"


def test_ban_mask_unknown_user(bot):
    assert bot._ban_mask("ghost") == "ghost!*@*"


async def test_maybe_command_chat_local(bot, monkeypatch):
    """chat 不经过 server，直接在本地调 LLM。"""
    sent = []

    async def fake_chat(text):
        return "回复内容"

    async def fake_message(target, msg):
        sent.append((target, msg))

    monkeypatch.setattr(bot.llm, "chat", fake_chat)
    monkeypatch.setattr(bot, "message", fake_message)

    await bot._maybe_command("!qsdwindows_bot chat 你好", "user1", "#chan1", is_channel=True)
    assert sent, "应发送 LLM 回复"
    assert sent[0][1] == "回复内容"


async def test_maybe_command_help_local(bot, monkeypatch):
    sent = []

    async def fake_message(target, msg):
        sent.append((target, msg))

    monkeypatch.setattr(bot, "message", fake_message)
    await bot._maybe_command("!qsdwindows_bot help", "user1", "#chan1", is_channel=True)
    assert sent and "chat" in sent[0][1]


async def test_maybe_command_unknown_goes_to_server(bot, monkeypatch):
    """非 chat/help 命令上报 server（不本地处理）。"""
    sent = []

    async def fake_server_send(msg):
        sent.append(msg)

    async def fake_whois(nick):
        return {"username": "user1", "hostname": "host.example", "oper": False}

    monkeypatch.setattr(bot, "_server_send", fake_server_send)
    monkeypatch.setattr(bot, "_whois_user", fake_whois)
    await bot._maybe_command("!qsdwindows_bot botop give a@b", "user1", "#chan1", is_channel=True)
    assert sent and sent[0]["type"] == "command"
    assert sent[0]["caller_userhost"] == "user1@host.example"


async def test_confirm_routes_as_vote(bot, monkeypatch):
    """confirm/reject 走 vote 消息而非 command（server 端无 _cmd_confirm 处理器）。"""
    sent = []

    async def fake_server_send(msg):
        sent.append(msg)

    async def fake_whois(nick):
        return {"username": "op1", "hostname": "host.example", "oper": True}

    monkeypatch.setattr(bot, "_server_send", fake_server_send)
    monkeypatch.setattr(bot, "_whois_user", fake_whois)
    await bot._maybe_command("!qsdwindows_bot confirm ab12cd34", "op1", "#chan1", is_channel=True)
    assert sent and sent[0]["type"] == "vote"
    assert sent[0]["vote"] == "confirm"
    assert sent[0]["proposal_id"] == "ab12cd34"
    assert sent[0]["voter_userhost"] == "op1@host.example"


async def test_maybe_command_not_for_bot(bot, monkeypatch):
    sent = []
    monkeypatch.setattr(bot, "message", lambda target, msg: sent.append((target, msg)))
    monkeypatch.setattr(bot, "_server_send", lambda msg: sent.append(msg))
    await bot._maybe_command("hello there", "user1", "#chan1", is_channel=True)
    assert sent == []


async def test_enforce_whitelist_kicks_non_allowed(bot, monkeypatch):
    bot.permissions.apply_snapshot({
        "botop": ["op1@host.example"],
        "shellop": [],
        "whitelist": {"enabled": True, "entries": [{"mask": "op1!*@*"}]},
        "blacklist": {"enabled": False, "entries": []},
    })
    kicked = []

    async def fake_kick(channel, nick, reason=None):
        kicked.append((channel, nick, reason))
    monkeypatch.setattr(bot, "kick", fake_kick)

    # op1 在白名单 → 不踢
    await bot._enforce_lists("#chan1", "op1")
    assert kicked == []
    # op2 不在白名单且不是 botop/oper → 踢
    await bot._enforce_lists("#chan1", "op2")
    assert kicked and kicked[0][1] == "op2"


async def test_enforce_whitelist_botop_exempt(bot, monkeypatch):
    bot.permissions.apply_snapshot({
        "botop": ["op2@host.example"],
        "shellop": [],
        "whitelist": {"enabled": True, "entries": []},
        "blacklist": {"enabled": False, "entries": []},
    })
    kicked = []

    async def fake_kick(channel, nick, reason=None):
        kicked.append((channel, nick, reason))
    monkeypatch.setattr(bot, "kick", fake_kick)
    await bot._enforce_lists("#chan1", "op2")
    assert kicked == []  # botop 豁免


async def test_enforce_blacklist_kicks(bot, monkeypatch):
    bot.permissions.apply_snapshot({
        "botop": [],
        "shellop": [],
        "whitelist": {"enabled": False, "entries": []},
        "blacklist": {"enabled": True, "entries": [{"mask": "op2!*@*"}]},
    })
    kicked = []

    async def fake_kick(channel, nick, reason=None):
        kicked.append((channel, nick, reason))
    monkeypatch.setattr(bot, "kick", fake_kick)
    await bot._enforce_lists("#chan1", "op2")
    assert kicked and kicked[0][1] == "op2"


async def test_no_lists_no_kick(bot, monkeypatch):
    kicked = []

    async def fake_kick(channel, nick, reason=None):
        kicked.append((channel, nick, reason))
    monkeypatch.setattr(bot, "kick", fake_kick)
    await bot._enforce_lists("#chan1", "op2")
    assert kicked == []
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_irc_client.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 irc_client.py**

```python
"""IRC 客户端：pydle 封装 + 命令分发 + oper WHOIS + 黑白名单踢人。"""

from __future__ import annotations

import asyncio
import logging
import ssl
from pathlib import Path

import pydle

from .commands import parse_command
from .config import load_yaml
from .llm import LLMClient, LLMError
from .permissions import Permissions
from .protocol import (
    decode_msg,
    encode_msg,
    make_auth,
    make_command,
    make_runcmd_result,
    make_vote,
)
from .util import backoff_delay, parse_userhost, split_text

log = logging.getLogger(__name__)

IRC_LINE_LIMIT = 400  # IRC 512 字节/行的安全上限
WHOIS_TIMEOUT = 8.0


class IRCBot(pydle.Client):
    """IRC 端机器人。"""

    def __init__(self, config_path: Path) -> None:
        self.config_path = Path(config_path)
        self.cfg = load_yaml(self.config_path)
        client_cfg = self.cfg["client"]
        irc_cfg = self.cfg.get("irc", {})
        super().__init__(
            client_cfg["bot_name"],
            username=client_cfg["bot_name"],
            realname=irc_cfg.get("realname", client_cfg["bot_name"]),
        )
        self.bot_name = client_cfg["bot_name"]
        self.permissions = Permissions()
        # userhost -> is_oper（本地镜像，来自 server 推送 + 本地 WHOIS）
        self.oper_cache: dict[str, bool] = {}
        self.llm = LLMClient(self.cfg.get("llm", {}))
        self._server_reader: asyncio.StreamReader | None = None
        self._server_writer: asyncio.StreamWriter | None = None
        self._server_task: asyncio.Task | None = None

    # ---------- pydle 生命周期 ----------

    async def on_connect(self) -> None:
        await super().on_connect()
        for channel in self.cfg.get("irc", {}).get("channels", []):
            await self.join(channel)
        if self._server_task is None or self._server_task.done():
            self._server_task = asyncio.create_task(self._server_loop())
        log.info("IRC 已连接并加入频道: %s", self.cfg.get("irc", {}).get("channels", []))

    # ---------- server 连接 ----------

    async def _server_loop(self) -> None:
        srv_cfg = self.cfg["client"]["server"]
        attempt = 0
        while True:
            try:
                await self._server_connect(srv_cfg)
                attempt = 0
            except (ConnectionError, OSError, asyncio.IncompleteReadError, TimeoutError) as e:
                delay = backoff_delay(attempt)
                log.warning("server 连接失败: %s，%.0fs 后重连", e, delay)
                await asyncio.sleep(delay)
                attempt += 1

    async def _server_connect(self, srv_cfg: dict) -> None:
        ssl_ctx = None
        tls_cfg = srv_cfg.get("tls", {})
        if tls_cfg:
            verify = tls_cfg.get("verify", True)
            ssl_ctx = ssl.create_default_context()
            if not verify:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
            if tls_cfg.get("ca_cert"):
                ssl_ctx.load_verify_locations(tls_cfg["ca_cert"])
        reader, writer = await asyncio.open_connection(
            srv_cfg["host"], int(srv_cfg["port"]), ssl=ssl_ctx
        )
        self._server_reader, self._server_writer = reader, writer
        client_cfg = self.cfg["client"]
        await self._server_send(make_auth(
            srv_cfg["token"], client_cfg["name"], "irc", self.bot_name
        ))
        log.info("已连接 server: %s:%s", srv_cfg["host"], srv_cfg["port"])
        while True:
            line = await reader.readline()
            if not line:
                raise ConnectionError("server 断开")
            try:
                msg = decode_msg(line.decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                log.warning("协议错误: %s", e)
                continue
            await self._on_server_msg(msg)

    async def _server_send(self, msg: dict) -> None:
        if self._server_writer is None:
            raise ConnectionError("未连接 server")
        self._server_writer.write(encode_msg(msg))
        await self._server_writer.drain()

    async def _on_server_msg(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "auth_ok":
            if not msg.get("ok"):
                log.error("server 认证失败，请检查 token")
        elif t == "permission_update":
            self.permissions.apply_snapshot(msg)
            self.oper_cache = {parse_userhost(k): bool(v) for k, v in (msg.get("oper_cache") or {}).items()}
            await self._enforce_lists_all_channels()
        elif t == "shellop_proposal":
            await self._dm_proposal(msg)
        elif t == "runcmd":
            await self._exec_runcmd(msg)
        elif t == "command_result":
            await self._handle_command_result(msg)
        else:
            log.debug("忽略 server 消息: %s", t)

    # ---------- 命令处理 ----------

    async def on_channel_message(self, target, source, message) -> None:
        await self._maybe_command(message, source, target, is_channel=True)

    async def on_message(self, target, source, message) -> None:
        # pydle 私信：target 是发送者
        await self._maybe_command(message, source, source, is_channel=False)

    async def _maybe_command(self, text, source, target, is_channel) -> None:
        if source == self.nickname:
            return
        parsed = parse_command(text, self.bot_name)
        if parsed is None:
            return
        if parsed.cmd == "chat":
            await self._chat(" ".join(parsed.args), source, target, is_channel)
            return
        if parsed.cmd == "help":
            await self.message(source, "命令: chat/help/ban/unban/whitelist/blacklist/botop/shellop/confirm/reject/runcmd/info")
            return
        if parsed.cmd in ("confirm", "reject"):
            # 投票走专用 vote 消息（server 端由 _handle_vote 处理）
            await self._server_vote(parsed.cmd, parsed.args, source)
            return
        # 其余命令上报 server 做权限判定
        await self._server_command(parsed.cmd, parsed.args, source, target, is_channel)

    async def _server_vote(self, vote: str, args: list[str], source: str) -> None:
        if not args:
            await self.message(source, f"用法: {vote} <提议ID>")
            return
        info = await self._whois_user(source)
        if info is None:
            await self.message(source, "无法获取你的身份信息，请稍后再试")
            return
        userhost = f"{info.get('username', '')}@{info.get('hostname', '')}"
        uh = parse_userhost(userhost)
        self.oper_cache[uh] = bool(info.get("oper"))
        await self._server_send(make_vote(args[0], vote, uh))

    async def _server_command(self, cmd, args, source, target, is_channel) -> None:
        info = await self._whois_user(source)
        if info is None:
            await self.message(source, "无法获取你的身份信息，请稍后再试")
            return
        userhost = f"{info.get('username', '')}@{info.get('hostname', '')}"
        uh = parse_userhost(userhost)
        is_oper = bool(info.get("oper"))
        self.oper_cache[uh] = is_oper
        channel = target if is_channel else None
        await self._server_send(make_command(cmd, args, uh, is_oper, channel))

    async def _whois_user(self, nick) -> dict | None:
        try:
            return await asyncio.wait_for(self.whois(nick), timeout=WHOIS_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("WHOIS %s 超时", nick)
            return None

    async def _chat(self, user_text, source, reply_target, is_channel) -> None:
        if not user_text:
            await self.message(source, "用法: chat <文本>")
            return
        try:
            reply = await self.llm.chat(user_text)
        except LLMError as e:
            reply = f"LLM 错误: {e}"
        limit = min(int(self.cfg.get("llm", {}).get("max_reply_chars", 1500)), IRC_LINE_LIMIT)
        for chunk in split_text(reply, limit):
            await self.message(reply_target, chunk)

    # ---------- shellop 提议私信 ----------

    async def _dm_proposal(self, msg: dict) -> None:
        import time
        remaining = max(0, int(msg.get("deadline_ts", 0) - time.time()))
        text = (
            f"shellop 提议 #{msg['proposal_id']}: 将 {msg['candidate']} 设为 shellop。"
            f"回复 !{self.bot_name} confirm {msg['proposal_id']} 同意 / "
            f"!{self.bot_name} reject {msg['proposal_id']} 拒绝（{remaining} 秒内，全员同意才生效）"
        )
        for voter in msg.get("voters", []):
            nick = self._nick_for_userhost(voter)
            if nick and nick != self.nickname:
                await self.message(nick, text)

    def _nick_for_userhost(self, userhost: str) -> str | None:
        want = parse_userhost(userhost)
        for nick, info in self.users.items():
            uh = parse_userhost(f"{info.get('username', '')}@{info.get('hostname', '')}")
            if uh == want:
                return nick
        return None

    # ---------- runcmd 执行 ----------

    async def _exec_runcmd(self, msg: dict) -> None:
        task_id = msg["task_id"]
        cmd = msg["cmd"]
        log.info("执行 runcmd: %s", cmd)
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode("utf-8", errors="replace")
        await self._server_send(make_runcmd_result(task_id, proc.returncode == 0, output))

    # ---------- 命令结果（含 ban/unban 动作） ----------

    async def _handle_command_result(self, msg: dict) -> None:
        ok = bool(msg.get("ok"))
        reply = msg.get("reply", "")
        target = msg.get("target", "")
        target_type = msg.get("target_type", "private")
        action = msg.get("action", "none")
        action_args = msg.get("action_args", [])
        if action == "ban" and action_args:
            mask = self._ban_mask(action_args[0])
            if mask:
                await self.rawmsg("MODE", target, "+b", mask)
        elif action == "unban" and action_args:
            mask = self._ban_mask(action_args[0])
            if mask:
                await self.rawmsg("MODE", target, "-b", mask)
        if not reply:
            return  # 无回复内容则不发送（ban/unban 与 runcmd 的 reply 恒非空）
        if target_type == "private":
            # server 返回的 target 是 user@host，需解析为当前 client 可见的 nick
            nick = self._nick_for_userhost(target)
            if nick is None:
                log.warning("私信目标 %s 在当前 client 不可见，跳过", target)
                return
            await self.message(nick, reply)
        else:
            await self.message(target, reply)

    def _ban_mask(self, nick: str) -> str:
        info = self.users.get(nick)
        if info and info.get("username") and info.get("hostname"):
            return f"{info['username']}@{info['hostname']}"
        return f"{nick}!*@*"

    # ---------- 黑白名单踢人 ----------

    async def on_join(self, channel, user) -> None:
        if user == self.nickname:
            return
        await self._enforce_lists(channel, user)

    async def _enforce_lists_all_channels(self) -> None:
        for channel in list(self.channels):
            for nick in list(self.channels[channel]["users"]):
                await self._enforce_lists(channel, nick)

    async def _enforce_lists(self, channel, nick) -> None:
        p = self.permissions
        if not p.whitelist_enabled and not p.blacklist_enabled:
            return
        info = self.users.get(nick)
        if info:
            hostmask = f"{nick}!{info.get('username', '*')}@{info.get('hostname', '*')}"
        else:
            hostmask = f"{nick}!*@*"
        uh = parse_userhost(hostmask)
        if p.whitelist_enabled:
            if p.whitelist_allows(hostmask, channel):
                return
            if p.is_botop(uh) or self.oper_cache.get(uh, False):
                return
            await self.kick(channel, nick, "白名单模式")
            log.info("白名单踢出 %s from %s", nick, channel)
            return
        if p.blacklist_blocks(hostmask, channel):
            await self.kick(channel, nick, "黑名单")
            log.info("黑名单踢出 %s from %s", nick, channel)

    # ---------- 主入口 ----------

    async def run(self) -> None:
        irc_cfg = self.cfg.get("irc", {})
        attempt = 0
        while True:
            try:
                await self.connect(
                    irc_cfg["host"],
                    port=int(irc_cfg.get("port", 6697)),
                    tls=bool(irc_cfg.get("tls", True)),
                    tls_verify=bool(irc_cfg.get("tls_verify", True)),
                )
                break  # 连接成功
            except (ConnectionError, OSError) as e:
                delay = backoff_delay(attempt)
                log.warning("IRC 连接失败: %s，%.0fs 后重连", e, delay)
                await asyncio.sleep(delay)
                attempt += 1
        # pydle 在断线时自动重连（RECONNECT_ON_ERROR）并再次触发 on_connect；
        # 主协程保持事件循环运行即可
        await asyncio.Event().wait()
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_irc_client.py -v`
Expected: PASS（14 passed）

- [ ] **Step 5: 提交（可选）**

```bash
git add src/ircxmppbot/irc_client.py tests/test_irc_client.py
git commit -m "feat: IRC 客户端（pydle/命令分发/WHOIS/踢人）"
```

---

### Task 10: xmpp_client.py（XMPP 客户端：slixmpp 封装，仅 chat）

**Files:**
- Create: `src/ircxmppbot/xmpp_client.py`
- Test: `tests/test_xmpp_client.py`

**Interfaces:**
- Consumes: `ircxmppbot.commands.parse_command`、`ircxmppbot.llm.{LLMClient, LLMError}`、`ircxmppbot.protocol.*`、`ircxmppbot.util.{backoff_delay, split_text}`、`ircxmppbot.config.load_yaml`
- Produces:
  - `XMPP_LINE_LIMIT: int = 1500`
  - `class XMPPBot(slixmpp.ClientXMPP)`:
    - `__init__(self, config_path: Path)` — 注册 xep_0030/xep_0045/xep_0199；事件：session_start/message/groupchat_message
    - `async def _session_start(event)` — get_roster、send_presence、join_muc 所有 `xmpp.mucs`
    - `def _on_message(msg)` — 仅 `type == "chat"` 私聊；免前缀直接 chat
    - `def _on_groupchat(msg)` — 群聊仅响应 `!<bot_name> chat <文本>`
    - `async def _handle_private(sender, body)` / `async def _chat(target, text, is_group)`
    - `async def _server_loop()` / `async def _server_connect(srv_cfg)` / `async def _server_send(msg)`
    - `async def _exec_runcmd(msg)` — 执行 shell 并回传结果
    - `async def run()` — connect 后 `await self.disconnected` 循环重连（slixmpp ≥1.9 已移除 process()）

- [ ] **Step 1: 写失败测试**

`tests/test_xmpp_client.py`:

```python
from pathlib import Path
import asyncio

import pytest

from ircxmppbot.config import load_yaml
from ircxmppbot.xmpp_client import XMPPBot, XMPP_LINE_LIMIT

def _write_cfg(tmp_path: Path, **llm_overrides) -> Path:
    cfg = {
        "client": {
            "name": "xmpp_main",
            "type": "xmpp",
            "bot_name": "qsdwindows_bot",
            "server": {
                "host": "127.0.0.1",
                "port": 8443,
                "token": "test-token",
                "tls": {"verify": False},
            },
        },
        "xmpp": {
            "jid": "bot@snikket.example",
            "password": "pw",
            "host": "snikket.example",
            "port": 5222,
            "tls": True,
            "mucs": ["room@conference.snikket.example"],
        },
        "llm": {
            "format": "openai_chat",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test",
            "model": "gpt-4o-mini",
            **llm_overrides,
        },
    }
    p = tmp_path / "client_xmpp.yaml"
    import yaml
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p


@pytest.fixture
def bot(tmp_path: Path) -> XMPPBot:
    b = XMPPBot(_write_cfg(tmp_path))
    b.boundjid = type("J", (), {"bare": "bot@snikket.example"})()
    return b


def test_line_limit_constant():
    assert XMPP_LINE_LIMIT == 1500


def test_bot_name(bot):
    assert bot.bot_name == "qsdwindows_bot"
    assert bot.nick == "qsdwindows_bot"


async def test_private_message_no_prefix_chats(bot, monkeypatch):
    sent = []

    async def fake_chat(text):
        assert text == "hello world"
        return "hi"

    def fake_send(mto, mbody, mtype):
        sent.append((mto, mbody, mtype))

    monkeypatch.setattr(bot.llm, "chat", fake_chat)
    monkeypatch.setattr(bot, "send_message", fake_send)
    await bot._handle_private("user@snikket.example", "hello world")
    assert sent == [("user@snikket.example", "hi", "chat")]


async def test_private_message_with_prefix_chat(bot, monkeypatch):
    sent = []

    async def fake_chat(text):
        assert text == "hi there"
        return "hello"

    def fake_send(mto, mbody, mtype):
        sent.append((mto, mbody, mtype))

    monkeypatch.setattr(bot.llm, "chat", fake_chat)
    monkeypatch.setattr(bot, "send_message", fake_send)
    await bot._handle_private("user@snikket.example", "!qsdwindows_bot chat hi there")
    assert sent == [("user@snikket.example", "hello", "chat")]


async def test_private_message_other_command_rejected(bot, monkeypatch):
    sent = []
    monkeypatch.setattr(bot, "send_message", lambda mto, mbody, mtype: sent.append((mto, mbody, mtype)))
    await bot._handle_private("user@snikket.example", "!qsdwindows_bot ban someone")
    assert sent and "仅支持 chat" in sent[0][1]


async def test_groupchat_only_chat_with_prefix(bot, monkeypatch):
    sent = []

    async def fake_chat(text):
        return "group reply"

    def fake_send(mto, mbody, mtype):
        sent.append((mto, mbody, mtype))

    monkeypatch.setattr(bot.llm, "chat", fake_chat)
    monkeypatch.setattr(bot, "send_message", fake_send)

    class FakeMsg:
        """模拟 slixmpp stanza：支持 .get() 与 [] 下标访问。"""

        def __init__(self, body, mucnick, bare):
            self._body = body
            self._mucnick = mucnick
            self._bare = bare

        def get(self, key, default=None):
            return {"body": self._body, "mucnick": self._mucnick}.get(key, default)

        def __getitem__(self, key):
            if key == "from":
                return type("F", (), {"bare": self._bare})()
            return {"body": self._body, "mucnick": self._mucnick}[key]

    bot._on_groupchat(FakeMsg("!qsdwindows_bot chat hello", "user1", "room@conference.snikket.example"))
    await asyncio.sleep(0.05)  # 事件处理器是异步任务
    assert sent == [("room@conference.snikket.example", "group reply", "groupchat")]


async def test_groupchat_without_prefix_ignored(bot, monkeypatch):
    sent = []
    monkeypatch.setattr(bot, "send_message", lambda mto, mbody, mtype: sent.append((mto, mbody, mtype)))

    class FakeMsg:
        """模拟 slixmpp stanza：支持 .get() 与 [] 下标访问。"""

        def __init__(self, body, mucnick, bare):
            self._body = body
            self._mucnick = mucnick
            self._bare = bare

        def get(self, key, default=None):
            return {"body": self._body, "mucnick": self._mucnick}.get(key, default)

        def __getitem__(self, key):
            if key == "from":
                return type("F", (), {"bare": self._bare})()
            return {"body": self._body, "mucnick": self._mucnick}[key]

    bot._on_groupchat(FakeMsg("just talking", "user1", "room@conference.snikket.example"))
    await asyncio.sleep(0.05)
    assert sent == []
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_xmpp_client.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 xmpp_client.py**

```python
"""XMPP 客户端：slixmpp 封装，仅支持 chat（私聊免前缀/群聊带前缀）。"""

from __future__ import annotations

import asyncio
import logging
import ssl
from pathlib import Path

import slixmpp

from .commands import parse_command
from .config import load_yaml
from .llm import LLMClient, LLMError
from .protocol import (
    decode_msg,
    encode_msg,
    make_auth,
    make_runcmd_result,
)
from .util import backoff_delay, split_text

log = logging.getLogger(__name__)

XMPP_LINE_LIMIT = 1500


class XMPPBot(slixmpp.ClientXMPP):
    """XMPP 端机器人（Snikket），不参与权限机制。"""

    def __init__(self, config_path: Path) -> None:
        self.config_path = Path(config_path)
        self.cfg = load_yaml(self.config_path)
        client_cfg = self.cfg["client"]
        xmpp_cfg = self.cfg.get("xmpp", {})
        super().__init__(xmpp_cfg["jid"], xmpp_cfg["password"])
        self.bot_name = client_cfg["bot_name"]
        self.nick = self.bot_name
        self.llm = LLMClient(self.cfg.get("llm", {}))
        self.register_plugin("xep_0030")
        self.register_plugin("xep_0045")
        self.register_plugin("xep_0199")
        self.add_event_handler("session_start", self._session_start)
        self.add_event_handler("message", self._on_message)
        self.add_event_handler("groupchat_message", self._on_groupchat)
        self._server_writer = None
        self._server_task: asyncio.Task | None = None

    # ---------- slixmpp 生命周期 ----------

    async def _session_start(self, event) -> None:
        await self.get_roster()
        self.send_presence()
        for room in self.cfg.get("xmpp", {}).get("mucs", []):
            self.plugin["xep_0045"].join_muc(room, self.nick)
        if self._server_task is None or self._server_task.done():
            self._server_task = asyncio.create_task(self._server_loop())
        log.info("XMPP 已连接并加入 MUC: %s", self.cfg.get("xmpp", {}).get("mucs", []))

    # ---------- 消息处理 ----------

    def _on_message(self, msg) -> None:
        if msg["type"] != "chat":
            return
        body = str(msg.get("body", "") or "")
        if not body:
            return
        sender = msg["from"].bare
        if sender == self.boundjid.bare:
            return
        asyncio.create_task(self._handle_private(sender, body))

    def _on_groupchat(self, msg) -> None:
        body = str(msg.get("body", "") or "")
        nick = msg["mucnick"]
        if not body or not nick or nick == self.nick:
            return
        parsed = parse_command(body, self.bot_name)
        if parsed is None or parsed.cmd != "chat":
            return
        asyncio.create_task(self._chat(msg["from"].bare, " ".join(parsed.args), is_group=True))

    async def _handle_private(self, sender: str, body: str) -> None:
        parsed = parse_command(body, self.bot_name)
        if parsed is None:
            # 私聊免前缀 → 直接 chat
            await self._chat(sender, body, is_group=False)
            return
        if parsed.cmd == "chat":
            await self._chat(sender, " ".join(parsed.args), is_group=False)
        else:
            self.send_message(mto=sender, mbody="XMPP 端仅支持 chat 命令", mtype="chat")

    async def _chat(self, target: str, user_text: str, is_group: bool) -> None:
        mtype = "groupchat" if is_group else "chat"
        if not user_text:
            self.send_message(mto=target, mbody="用法: chat <文本>", mtype=mtype)
            return
        try:
            reply = await self.llm.chat(user_text)
        except LLMError as e:
            reply = f"LLM 错误: {e}"
        for chunk in split_text(reply, XMPP_LINE_LIMIT):
            self.send_message(mto=target, mbody=chunk, mtype=mtype)

    # ---------- server 连接 ----------

    async def _server_loop(self) -> None:
        srv_cfg = self.cfg["client"]["server"]
        attempt = 0
        while True:
            try:
                await self._server_connect(srv_cfg)
                attempt = 0
            except (ConnectionError, OSError, asyncio.IncompleteReadError, TimeoutError) as e:
                delay = backoff_delay(attempt)
                log.warning("server 连接失败: %s，%.0fs 后重连", e, delay)
                await asyncio.sleep(delay)
                attempt += 1

    async def _server_connect(self, srv_cfg: dict) -> None:
        ssl_ctx = None
        tls_cfg = srv_cfg.get("tls", {})
        if tls_cfg:
            verify = tls_cfg.get("verify", True)
            ssl_ctx = ssl.create_default_context()
            if not verify:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
            if tls_cfg.get("ca_cert"):
                ssl_ctx.load_verify_locations(tls_cfg["ca_cert"])
        reader, writer = await asyncio.open_connection(
            srv_cfg["host"], int(srv_cfg["port"]), ssl=ssl_ctx
        )
        self._server_writer = writer
        client_cfg = self.cfg["client"]
        await self._server_send(make_auth(
            srv_cfg["token"], client_cfg["name"], "xmpp", self.bot_name
        ))
        log.info("已连接 server: %s:%s", srv_cfg["host"], srv_cfg["port"])
        while True:
            line = await reader.readline()
            if not line:
                raise ConnectionError("server 断开")
            try:
                msg = decode_msg(line.decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                log.warning("协议错误: %s", e)
                continue
            await self._on_server_msg(msg)

    async def _server_send(self, msg: dict) -> None:
        if self._server_writer is None:
            raise ConnectionError("未连接 server")
        self._server_writer.write(encode_msg(msg))
        await self._server_writer.drain()

    async def _on_server_msg(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "runcmd":
            await self._exec_runcmd(msg)
        elif t == "auth_ok":
            if not msg.get("ok"):
                log.error("server 认证失败，请检查 token")
        # XMPP 端不参与权限机制，其余消息忽略

    async def _exec_runcmd(self, msg: dict) -> None:
        task_id = msg["task_id"]
        cmd = msg["cmd"]
        log.info("执行 runcmd: %s", cmd)
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode("utf-8", errors="replace")
        await self._server_send(make_runcmd_result(task_id, proc.returncode == 0, output))

    # ---------- 主入口 ----------

    async def run(self) -> None:
        xmpp_cfg = self.cfg.get("xmpp", {})
        attempt = 0
        while True:
            try:
                # slixmpp connect() 返回 Future，内部会自旋重连直到成功；
                # 初次连接失败后其 _connect_loop 会持续重试，这里用 disconnected 等待
                if xmpp_cfg.get("host"):
                    self.connect((xmpp_cfg["host"], int(xmpp_cfg.get("port", 5222))))
                else:
                    self.connect()
                # XMLStream.disconnected 是文档化的 Future：断开时完成
                await self.disconnected
            except (ConnectionError, OSError) as e:
                log.warning("XMPP 连接异常: %s", e)
            delay = backoff_delay(attempt)
            await asyncio.sleep(delay)
            attempt += 1
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_xmpp_client.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交（可选）**

```bash
git add src/ircxmppbot/xmpp_client.py tests/test_xmpp_client.py
git commit -m "feat: XMPP 客户端（slixmpp，仅 chat）"
```

---

### Task 11: __main__.py（CLI 入口）+ 样例配置文件

**Files:**
- Create: `src/ircxmppbot/__main__.py`
- Create: `configs/server.yaml`
- Create: `configs/client_irc.yaml`
- Create: `configs/client_xmpp.yaml`
- Create: `tests/test_main.py`（轻量冒烟测试）

**Interfaces:**
- Consumes: `BotServer.run()`、`IRCBot.run()`、`XMPPBot.run()`、`load_yaml`
- Produces:
  - `python -m ircxmppbot server [config]` — 默认 `configs/server.yaml`
  - `python -m ircxmppbot client-irc [config]` — 默认 `configs/client_irc.yaml`
  - `python -m ircxmppbot client-xmpp [config]` — 默认 `configs/client_xmpp.yaml`
  - `-d/--debug` 开启 DEBUG 日志

- [ ] **Step 1: 写失败测试**

`tests/test_main.py`:

```python
from pathlib import Path

from ircxmppbot.config import load_yaml
from ircxmppbot.protocol import decode_msg, encode_msg, make_auth


def test_sample_server_config_loads():
    cfg = load_yaml(Path("configs/server.yaml"))
    assert "server" in cfg and "permissions" in cfg
    assert "listen_port" in cfg["server"]
    assert "whitelist" in cfg["permissions"] and "blacklist" in cfg["permissions"]


def test_sample_irc_config_loads():
    cfg = load_yaml(Path("configs/client_irc.yaml"))
    assert cfg["client"]["type"] == "irc"
    assert cfg["client"]["bot_name"]
    assert "irc" in cfg and "llm" in cfg
    assert "realname" in cfg["irc"]
    assert cfg["irc"]["channels"]


def test_sample_xmpp_config_loads():
    cfg = load_yaml(Path("configs/client_xmpp.yaml"))
    assert cfg["client"]["type"] == "xmpp"
    assert "xmpp" in cfg and "llm" in cfg
    assert "jid" in cfg["xmpp"]


def test_protocol_auth_roundtrip():
    msg = make_auth("token", "n", "irc", "bot")
    assert decode_msg(encode_msg(msg).decode("utf-8")) == msg
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_main.py -v`
Expected: FAIL（configs 文件不存在 → FileNotFoundError → ConfigError）

- [ ] **Step 3: 实现 __main__.py**

```python
"""CLI 入口：python -m ircxmppbot server|client-irc|client-xmpp [config]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .config import load_yaml

log = logging.getLogger(__name__)

DEFAULT_CONFIGS = {
    "server": "configs/server.yaml",
    "client-irc": "configs/client_irc.yaml",
    "client-xmpp": "configs/client_xmpp.yaml",
}


def _setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


async def _run_server(config_path: Path) -> None:
    from .server import BotServer

    cfg = load_yaml(config_path)
    server = BotServer(config_path)
    server.reload(cfg)
    log.info("server 启动（配置 %s）", config_path)
    await server.run()


async def _run_client(config_path: Path, kind: str) -> None:
    if kind == "irc":
        from .irc_client import IRCBot

        await IRCBot(config_path).run()
    else:
        from .xmpp_client import XMPPBot

        await XMPPBot(config_path).run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ircxmppbot")
    sub = parser.add_subparsers(dest="mode", required=True)
    for name in ("server", "client-irc", "client-xmpp"):
        p = sub.add_parser(name)
        p.add_argument("config", nargs="?", default=DEFAULT_CONFIGS[name])
    parser.add_argument("-d", "--debug", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.debug)
    config_path = Path(args.config)
    try:
        if args.mode == "server":
            asyncio.run(_run_server(config_path))
        else:
            kind = args.mode.removeprefix("client-")
            asyncio.run(_run_client(config_path, kind))
    except KeyboardInterrupt:
        log.info("收到中断信号，退出")
    except Exception as e:  # noqa: BLE001
        log.error("运行失败: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 创建样例配置**

`configs/server.yaml`:

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

permissions:
  botop:
    - "user1@host.example"
  shellop:
    - "user2@host.example"
  whitelist:
    enabled: false
    entries:
      - mask: "user1!*@*.example"
        channel: "#chan1"
  blacklist:
    enabled: false
    entries:
      - mask: "bad!*@*"
```

`configs/client_irc.yaml`:

```yaml
client:
  name: irc_main
  type: irc
  bot_name: qsdwindows_bot
  server:
    host: 127.0.0.1
    port: 8443
    token: "change-me-shared-secret"
    tls:
      verify: false

irc:
  host: irc.example.com
  port: 6697
  tls: true
  tls_verify: true
  realname: "My IRC Bot"
  channels:
    - "#chan1"

llm:
  format: openai_chat
  base_url: "https://api.openai.com/v1"
  api_key: "sk-xxx"
  model: "gpt-4o-mini"
  system_prompt: "You are a helpful IRC bot."
  temperature: 0.7
  max_tokens: 1000
  timeout: 60
  max_reply_chars: 1500
```

`configs/client_xmpp.yaml`:

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
  password: "change-me"
  host: "snikket.example"
  port: 5222
  tls: true
  mucs:
    - "room@conference.snikket.example"

llm:
  format: openai_chat
  base_url: "https://api.openai.com/v1"
  api_key: "sk-xxx"
  model: "gpt-4o-mini"
  system_prompt: "You are a helpful XMPP bot."
  temperature: 0.7
  max_tokens: 1000
  timeout: 60
  max_reply_chars: 1500
```

- [ ] **Step 5: 运行确认通过 + CLI 冒烟**

Run: `pytest tests/test_main.py -v`
Expected: PASS（4 passed）

```bash
python -m ircxmppbot --help
```
Expected: 显示三个子命令 server/client-irc/client-xmpp

```bash
python -m ircxmppbot client-irc configs/client_irc.yaml -d
```
Expected: 启动后尝试连接 IRC 并进入退避重连（Ctrl+C 退出）——此步骤验证配置加载与入口链路，可观察日志后中断

- [ ] **Step 6: 提交（可选）**

```bash
git add src/ircxmppbot/__main__.py configs tests/test_main.py
git commit -m "feat: CLI 入口与样例配置"
```

---

### Task 12: 全量测试回归 + README

**Files:**
- Create: `README.md`

**Interfaces:**
- Consumes: 全部模块
- Produces: 可运行的项目说明文档

- [ ] **Step 1: 全量测试回归**

Run: `pytest tests/ -v`
Expected: 全部 PASS（util 10 + config 5 + protocol 12 + permissions 10 + commands 8 + llm 10 + server 12 + irc_client 14 + xmpp_client 7 + main 4 = 92 passed）

- [ ] **Step 2: 创建 README.md**

```markdown
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
```

- [ ] **Step 3: 提交（可选）**

```bash
git add README.md
git commit -m "docs: README 与全量测试回归"
```

---

## 自审清单

**1. Spec 覆盖核查：**

| 设计文档要求 | 对应任务 |
|---|---|
| TLS 三条链路 | Task 8 (server ssl)、Task 9/10 (client ssl)、IRC/XMPP tls 配置 |
| 三种 LLM 格式 | Task 7（openai_chat/openai_responses/anthropic） |
| 配置热重载（除连接参数） | Task 3 ConfigWatcher + Task 8 reload + 各 client 配置 |
| IRC 完整命令集 | Task 6 注册表 + Task 8 server 命令处理器 + Task 9 client 分发 |
| oper WHOIS 313 检测 | Task 9 `_whois_user` + Task 8 oper_cache |
| 黑白名单（全局+频道、互斥、豁免、JOIN 踢人） | Task 5 模型 + Task 8 管理 + Task 9 `_enforce_lists` |
| botop give/remove（仅 oper） | Task 8 `_cmd_botop` |
| shellop 确认流程（botop+oper 候选人、全员同意、超时、发起者自动同意） | Task 8 `_cmd_shellop`/`_handle_vote`/`_finalize_proposal`/`_proposal_timer` |
| runcmd 跨 client + 结果私信 | Task 8 `_cmd_runcmd` + Task 9/10 `_exec_runcmd` |
| XMPP 仅 chat（私聊免前缀） | Task 10 `_handle_private` |
| server+client 结构 + token 认证 | Task 8 认证 + Task 9/10 auth |
| 唤醒词 `!<bot_name>` + realname 独立配置 | Task 6 parse + Task 9/10 配置 |

**2. 占位符扫描：** 所有步骤含完整代码与预期输出，无 TBD/TODO。

**3. 类型/签名一致性：** `make_*` 构造器、`Permissions` 方法、`_cmd_*` 签名在 server 测试与实现间一致；`_reply(conn, ok, reply, channel, caller, ...)` 调用处统一。

**4. 已知边界（实现时注意）：**
- shellop 相关测试（`test_shellop_full_confirm_flow`、`test_shellop_reject_flow`、`test_botop_give_persists`）须先把 `conn` 注册进 `srv.conns`，否则广播（proposal/permission_update）不会到达 `conn.sent`——计划中已包含该注册
- `_cmd_runcmd` 的 `runcmd_pending` future 在测试中由 `_handle_runcmd_result` 手动完成
- pydle 无 `disconnected` 属性：IRC `run()` 连接成功后用 `await asyncio.Event().wait()` 保持循环，重连由 pydle 内部（RECONNECT_ON_ERROR）处理；slixmpp 的 `XMLStream.disconnected` 是文档化的 Future，XMPP `run()` 通过 `await self.disconnected` 循环重连
- ngIRCd 自动 op 依赖服务器配置（OperChanPAutoOp 或服务），本代码不主动获取 op
- confirm/reject 在 IRC client 走 `_server_vote`（发 `vote` 消息），不走 `command`——server 端无 `_cmd_confirm/_cmd_reject` 处理器
