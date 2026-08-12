# server↔client TLS 可选统一判定 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 统一 server↔client 链路的 TLS 启用判定（显式 `tls.enabled` 开关）、缺证书启动报错、TLS 不匹配时日志提示。

**Architecture:** `util.py` 新增共享 `tls_enabled()` 判定函数；`server.py`/`server_link.py` 各自提取 `build_*_ssl_ctx()` 构建函数并在启动/连接时调用；`_handle_client`/`_server_loop` 在 `ssl.SSLError` 时输出可操作日志提示。

**Tech Stack:** Python 3.14+ asyncio + ssl；pytest（pytest-asyncio auto 模式）；YAML 配置。

## Global Constraints

- 测试/运行命令一律用 `/tmp/opencode/ircxmppbot-venv/bin/pytest`（项目在 exFAT，本地不可建 venv）
- 不加新依赖；不动 `protocol.py`（6 种消息不变）；不动 IRC/XMPP 链路 TLS（`irc.tls`/`xmpp.tls`）
- `tls` 段缺失 → 明文；`tls.enabled: false` → 明文；`tls` 段存在无 `enabled` → 启用（向后兼容）
- 每个任务一个 commit，消息前缀 `feat:`/`fix:`/`docs:`
- 用户偏好 Inline 执行，不使用子代理

---

### Task 1: `util.py` 新增 `tls_enabled()` 共享判定

**Files:**
- Modify: `src/ircxmppbot/util.py`（末尾追加函数）
- Test: `tests/test_util.py`（修改 import 行 + 追加测试）

**Interfaces:**
- Produces: `tls_enabled(tls_cfg: dict | None) -> bool` — 无段→False；段存在→`bool(cfg.get("enabled", True))`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_util.py` 末尾）

```python
def test_tls_enabled_missing_section():
    assert tls_enabled(None) is False
    assert tls_enabled({}) is False


def test_tls_enabled_section_default_true():
    assert tls_enabled({"verify": False}) is True


def test_tls_enabled_explicit_false():
    assert tls_enabled({"enabled": False}) is False


def test_tls_enabled_explicit_true():
    assert tls_enabled({"enabled": True, "verify": False}) is True
```

同时修改文件顶部 import 行：

```python
from ircxmppbot.util import (
    backoff_delay, match_mask, parse_userhost, split_text, split_text_bytes, tls_enabled,
)
```

- [ ] **Step 2: 运行确认失败**

Run: `/tmp/opencode/ircxmppbot-venv/bin/pytest tests/test_util.py -q`
Expected: `ImportError: cannot import name 'tls_enabled'`

- [ ] **Step 3: 最小实现**（追加到 `src/ircxmppbot/util.py` 末尾）

```python
def tls_enabled(tls_cfg: dict | None) -> bool:
    """server↔client 链路 TLS 启用判定：无段→False；段存在→默认启用，enabled:false 显式禁用。"""
    if not tls_cfg:
        return False
    return bool(tls_cfg.get("enabled", True))
```

- [ ] **Step 4: 运行确认通过**

Run: `/tmp/opencode/ircxmppbot-venv/bin/pytest tests/test_util.py -q`
Expected: `19 passed`（原 14 + 新 4，注：若前次运行已加过则数量相应）

- [ ] **Step 5: 提交**

```bash
git add src/ircxmppbot/util.py tests/test_util.py
git commit -m "feat(util): tls_enabled() 统一 TLS 启用判定"
```

---

### Task 2: `server.py` 用统一判定 + 缺证书启动报错 + 握手失败日志提示

**Files:**
- Modify: `src/ircxmppbot/server.py` — import 行、`run()` 的 TLS 构建（104-116 行）、`_handle_client` 的 except 分支（181 行）
- Test: `tests/test_server.py` — import 行 + 追加测试

**Interfaces:**
- Consumes: `tls_enabled` from `.util`
- Produces: `build_server_ssl_ctx(tls_cfg: dict | None, config_dir: Path) -> ssl.SSLContext | None` — 未启用返回 None；启用但缺 `certfile` 抛 `ConfigError`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_server.py` 末尾）

```python
# ---------- TLS 可选（统一判定） ----------

class SSLFailReader:
    async def readline(self):
        raise ssl.SSLError("wrong version number")


class FakeWriter:
    def __init__(self):
        self.closed = False
        self.sent = []

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None

    def write(self, data):
        self.sent.append(data)

    async def drain(self):
        return None


class FakeTCPServer:
    async def serve_forever(self):
        return None


def test_build_server_ssl_ctx_disabled():
    assert build_server_ssl_ctx(None, Path(".")) is None
    assert build_server_ssl_ctx({"enabled": False}, Path(".")) is None


def test_build_server_ssl_ctx_missing_certfile():
    with pytest.raises(ConfigError):
        build_server_ssl_ctx({"enabled": True}, Path("."))


def test_build_server_ssl_ctx_ok(monkeypatch, tmp_path):
    def fake_load(self, certfile, keyfile=None):
        self._loaded = (certfile, keyfile)

    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", fake_load)
    (tmp_path / "server.crt").write_text("crt")
    (tmp_path / "server.key").write_text("key")
    ctx = build_server_ssl_ctx(
        {"enabled": True, "certfile": "server.crt", "keyfile": "server.key"}, tmp_path
    )
    assert ctx is not None
    assert ctx._loaded == (str(tmp_path / "server.crt"), str(tmp_path / "server.key"))


async def test_run_tls_disabled_listens_plain(monkeypatch, tmp_path):
    server = BotServer(_write_cfg(tmp_path))
    server.reload(load_yaml(server.config_path))
    captured = {}

    async def fake_start_server(handler, host, port, ssl=None):
        captured["ssl"] = ssl
        return FakeTCPServer()

    async def fake_watcher_run(self):
        return None

    def fake_watcher_stop(self):
        return None

    monkeypatch.setattr(asyncio, "start_server", fake_start_server)
    monkeypatch.setattr(ConfigWatcher, "run", fake_watcher_run)
    monkeypatch.setattr(ConfigWatcher, "stop", fake_watcher_stop)
    await server.run()
    assert captured["ssl"] is None


async def test_run_tls_enabled_missing_certfile_raises(tmp_path):
    cfg = load_yaml(_write_cfg(tmp_path))
    cfg["server"]["tls"] = {"enabled": True}
    p = tmp_path / "server.yaml"
    p.write_text(_yaml(cfg), encoding="utf-8")
    server = BotServer(p)
    server.reload(load_yaml(server.config_path))
    with pytest.raises(ConfigError):
        await server.run()


async def test_handle_client_tls_mismatch_logs_hint(srv, caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        await srv._handle_client(SSLFailReader(), FakeWriter())
    assert "tls.enabled" in caplog.text
```

同时修改文件顶部 import 行：

```python
import asyncio
import logging
import ssl
import time
from pathlib import Path

import pytest

from ircxmppbot.config import ConfigError, ConfigWatcher, load_yaml
from ircxmppbot.protocol import make_runcmd_result
from ircxmppbot.server import BotServer, build_server_ssl_ctx
```

注：`logging` 与 `ssl` 若原文件已 import 则不重复（原文件现有 `import asyncio/time`、`from ircxmppbot.config import load_yaml`、`from ircxmppbot.server import BotServer`；`ssl` 是新增、`logging` 仅在测试内 import 可接受——Step 3 后如 `_yaml`/`_write_cfg` 已有引用则统一在顶部）。

- [ ] **Step 2: 运行确认失败**

Run: `/tmp/opencode/ircxmppbot-venv/bin/pytest tests/test_server.py -q`
Expected: FAIL——`ImportError: cannot import name 'build_server_ssl_ctx'`

- [ ] **Step 3: 实现 `build_server_ssl_ctx` + 接入 `run()` + `_handle_client` 日志提示**

3a. `src/ircxmppbot/server.py` 顶部 import 行修改：

```python
from .util import parse_userhost, tls_enabled
```

3b. 在 `BotServer` 类之前（`AUTH_TIMEOUT` 常量之后）新增模块级函数：

```python
def build_server_ssl_ctx(tls_cfg: dict | None, config_dir: Path) -> ssl.SSLContext | None:
    """构建 server 端 SSLContext；TLS 未启用返回 None。启用但缺 certfile 抛 ConfigError。"""
    if not tls_enabled(tls_cfg):
        return None
    if not tls_cfg.get("certfile"):
        raise ConfigError("server tls 已启用但缺少 certfile")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    certfile = tls_cfg["certfile"]
    keyfile = tls_cfg.get("keyfile")
    if not Path(certfile).is_absolute():
        certfile = str(config_dir / certfile)
    if keyfile and not Path(keyfile).is_absolute():
        keyfile = str(config_dir / keyfile)
    ctx.load_cert_chain(certfile, keyfile)
    return ctx
```

3c. `run()` 内替换原 TLS 构建逻辑（原 104-116 行）：

```python
        ssl_ctx = build_server_ssl_ctx(srv.get("tls"), self.config_path.parent)
        server = await asyncio.start_server(self._handle_client, host, port, ssl=ssl_ctx)
        log.info("server 监听 %s:%s (TLS=%s)", host, port, ssl_ctx is not None)
```

3d. `_handle_client` 的 except 分支（原 181 行）——在现有 `except (ConnectionError, ...)` **之前**插入 `ssl.SSLError` 分支（注意 `ssl.SSLError` 是 `OSError` 子类，顺序必须在前面）：

```python
        except ssl.SSLError as e:
            log.warning("TLS 握手失败（client 可能未启用 TLS，请检查两端 tls.enabled 配置是否一致）: %s", e)
        except (ConnectionError, asyncio.IncompleteReadError, TimeoutError, OSError):
            pass
```

- [ ] **Step 4: 运行确认通过**

Run: `/tmp/opencode/ircxmppbot-venv/bin/pytest tests/test_server.py -q`
Expected: `26 passed`（原 20 + 新 6）

- [ ] **Step 5: 提交**

```bash
git add src/ircxmppbot/server.py tests/test_server.py
git commit -m "feat(server): TLS 统一判定 + 缺证书启动报错 + 握手失败提示"
```

---

### Task 3: `server_link.py` 用统一判定 + `ssl.SSLError` 日志提示

**Files:**
- Modify: `src/ircxmppbot/server_link.py` — import 行、`_server_connect` 的 TLS 构建（58-67 行）、`_server_loop` 的 except 分支（51 行）
- Test: `tests/test_server_link.py` — import 行 + 追加测试

**Interfaces:**
- Consumes: `tls_enabled` from `.util`
- Produces: `build_client_ssl_ctx(tls_cfg: dict | None) -> ssl.SSLContext | None` — 未启用返回 None

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_server_link.py` 末尾）

```python
# ---------- TLS 可选（统一判定） ----------

class _BreakLoop(Exception):
    pass


def test_build_client_ssl_ctx_disabled():
    assert build_client_ssl_ctx(None) is None
    assert build_client_ssl_ctx({}) is None
    assert build_client_ssl_ctx({"enabled": False}) is None


def test_build_client_ssl_ctx_default_enabled():
    ctx = build_client_ssl_ctx({"verify": False})
    assert ctx is not None
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


async def test_server_loop_tls_mismatch_logs_hint(link, monkeypatch, caplog):
    import logging

    async def fake_connect(cfg):
        raise ssl.SSLError("wrong version number")

    async def fake_sleep(delay):
        raise _BreakLoop()

    monkeypatch.setattr(link, "_server_connect", fake_connect)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(_BreakLoop):
            await link._server_loop()
    assert "TLS 握手失败" in caplog.text
```

同时修改文件顶部 import 行：

```python
import asyncio
import logging
import ssl
import time
from pathlib import Path

import pytest

from ircxmppbot.server_link import ServerLink, build_client_ssl_ctx
```

注：原文件已有 `import asyncio/time`、`from pathlib import Path`、`import pytest`、`from ircxmppbot.server_link import ServerLink`；新增 `logging`/`ssl`（`logging` 在测试内 import 也可，保持与 Task 2 一致风格）。

- [ ] **Step 2: 运行确认失败**

Run: `/tmp/opencode/ircxmppbot-venv/bin/pytest tests/test_server_link.py -q`
Expected: FAIL——`ImportError: cannot import name 'build_client_ssl_ctx'`

- [ ] **Step 3: 实现 `build_client_ssl_ctx` + 接入 `_server_connect` + `_server_loop` 日志提示**

3a. `src/ircxmppbot/server_link.py` 顶部 import 行修改：

```python
from .util import backoff_delay, parse_userhost, tls_enabled
```

3b. 在 `log = logging.getLogger(__name__)` 之后（`RootSession` 定义之前）新增模块级函数：

```python
def build_client_ssl_ctx(tls_cfg: dict | None) -> ssl.SSLContext | None:
    """构建 client 端 SSLContext；TLS 未启用返回 None。"""
    if not tls_enabled(tls_cfg):
        return None
    ctx = ssl.create_default_context()
    verify = tls_cfg.get("verify", True)
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if tls_cfg.get("ca_cert"):
        ctx.load_verify_locations(tls_cfg["ca_cert"])
    return ctx
```

3c. `_server_connect` 内替换原 TLS 构建逻辑（原 57-70 行）：

```python
    async def _server_connect(self, srv_cfg: dict) -> None:
        ssl_ctx = build_client_ssl_ctx(srv_cfg.get("tls"))
        reader, writer = await asyncio.open_connection(
            srv_cfg["host"], int(srv_cfg["port"]), ssl=ssl_ctx
        )
```

3d. `_server_loop` 的 except 分支（原 47-55 行）——在现有 except **之前**插入 `ssl.SSLError` 分支：

```python
        while True:
            try:
                await self._server_connect(srv_cfg)
                attempt = 0
            except ssl.SSLError as e:
                log.warning("TLS 握手失败（server 可能未启用 TLS，或两端 tls 配置不匹配）: %s", e)
                delay = backoff_delay(attempt)
                await asyncio.sleep(delay)
                attempt += 1
            except (ConnectionError, OSError, asyncio.IncompleteReadError, TimeoutError) as e:
                delay = backoff_delay(attempt)
                log.warning("server 连接失败: %s，%.0fs 后重连", e, delay)
                await asyncio.sleep(delay)
                attempt += 1
```

- [ ] **Step 4: 运行确认通过**

Run: `/tmp/opencode/ircxmppbot-venv/bin/pytest tests/test_server_link.py -q`
Expected: `12 passed`（原 9 + 新 3）

- [ ] **Step 5: 提交**

```bash
git add src/ircxmppbot/server_link.py tests/test_server_link.py
git commit -m "feat(client): TLS 统一判定 + 握手失败提示"
```

---

### Task 4: 配置示例 + 文档 + 全量回归

**Files:**
- Modify: `configs/server.yaml`、`configs/client.yaml`、`README.md`、`AGENTS.md`

**Interfaces:**
- Consumes: 前 3 任务的实现结果（`tls.enabled` 字段语义）

- [ ] **Step 1: server.yaml 的 `tls` 段加注释**

`configs/server.yaml` 的 `tls:` 段改为：

```yaml
  tls:
    certfile: server.crt
    keyfile: server.key
    # enabled: true   # 默认启用；设 false 则走明文（不启用 TLS）
```

- [ ] **Step 2: client.yaml 的 `server.tls` 段加注释**

`configs/client.yaml` 的 `tls:` 段改为：

```yaml
    tls:
      verify: false
      # enabled: true   # 默认启用；设 false 则走明文（不启用 TLS，需与 server 一致）
```

- [ ] **Step 3: README 功能列表更新**

`README.md` 中 `- TLS 连接（IRC / XMPP / server↔client 三条链路）` 改为：

```markdown
- TLS 连接：IRC/XMPP 各自配置；server↔client 可选（`tls.enabled` 控制，默认启用，`enabled: false` 走明文）
```

- [ ] **Step 4: AGENTS.md 架构段补充**

`AGENTS.md` 架构段（server.py 条目附近）新增：

```markdown
- server↔client TLS 可选：`tls.enabled` 统一判定（默认启用，`enabled: false` 走明文；server 缺 certfile 启动报错；握手失败日志提示检查两端配置）
```

- [ ] **Step 5: 全量回归**

Run: `/tmp/opencode/ircxmppbot-venv/bin/pytest tests/ -q`
Expected: `124 passed`（118 + 新 6：Task1 4 + Task2 6 + Task3 3 中与预期差异按实际为准，应 ≥124 且 0 failed）

- [ ] **Step 6: 提交**

```bash
git add configs/server.yaml configs/client.yaml README.md AGENTS.md
git commit -m "docs(config): tls.enabled 配置示例与 TLS 可选说明"
```

---

## Self-Review

**Spec 覆盖核对：**

| Spec 需求 | 实现任务 |
|---|---|
| `tls_enabled()` 共享判定 | Task 1 |
| server 用 `tls_enabled()` 构建 | Task 2（3b/3c） |
| client 用 `tls_enabled()` 构建 | Task 3（3b/3c） |
| server 缺 certfile 启动抛 ConfigError | Task 2（3b，测试 `test_build_server_ssl_ctx_missing_certfile`/`test_run_tls_enabled_missing_certfile_raises`） |
| server 握手失败日志提示 | Task 2（3d，测试 `test_handle_client_tls_mismatch_logs_hint`） |
| client `ssl.SSLError` 日志提示 | Task 3（3d，测试 `test_server_loop_tls_mismatch_logs_hint`） |
| 配置示例 | Task 4（Step 1/2） |
| README/AGENTS.md | Task 4（Step 3/4） |
| 不改协议/IRC/XMPP 链路 | 全计划未触碰 protocol.py 与 irc_session/xmpp_session |

**类型一致性：** `tls_enabled(tls_cfg: dict | None) -> bool` 在 Task 1 定义、Task 2/3 消费，签名一致；`build_server_ssl_ctx(tls_cfg, config_dir)` 与 `build_client_ssl_ctx(tls_cfg)` 各任务内定义与测试一致。
