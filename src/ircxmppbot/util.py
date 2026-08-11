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


def split_text_bytes(text: str, limit: int) -> list[str]:
    """按 UTF-8 字节数分片，不切断多字节字符（如中文 3 字节/字）。"""
    if not text or limit <= 0:
        return []
    chunks: list[str] = []
    current = ""
    current_bytes = 0
    for ch in text:
        ch_bytes = len(ch.encode("utf-8"))
        if current and current_bytes + ch_bytes > limit:
            chunks.append(current)
            current = ch
            current_bytes = ch_bytes
        else:
            current += ch
            current_bytes += ch_bytes
    if current:
        chunks.append(current)
    return chunks


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
