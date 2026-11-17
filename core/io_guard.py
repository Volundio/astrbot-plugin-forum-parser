from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp

from .models import ForumParseError

# 网络响应体积硬上限（防止异常大页面或恶意响应导致内存溢出）
MAX_HTML_JSON_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_RENDERED_IMAGE_BYTES = 35 * 1024 * 1024  # 35 MB（适配 10MB 内嵌图片与完整主楼长图截图）
STREAM_CHUNK_SIZE = 65536  # 64 KB 流式块大小


class ResponseTooLargeError(ForumParseError):
    """远端响应体积超出内存安全阈值异常。"""


class ConcurrencyLimitError(ForumParseError):
    """并发排队任务过多保护异常。"""


def sniff_known_raster_mime(head: bytes) -> str | None:
    """根据文件头魔数严格识别常见位图格式 MIME（无法识别时返回 None，用于防 HTML 伪图污染）。"""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in {b"avif", b"avis"}:
        return "image/avif"
    if len(head) >= 14 and head[:2] == b"BM":
        return "image/bmp"
    return None


def sniff_image_mime(head: bytes, fallback: str = "image/jpeg") -> str:
    """根据文件头魔数识别图片 MIME 类型。"""
    return sniff_known_raster_mime(head) or fallback


def mime_to_ext(mime: str) -> str:
    m = (mime or "").lower()
    if "png" in m:
        return "png"
    if "gif" in m:
        return "gif"
    if "webp" in m:
        return "webp"
    if "avif" in m:
        return "avif"
    if "bmp" in m:
        return "bmp"
    return "jpg"


async def read_response_bounded(
    resp: aiohttp.ClientResponse,
    *,
    max_bytes: int = MAX_HTML_JSON_RESPONSE_BYTES,
) -> bytes:
    """按块安全读取 HTTP 响应体至内存，超过 max_bytes 立即中止并抛错以防 OOM。"""
    cl_header = resp.headers.get("Content-Length")
    if cl_header and cl_header.isdigit() and int(cl_header) > max_bytes:
        raise ResponseTooLargeError(
            f"响应体积 ({int(cl_header)} bytes) 超过安全上限 ({max_bytes} bytes)"
        )

    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.content.iter_chunked(STREAM_CHUNK_SIZE):
        total += len(chunk)
        if total > max_bytes:
            chunks.clear()
            raise ResponseTooLargeError(f"流式响应超过安全上限 ({max_bytes} bytes)")
        chunks.append(chunk)
    return b"".join(chunks)


async def stream_response_to_file(
    resp: aiohttp.ClientResponse,
    dest_path: Path,
    *,
    max_bytes: int = MAX_RENDERED_IMAGE_BYTES,
) -> tuple[int, str]:
    """将 HTTP 二进制响应按 64KB 分块直接流式落盘（不在内存缓存完整文件），原子替换目标路径。

    若超过 max_bytes、连接中断或协程取消，立即删除半成品 .tmp 文件，确保零垃圾残留与低内存峰值。

    Returns:
        (写入总字节数, 识别出的 MIME 类型)
    """
    cl_header = resp.headers.get("Content-Length")
    if cl_header and cl_header.isdigit() and int(cl_header) > max_bytes:
        raise ResponseTooLargeError(
            f"文件体积 ({int(cl_header)} bytes) 超过落盘安全上限 ({max_bytes} bytes)"
        )

    await asyncio.to_thread(dest_path.parent.mkdir, parents=True, exist_ok=True)
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")

    total = 0
    head_bytes = b""
    fd: int | None = None

    def _open_tmp() -> int:
        return os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)

    def _close_and_cleanup(file_desc: int | None) -> None:
        if file_desc is not None:
            try:
                os.close(file_desc)
            except OSError:
                pass
        tmp_path.unlink(missing_ok=True)
        dest_path.unlink(missing_ok=True)

    try:
        fd = await asyncio.to_thread(_open_tmp)
        async for chunk in resp.content.iter_chunked(STREAM_CHUNK_SIZE):
            if not chunk:
                continue
            if len(head_bytes) < 16:
                need = 16 - len(head_bytes)
                head_bytes += chunk[:need]
            total += len(chunk)
            if total > max_bytes:
                raise ResponseTooLargeError(f"流式写入文件超过安全上限 ({max_bytes} bytes)")
            await asyncio.to_thread(os.write, fd, chunk)

        if total == 0:
            raise ForumParseError("远端返回了空的媒体响应内容")

        os.close(fd)
        fd = None
        await asyncio.to_thread(os.replace, tmp_path, dest_path)

        header_ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        mime = sniff_image_mime(head_bytes, fallback=header_ctype or "image/jpeg")
        return total, mime
    except BaseException:
        await asyncio.to_thread(_close_and_cleanup, fd)
        raise


class BoundedTaskLimiter:
    """全局并发与排队背压控制器，防止瞬间大量链接触发并发拉图导致内存峰值叠加溢出。"""

    def __init__(self, max_concurrent: int = 2, max_waiting: int = 6) -> None:
        self._max_concurrent = max(1, max_concurrent)
        self._max_waiting = max(1, max_waiting)
        self._sem = asyncio.Semaphore(self._max_concurrent)
        self._waiting = 0

    def update_limits(self, max_concurrent: int, max_waiting: int = 6) -> None:
        new_c = max(1, max_concurrent)
        self._max_waiting = max(1, max_waiting)
        if new_c != self._max_concurrent and self._waiting == 0:
            self._max_concurrent = new_c
            self._sem = asyncio.Semaphore(new_c)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        if self._waiting >= self._max_waiting:
            raise ConcurrencyLimitError("当前正在解析的帖子较多，请稍后再试。")
        self._waiting += 1
        try:
            await self._sem.acquire()
        finally:
            self._waiting -= 1
        try:
            yield
        finally:
            self._sem.release()
