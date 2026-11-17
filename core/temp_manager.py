from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Generic, TypeVar

from astrbot.api import logger

K = TypeVar("K")
V = TypeVar("V")


def _is_safe_managed_temp_path(path: Path, plugin_temp_dir: Path) -> bool:
    """校验路径是否为受管控的临时渲染文件或目录，防止误删非临时文件。"""
    try:
        resolved = path.resolve(strict=False)
        plugin_root = plugin_temp_dir.resolve(strict=False)
        if resolved.is_relative_to(plugin_root):
            return True

        sys_tmp = Path(tempfile.gettempdir()).resolve(strict=False)
        if resolved.is_relative_to(sys_tmp):
            return True

        # AstrBot 内置 html_renderer 下载落盘的临时图片位于 data/temp/io_temp_img_*
        name = resolved.name.lower()
        parent_name = resolved.parent.name.lower()
        if parent_name in {"temp", "tmp", "render_cache"} and (
            name.startswith(("io_temp_img_", "forum_", "t2i_", "rendered_"))
            or name.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))
        ):
            return True
    except Exception:
        return False
    return False


def _calc_entry_size_sync(entry: Path) -> int:
    try:
        if entry.is_file():
            return entry.stat().st_size
        if entry.is_dir():
            return sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
    except OSError:
        return 0
    return 0


class TempScope:
    """单次解析/发送请求专属的临时文件作用域。

    所有落盘文件（含渲染长图与合并转发原图）均集中存放于独立的 scope 子目录下（外部 AstrBot 核心生成的
    io_temp_img_* 也同步登记到此作用域）；请求发送完毕或异常中断时调用 cleanup() 即可一次性彻底清空。
    """

    def __init__(self, manager: TempFileManager) -> None:
        self._manager = manager
        self.scope_id = f"scope_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        self.scope_dir = manager.temp_dir / self.scope_id
        self._external_files: list[Path] = []
        self._cleaned = False

    def alloc_file(self, prefix: str = "img", ext: str = "jpg") -> Path:
        """分配一个位于当前请求私有临时目录下的文件路径（调用方写入时懒创建目录）。"""
        clean_ext = ext.lstrip(".") or "jpg"
        path = self.scope_dir / f"{prefix}_{uuid.uuid4().hex[:8]}.{clean_ext}"
        self._manager.register(path)
        return path

    def track_file(self, file_path: str | Path | None) -> None:
        """将外部产生的临时文件（如 AstrBot html_renderer 的 io_temp_img_*）纳入本作用域托管。"""
        if not file_path:
            return
        registered = self._manager.register(file_path)
        if registered is not None and registered not in self._external_files:
            self._external_files.append(registered)

    async def cleanup(self) -> None:
        """幂等清理当前作用域下的全部落盘文件与子目录。"""
        if self._cleaned:
            return
        self._cleaned = True
        ext_files = list(self._external_files)
        self._external_files.clear()
        await self._manager.cleanup_scope(self.scope_dir, ext_files)


class TempFileManager:
    """临时文件全生命周期与磁盘配额管理器。

    保障策略：
    1. 支持按请求分配独立 TempScope 目录承载流式落盘文件，既防止大图常驻内存导致 OOM，又便于原子级整目录销毁；
    2. 追踪所有内部落盘文件与 AstrBot html_renderer 生成的 data/temp/io_temp_img_*，发送后立即删除；
    3. 启动自检清扫崩溃残留，运行期定时巡检同时执行 TTL 超时回收、文件数上限与磁盘总字节配额（默认 160MB）淘汰，停用时全量清空。
    """

    def __init__(
        self,
        temp_dir: Path,
        *,
        legacy_dirs: list[Path] | None = None,
        ttl_seconds: float = 180.0,
        max_dir_files: int = 60,
        max_dir_bytes: int = 160 * 1024 * 1024,
    ) -> None:
        self.temp_dir = temp_dir
        self.legacy_dirs = legacy_dirs or []
        self.ttl_seconds = ttl_seconds
        self.max_dir_files = max_dir_files
        self.max_dir_bytes = max_dir_bytes
        self._tracked: dict[Path, float] = {}

    @property
    def tracked_count(self) -> int:
        return len(self._tracked)

    def create_scope(self) -> TempScope:
        return TempScope(self)

    def register(self, file_path: str | Path | None) -> Path | None:
        """登记一个待清理的临时文件路径。"""
        if not file_path:
            return None
        p = Path(file_path).resolve(strict=False)
        if _is_safe_managed_temp_path(p, self.temp_dir):
            self._tracked[p] = time.monotonic()
            return p
        return None

    def _prune_empty_root_sync(self) -> None:
        try:
            if self.temp_dir.exists() and self.temp_dir.is_dir():
                if not any(self.temp_dir.iterdir()):
                    self.temp_dir.rmdir()
        except OSError:
            pass

    def _remove_file_sync(self, target: Path) -> bool:
        removed = False
        try:
            if target.is_file() or target.is_symlink():
                target.unlink(missing_ok=True)
                removed = True
        except OSError as e:
            logger.debug(f"删除临时文件失败 ({target}): {e}")

        # 若父级是空 scope 子目录，顺带移除
        try:
            parent = target.parent
            if (
                parent != self.temp_dir
                and parent.exists()
                and parent.is_dir()
                and parent.name.startswith("scope_")
                and not any(parent.iterdir())
            ):
                parent.rmdir()
        except OSError:
            pass

        self._prune_empty_root_sync()
        return removed

    def _resolve_and_remove_sync(self, raw_file_path: str | Path) -> bool:
        resolved = Path(raw_file_path).resolve(strict=False)
        was_tracked = self._tracked.pop(resolved, None) is not None
        if not was_tracked and not _is_safe_managed_temp_path(resolved, self.temp_dir):
            return False
        return self._remove_file_sync(resolved)

    async def remove_file(self, file_path: str | Path | None) -> bool:
        """立即异步删除指定临时文件（幂等安全，可重复调用）。"""
        if not file_path:
            return False
        return await asyncio.to_thread(self._resolve_and_remove_sync, file_path)

    def _cleanup_scope_sync(self, scope_dir: Path, external_files: list[Path]) -> None:
        resolved_scope = scope_dir.resolve(strict=False)
        for tracked_p in list(self._tracked.keys()):
            if tracked_p == resolved_scope or tracked_p.is_relative_to(resolved_scope):
                self._tracked.pop(tracked_p, None)

        if scope_dir.exists():
            shutil.rmtree(scope_dir, ignore_errors=True)

        for ext_p in external_files:
            self._tracked.pop(ext_p, None)
            if _is_safe_managed_temp_path(ext_p, self.temp_dir):
                try:
                    ext_p.unlink(missing_ok=True)
                except OSError:
                    pass

        self._prune_empty_root_sync()

    async def cleanup_scope(
        self, scope_dir: Path, external_files: list[Path] | None = None
    ) -> None:
        await asyncio.to_thread(self._cleanup_scope_sync, scope_dir, external_files or [])

    def _sweep_sync(
        self,
        expired_tracked: list[Path],
        max_age_seconds: float,
        max_files: int,
    ) -> int:
        deleted = 0

        # 1. 清理所有已超时的追踪路径（含位于 AstrBot data/temp 下的临时截图）
        for p in expired_tracked:
            try:
                if p.is_file() or p.is_symlink():
                    p.unlink(missing_ok=True)
                    deleted += 1
            except OSError:
                pass

        # 2. 清理历史遗留旧目录（例如旧版 render_cache）
        for leg_dir in self.legacy_dirs:
            if leg_dir.exists() and leg_dir.is_dir():
                shutil.rmtree(leg_dir, ignore_errors=True)

        # 3. 扫描插件临时目录，清理孤立文件/scope目录、半成品 .tmp 及超配额条目
        if self.temp_dir.exists() and self.temp_dir.is_dir():
            now_wall = time.time()
            survivors: list[tuple[float, int, Path]] = []

            for entry in list(self.temp_dir.iterdir()):
                try:
                    st = entry.stat()
                    age = now_wall - st.st_mtime
                    is_tmp = entry.name.endswith(".tmp")
                    if max_age_seconds <= 0 or is_tmp or age >= max_age_seconds:
                        if entry.is_dir():
                            shutil.rmtree(entry, ignore_errors=True)
                        else:
                            entry.unlink(missing_ok=True)
                        self._tracked.pop(entry.resolve(strict=False), None)
                        deleted += 1
                    else:
                        sz = _calc_entry_size_sync(entry)
                        survivors.append((st.st_mtime, sz, entry))
                except OSError:
                    continue

            # 按最旧优先顺序同时执行条目数上限与磁盘总字节配额（max_dir_bytes）淘汰
            survivors.sort(key=lambda item: item[0])
            total_bytes = sum(item[1] for item in survivors)
            while survivors and (
                (max_files >= 0 and len(survivors) > max_files)
                or (self.max_dir_bytes > 0 and total_bytes > self.max_dir_bytes)
            ):
                _, sz, victim = survivors.pop(0)
                total_bytes = max(0, total_bytes - sz)
                try:
                    if victim.is_dir():
                        shutil.rmtree(victim, ignore_errors=True)
                    else:
                        victim.unlink(missing_ok=True)
                    self._tracked.pop(victim.resolve(strict=False), None)
                    deleted += 1
                except OSError:
                    pass

            self._prune_empty_root_sync()

        return deleted

    async def sweep_expired(
        self,
        *,
        max_age_seconds: float | None = None,
        max_files: int | None = None,
    ) -> int:
        """执行一次过期与超额临时文件清扫，返回清理的条目数量。"""
        eff_age = self.ttl_seconds if max_age_seconds is None else max_age_seconds
        eff_max = self.max_dir_files if max_files is None else max_files

        now_mono = time.monotonic()
        expired_tracked: list[Path] = []
        for p, created_at in list(self._tracked.items()):
            if eff_age <= 0 or (now_mono - created_at) >= eff_age:
                expired_tracked.append(p)
                self._tracked.pop(p, None)

        return await asyncio.to_thread(self._sweep_sync, expired_tracked, eff_age, eff_max)

    def _purge_all_sync(self, all_tracked: list[Path]) -> int:
        deleted = self._sweep_sync(all_tracked, 0.0, 0)
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        return deleted

    async def purge_all(self) -> int:
        """无条件清空所有受追踪临时文件及临时目录（用于插件启动自检与停用卸载）。"""
        all_tracked = list(self._tracked.keys())
        self._tracked.clear()
        return await asyncio.to_thread(self._purge_all_sync, all_tracked)


class BoundedTTLCache(Generic[K, V]):
    """严格限制条目数、单条体积与总字节预算的轻量级内存 LRU 缓存。"""

    def __init__(
        self,
        *,
        max_entries: int = 8,
        ttl_seconds: float = 60.0,
        size_estimator: Callable[[V], int] | None = None,
        max_item_bytes: int = 128 * 1024,
        max_total_bytes: int = 1024 * 1024,
    ) -> None:
        self.max_entries = max(0, max_entries)
        self.ttl_seconds = ttl_seconds
        self._size_estimator = size_estimator
        self.max_item_bytes = max_item_bytes
        self.max_total_bytes = max_total_bytes
        self._store: OrderedDict[K, tuple[float, int, V]] = OrderedDict()
        self._total_bytes = 0

    def __len__(self) -> int:
        return len(self._store)

    def get(self, key: K) -> V | None:
        if self.max_entries <= 0 or self.ttl_seconds <= 0:
            return None
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, sz, val = entry
        if time.monotonic() >= expires_at:
            self._store.pop(key, None)
            self._total_bytes = max(0, self._total_bytes - sz)
            return None
        self._store.move_to_end(key)
        return val

    def set(self, key: K, value: V) -> None:
        if self.max_entries <= 0 or self.ttl_seconds <= 0:
            return
        est_size = self._size_estimator(value) if self._size_estimator else 0
        # 含 Base64 大图的重型对象直接拒绝常驻内存，仅缓存纯文本轻量结果以杜绝内存堆积
        if est_size > self.max_item_bytes or est_size > (self.max_total_bytes // 2):
            return

        self.prune_expired()
        if key in self._store:
            _, old_sz, _ = self._store.pop(key)
            self._total_bytes = max(0, self._total_bytes - old_sz)

        while self._store and (
            len(self._store) >= self.max_entries
            or (self._total_bytes + est_size) > self.max_total_bytes
        ):
            _, (_, evicted_sz, _) = self._store.popitem(last=False)
            self._total_bytes = max(0, self._total_bytes - evicted_sz)

        self._store[key] = (time.monotonic() + self.ttl_seconds, est_size, value)
        self._total_bytes += est_size

    def prune_expired(self) -> int:
        now = time.monotonic()
        removed = 0
        for k in list(self._store.keys()):
            expires_at, sz, _ = self._store[k]
            if now >= expires_at:
                self._store.pop(k, None)
                self._total_bytes = max(0, self._total_bytes - sz)
                removed += 1
        return removed

    def clear(self) -> None:
        self._store.clear()
        self._total_bytes = 0
