from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Any

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

if __package__:
    from .core.html_sanitizer import prepare_post_images_for_forward
    from .core.io_guard import BoundedTaskLimiter
    from .core.models import (
        ForumLink,
        ForumLoginRequiredError,
        ForumParseError,
        ForumType,
        ParsedThread,
        RenderResult,
    )
    from .core.parsers import NgaParser, Stage1stParser
    from .core.renderer import ForumCardRenderer
    from .core.telegraph_publisher import DEFAULT_SDA1_UPLOAD_URL, TelegraphPublisher
    from .core.temp_manager import BoundedTTLCache, TempFileManager, TempScope
    from .core.url_extractor import (
        CooldownTracker,
        extract_forum_links_from_event,
        parse_forum_url,
    )
else:
    from core.html_sanitizer import prepare_post_images_for_forward
    from core.io_guard import BoundedTaskLimiter
    from core.models import (
        ForumLink,
        ForumLoginRequiredError,
        ForumParseError,
        ForumType,
        ParsedThread,
        RenderResult,
    )
    from core.parsers import NgaParser, Stage1stParser
    from core.renderer import ForumCardRenderer
    from core.telegraph_publisher import DEFAULT_SDA1_UPLOAD_URL, TelegraphPublisher
    from core.temp_manager import BoundedTTLCache, TempFileManager, TempScope
    from core.url_extractor import (
        CooldownTracker,
        extract_forum_links_from_event,
        parse_forum_url,
    )

MANUAL_CMD_NAMES = ("forum_parse", "解析帖子", "帖子解析")
EXTRA_TEMP_SCOPES_KEY = "_forum_parser_temp_scopes"
GC_INTERVAL_SECONDS = 60.0


def _is_manual_cmd_text(message_str: str) -> bool:
    """判断消息文本是否为插件自身的手动解析指令，避免与自动监听重复触发。"""
    stripped = (message_str or "").strip().lstrip("/!！.")
    if not stripped:
        return False
    first_token = stripped.split(maxsplit=1)[0].lower()
    return first_token in MANUAL_CMD_NAMES


def _estimate_parsed_thread_bytes(t: ParsedThread) -> int:
    return len(t.content_html) + len(t.author_avatar) + len(t.title) + 512


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _write_or_prune_json_file(path: Path, key: str, value: Any) -> None:
    """原子更新本地兜底状态文件；若字典为空则直接移除文件不留残余。"""
    data = _read_json_file(path)
    if value is None or value == "" or value == {}:
        data.pop(key, None)
    else:
        data[key] = value

    if not data:
        path.unlink(missing_ok=True)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


class ForumAutoLinkFilter(filter.CustomFilter):
    """快速预筛包含 Stage1st 或 NGA 链接的消息，避免无关闲聊唤醒消息管线。"""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        try:
            self_id = str(event.get_self_id() or "")
            sender_id = str(event.get_sender_id() or "")
            if self_id and sender_id and self_id == sender_id:
                return False

            if _is_manual_cmd_text(event.message_str or ""):
                return False

            links = extract_forum_links_from_event(
                event,
                allow_alias_domains=True,
                parse_share_cards=True,
                enable_s1=True,
                enable_nga=True,
            )
            return len(links) > 0
        except Exception:
            return False


class ForumParserPlugin(Star):
    """自动解析聊天中的 Stage1st 与 NGA 论坛帖子主楼并渲染为图片发送。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config: dict[str, Any] = config if config is not None else {}
        self._data_dir: Path = self._resolve_plugin_data_dir()
        self._temp_dir: Path = self._data_dir / "temp_render"
        self._state_file: Path = self._data_dir / "auth_state.json"

        # 统一管控临时文件（支持按请求分配独立磁盘落盘目录防止大图占满内存，并在发送后立即整目录清理）
        self._temp_mgr = TempFileManager(
            self._temp_dir,
            legacy_dirs=[self._data_dir / "render_cache"],
            ttl_seconds=180.0,
            max_dir_files=60,
            max_dir_bytes=160 * 1024 * 1024,
        )
        # 严格受限的纯文本轻量缓存（单条上限 128KB、总上限 1MB，含 Base64 图片的重型对象绝不驻留内存）
        self._parse_cache: BoundedTTLCache[tuple[Any, ...], ParsedThread] = BoundedTTLCache(
            max_entries=8,
            ttl_seconds=60.0,
            size_estimator=_estimate_parsed_thread_bytes,
            max_item_bytes=128 * 1024,
            max_total_bytes=1024 * 1024,
        )
        self._cooldown = CooldownTracker()

        parse_opts = self._get_sub_cfg("parse_options")
        max_conc = int(parse_opts.get("max_concurrent_tasks", 2))
        self._task_limiter = BoundedTaskLimiter(max_concurrent=max_conc, max_waiting=6)

        self._http_session: aiohttp.ClientSession | None = None
        self._s1_parser: Stage1stParser | None = None
        self._nga_parser: NgaParser | None = None
        self._renderer: ForumCardRenderer | None = None
        self._telegraph_publisher: TelegraphPublisher | None = None
        self._gc_task: asyncio.Task[None] | None = None

    def _resolve_plugin_data_dir(self) -> Path:
        """按照 AstrBot 规范获取位于 data/plugin_data 下的持久化目录。"""
        try:
            d = StarTools.get_data_dir("astrbot_plugin_forum_parser")
            p = Path(d)
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            fallback = Path("data") / "plugin_data" / "astrbot_plugin_forum_parser"
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    async def _kv_get(self, key: str, default: Any = None) -> Any:
        if hasattr(self, "get_kv_data"):
            try:
                val = await self.get_kv_data(key, default)
                if val is not None:
                    return val
            except Exception:
                pass
        data = await asyncio.to_thread(_read_json_file, self._state_file)
        return data.get(key, default)

    async def _kv_set(self, key: str, value: Any) -> None:
        if hasattr(self, "put_kv_data"):
            try:
                if (value is None or value == "" or value == {}) and hasattr(
                    self, "delete_kv_data"
                ):
                    await self.delete_kv_data(key)
                else:
                    await self.put_kv_data(key, value)
                return
            except Exception:
                pass
        try:
            await asyncio.to_thread(_write_or_prune_json_file, self._state_file, key, value)
        except Exception as e:
            logger.warning(f"保存论坛登录状态失败: {e}")

    def _ensure_services(self) -> None:
        parse_opts = self._get_sub_cfg("parse_options")
        self._task_limiter.update_limits(
            max_concurrent=int(parse_opts.get("max_concurrent_tasks", 2)),
            max_waiting=6,
        )

        if self._http_session is None or self._http_session.closed:
            connector = aiohttp.TCPConnector(
                limit=16,
                limit_per_host=6,
                keepalive_timeout=10.0,
                enable_cleanup_closed=True,
            )
            self._http_session = aiohttp.ClientSession(trust_env=True, connector=connector)
            self._s1_parser = Stage1stParser(
                self._http_session,
                config=self._get_sub_cfg("stage1st"),
                kv_getter=self._kv_get,
                kv_setter=self._kv_set,
            )
            self._nga_parser = NgaParser(
                self._http_session,
                config=self._get_sub_cfg("nga"),
                kv_getter=self._kv_get,
                kv_setter=self._kv_set,
            )
            self._renderer = ForumCardRenderer(
                context=self.context,
                session=self._http_session,
                temp_dir=self._temp_dir,
                temp_manager=self._temp_mgr,
            )
            self._telegraph_publisher = TelegraphPublisher(
                self._http_session,
                kv_getter=self._kv_get,
                kv_setter=self._kv_set,
            )
        else:
            if self._s1_parser is not None:
                self._s1_parser.update_config(self._get_sub_cfg("stage1st"))
            if self._nga_parser is not None:
                self._nga_parser.update_config(self._get_sub_cfg("nga"))
            if self._telegraph_publisher is None:
                self._telegraph_publisher = TelegraphPublisher(
                    self._http_session,
                    kv_getter=self._kv_get,
                    kv_setter=self._kv_set,
                )

        if self._gc_task is None or self._gc_task.done():
            with contextlib.suppress(RuntimeError):
                loop = asyncio.get_running_loop()
                self._gc_task = loop.create_task(
                    self._periodic_gc_loop(), name="forum_parser_temp_gc"
                )

    def _get_sub_cfg(self, key: str) -> dict[str, Any]:
        val = self.config.get(key)
        return val if isinstance(val, dict) else {}

    async def _periodic_gc_loop(self) -> None:
        """定时后台清理协程：清除超时孤立临时文件与过期内存缓存。"""
        try:
            while True:
                await asyncio.sleep(GC_INTERVAL_SECONDS)
                try:
                    await self._temp_mgr.sweep_expired()
                    self._parse_cache.prune_expired()
                    self._cooldown.prune_expired()
                except Exception as e:
                    logger.debug(f"论坛插件定时缓存清理巡检异常: {e}")
        except asyncio.CancelledError:
            raise

    async def initialize(self) -> None:
        await self._temp_mgr.purge_all()
        self._ensure_services()

    async def terminate(self) -> None:
        if self._gc_task is not None:
            self._gc_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._gc_task
            self._gc_task = None

        if self._http_session is not None and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None

        self._parse_cache.clear()
        self._cooldown.clear()
        await self._temp_mgr.purge_all()

    @staticmethod
    def _attach_scope_to_event(event: AstrMessageEvent, scope: TempScope) -> None:
        """将当前请求的临时文件作用域挂载到 event 上，供 after_message_sent 钩子即时清理。"""
        get_extra = getattr(event, "get_extra", None)
        set_extra = getattr(event, "set_extra", None)
        if callable(get_extra) and callable(set_extra):
            existing = get_extra(EXTRA_TEMP_SCOPES_KEY, None)
            scopes = list(existing) if isinstance(existing, list) else []
            if scope not in scopes:
                scopes.append(scope)
            set_extra(EXTRA_TEMP_SCOPES_KEY, scopes)

    @filter.after_message_sent()
    async def on_after_message_sent_cleanup(self, event: AstrMessageEvent) -> None:
        """消息发送完成钩子：第一时间清空本次请求落盘的全部临时文件与子目录。"""
        get_extra = getattr(event, "get_extra", None)
        set_extra = getattr(event, "set_extra", None)
        if not callable(get_extra):
            return
        pending = get_extra(EXTRA_TEMP_SCOPES_KEY, None)
        if not isinstance(pending, list) or not pending:
            return
        if callable(set_extra):
            set_extra(EXTRA_TEMP_SCOPES_KEY, [])
        for scope in pending:
            if isinstance(scope, TempScope):
                await scope.cleanup()

    async def _build_onebot_forward_result(
        self,
        event: AstrMessageEvent,
        parsed_thread: ParsedThread,
        render_res: RenderResult,
        onebot_opts: dict[str, Any],
        temp_scope: TempScope,
    ) -> Any:
        """构造 OneBot v11 (aiocqhttp) 合并转发消息链（图片统一走短生命周期磁盘落盘，避免常驻内存引发 OOM）。"""
        raw_self_id = str(event.get_self_id() or "").strip()
        bot_uin = raw_self_id if raw_self_id.isdigit() else "10000"

        title_line = (
            f"[{parsed_thread.category}] {parsed_thread.title}"
            if parsed_thread.category
            else parsed_thread.title
        )
        if parsed_thread.is_main_post:
            author_part = f"楼主：{parsed_thread.author}"
            card_node_name = f"{parsed_thread.author} · 主楼长图"
        else:
            author_part = f"发言：{parsed_thread.author}（{parsed_thread.display_floor_badge}）"
            card_node_name = f"{parsed_thread.author} · {parsed_thread.display_floor_badge}"

        meta_parts = [author_part]
        if parsed_thread.forum_name:
            meta_parts.append(f"版块：{parsed_thread.forum_name}")
        if parsed_thread.created_at:
            meta_parts.append(parsed_thread.created_at)
        summary_text = f"📌 {title_line}\n{'  |  '.join(meta_parts)}\n🔗 {parsed_thread.url}"

        card_img_comp = (
            Comp.Image.fromFileSystem(render_res.image_path)
            if hasattr(Comp.Image, "fromFileSystem")
            else Comp.Image(file=render_res.image_path)
        )

        nodes: list[Any] = [
            Comp.Node(
                uin=bot_uin,
                name=parsed_thread.forum.display_name,
                content=[Comp.Plain(summary_text)],
            ),
            Comp.Node(
                uin=bot_uin,
                name=card_node_name,
                content=[card_img_comp],
            ),
        ]

        include_post_imgs = bool(onebot_opts.get("forward_include_images", True))
        max_forward_imgs = int(onebot_opts.get("forward_max_images", 20))
        if include_post_imgs and parsed_thread.post_image_urls and max_forward_imgs > 0:
            if parsed_thread.forum == ForumType.STAGE1ST and self._s1_parser is not None:
                cookies, same_hosts = await self._s1_parser.get_media_auth_context()
            elif self._nga_parser is not None:
                cookies, same_hosts = self._nga_parser.get_media_auth_context()
            else:
                cookies, same_hosts = None, set()

            local_img_files, skipped_imgs = await prepare_post_images_for_forward(
                parsed_thread.content_html,
                parsed_thread.post_image_urls,
                temp_scope,
                max_images=max_forward_imgs,
                session=self._http_session,
                referer=parsed_thread.url,
                cookies=cookies,
                same_site_hosts=same_hosts,
            )
            total_imgs = len(local_img_files)
            for idx, file_path in enumerate(local_img_files, start=1):
                img_seg = (
                    Comp.Image.fromFileSystem(file_path)
                    if hasattr(Comp.Image, "fromFileSystem")
                    else Comp.Image(file=file_path)
                )
                nodes.append(
                    Comp.Node(
                        uin=bot_uin,
                        name=f"帖子配图 ({idx}/{total_imgs})",
                        content=[img_seg],
                    )
                )
            if skipped_imgs > 0:
                nodes.append(
                    Comp.Node(
                        uin=bot_uin,
                        name=parsed_thread.forum.display_name,
                        content=[
                            Comp.Plain(
                                f"ℹ️ 另有 {skipped_imgs} 张配图因体积超限或源站无法访问未附带，可点击原帖链接查看。"
                            )
                        ],
                    )
                )

        return event.chain_result([Comp.Nodes(nodes)])

    async def _parse_and_render_link(
        self,
        event: AstrMessageEvent,
        link: ForumLink,
        temp_scope: TempScope,
        *,
        bypass_cache: bool = False,
    ) -> Any:
        """在全局并发信号量与独立 TempScope 保护下完成解析、Telegraph 发布或远程渲染与消息构建。"""
        self._ensure_services()
        assert self._s1_parser is not None
        assert self._nga_parser is not None
        assert self._renderer is not None
        assert self._telegraph_publisher is not None

        async with self._task_limiter.acquire():
            platform_name = ""
            if hasattr(event, "get_platform_name") and callable(event.get_platform_name):
                platform_name = str(event.get_platform_name() or "").lower()

            render_opts = self._get_sub_cfg("render_options")
            embed_images = bool(render_opts.get("embed_images", True))
            max_inline_images = int(render_opts.get("max_inline_images", 15))
            enable_length_limit = bool(render_opts.get("enable_length_limit", False))
            max_content_chars = int(render_opts.get("max_content_chars", 15000))

            tg_opts = self._get_sub_cfg("telegram_options")
            tg_mode = str(tg_opts.get("delivery_mode") or "telegraph").strip().lower()
            use_telegraph = platform_name == "telegram" and tg_mode == "telegraph"
            tg_max_images = int(tg_opts.get("max_upload_images", 25))
            tg_upload_url = str(tg_opts.get("image_upload_url") or DEFAULT_SDA1_UPLOAD_URL).strip()
            tg_fallback_to_image = bool(tg_opts.get("fallback_to_image", True))

            parser = self._s1_parser if link.forum == ForumType.STAGE1ST else self._nga_parser

            # 1. Telegram 默认优先走 telegra.ph 即时预览发布（跳过 Base64 内存预加载，配图流式上传至 p.sda1.dev）
            if use_telegraph:
                tg_cache_key = (
                    link.forum.value,
                    link.target_id,
                    embed_images,
                    False,
                    tg_max_images,
                    enable_length_limit,
                    max_content_chars,
                )
                tg_from_cache = False
                tg_thread = None if bypass_cache else self._parse_cache.get(tg_cache_key)
                if tg_thread is not None:
                    tg_from_cache = True
                else:
                    tg_thread = await parser.parse_thread(
                        link,
                        embed_images=embed_images,
                        prefetch_data_uri=False,
                        max_inline_images=tg_max_images,
                        enable_length_limit=enable_length_limit,
                        max_content_chars=max_content_chars,
                    )
                    self._parse_cache.set(tg_cache_key, tg_thread)
                    tg_from_cache = self._parse_cache.get(tg_cache_key) is tg_thread

                try:
                    if tg_thread.forum == ForumType.STAGE1ST:
                        cookies, same_hosts = await self._s1_parser.get_media_auth_context()
                    else:
                        cookies, same_hosts = self._nga_parser.get_media_auth_context()

                    telegraph_url = await self._telegraph_publisher.publish_thread(
                        tg_thread,
                        temp_scope,
                        cookies=cookies,
                        same_site_hosts=same_hosts,
                        upload_images=embed_images,
                        upload_url=tg_upload_url,
                        max_upload_images=tg_max_images,
                    )
                    if not tg_from_cache:
                        tg_thread.content_html = ""
                        tg_thread.author_avatar = ""
                    await temp_scope.cleanup()
                    # 按约定仅发送纯粹的 telegra.ph 链接，不附带额外文字，直接触发 Telegram 原生 Instant View
                    return event.plain_result(telegraph_url)
                except Exception as e:
                    if not tg_fallback_to_image:
                        await temp_scope.cleanup()
                        raise
                    logger.warning(
                        f"发布至 telegra.ph 失败，自动回退为自定义模板长图发送 ({link.canonical_url}): {e}"
                    )

            # 2. 常规长图渲染路径（用于 OneBot11、Telegram image_file 模式及 Telegraph 失败自动兜底）
            cache_key = (
                link.forum.value,
                link.target_id,
                embed_images,
                True,
                max_inline_images,
                enable_length_limit,
                max_content_chars,
            )
            from_cache = False
            parsed_thread = None if bypass_cache else self._parse_cache.get(cache_key)
            if parsed_thread is not None:
                from_cache = True
            else:
                parsed_thread = await parser.parse_thread(
                    link,
                    embed_images=embed_images,
                    prefetch_data_uri=True,
                    max_inline_images=max_inline_images,
                    enable_length_limit=enable_length_limit,
                    max_content_chars=max_content_chars,
                )
                self._parse_cache.set(cache_key, parsed_thread)
                from_cache = self._parse_cache.get(cache_key) is parsed_thread

            plugin_t2i_ep = str(self.config.get("t2i_endpoint") or "").strip()
            umo = getattr(event, "unified_msg_origin", None)

            render_res = await self._renderer.render_thread_card(
                parsed_thread,
                plugin_t2i_endpoint=plugin_t2i_ep,
                render_options=render_opts,
                umo=umo,
                temp_scope=temp_scope,
            )

            try:
                self._attach_scope_to_event(event, temp_scope)

                onebot_opts = self._get_sub_cfg("onebot_options")
                use_onebot_forward = bool(onebot_opts.get("use_forward", True))

                if platform_name == "aiocqhttp" and use_onebot_forward:
                    msg_result = await self._build_onebot_forward_result(
                        event, parsed_thread, render_res, onebot_opts, temp_scope
                    )
                elif platform_name == "telegram":
                    # Telegram 长图模式（或 Telegraph 失败兜底）：仅推送单张完整长图，固定走无损文件形式 (Send as File)
                    ext = "png" if render_res.mime_type == "image/png" else "jpg"
                    tid_part = parsed_thread.tid or link.tid
                    floor_suffix = (
                        f"_p{parsed_thread.pid}"
                        if (not parsed_thread.is_main_post and parsed_thread.pid)
                        else (f"_f{link.floor_hint}" if link.floor_hint > 0 else "")
                    )
                    fname = f"{link.forum.value}_{tid_part}{floor_suffix}.{ext}"
                    msg_result = event.chain_result(
                        [Comp.File(name=fname, file=render_res.image_path)]
                    )
                else:
                    msg_result = event.image_result(render_res.image_path)

                # 若该帖子包含内嵌大图而未进入轻量缓存，组装完毕后立即释放大字符串引用以降低发送期内存占用
                if not from_cache:
                    parsed_thread.content_html = ""
                    parsed_thread.author_avatar = ""

                return msg_result
            except BaseException:
                await temp_scope.cleanup()
                raise

    @filter.command("forum_parse", alias={"解析帖子", "帖子解析"})
    @filter.platform_adapter_type(
        filter.PlatformAdapterType.AIOCQHTTP | filter.PlatformAdapterType.TELEGRAM
    )
    async def cmd_parse_forum(self, event: AstrMessageEvent, url: str = ""):
        """手动解析指定的 Stage1st 或 NGA 论坛帖子链接并渲染为图片。"""
        s1_cfg = self._get_sub_cfg("stage1st")
        nga_cfg = self._get_sub_cfg("nga")
        parse_opts = self._get_sub_cfg("parse_options")

        links: list[ForumLink] = []
        if url:
            single = parse_forum_url(
                url,
                allow_alias_domains=bool(parse_opts.get("allow_alias_domains", True)),
                enable_s1=bool(s1_cfg.get("enabled", True)),
                enable_nga=bool(nga_cfg.get("enabled", True)),
            )
            if single:
                links.append(single)

        if not links:
            links = extract_forum_links_from_event(
                event,
                allow_alias_domains=bool(parse_opts.get("allow_alias_domains", True)),
                parse_share_cards=True,
                enable_s1=bool(s1_cfg.get("enabled", True)),
                enable_nga=bool(nga_cfg.get("enabled", True)),
            )

        if not links:
            yield event.plain_result(
                "未识别到受支持的论坛帖子链接，请提供有效的 Stage1st 或 NGA 帖子地址。"
            )
            return

        for link in links[: max(1, int(parse_opts.get("max_links_per_message", 2)))]:
            temp_scope = self._temp_mgr.create_scope()
            try:
                msg_res = await self._parse_and_render_link(
                    event, link, temp_scope, bypass_cache=True
                )
                yield msg_res
            except (ForumLoginRequiredError, ForumParseError) as e:
                logger.warning(f"手动解析论坛链接失败 ({link.canonical_url}): {e}")
                yield event.plain_result(f"解析失败：{e}")
            except Exception as e:
                logger.exception(f"渲染论坛帖子异常 ({link.canonical_url}): {e}")
                yield event.plain_result(f"解析或渲染帖子时发生错误：{e}")
            finally:
                await temp_scope.cleanup()

    @filter.custom_filter(ForumAutoLinkFilter)
    @filter.platform_adapter_type(
        filter.PlatformAdapterType.AIOCQHTTP | filter.PlatformAdapterType.TELEGRAM
    )
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_forum_link_message(self, event: AstrMessageEvent):
        """自动监听聊天中的 Stage1st 与 NGA 帖子链接并渲染主楼图片发送。"""
        s1_cfg = self._get_sub_cfg("stage1st")
        nga_cfg = self._get_sub_cfg("nga")
        parse_opts = self._get_sub_cfg("parse_options")

        enable_s1 = bool(s1_cfg.get("enabled", True))
        enable_nga = bool(nga_cfg.get("enabled", True))
        if not enable_s1 and not enable_nga:
            return

        links = extract_forum_links_from_event(
            event,
            allow_alias_domains=bool(parse_opts.get("allow_alias_domains", True)),
            parse_share_cards=bool(parse_opts.get("parse_share_cards", True)),
            enable_s1=enable_s1,
            enable_nga=enable_nga,
        )
        if not links:
            return

        max_links = max(1, int(parse_opts.get("max_links_per_message", 2)))
        cooldown_sec = max(0, int(parse_opts.get("cooldown_seconds", 30)))
        send_error_hint = bool(parse_opts.get("send_error_hint", False))
        session_key = str(
            getattr(event, "unified_msg_origin", None)
            or getattr(event, "session_id", None)
            or "default"
        )

        for link in links[:max_links]:
            if not self._cooldown.check_and_mark(
                session_key, link.forum, link.target_id, cooldown_sec
            ):
                logger.debug(
                    f"跳过冷却期内的重复论坛链接: {link.forum.value} target={link.target_id}"
                )
                continue

            temp_scope = self._temp_mgr.create_scope()
            try:
                logger.info(
                    f"正在自动解析论坛帖子: [{link.forum.display_name}] {link.canonical_url}"
                )
                msg_res = await self._parse_and_render_link(event, link, temp_scope)
                yield msg_res
            except (ForumLoginRequiredError, ForumParseError) as e:
                logger.warning(f"自动解析论坛帖子失败 ({link.canonical_url}): {e}")
                if send_error_hint:
                    yield event.plain_result(f"[{link.forum.display_name}] {e}")
            except Exception as e:
                logger.exception(f"自动解析渲染论坛帖子异常 ({link.canonical_url}): {e}")
                if send_error_hint:
                    yield event.plain_result(f"[{link.forum.display_name}] 渲染图片失败：{e}")
            finally:
                await temp_scope.cleanup()
