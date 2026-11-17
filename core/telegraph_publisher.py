from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

from .html_sanitizer import (
    INLINED_FORUM_IMG_RE,
    MAX_FORWARD_SINGLE_IMAGE_BYTES,
    MAX_FORWARD_TOTAL_FILE_BYTES,
    MiniNode,
    _data_uri_raw_byte_len,
    _decode_data_uri_to_file_sync,
    _stream_single_image_to_scope,
    parse_html,
    rewrite_legacy_media_url,
)
from .io_guard import mime_to_ext
from .models import ForumParseError, ParsedThread
from .parsers.base import DEFAULT_BROWSER_UA
from .temp_manager import TempScope

logger = logging.getLogger("astrbot")

TELEGRAPH_API_BASE = "https://api.telegra.ph"
DEFAULT_SDA1_UPLOAD_URL = "https://p.sda1.dev/api/v1/upload_external_noform"

# Telegraph 单页 content JSON 最大限制为 64KB，预留安全余量按 54KB 自动分页
MAX_TELEGRAPH_CONTENT_BYTES = 54 * 1024

SMILEY_CODE_FROM_URL_RE = re.compile(r"/smiley/([^/]+)/([^/.?]+)", re.IGNORECASE)

TelegraphNode = str | dict[str, Any]


class TelegraphPublishError(ForumParseError):
    """发布到 telegra.ph 失败异常。"""


def _smiley_node_to_text(node: MiniNode) -> str:
    src = node.get_attr("src")
    m = SMILEY_CODE_FROM_URL_RE.search(src)
    if m:
        grp, code = m.group(1), m.group(2)
        if grp.lower().startswith("face"):
            return f"[s1:{code}]"
        return f"[{grp}:{code}]"
    alt = node.get_attr("alt").strip()
    if alt and alt not in {"图片", "s1表情"}:
        return f"[{alt}]"
    return "[表情]"


def _merge_adjacent_strings(items: list[TelegraphNode]) -> list[TelegraphNode]:
    merged: list[TelegraphNode] = []
    for item in items:
        if isinstance(item, str):
            if not item:
                continue
            if merged and isinstance(merged[-1], str):
                merged[-1] = merged[-1] + item
            else:
                merged.append(item)
        else:
            merged.append(item)
    return merged


def _trim_inline_whitespace(items: list[TelegraphNode]) -> list[TelegraphNode]:
    items = _merge_adjacent_strings(items)
    while items:
        first = items[0]
        if isinstance(first, str):
            s = first.lstrip(" \t\r\n")
            if not s:
                items.pop(0)
                continue
            items[0] = s
            break
        if isinstance(first, dict) and first.get("tag") == "br":
            items.pop(0)
            continue
        break

    while items:
        last = items[-1]
        if isinstance(last, str):
            s = last.rstrip(" \t\r\n")
            if not s:
                items.pop()
                continue
            items[-1] = s
            break
        if isinstance(last, dict) and last.get("tag") == "br":
            items.pop()
            continue
        break

    # 压缩连续多余的 <br>（最多保留连续 2 个）
    compacted: list[TelegraphNode] = []
    br_streak = 0
    for item in items:
        if isinstance(item, dict) and item.get("tag") == "br":
            br_streak += 1
            if br_streak <= 2:
                compacted.append(item)
        else:
            if isinstance(item, str) and not item.strip() and br_streak > 0:
                continue
            br_streak = 0
            compacted.append(item)
    return _merge_adjacent_strings(compacted)


def convert_html_to_telegraph_nodes(
    content_html: str,
    *,
    img_url_map: dict[str, str] | None = None,
    idx_url_map: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    """将清洗后的论坛楼层 HTML 安全转换为符合 Telegraph Instant View 规范的顶层块节点列表。"""
    u_map = img_url_map or {}
    i_map = idx_url_map or {}
    root = parse_html(content_html or "")

    blocks: list[dict[str, Any]] = []
    inline_buf: list[TelegraphNode] = []

    def _flush_inline_to_p() -> None:
        nonlocal inline_buf
        cleaned = _trim_inline_whitespace(inline_buf)
        inline_buf = []
        if cleaned:
            blocks.append({"tag": "p", "children": cleaned})

    def _collect_inline(node: MiniNode) -> list[TelegraphNode]:
        """将子树收集为纯内联 Telegraph 节点序列（用于在标题、列表项、引用内部安全渲染）。"""
        if node.is_text:
            txt = re.sub(r"[ \t\r\f\v]+", " ", node.text.replace("\n", " "))
            return [txt] if txt else []

        tag = node.tag
        classes = node.classes

        if tag == "br":
            return [{"tag": "br"}]

        if tag == "img":
            if "forum-smiley" in classes:
                return [_smiley_node_to_text(node)]
            resolved = _resolve_img_src(node)
            if resolved:
                return [{"tag": "a", "attrs": {"href": resolved}, "children": ["[查看图片]"]}]
            return ["[图片]"]

        child_inlines: list[TelegraphNode] = []
        for ch in node.children:
            child_inlines.extend(_collect_inline(ch))
        child_inlines = _merge_adjacent_strings(child_inlines)

        if not child_inlines:
            return []

        if "forum-spoiler" in classes:
            return [
                {
                    "tag": "em",
                    "children": _merge_adjacent_strings(["【反白：", *child_inlines, "】"]),
                }
            ]

        if tag in {"strong", "b"}:
            return [{"tag": "strong", "children": child_inlines}]
        if tag in {"em", "i"} or "forum-edit-note" in classes:
            return [{"tag": "em", "children": child_inlines}]
        if tag in {"s", "strike", "del"} or "forum-del" in classes:
            return [{"tag": "s", "children": child_inlines}]
        if tag in {"u", "ins"}:
            return [{"tag": "u", "children": child_inlines}]
        if tag == "code":
            plain_c = node.inner_text()
            return [{"tag": "code", "children": [plain_c]}] if plain_c else []
        if tag == "a":
            href = node.get_attr("href").strip()
            if href.startswith(("http://", "https://")) and len(href) <= 512:
                return [{"tag": "a", "attrs": {"href": href}, "children": child_inlines}]
            return child_inlines

        if tag in {"p", "div", "li", "tr", "blockquote"}:
            return [*child_inlines, {"tag": "br"}]

        return child_inlines

    def _resolve_img_src(img_node: MiniNode) -> str:
        idx_s = img_node.get_attr("data-idx")
        if idx_s.isdigit() and int(idx_s) in i_map:
            return i_map[int(idx_s)]
        raw_src = img_node.get_attr("src").strip()
        if raw_src in u_map:
            return u_map[raw_src]
        rewritten = rewrite_legacy_media_url(raw_src)
        if rewritten in u_map:
            return u_map[rewritten]
        if rewritten.startswith("https://"):
            return rewritten
        if rewritten.startswith("http://"):
            return "https://" + rewritten.removeprefix("http://")
        return ""

    def _emit_image_block(img_node: MiniNode) -> None:
        _flush_inline_to_p()
        resolved = _resolve_img_src(img_node)
        if resolved:
            blocks.append(
                {"tag": "figure", "children": [{"tag": "img", "attrs": {"src": resolved}}]}
            )
        else:
            blocks.append(
                {
                    "tag": "p",
                    "children": [
                        {"tag": "em", "children": ["[🖼️ 图片无法加载，请点击文末原帖查看]"]}
                    ],
                }
            )

    def _walk_block(node: MiniNode) -> None:
        if node.is_text:
            txt = node.text.replace("\r", "")
            if "\n" in txt:
                parts = txt.split("\n")
                for idx, part in enumerate(parts):
                    if part:
                        inline_buf.append(part)
                    if idx + 1 < len(parts):
                        inline_buf.append({"tag": "br"})
            elif txt:
                inline_buf.append(txt)
            return

        tag = node.tag
        classes = node.classes

        if tag == "#root":
            for ch in node.children:
                _walk_block(ch)
            _flush_inline_to_p()
            return

        if tag == "hr":
            _flush_inline_to_p()
            blocks.append({"tag": "hr"})
            return

        if tag == "br":
            inline_buf.append({"tag": "br"})
            return

        if tag == "img":
            if "forum-smiley" in classes:
                inline_buf.append(_smiley_node_to_text(node))
            else:
                _emit_image_block(node)
            return

        if "forum-img-placeholder" in classes:
            _flush_inline_to_p()
            txt = node.inner_text().strip()
            if txt:
                blocks.append({"tag": "p", "children": [{"tag": "em", "children": [txt]}]})
            return

        # 展开折叠块 (<details class="forum-collapse"> 或 <div class="forum-collapse">)
        if tag == "details" or "forum-collapse" in classes:
            _flush_inline_to_p()
            header_title = "折叠内容"
            body_children: list[MiniNode] = []
            for ch in node.children:
                if not ch.is_text and (
                    ch.tag == "summary" or "forum-collapse-header" in ch.classes
                ):
                    t = ch.inner_text().strip()
                    if t:
                        header_title = t
                else:
                    body_children.append(ch)
            blocks.append({"tag": "h4", "children": [f"📂 {header_title}"]})
            for ch in body_children:
                _walk_block(ch)
            _flush_inline_to_p()
            return

        # 引用块（扁平化处理嵌套引用，防止 Telegraph 拒绝嵌套 blockquote）
        if tag == "blockquote" or "forum-quote" in classes:
            _flush_inline_to_p()
            q_inlines: list[TelegraphNode] = []
            for ch in node.children:
                if not ch.is_text and (ch.tag == "blockquote" or "forum-quote" in ch.classes):
                    inner_q = _trim_inline_whitespace(_collect_inline(ch))
                    if inner_q:
                        q_inlines.extend(["💬 ", *inner_q, {"tag": "br"}])
                else:
                    q_inlines.extend(_collect_inline(ch))
            q_cleaned = _trim_inline_whitespace(q_inlines)
            if q_cleaned:
                blocks.append({"tag": "blockquote", "children": q_cleaned})
            return

        if tag == "pre" or "forum-code" in classes:
            _flush_inline_to_p()
            code_txt = node.inner_text().strip("\r\n")
            if code_txt:
                blocks.append({"tag": "pre", "children": [code_txt]})
            return

        if tag in {"h1", "h2", "h3"}:
            _flush_inline_to_p()
            h_inlines = _trim_inline_whitespace(_collect_inline(node))
            if h_inlines:
                blocks.append({"tag": "h3", "children": h_inlines})
            return

        if tag in {"h4", "h5", "h6"}:
            _flush_inline_to_p()
            h_inlines = _trim_inline_whitespace(_collect_inline(node))
            if h_inlines:
                blocks.append({"tag": "h4", "children": h_inlines})
            return

        if tag in {"ul", "ol"}:
            _flush_inline_to_p()
            li_nodes: list[dict[str, Any]] = []
            for ch in node.children:
                if not ch.is_text and ch.tag == "li":
                    li_inlines = _trim_inline_whitespace(_collect_inline(ch))
                    if li_inlines:
                        li_nodes.append({"tag": "li", "children": li_inlines})
            if li_nodes:
                blocks.append({"tag": tag, "children": li_nodes})
            return

        # Telegraph 不支持 <table>，转换为清晰的列表行
        if tag == "table":
            _flush_inline_to_p()
            rows = node.find_all(tag="tr")
            li_rows: list[dict[str, Any]] = []
            for tr in rows:
                cells = [c for c in tr.children if not c.is_text and c.tag in {"td", "th"}]
                cell_parts: list[TelegraphNode] = []
                for c_idx, cell in enumerate(cells):
                    c_in = _trim_inline_whitespace(_collect_inline(cell))
                    if not c_in:
                        continue
                    if cell_parts:
                        cell_parts.append("  |  ")
                    if cell.tag == "th" or c_idx == 0:
                        cell_parts.append({"tag": "strong", "children": c_in})
                    else:
                        cell_parts.extend(c_in)
                if cell_parts:
                    li_rows.append({"tag": "li", "children": _merge_adjacent_strings(cell_parts)})
            if li_rows:
                blocks.append({"tag": "ul", "children": li_rows})
            return

        if tag in {"p", "div"}:
            if "forum-edit-note" in classes:
                _flush_inline_to_p()
                note_txt = node.inner_text().strip()
                if note_txt:
                    blocks.append({"tag": "p", "children": [{"tag": "em", "children": [note_txt]}]})
                return
            _flush_inline_to_p()
            for ch in node.children:
                _walk_block(ch)
            _flush_inline_to_p()
            return

        # 内联修饰标签（保留在其内部的非表情大图仍能正常拆出为独立 figure）
        has_block_img = any(
            "forum-smiley" not in img_n.classes for img_n in node.find_all(tag="img")
        )
        if has_block_img:
            for ch in node.children:
                _walk_block(ch)
            return

        inline_buf.extend(_collect_inline(node))

    _walk_block(root)
    return blocks


def build_full_telegraph_document(
    thread: ParsedThread,
    *,
    img_url_map: dict[str, str] | None = None,
    idx_url_map: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    """构造包含楼层元信息头、正文节点、评分表与原帖跳转尾部的完整 Telegraph 文档节点。"""
    nodes: list[dict[str, Any]] = []

    # 1. 顶部元信息摘要行
    meta_pieces: list[str] = []
    if thread.forum_name:
        meta_pieces.append(f"版块：{thread.forum_name}")
    author_desc = thread.author
    if thread.author_group:
        author_desc = f"{author_desc} ({thread.author_group})"
    meta_pieces.append(f"{'楼主' if thread.is_main_post else '发言'}：{author_desc}")
    meta_pieces.append(thread.display_floor_badge)
    if thread.created_at:
        meta_pieces.append(thread.created_at)
    if thread.views is not None:
        meta_pieces.append(f"浏览 {thread.views}")
    if thread.replies is not None:
        meta_pieces.append(f"回复 {thread.replies}")
    if thread.score is not None:
        meta_pieces.append(f"赞 {thread.score}")

    meta_children: list[TelegraphNode] = [
        {"tag": "em", "children": ["  ·  ".join(meta_pieces)]},
    ]
    nodes.append({"tag": "p", "children": meta_children})

    if thread.is_unblocked_invisible:
        reason_suffix = f"（{thread.invisible_reason}）" if thread.invisible_reason else ""
        nodes.append(
            {
                "tag": "aside",
                "children": [f"🔓 已通过官方接口解锁不可见内容{reason_suffix}"],
            }
        )

    nodes.append({"tag": "hr"})

    # 2. 正文节点
    body_nodes = convert_html_to_telegraph_nodes(
        thread.content_html,
        img_url_map=img_url_map,
        idx_url_map=idx_url_map,
    )
    if body_nodes:
        nodes.extend(body_nodes)
    else:
        nodes.append({"tag": "p", "children": [{"tag": "em", "children": ["（本层无文字内容）"]}]})

    # 3. 评分记录（如 S1 鹅评）
    if thread.rates:
        nodes.append({"tag": "hr"})
        rate_title = "主楼评分" if thread.is_main_post else "本层评分"
        if thread.rate_summary:
            rate_title = f"{rate_title}（{thread.rate_summary}）"
        nodes.append({"tag": "h4", "children": [f"📊 {rate_title}"]})
        rate_items: list[dict[str, Any]] = []
        for r in thread.rates:
            line_parts: list[TelegraphNode] = [
                {"tag": "strong", "children": [r.user]},
                f" ({r.score})",
            ]
            if r.reason:
                line_parts.append(f"：{r.reason}")
            rate_items.append({"tag": "li", "children": line_parts})
        nodes.append({"tag": "ul", "children": rate_items})

    # 4. 页脚原帖直达链接
    if thread.url.startswith(("http://", "https://")):
        nodes.append({"tag": "hr"})
        nodes.append(
            {
                "tag": "p",
                "children": [
                    {
                        "tag": "a",
                        "attrs": {"href": thread.url},
                        "children": [f"🔗 查看 {thread.forum.display_name} 原帖"],
                    }
                ],
            }
        )

    return nodes


def _split_nodes_by_byte_budget(
    nodes: list[dict[str, Any]],
    max_bytes: int = MAX_TELEGRAPH_CONTENT_BYTES,
) -> list[list[dict[str, Any]]]:
    """按 Telegraph 单页 64KB 限制安全切分节点列表，支持自动多页续读。"""
    full_encoded = json.dumps(nodes, ensure_ascii=False).encode("utf-8")
    if len(full_encoded) <= max_bytes:
        return [nodes]

    chunks: list[list[dict[str, Any]]] = []
    cur_chunk: list[dict[str, Any]] = []
    cur_bytes = 2  # "[]"

    for node in nodes:
        node_bytes = len(json.dumps(node, ensure_ascii=False).encode("utf-8")) + 2
        if cur_chunk and (cur_bytes + node_bytes) > max_bytes:
            chunks.append(cur_chunk)
            cur_chunk = [node]
            cur_bytes = 2 + node_bytes
        else:
            cur_chunk.append(node)
            cur_bytes += node_bytes

    if cur_chunk:
        chunks.append(cur_chunk)
    return chunks or [nodes]


class TelegraphPublisher:
    """负责将帖子图片通过流式二进制上传至 p.sda1.dev，并发布完整帖子内容至 telegra.ph。"""

    KV_TELEGRAPH_TOKEN = "telegraph_access_token"

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
        self._mem_token: str = ""

    async def _get_state(self, key: str, default: Any = None) -> Any:
        if self._kv_getter is not None:
            try:
                return await self._kv_getter(key, default)
            except Exception:
                return default
        return default

    async def _set_state(self, key: str, value: Any) -> None:
        if self._kv_setter is not None:
            try:
                await self._kv_setter(key, value)
            except Exception:
                pass

    async def _get_or_create_token(self, *, force_recreate: bool = False) -> str:
        if not force_recreate:
            if self._mem_token:
                return self._mem_token
            saved = await self._get_state(self.KV_TELEGRAPH_TOKEN, "")
            if saved and isinstance(saved, str):
                self._mem_token = saved.strip()
                if self._mem_token:
                    return self._mem_token

        timeout = aiohttp.ClientTimeout(total=12, connect=4, sock_connect=4, sock_read=8)
        last_err = ""
        for attempt in range(2):
            headers = {"User-Agent": DEFAULT_BROWSER_UA}
            if attempt > 0:
                headers["Connection"] = "close"
            try:
                async with self.session.post(
                    f"{TELEGRAPH_API_BASE}/createAccount",
                    data={
                        "short_name": "AstrBotForumParser",
                        "author_name": "AstrBot Forum Parser",
                    },
                    headers=headers,
                    timeout=timeout,
                ) as resp:
                    payload = await resp.json(content_type=None)
                    if resp.status == 200 and isinstance(payload, dict) and payload.get("ok"):
                        token = str((payload.get("result") or {}).get("access_token") or "").strip()
                        if token:
                            self._mem_token = token
                            await self._set_state(self.KV_TELEGRAPH_TOKEN, token)
                            logger.info("已自动创建并持久化缓存 telegra.ph 访问凭证。")
                            return token
                    last_err = str((payload or {}).get("error") or f"HTTP {resp.status}")
            except Exception as e:
                last_err = str(e).strip() or type(e).__name__
                if attempt == 0:
                    continue
        raise TelegraphPublishError(f"创建 telegra.ph 账号凭证失败: {last_err}")

    async def _upload_file_to_sda1(
        self,
        local_path: Path,
        filename: str,
        *,
        upload_url: str = DEFAULT_SDA1_UPLOAD_URL,
    ) -> str | None:
        """按 Binary 请求体调用 POST https://p.sda1.dev/api/v1/upload_external_noform?filename=... 上传单张图片。"""
        target_ep = (upload_url or DEFAULT_SDA1_UPLOAD_URL).strip()
        timeout = aiohttp.ClientTimeout(total=25, connect=5, sock_connect=5, sock_read=15)

        for attempt in range(2):
            headers = {
                "User-Agent": DEFAULT_BROWSER_UA,
                "Content-Type": "application/octet-stream",
            }
            if attempt > 0:
                headers["Connection"] = "close"
            try:
                with local_path.open("rb") as fp:
                    async with self.session.post(
                        target_ep,
                        params={"filename": filename},
                        data=fp,
                        headers=headers,
                        timeout=timeout,
                    ) as resp:
                        if resp.status != 200:
                            if resp.status >= 500 and attempt == 0:
                                continue
                            return None
                        payload = await resp.json(content_type=None)
                        if isinstance(payload, dict) and payload.get("success"):
                            u = str((payload.get("data") or {}).get("url") or "").strip()
                            if u.startswith("https://"):
                                return u
                            if u.startswith("http://"):
                                return "https://" + u.removeprefix("http://")
                        return None
            except Exception as e:
                logger.debug(f"上传图片至图床第 {attempt + 1} 次异常 ({filename}): {e}")
                if attempt == 0:
                    continue
        return None

    async def _rehost_post_images(
        self,
        thread: ParsedThread,
        temp_scope: TempScope,
        *,
        cookies: dict[str, str] | None,
        same_site_hosts: set[str] | None,
        upload_url: str,
        max_images: int,
    ) -> tuple[dict[str, str], dict[int, str]]:
        """流式下载帖子配图并转存至 p.sda1.dev，每传完一张立即删除本地临时文件以保障低内存与零磁盘堆积。"""
        if not thread.post_image_urls or max_images <= 0:
            return {}, {}

        target_urls = thread.post_image_urls[:max_images]
        idx_to_inlined_uri: dict[int, str] = {}
        if thread.content_html and "data:image/" in thread.content_html:
            for m in INLINED_FORUM_IMG_RE.finditer(thread.content_html):
                uri = m.group(1) or m.group(4) or ""
                idx_s = m.group(2) or m.group(3) or ""
                if uri and idx_s.isdigit():
                    idx_to_inlined_uri[int(idx_s)] = uri

        url_map: dict[str, str] = {}
        idx_map: dict[int, str] = {}
        used_bytes = 0
        sem = asyncio.Semaphore(2)
        prefix_tag = f"{thread.forum.value}_{thread.tid or 'post'}"

        async def _process_one(idx: int, orig_url: str) -> None:
            nonlocal used_bytes
            async with sem:
                remain = MAX_FORWARD_TOTAL_FILE_BYTES - used_bytes
                if remain <= 0:
                    return
                eff_cap = min(MAX_FORWARD_SINGLE_IMAGE_BYTES, remain)
                local_file: Path | None = None
                written = 0

                try:
                    inlined = idx_to_inlined_uri.get(idx)
                    if inlined:
                        est_sz = _data_uri_raw_byte_len(inlined)
                        if est_sz <= eff_cap:
                            mime_part = inlined.split(";", 1)[0].removeprefix("data:")
                            ext = mime_to_ext(mime_part)
                            dest = temp_scope.alloc_file(prefix=f"tg_up_{idx}", ext=ext)
                            written = await asyncio.to_thread(
                                _decode_data_uri_to_file_sync, inlined, dest
                            )
                            local_file = dest
                    else:
                        clean_u = rewrite_legacy_media_url(orig_url)
                        local_file, written = await _stream_single_image_to_scope(
                            self.session,
                            clean_u,
                            temp_scope,
                            referer=thread.url,
                            cookies=cookies,
                            same_site_hosts=same_site_hosts,
                            max_bytes=eff_cap,
                        )

                    if local_file is None or written <= 0:
                        return
                    used_bytes += written

                    ext = local_file.suffix.lstrip(".") or "jpg"
                    fname = f"{prefix_tag}_{idx + 1}.{ext}"
                    hosted_url = await self._upload_file_to_sda1(
                        local_file,
                        fname,
                        upload_url=upload_url,
                    )
                    if hosted_url:
                        idx_map[idx] = hosted_url
                        url_map[orig_url] = hosted_url
                        url_map[rewrite_legacy_media_url(orig_url)] = hosted_url
                except Exception as e:
                    logger.debug(f"转存帖子第 {idx + 1} 张配图至图床跳过: {e}")
                finally:
                    if local_file is not None:
                        await asyncio.to_thread(local_file.unlink, True)

        for batch_start in range(0, len(target_urls), 2):
            if used_bytes >= MAX_FORWARD_TOTAL_FILE_BYTES:
                break
            batch = [
                (i, target_urls[i])
                for i in range(batch_start, min(batch_start + 2, len(target_urls)))
            ]
            await asyncio.gather(*(_process_one(i, u) for i, u in batch))

        return url_map, idx_map

    async def _create_single_page(
        self,
        *,
        title: str,
        author_name: str,
        author_url: str,
        nodes: list[dict[str, Any]],
    ) -> str:
        token = await self._get_or_create_token(force_recreate=False)
        content_json = json.dumps(nodes, ensure_ascii=False)
        timeout = aiohttp.ClientTimeout(total=15, connect=4, sock_connect=4, sock_read=10)
        last_err = ""

        for attempt in range(2):
            form_data: dict[str, str] = {
                "access_token": token,
                "title": title[:250] or "论坛帖子",
                "content": content_json,
                "return_content": "false",
            }
            if author_name:
                form_data["author_name"] = author_name[:120]
            if author_url.startswith(("http://", "https://")) and len(author_url) <= 512:
                parsed_au = urlparse(author_url)
                if parsed_au.hostname:
                    form_data["author_url"] = author_url

            headers = {"User-Agent": DEFAULT_BROWSER_UA}
            if attempt > 0:
                headers["Connection"] = "close"

            try:
                async with self.session.post(
                    f"{TELEGRAPH_API_BASE}/createPage",
                    data=form_data,
                    headers=headers,
                    timeout=timeout,
                ) as resp:
                    payload = await resp.json(content_type=None)
                    if resp.status == 200 and isinstance(payload, dict) and payload.get("ok"):
                        page_url = str((payload.get("result") or {}).get("url") or "").strip()
                        if page_url.startswith("https://"):
                            return page_url
                    err_code = str((payload or {}).get("error") or f"HTTP {resp.status}")
                    last_err = err_code
                    if "ACCESS_TOKEN_INVALID" in err_code and attempt == 0:
                        token = await self._get_or_create_token(force_recreate=True)
                        continue
            except Exception as e:
                last_err = str(e).strip() or type(e).__name__
                if attempt == 0:
                    continue

        raise TelegraphPublishError(f"调用 telegra.ph createPage 失败: {last_err}")

    async def publish_thread(
        self,
        thread: ParsedThread,
        temp_scope: TempScope,
        *,
        cookies: dict[str, str] | None = None,
        same_site_hosts: set[str] | None = None,
        upload_images: bool = True,
        upload_url: str = DEFAULT_SDA1_UPLOAD_URL,
        max_upload_images: int = 25,
    ) -> str:
        """将解析后的帖子（及上传至 p.sda1.dev 的配图）发布到 telegra.ph 并返回文章链接。"""
        url_map: dict[str, str] = {}
        idx_map: dict[int, str] = {}
        if upload_images and thread.post_image_urls and max_upload_images > 0:
            url_map, idx_map = await self._rehost_post_images(
                thread,
                temp_scope,
                cookies=cookies,
                same_site_hosts=same_site_hosts,
                upload_url=upload_url,
                max_images=max_upload_images,
            )

        all_nodes = build_full_telegraph_document(
            thread,
            img_url_map=url_map,
            idx_url_map=idx_map,
        )
        pages = _split_nodes_by_byte_budget(all_nodes)

        base_title = f"[{thread.category}] {thread.title}" if thread.category else thread.title
        if not thread.is_main_post:
            base_title = f"{base_title} · {thread.display_floor_badge}"

        author_parts = [thread.forum.display_name]
        if thread.forum_name and thread.forum_name != thread.forum.display_name:
            author_parts.append(thread.forum_name)
        author_parts.append(thread.author)
        author_name = " · ".join(p for p in author_parts if p)

        # 若为多页超长帖，从最后一页倒序创建并在前一页末尾附加下一页跳转链接
        next_page_url = ""
        total_pages = len(pages)
        for page_idx in range(total_pages - 1, -1, -1):
            chunk_nodes = list(pages[page_idx])
            if next_page_url:
                chunk_nodes.append({"tag": "hr"})
                chunk_nodes.append(
                    {
                        "tag": "h4",
                        "children": [
                            {
                                "tag": "a",
                                "attrs": {"href": next_page_url},
                                "children": [
                                    f"👉 点击继续阅读下一页 ({page_idx + 2}/{total_pages})"
                                ],
                            }
                        ],
                    }
                )
            page_title = (
                base_title
                if total_pages == 1 or page_idx == 0
                else f"{base_title} ({page_idx + 1}/{total_pages})"
            )
            next_page_url = await self._create_single_page(
                title=page_title,
                author_name=author_name,
                author_url=thread.url,
                nodes=chunk_nodes,
            )

        return next_page_url
