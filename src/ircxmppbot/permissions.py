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
            if e.get("channel") and e["channel"].casefold() != (channel or "").casefold():
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
