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
