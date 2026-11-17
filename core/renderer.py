from __future__ import annotations

import asyncio
import dataclasses
import inspect
import os
import struct
import tempfile
import uuid
from pathlib import Path
from typing import Any

import aiohttp
from astrbot.api import logger

from .io_guard import (
    MAX_RENDERED_IMAGE_BYTES,
    ResponseTooLargeError,
    read_response_bounded,
    stream_response_to_file,
)
from .models import ForumType, ParsedThread, RenderResult
from .temp_manager import TempFileManager, TempScope

TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "forum_card.html"
BANGUMI_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "bangumi_card.html"
DEFAULT_ASTRBOT_T2I_ENDPOINT = "https://t2i.soulter.top/text2img"

SCALE_FACTOR_VALUES = {
    "normal": 1.0,
    "high": 1.3,
    "ultra": 1.8,
}


def load_forum_card_template() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def load_bangumi_card_template() -> str:
    return BANGUMI_TEMPLATE_PATH.read_text(encoding="utf-8")


def _inspect_file_meta(file_path: str) -> tuple[int, int, str, int]:
    width, height, mime_type = inspect_image_dimensions(file_path)
    try:
        file_size = os.path.getsize(file_path)
    except OSError:
        file_size = 0
    return width, height, mime_type, file_size


def normalize_t2i_endpoint(url: str) -> str:
    """将用户配置的 T2I API 地址规范化为以 /text2img 结尾的基础路径。"""
    cleaned = (url or "").strip().rstrip("/")
    if not cleaned:
        return ""
    if cleaned.endswith("/text2img/generate"):
        cleaned = cleaned.removesuffix("/generate")
    elif not cleaned.endswith("/text2img"):
        cleaned = f"{cleaned}/text2img"
    return cleaned


def inspect_image_dimensions(file_path: str) -> tuple[int, int, str]:
    """纯 Python 解析本地 PNG / JPEG 图片宽高与 MIME 类型（无需依赖 Pillow）。"""
    try:
        with open(file_path, "rb") as f:
            head = f.read(64)
            if len(head) < 24:
                return 0, 0, "image/jpeg"

            # PNG: 8 字节签名 + 4 字节长度 + 'IHDR' + 4 字节宽 + 4 字节高
            if head.startswith(b"\x89PNG\r\n\x1a\n") and head[12:16] == b"IHDR":
                w, h = struct.unpack(">II", head[16:24])
                return int(w), int(h), "image/png"

            # JPEG: 扫描 SOF0~SOF15 段读取宽高
            if head.startswith(b"\xff\xd8"):
                f.seek(2)
                while True:
                    marker_prefix = f.read(1)
                    if not marker_prefix:
                        break
                    if marker_prefix != b"\xff":
                        continue
                    marker = f.read(1)
                    while marker == b"\xff":
                        marker = f.read(1)
                    if not marker:
                        break
                    m_byte = marker[0]
                    if m_byte in {0xD8, 0xD9} or (0xD0 <= m_byte <= 0xD7):
                        continue
                    seg_len_bytes = f.read(2)
                    if len(seg_len_bytes) < 2:
                        break
                    seg_len = struct.unpack(">H", seg_len_bytes)[0]
                    if seg_len < 2:
                        break
                    if m_byte in {
                        0xC0,
                        0xC1,
                        0xC2,
                        0xC3,
                        0xC5,
                        0xC6,
                        0xC7,
                        0xC9,
                        0xCA,
                        0xCB,
                        0xCD,
                        0xCE,
                        0xCF,
                    }:
                        sof_data = f.read(5)
                        if len(sof_data) == 5:
                            h, w = struct.unpack(">HH", sof_data[1:5])
                            return int(w), int(h), "image/jpeg"
                        break
                    f.seek(seg_len - 2, os.SEEK_CUR)
    except Exception:
        pass
    mime = "image/png" if file_path.lower().endswith(".png") else "image/jpeg"
    return 0, 0, mime


def build_template_payload(
    thread: ParsedThread,
    render_options: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """根据 ParsedThread 与渲染设置构造 Jinja2 模板数据字典及 Playwright 截图选项。"""
    opts = render_options or {}
    theme_setting = str(opts.get("theme") or "auto").lower()
    if theme_setting == "auto":
        if thread.forum == ForumType.STAGE1ST:
            theme_key = "s1"
        elif thread.forum == ForumType.BANGUMI:
            theme_key = "bgm"
        else:
            theme_key = "nga"
    elif theme_setting in {"light", "dark", "s1", "nga", "bgm"}:
        theme_key = theme_setting
    else:
        if thread.forum == ForumType.STAGE1ST:
            theme_key = "s1"
        elif thread.forum == ForumType.BANGUMI:
            theme_key = "bgm"
        else:
            theme_key = "nga"

    viewport_width = int(opts.get("viewport_width") or 780)
    viewport_width = max(480, min(1400, viewport_width))

    scale_level = str(opts.get("device_scale_factor") or "high").lower()
    if scale_level not in SCALE_FACTOR_VALUES:
        scale_level = "high"
    scale_factor = SCALE_FACTOR_VALUES[scale_level]

    img_fmt = str(opts.get("image_format") or "jpeg").lower()
    if img_fmt not in {"jpeg", "png"}:
        img_fmt = "jpeg"

    jpeg_quality = int(opts.get("jpeg_quality") or 90)
    jpeg_quality = max(40, min(100, jpeg_quality))

    enable_length_limit = bool(opts.get("enable_length_limit", False))
    raw_max_height = int(opts.get("max_card_height_px") or 0)
    max_body_height_px = 0
    if enable_length_limit and raw_max_height > 0:
        # 仅受限于 Chromium Skia 纹理高度安全边界（约 30000 物理像素），不再迁就 10000px 普通图片限制
        chromium_safe_css_cap = int(30000 / scale_factor)
        effective_total_cap = min(raw_max_height, max(1200, chromium_safe_css_cap))
        max_body_height_px = max(800, effective_total_cap - 380)

    author_clean = (thread.author or "?").strip()
    author_initial = author_clean[0].upper() if author_clean else "?"

    if thread.forum == ForumType.STAGE1ST:
        forum_badge = "Stage1st · S1"
    elif thread.forum == ForumType.BANGUMI:
        forum_badge = "Bangumi 番组计划"
    else:
        forum_badge = "NGA · 玩家社区"

    if thread.sub_type == "blog":
        sub_type_badge = "日志 · 长评"
    elif thread.sub_type == "subject_topic":
        sub_type_badge = "条目讨论"
    elif thread.sub_type == "group_topic":
        sub_type_badge = "小组话题"
    else:
        sub_type_badge = ""

    tmpl_data: dict[str, Any] = {
        "theme_key": theme_key,
        "viewport_width": viewport_width,
        "forum_badge": forum_badge,
        "forum_name": thread.forum_name,
        "sub_type": thread.sub_type,
        "sub_type_badge": sub_type_badge,
        "group_icon": thread.group_icon,
        "tid": thread.tid,
        "pid": thread.pid,
        "is_main_post": thread.is_main_post,
        "floor_badge": thread.display_floor_badge,
        "url": thread.url,
        "title": thread.title,
        "category": thread.category,
        "author": thread.author,
        "author_initial": author_initial,
        "author_id": thread.author_id,
        "author_avatar": thread.author_avatar,
        "author_group": thread.author_group,
        "author_sign": thread.author_sign,
        "reading_time": thread.reading_time,
        "created_at": thread.created_at,
        "views": thread.views,
        "replies": thread.replies,
        "score": thread.score,
        "is_unblocked_invisible": thread.is_unblocked_invisible,
        "invisible_reason": thread.invisible_reason,
        "content_html": thread.content_html,
        "max_body_height_px": max_body_height_px,
        "is_truncated": thread.is_truncated,
        "truncated_at_chars": thread.truncated_at_chars,
        "omitted_images_count": thread.omitted_images_count,
        "rate_summary": thread.rate_summary,
        "rates": [{"user": r.user, "score": r.score, "reason": r.reason} for r in thread.rates],
        "reactions": [
            {"emoji_url": rx.emoji_url, "label": rx.label, "count": rx.count}
            for rx in thread.reactions
        ],
        "related_subjects": [
            {
                "subject_id": rs.subject_id,
                "title": rs.title,
                "subtitle": rs.subtitle,
                "cover": rs.cover,
            }
            for rs in thread.related_subjects
        ],
        "bmo_script_html": thread.bmo_script_html,
    }

    screenshot_options: dict[str, Any] = {
        "full_page": True,
        "type": img_fmt,
        "viewport_width": viewport_width,
        # 设为紧凑视口高度，使短帖截图紧贴卡片底部不留空白
        "viewport_height": 120,
        "device_scale_factor_level": scale_level,
        "timeout": 30000,
    }
    if img_fmt == "jpeg":
        screenshot_options["quality"] = jpeg_quality

    return tmpl_data, screenshot_options


class ForumCardRenderer:
    """使用 AstrBot 远程 HTML 渲染服务 (remote t2i) 将帖子主楼渲染为图片。"""

    def __init__(
        self,
        *,
        context: Any = None,
        session: aiohttp.ClientSession | None = None,
        temp_dir: Path | None = None,
        temp_manager: TempFileManager | None = None,
    ) -> None:
        self.context = context
        self.session = session
        self.temp_dir = temp_dir or (Path(tempfile.gettempdir()) / "astrbot_forum_parser")
        self.temp_manager = temp_manager
        self._tmpl_str = load_forum_card_template()
        self._bgm_tmpl_str = load_bangumi_card_template()

    def _resolve_effective_endpoint(
        self, plugin_t2i_endpoint: str = "", umo: str | None = None
    ) -> str | None:
        """优先读取插件设置中的 T2I API 地址；为空时读取 AstrBot 全局配置的 t2i_endpoint。"""
        if plugin_t2i_endpoint and plugin_t2i_endpoint.strip():
            return plugin_t2i_endpoint.strip()

        if self.context is not None:
            get_cfg = getattr(self.context, "get_config", None)
            if callable(get_cfg):
                try:
                    ab_cfg = get_cfg(umo)
                    if isinstance(ab_cfg, dict):
                        ep = str(ab_cfg.get("t2i_endpoint") or "").strip()
                        if ep:
                            return ep
                except Exception:
                    pass
        return None

    async def _render_via_direct_http(
        self,
        endpoint: str,
        tmpl_data: dict[str, Any],
        options: dict[str, Any],
        *,
        tmpl_str: str | None = None,
        temp_scope: TempScope | None = None,
        max_bytes: int = MAX_RENDERED_IMAGE_BYTES,
    ) -> str:
        """直接调用 astrbot-t2i-service 的 POST /text2img/generate 接口并按块流式保存为本地临时图片。

        全程采用 64KB 分块直写磁盘，不在内存中缓存完整截图字节，防止高分辨率长图引发内存溢出。
        """
        base_ep = normalize_t2i_endpoint(endpoint) or DEFAULT_ASTRBOT_T2I_ENDPOINT
        gen_url = f"{base_ep}/generate"
        payload = {
            "tmpl": tmpl_str or self._tmpl_str,
            "json": False,
            "tmpldata": tmpl_data,
            "options": options,
        }
        ext = "png" if options.get("type") == "png" else "jpg"
        if temp_scope is not None:
            out_path = temp_scope.alloc_file(prefix="card", ext=ext)
        else:
            out_path = self.temp_dir / f"forum_{uuid.uuid4().hex}.{ext}"

        owns_session = self.session is None
        sess = self.session or aiohttp.ClientSession(trust_env=True)
        try:
            timeout = aiohttp.ClientTimeout(total=45)
            async with sess.post(gen_url, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    err_raw = await read_response_bounded(resp, max_bytes=16384)
                    err_body = err_raw.decode("utf-8", errors="replace")[:300]
                    raise RuntimeError(
                        f"远程 T2I 服务 ({gen_url}) 返回 HTTP {resp.status}: {err_body}"
                    )
                await stream_response_to_file(resp, out_path, max_bytes=max_bytes)
                return str(out_path)
        finally:
            if owns_session:
                await sess.close()

    async def _discard_single_rendered_file(
        self, img_path: str | None, temp_scope: TempScope | None
    ) -> None:
        if not img_path:
            return
        if self.temp_manager is not None:
            await self.temp_manager.remove_file(img_path)
        elif temp_scope is None:
            await asyncio.to_thread(Path(img_path).unlink, True)

    async def _invoke_render_once(
        self,
        tmpl_str: str,
        tmpl_data: dict[str, Any],
        options: dict[str, Any],
        *,
        effective_endpoint: str | None,
        temp_scope: TempScope | None,
        max_bytes: int,
    ) -> str:
        img_path: str | None = None
        html_renderer = getattr(self.context, "html_renderer", None) if self.context else None

        if html_renderer is not None and hasattr(html_renderer, "render_custom_template"):
            render_fn = html_renderer.render_custom_template
            sig = inspect.signature(render_fn)
            supports_endpoint_kw = "endpoint" in sig.parameters

            if supports_endpoint_kw:
                img_path = await render_fn(
                    tmpl_str,
                    tmpl_data,
                    return_url=False,
                    options=options,
                    endpoint=effective_endpoint,
                )
            elif not effective_endpoint:
                # 未单独覆盖 endpoint 时，直接走旧版 AstrBot 内置 html_renderer
                img_path = await render_fn(
                    tmpl_str,
                    tmpl_data,
                    return_url=False,
                    options=options,
                )

        if not img_path:
            # 若用户在插件设置中指定了自定义 T2I 地址且当前 AstrBot 版本不支持 endpoint 参数，直接请求该远程服务
            img_path = await self._render_via_direct_http(
                effective_endpoint or DEFAULT_ASTRBOT_T2I_ENDPOINT,
                tmpl_data,
                options,
                tmpl_str=tmpl_str,
                temp_scope=temp_scope,
                max_bytes=max_bytes,
            )

        if temp_scope is not None:
            temp_scope.track_file(img_path)
        elif self.temp_manager is not None:
            self.temp_manager.register(img_path)

        return img_path

    async def render_thread_card(
        self,
        thread: ParsedThread,
        *,
        plugin_t2i_endpoint: str = "",
        render_options: dict[str, Any] | None = None,
        umo: str | None = None,
        temp_scope: TempScope | None = None,
        platform_hint: str = "",
    ) -> RenderResult:
        """将 ParsedThread 渲染为本地图片文件并返回 RenderResult（超限或 413 时自动降级重绘）。"""
        base_opts = dict(render_options or {})
        max_rendered_mb = int(base_opts.get("max_rendered_card_mb") or 15)
        max_rendered_bytes = (
            max_rendered_mb * 1024 * 1024 if max_rendered_mb > 0 else MAX_RENDERED_IMAGE_BYTES
        )
        stream_limit = max(MAX_RENDERED_IMAGE_BYTES, max_rendered_bytes)
        selected_tmpl = self._bgm_tmpl_str if thread.forum == ForumType.BANGUMI else self._tmpl_str
        effective_endpoint = self._resolve_effective_endpoint(plugin_t2i_endpoint, umo=umo)

        cur_thread = thread
        cur_opts = dict(base_opts)
        last_exc: BaseException | None = None

        for attempt in range(3):
            tmpl_data, options = build_template_payload(cur_thread, cur_opts)
            img_path: str | None = None
            try:
                eff_stream_cap = (
                    min(stream_limit, max_rendered_bytes)
                    if (attempt < 2 and max_rendered_bytes > 0)
                    else stream_limit
                )
                img_path = await self._invoke_render_once(
                    selected_tmpl,
                    tmpl_data,
                    options,
                    effective_endpoint=effective_endpoint,
                    temp_scope=temp_scope,
                    max_bytes=eff_stream_cap,
                )
                width, height, mime_type, file_size = await asyncio.to_thread(
                    _inspect_file_meta, img_path
                )
            except BaseException as exc:
                await self._discard_single_rendered_file(img_path, temp_scope)
                err_low = str(exc).lower()
                is_limit_err = isinstance(exc, ResponseTooLargeError) or any(
                    k in err_low
                    for k in ("413", "dimension limit", "pixel limit", "超过落盘安全上限")
                )
                if not is_limit_err or attempt == 2:
                    raise
                last_exc = exc
                hit_dim = any(k in err_low for k in ("413", "dimension", "pixel"))
                logger.warning(
                    f"渲染卡片超出尺寸或体积上限，正在降级重试 (第 {attempt + 1} 次): {exc}"
                )
                cur_opts = dict(cur_opts)
                cur_opts["image_format"] = "jpeg"
                cur_opts["device_scale_factor"] = "normal"
                cur_opts["jpeg_quality"] = 80 if attempt == 0 else 68
                if hit_dim or attempt == 1:
                    cur_opts["enable_length_limit"] = True
                    cur_opts["max_card_height_px"] = 9200 if attempt == 0 else 6500
                    cur_thread = dataclasses.replace(cur_thread, is_truncated=True)
                continue

            oversize = max_rendered_bytes > 0 and file_size > max_rendered_bytes
            too_tall_for_onebot = platform_hint == "aiocqhttp" and height > 22000
            if not (oversize or too_tall_for_onebot) or attempt == 2:
                return RenderResult(
                    image_path=img_path,
                    width=width,
                    height=height,
                    file_size=file_size,
                    mime_type=mime_type,
                )

            await self._discard_single_rendered_file(img_path, temp_scope)
            logger.warning(
                f"渲染长图尺寸/体积超限 ({width}x{height}, {file_size} bytes)，正在自动降级重绘 (第 {attempt + 1} 次)"
            )
            cur_opts = dict(cur_opts)
            cur_opts["image_format"] = "jpeg"
            cur_opts["device_scale_factor"] = "normal"
            cur_opts["jpeg_quality"] = 78 if attempt == 0 else 65
            if too_tall_for_onebot or attempt == 1:
                cur_opts["enable_length_limit"] = True
                cur_opts["max_card_height_px"] = 9200 if attempt == 0 else 6500
                cur_thread = dataclasses.replace(cur_thread, is_truncated=True)

        assert last_exc is not None
        raise last_exc
