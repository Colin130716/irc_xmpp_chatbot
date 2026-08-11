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
