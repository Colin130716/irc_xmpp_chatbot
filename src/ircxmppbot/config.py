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
