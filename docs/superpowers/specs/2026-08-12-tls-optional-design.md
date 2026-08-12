# 设计：server↔client TLS 可选统一判定

日期：2026-08-12
状态：已批准

## 背景

server↔client 的 TLS 连接当前**已可选**（未配 `tls` 段即明文），但判定逻辑两端不对称、缺证书时静默降级、不匹配时无明确提示：

| 端 | 当前判定 | 问题 |
|---|---|---|
| server（server.py:104-116） | `tls_cfg` 非空 **且** 有 `certfile` 才启用 TLS | 有 `keyfile` 无 `certfile` → 静默降级明文 |
| client（server_link.py:58-70） | `tls_cfg` 非空即启用 TLS | 与 server 判定不对称 |

TLS 不匹配（一端 TLS 一端明文）时表现为连接失败 + 无限重连，日志无指向"TLS 配置不一致"的提示。

## 目标

1. 两端统一 TLS 启用判定逻辑（显式 `tls.enabled` 开关，向后兼容）
2. 配置了 TLS 但缺证书 → 启动明确报错（不再静默降级）
3. TLS 不匹配时日志给出可操作提示（不改连接行为）

## 设计

### 1. 统一判定 `tls_enabled()`（共享 helper）

放 `src/ircxmppbot/util.py`，两端复用：

```python
def tls_enabled(tls_cfg: dict | None) -> bool:
    """server↔client 链路 TLS 启用判定：tls 段不存在→False；存在→默认启用，enabled:false 显式禁用。"""
    if not tls_cfg:
        return False
    return bool(tls_cfg.get("enabled", True))
```

- `server.py run()`：`tls_enabled(srv.get("tls"))` 为 True 才构建 server SSLContext
- `server_link.py _server_connect()`：`tls_enabled(srv_cfg.get("tls"))` 为 True 才构建 client SSLContext
- 消除现有不对称（server 看 `certfile` 存在、client 看段存在）

### 2. 启动校验：缺证书明确报错（server 端）

`tls_enabled()==True` 且无 `certfile` → 启动抛 `ConfigError`（"server tls 已启用但缺少 certfile"）。

- 安全改进：配置了 TLS 却悄悄降级明文是隐患，改为启动即失败
- 校验放 `run()` 中构建 SSLContext 之前

### 3. 不匹配时日志提示（仅日志，不改连接行为）

两端独立进程，只能运行时检测：

- **server 端**：`_handle_client` 中 TLS 握手失败（client 明文连 TLS server）→ 捕获 `ssl.SSLError`，log.warning 提示 "client 可能未启用 TLS，请检查两端 tls.enabled 配置是否一致"
- **client 端**：`_server_loop` 捕获 `ssl.SSLError` → 日志提示 "server 可能未启用 TLS，或两端 tls 配置不匹配"

连接行为不变（失败仍按现有重连策略重试）。

### 4. 配置示例与文档

- `configs/server.yaml`：`tls:` 段注释说明 `enabled` 字段
- `configs/client.yaml`：`server.tls` 段注释说明 `enabled` 字段
- `README.md`：功能列表改为 "TLS 可选（server↔client，tls.enabled 控制）"
- `AGENTS.md`：架构段补充 TLS 可选说明

## 错误处理

| 场景 | 行为 |
|---|---|
| `tls` 段缺失（两端） | 明文 TCP，日志 `TLS=False`（server 已有） |
| `tls.enabled: false` | 明文 TCP，与段缺失等价 |
| `tls` 段存在无 `enabled` | 启用 TLS（向后兼容现有配置） |
| server `tls.enabled` 为真但无 `certfile` | 启动抛 `ConfigError` |
| TLS 握手失败（任一方向不匹配） | log.warning 提示 + 按现有重连策略 |

## 测试

- `tls_enabled()` 单元测试：无段 / 段存在 / `enabled:false` / `enabled:true` 四分支
- server 启动：`enabled:false` → `start_server(ssl=None)`；`enabled:true` 无 `certfile` → 抛 `ConfigError`
- client 连接：`enabled:false` → `open_connection(ssl=None)`；段存在无 `enabled` → ssl 非 None
- 日志提示：mock 握手失败场景，断言日志含提示文案

## 范围

- 不改协议（protocol.py 6 种消息不变）
- 不改配置格式（纯增量字段 `tls.enabled`）
- 不动 IRC/XMPP 链路的 TLS（irc.tls / xmpp.tls 不受影响）
