from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp

from ..models import ForumLink, ParsedThread
from ..temp_manager import TempScope

DEFAULT_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def parse_cookie_string(cookie_str: str) -> dict[str, str]:
    """将浏览器复制的 Cookie 字符串解析为字典。"""
    cookies: dict[str, str] = {}
    if not cookie_str:
        return cookies
    for part in cookie_str.replace("\n", ";").split(";"):
        item = part.strip()
        if not item or "=" not in item:
            continue
        k, v = item.split("=", 1)
        k = k.strip()
        v = v.strip()
        if k and k.lower() not in {"path", "domain", "expires", "max-age", "samesite"}:
            cookies[k] = v
    return cookies


class BaseForumParser(ABC):
    """论坛解析器抽象基类。"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        kv_getter: Callable[[str, Any], Awaitable[Any]] | None = None,
        kv_setter: Callable[[str, Any], Awaitable[None]] | None = None,
    ) -> None:
        self.session = session
        self._kv_getter = kv_getter
        self._kv_setter = kv_setter

    async def get_state(self, key: str, default: Any = None) -> Any:
        if self._kv_getter is not None:
            try:
                return await self._kv_getter(key, default)
            except Exception:
                return default
        return default

    async def set_state(self, key: str, value: Any) -> None:
        if self._kv_setter is not None:
            try:
                await self._kv_setter(key, value)
            except Exception:
                pass

    @abstractmethod
    async def parse_thread(
        self,
        link: ForumLink,
        *,
        embed_images: bool = True,
        prefetch_data_uri: bool = True,
        max_inline_images: int = 15,
        enable_length_limit: bool = False,
        max_content_chars: int = 15000,
        temp_scope: TempScope | None = None,
        rehost_oversize_fn: Callable[[Path, str], Awaitable[str | None]] | None = None,
        max_inline_single_bytes: int | None = None,
        max_inline_total_bytes: int | None = None,
        max_download_single_bytes: int | None = None,
        max_download_total_bytes: int | None = None,
    ) -> ParsedThread:
        """解析帖子主楼并返回统一的 ParsedThread 结构。"""
        raise NotImplementedError
