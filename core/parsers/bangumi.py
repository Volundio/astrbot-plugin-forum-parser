from __future__ import annotations

import asyncio
import base64
import copy
import html
import json
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import aiohttp
from astrbot.api import logger

from ..html_sanitizer import (
    MiniNode,
    _fetch_image_as_data_uri,
    parse_html,
    process_post_html,
)
from ..io_guard import read_response_bounded
from ..models import (
    ForumLink,
    ForumLoginRequiredError,
    ForumParseError,
    ForumType,
    ParsedThread,
    ReactionItem,
    RelatedSubject,
)
from ..temp_manager import TempScope
from .base import DEFAULT_BROWSER_UA, BaseForumParser, parse_cookie_string

BGM_WEB_BASE = "https://bangumi.tv"
BGM_NEXT_API_BASE = "https://next.bgm.tv/p1"
BGM_API_UA = (
    "Volundio/astrbot-plugin-forum-parser (https://github.com/Volundio/astrbot-plugin-forum-parser)"
)
BGM_SAME_SITE_HOSTS = {
    "bangumi.tv",
    "bgm.tv",
    "chii.in",
    "lain.bgm.tv",
}

BGM_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=14, connect=5, sock_connect=5, sock_read=9)
CHINA_TZ = timezone(timedelta(hours=8))

BG_IMAGE_URL_RE = re.compile(r"url\(['\"]?([^'\")]+)['\"]?\)", re.IGNORECASE)
DATA_LIKES_RE = re.compile(r"var\s+data_likes_list\s*=\s*(\{.*?\});", re.DOTALL)
SUBJECT_ID_RE = re.compile(r"/subject/(\d+)")
USER_ID_RE = re.compile(r"/user/([^/?#]+)")
BGM_SMILE_RE = re.compile(r"\(bgm(\d+)\)", re.IGNORECASE)
BGM_MUSUME_RE = re.compile(r"\(musume_(\d+)\)", re.IGNORECASE)
BGM_BLAKE_RE = re.compile(r"\(blake_(\d+)\)", re.IGNORECASE)
BGM_BMO_TEXT_RE = re.compile(r"\(bmo[A-Za-z0-9_:|=-]{1,200}\)")
BMO_SPAN_RE = re.compile(
    r'<span\b[^>]*\bclass="[^"]*\bbmo\b[^"]*"[^>]*\bdata-code="([^"]+)"[^>]*>(?:</span>)?',
    re.IGNORECASE,
)
BMO_SPAN_FALLBACK_RE = re.compile(
    r'<span\b[^>]*\bclass="[^"]*\bbmo\b[^"]*"[^>]*>.*?</span>',
    re.IGNORECASE | re.DOTALL,
)
BGM_CLASSIC_GIF_CODES = frozenset({11, 23})
BGM_BMO_JS_URL = f"{BGM_WEB_BASE}/js/lib/bmo/bmo.js"
BGM_BMO_MANIFEST_URL = f"{BGM_WEB_BASE}/js/lib/bmo/assets/manifest.local.json"

BGM_KNOWN_BBCODE_TAGS = frozenset(
    {
        "b",
        "i",
        "u",
        "s",
        "mask",
        "color",
        "size",
        "align",
        "center",
        "left",
        "right",
        "url",
        "img",
        "quote",
        "code",
        "list",
    }
)


def _format_bgm_exc(exc: BaseException) -> str:
    msg = str(exc).strip()
    if isinstance(exc, TimeoutError | asyncio.TimeoutError):
        return f"连接或读取超时 ({msg})" if msg else "连接或读取超时 (TimeoutError)"
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def _format_ts_cn(ts: int | float | str | None) -> str:
    if ts is None or ts == "":
        return ""
    try:
        val = float(ts)
        if val > 1e11:
            val /= 1000.0
        if val <= 0:
            return ""
        return datetime.fromtimestamp(val, tz=CHINA_TZ).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OSError):
        return str(ts)


def _resolve_bgm_url(raw: str) -> str:
    u = (raw or "").strip()
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.startswith(("http://", "https://")):
        return u
    if u.startswith(("javascript:", "vbscript:", "data:", "file:", "mailto:")):
        return ""
    return urljoin(BGM_WEB_BASE + "/", u)


def _extract_bg_image_url(style_str: str) -> str:
    if not style_str:
        return ""
    m = BG_IMAGE_URL_RE.search(style_str)
    return _resolve_bgm_url(m.group(1)) if m else ""


def _split_floor_and_time(raw_meta: str) -> tuple[str, str]:
    """解析 `#1 - 2026-11-8 10:51` 为 `(floor, time)`。"""
    cleaned = re.sub(r"\s+", " ", (raw_meta or "").strip())
    if " - " in cleaned:
        fl, ts = cleaned.split(" - ", 1)
        return fl.strip(), ts.strip()
    return cleaned, ""


def _extract_leading_category(title: str) -> tuple[str, str]:
    m = re.match(r"^\[([^\[\]]{1,12})\]\s*(.+)$", (title or "").strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", (title or "").strip()


def _bgm_reaction_emoji_url(emoji_val: str) -> str:
    """根据 Bangumi data_likes_list 的 emoji 编号构造正确的贴贴表情地址（兼容零填充与分段目录）。"""
    e = (emoji_val or "").strip()
    if not e:
        return ""
    if e.isdigit():
        num = int(e)
        if 1 <= num <= 105:
            return f"{BGM_WEB_BASE}/img/smiles/tv/{num:02d}.gif"
        if 200 <= num <= 299:
            return f"{BGM_WEB_BASE}/img/smiles/tv_vs/bgm_{num}.png"
        if 500 <= num <= 599:
            return f"{BGM_WEB_BASE}/img/smiles/tv_500/bgm_{num}.gif"
    return f"{BGM_WEB_BASE}/img/smiles/tv/{e}.gif"


def _parse_likes_reactions(raw_html: str, post_id: str) -> tuple[int | None, list[ReactionItem]]:
    """从页面内联脚本 `var data_likes_list = ...` 提取指定楼层/日志的贴贴表态分布（兼容 dict 与 list）。"""
    if not raw_html or not post_id:
        return None, []
    m = DATA_LIKES_RE.search(raw_html)
    if not m:
        return None, []
    try:
        data = json.loads(m.group(1))
    except Exception:
        return None, []
    if not isinstance(data, dict):
        return None, []

    raw_target = data.get(str(post_id))
    if isinstance(raw_target, dict):
        raw_entries = list(raw_target.values())
    elif isinstance(raw_target, list):
        raw_entries = raw_target
    else:
        return None, []
    if not raw_entries:
        return None, []

    items: list[ReactionItem] = []
    total_cnt = 0
    for val in raw_entries:
        if not isinstance(val, dict):
            continue
        emoji = str(val.get("emoji") or "").strip()
        try:
            cnt = int(val.get("total") or 0)
        except (TypeError, ValueError):
            cnt = 0
        if cnt <= 0:
            continue
        total_cnt += cnt
        emoji_url = _bgm_reaction_emoji_url(emoji)
        items.append(
            ReactionItem(
                emoji_url=emoji_url,
                label=f"bgm{emoji}" if emoji else "👍",
                count=cnt,
            )
        )

    items.sort(key=lambda r: r.count, reverse=True)
    return (total_cnt if total_cnt > 0 else None), items[:10]


def _bgm_smile_url(code_num: int) -> str:
    """按 Bangumi 官方表情编号规则返回对应图标 URL（注意 bgm01~23 仅 11/23 为 .gif，其余为 .png）。"""
    if 1 <= code_num <= 23:
        ext = "gif" if code_num in BGM_CLASSIC_GIF_CODES else "png"
        return f"{BGM_WEB_BASE}/img/smiles/bgm/{code_num:02d}.{ext}"
    if 24 <= code_num <= 125:
        return f"{BGM_WEB_BASE}/img/smiles/tv/{code_num - 23:02d}.gif"
    if 200 <= code_num <= 299:
        return f"{BGM_WEB_BASE}/img/smiles/tv_vs/bgm_{code_num}.png"
    if 500 <= code_num <= 599:
        return f"{BGM_WEB_BASE}/img/smiles/tv_500/bgm_{code_num}.gif"
    return ""


def convert_bgm_bbcode_to_html(bbcode: str) -> str:
    """将 Bangumi (Chii BBCode) 文本安全转换为语义化 HTML（用于 next.bgm.tv API 兜底）。"""
    if not bbcode:
        return ""
    text = bbcode.replace("\r\n", "\n").replace("\r", "\n")

    # 预先保护 [code]...[/code] 与 [img]...[/img] 内容不被内部标签误切分
    protected_blocks: list[str] = []

    def _stash(fragment: str) -> str:
        idx = len(protected_blocks)
        protected_blocks.append(fragment)
        return f"\x00BGMPROT_{idx}\x00"

    text = re.sub(
        r"\[code\](.*?)\[/code\]",
        lambda m: _stash(f'<pre class="forum-code">{html.escape(m.group(1).strip())}</pre>'),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    def _img_sub(m: re.Match[str]) -> str:
        raw_u = _resolve_bgm_url(m.group(1).strip())
        if not raw_u:
            return ""
        esc_u = html.escape(raw_u, quote=True)
        return _stash(f'<img class="forum-img" src="{esc_u}" alt="Bangumi图片" />')

    text = re.sub(r"\[img\](.*?)\[/img\]", _img_sub, text, flags=re.IGNORECASE | re.DOTALL)

    # 先进行 HTML 转义，防止 XSS 注入
    escaped = html.escape(text, quote=False)

    # 还原 `(bgmXXX)`、`(musume_XX)`、`(blake_XX)` 与 `(bmo...)` 表情为标准 HTML 节点
    def _smile_sub(m: re.Match[str]) -> str:
        num = int(m.group(1))
        su = _bgm_smile_url(num)
        if not su:
            return m.group(0)
        return _stash(
            f'<img src="{html.escape(su, quote=True)}" class="smile" '
            f'smileid="{num}" alt="(bgm{num})" />'
        )

    escaped = BGM_SMILE_RE.sub(_smile_sub, escaped)
    escaped = BGM_MUSUME_RE.sub(
        lambda m: _stash(
            f'<img src="https://lain.bgm.tv/img/smiles/musume/musume_{m.group(1)}.gif" '
            f'class="smile smile-dynamic" smileid="musume_{m.group(1)}" alt="(musume_{m.group(1)})" />'
        ),
        escaped,
    )
    escaped = BGM_BLAKE_RE.sub(
        lambda m: _stash(
            f'<img src="https://lain.bgm.tv/img/smiles/blake/blake_{m.group(1)}.gif" '
            f'class="smile smile-dynamic" smileid="blake_{m.group(1)}" alt="(blake_{m.group(1)})" />'
        ),
        escaped,
    )
    escaped = BGM_BMO_TEXT_RE.sub(
        lambda m: _stash(
            f'<span class="bmo" data-code="{html.escape(m.group(0), quote=True)}"></span>'
        ),
        escaped,
    )

    # 处理 [url=...]text[/url] 与 [url]link[/url]
    def _url_sub(m: re.Match[str]) -> str:
        href = _resolve_bgm_url(html.unescape((m.group(1) or m.group(2) or "").strip()))
        label = (m.group(2) or m.group(1) or "").strip()
        if not href:
            return label
        return f'<a href="{html.escape(href, quote=True)}">{label}</a>'

    escaped = re.sub(
        r"\[url=([^\]]+)\](.*?)\[/url\]|\[url\](.*?)\[/url\]",
        lambda m: (
            _url_sub(m)
            if m.group(1) is not None
            else (
                f'<a href="{html.escape(_resolve_bgm_url(html.unescape(m.group(3))), quote=True)}">'
                f"{m.group(3)}</a>"
                if _resolve_bgm_url(html.unescape(m.group(3)))
                else m.group(3)
            )
        ),
        escaped,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # 简单成对标签多次迭代展开（由内向外）
    simple_rules = [
        (r"\[b\](.*?)\[/b\]", r"<strong>\1</strong>"),
        (r"\[i\](.*?)\[/i\]", r"<em>\1</em>"),
        (r"\[u\](.*?)\[/u\]", r"<u>\1</u>"),
        (r"\[s\](.*?)\[/s\]", r'<del class="forum-del">\1</del>'),
        (r"\[mask\](.*?)\[/mask\]", r'<span class="forum-spoiler">\1</span>'),
        (r"\[quote\](.*?)\[/quote\]", r'<div class="quote">\1</div>'),
        (r"\[center\](.*?)\[/center\]", r'<p style="text-align:center">\1</p>'),
        (r"\[right\](.*?)\[/right\]", r'<p style="text-align:right">\1</p>'),
        (r"\[left\](.*?)\[/left\]", r'<p style="text-align:left">\1</p>'),
        (r"\[align=(left|center|right)\](.*?)\[/align\]", r'<p style="text-align:\1">\2</p>'),
        (
            r"\[color=([#a-zA-Z0-9(),.\s]+)\](.*?)\[/color\]",
            r'<span style="color:\1">\2</span>',
        ),
        (r"\[size=(\d+(?:\.\d+)?)\](.*?)\[/size\]", r'<span style="font-size:\1px">\2</span>'),
    ]
    for _ in range(4):
        prev = escaped
        for pat, repl in simple_rules:
            escaped = re.sub(pat, repl, escaped, flags=re.IGNORECASE | re.DOTALL)
        if escaped == prev:
            break

    # 列表 [*] ...
    def _list_sub(m: re.Match[str]) -> str:
        inner = m.group(1).strip()
        items = [x.strip() for x in re.split(r"\[\*\]", inner) if x.strip()]
        if not items:
            return inner
        return "<ul>" + "".join(f"<li>{it}</li>" for it in items) + "</ul>"

    escaped = re.sub(r"\[list(?:=[^\]]*)?\](.*?)\[/list\]", _list_sub, escaped, flags=re.I | re.S)

    escaped = escaped.replace("\n", "<br />\n")

    for idx, frag in enumerate(protected_blocks):
        escaped = escaped.replace(f"\x00BGMPROT_{idx}\x00", frag)

    return escaped


class BangumiParser(BaseForumParser):
    """Bangumi 番组计划（bangumi.tv / bgm.tv / chii.in）小组话题与日志长评解析器。"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(session, **kwargs)
        self.cfg: dict[str, Any] = config or {}
        self._cached_bmo_js: str | None = None
        self._cached_bmo_manifest: dict[str, Any] | None = None
        self._bmo_png_cache: OrderedDict[str, str] = OrderedDict()

    def update_config(self, config: dict[str, Any] | None) -> None:
        self.cfg = config or {}

    def _build_cookies(self) -> tuple[dict[str, str], bool]:
        raw_cookie = str(self.cfg.get("cookie") or "").strip()
        cookies = parse_cookie_string(raw_cookie) if raw_cookie else {}
        return cookies, bool(cookies)

    def get_media_auth_context(self) -> tuple[dict[str, str] | None, set[str]]:
        cookies, has_auth = self._build_cookies()
        return (cookies if has_auth else None), BGM_SAME_SITE_HOSTS

    async def _fetch_web_html(
        self, url: str, cookies: dict[str, str] | None
    ) -> tuple[str | None, str]:
        headers = {
            "User-Agent": DEFAULT_BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": BGM_WEB_BASE + "/",
        }
        last_err = ""
        for attempt in range(2):
            req_headers = dict(headers)
            if attempt > 0:
                req_headers["Connection"] = "close"
            try:
                async with self.session.get(
                    url,
                    headers=req_headers,
                    cookies=cookies or None,
                    timeout=BGM_REQUEST_TIMEOUT,
                    allow_redirects=True,
                ) as resp:
                    if resp.status != 200:
                        last_err = f"HTTP {resp.status}"
                        if resp.status >= 500 and attempt == 0:
                            continue
                        return None, last_err
                    raw_bytes = await read_response_bounded(resp)
                    return raw_bytes.decode("utf-8", errors="replace"), ""
            except Exception as e:
                last_err = _format_bgm_exc(e)
                if attempt == 0:
                    continue
        logger.warning(f"Bangumi 网页请求失败 ({url}): {last_err}")
        return None, last_err

    async def _fetch_next_api_json(self, path: str) -> tuple[Any, str]:
        url = f"{BGM_NEXT_API_BASE}/{path.lstrip('/')}"
        headers = {
            "User-Agent": BGM_API_UA,
            "Accept": "application/json",
        }
        last_err = ""
        for attempt in range(2):
            req_headers = dict(headers)
            if attempt > 0:
                req_headers["Connection"] = "close"
            try:
                async with self.session.get(
                    url, headers=req_headers, timeout=BGM_REQUEST_TIMEOUT
                ) as resp:
                    if resp.status != 200:
                        last_err = f"HTTP {resp.status}"
                        if resp.status >= 500 and attempt == 0:
                            continue
                        return None, last_err
                    return await resp.json(content_type=None), ""
            except Exception as e:
                last_err = _format_bgm_exc(e)
                if attempt == 0:
                    continue
        return None, last_err

    @staticmethod
    def _extract_notice_error(doc: MiniNode) -> str:
        notice = doc.find_first(id_exact="colunmNotice") or doc.find_first(id_exact="columnNotice")
        if notice is None:
            return ""
        text_el = notice.find_first(class_name="text")
        if text_el:
            return text_el.inner_text().strip()
        msg_el = notice.find_first(class_name="message")
        return msg_el.inner_text().strip() if msg_el else "页面无法访问"

    @staticmethod
    def _extract_reply_div(
        doc: MiniNode,
        post_div: MiniNode,
        *,
        op_uid: str,
    ) -> dict[str, Any]:
        """提取评论区主回复或楼中楼子回复的详细信息。"""
        post_pid = post_div.get_attr("id").removeprefix("post_").strip()
        is_sub = "sub_reply_bg" in post_div.classes

        re_info = post_div.find_first(class_name="re_info")
        small_el = re_info.find_first(tag="small") if re_info else None
        floor_txt, created_at = _split_floor_and_time(small_el.inner_text() if small_el else "")

        author = "匿名"
        author_id = ""
        inner = post_div.find_first(class_name="inner")
        if inner:
            author_a = inner.find_first(tag="a", class_name="l")
            if author_a:
                author = author_a.inner_text().strip() or author
                m_uid = USER_ID_RE.search(author_a.get_attr("href"))
                if m_uid:
                    author_id = m_uid.group(1)

        if not author_id:
            author_id = post_div.get_attr("data-item-user").strip()

        sign_el = post_div.find_first(class_name="sign")
        author_sign = sign_el.inner_text().strip() if sign_el else ""
        if author_sign.startswith("(") and author_sign.endswith(")"):
            author_sign = author_sign[1:-1].strip()

        avatar_url = ""
        av_span = post_div.find_first(class_name="avatarNeue")
        if av_span:
            avatar_url = _extract_bg_image_url(av_span.get_attr("style"))

        # 正文与子回复父楼引用上下文
        raw_html = ""
        if is_sub:
            cmt_div = post_div.find_first(class_name="cmt_sub_content")
            raw_html = "".join(c.to_html() for c in cmt_div.children) if cmt_div else ""

            # 查找所属父回复以还原语境
            parent_row = post_div.parent
            while parent_row is not None and "row_reply" not in parent_row.classes:
                parent_row = parent_row.parent
            if parent_row is not None:
                p_re_info = parent_row.find_first(class_name="re_info")
                p_small = p_re_info.find_first(tag="small") if p_re_info else None
                p_fl, _ = _split_floor_and_time(p_small.inner_text() if p_small else "")
                p_inner = parent_row.find_first(class_name="inner")
                p_author_a = p_inner.find_first(tag="a", class_name="l") if p_inner else None
                p_author = p_author_a.inner_text().strip() if p_author_a else "层主"
                p_msg_el = parent_row.find_first(class_name="message")
                p_snippet = re.sub(r"\s+", " ", p_msg_el.inner_text().strip()) if p_msg_el else ""
                if len(p_snippet) > 140:
                    p_snippet = p_snippet[:140] + "……"
                if p_snippet:
                    quote_hdr = (
                        f"@{html.escape(p_author)} ({html.escape(p_fl)})"
                        if p_fl
                        else f"@{html.escape(p_author)}"
                    )
                    raw_html = (
                        f'<div class="quote"><strong>{quote_hdr}：</strong>'
                        f"<br />{html.escape(p_snippet)}</div>{raw_html}"
                    )
        else:
            msg_el = post_div.find_first(class_name="message")
            raw_html = "".join(c.to_html() for c in msg_el.children) if msg_el else ""

        item_uid = post_div.get_attr("data-item-user").strip()
        is_thread_author = bool(op_uid and item_uid and op_uid == item_uid)
        fl_badge = floor_txt or f"PID #{post_pid}"
        if is_thread_author and fl_badge != "#1":
            floor_label = f"楼主 · {fl_badge}"
        else:
            floor_label = fl_badge

        return {
            "pid": post_pid,
            "floor_label": floor_label,
            "is_main_post": False,
            "is_thread_author": is_thread_author,
            "author": author,
            "author_id": author_id,
            "author_avatar": avatar_url,
            "author_sign": author_sign,
            "created_at": created_at,
            "raw_html": raw_html,
        }

    def _parse_topic_html(
        self,
        raw_html: str,
        *,
        sub_type: str,
        tid: str,
        target_pid: str,
    ) -> dict[str, Any]:
        doc = parse_html(raw_html)
        notice_err = self._extract_notice_error(doc)
        if notice_err:
            raise ForumParseError(notice_err)

        forum_name = ""
        group_icon = ""
        raw_title = ""
        related_subjects: list[RelatedSubject] = []

        if sub_type == "subject_topic":
            hdr_title = doc.find_first(tag="h1", class_name="title")
            if hdr_title:
                raw_title = hdr_title.inner_text().strip()
            subj_info = doc.find_first(id_exact="subject_inner_info")
            if subj_info:
                av_a = subj_info.find_first(tag="a", class_name="avatar")
                if av_a:
                    img = av_a.find_first(tag="img")
                    if img:
                        group_icon = _resolve_bgm_url(img.get_attr("src"))
                    m_sid = SUBJECT_ID_RE.search(av_a.get_attr("href"))
                    subj_id = m_sid.group(1) if m_sid else ""
                else:
                    subj_id = ""
                first_li_a = subj_info.find_first(tag="li")
                if first_li_a:
                    link_a = first_li_a.find_first(tag="a")
                    if link_a:
                        forum_name = link_a.inner_text().strip()
                if forum_name and subj_id:
                    related_subjects.append(
                        RelatedSubject(
                            subject_id=subj_id,
                            title=forum_name,
                            subtitle="",
                            cover=group_icon,
                        )
                    )
        else:
            page_hdr = doc.find_first(id_exact="pageHeader")
            h1 = page_hdr.find_first(tag="h1") if page_hdr else None
            if h1:
                grp_span = h1.find_first(tag="span")
                if grp_span:
                    grp_a = grp_span.find_first(tag="a", class_name="avatar")
                    if grp_a:
                        grp_img = grp_a.find_first(tag="img")
                        if grp_img:
                            group_icon = _resolve_bgm_url(grp_img.get_attr("src"))
                        forum_name = re.sub(r"\s+", " ", grp_a.inner_text()).strip()
                # 取 h1 中不属于顶部面包屑 span 的正文文本作为主标题
                title_parts = [
                    ch.inner_text() if not ch.is_text else ch.text
                    for ch in h1.children
                    if ch is not grp_span and (ch.is_text or ch.tag != "br")
                ]
                raw_title = re.sub(r"\s+", " ", "".join(title_parts)).strip()

        category, title = _extract_leading_category(raw_title or f"Bangumi 话题 #{tid}")

        post_topic = doc.find_first(class_name="postTopic")
        if post_topic is None:
            raise ForumParseError("未能从 Bangumi 页面提取到话题主楼内容")

        op_pid = post_topic.get_attr("id").removeprefix("post_").strip()
        op_uid = post_topic.get_attr("data-item-user").strip()

        cmt_list = doc.find_first(id_exact="comment_list")
        reply_divs = cmt_list.find_all(tag="div", id_prefix="post_") if cmt_list is not None else []
        replies_count = len(reply_divs)

        # 指定楼层匹配（若传入 #post_... 且不等于主楼 post_id）
        if target_pid and target_pid != op_pid and cmt_list is not None:
            target_div = cmt_list.find_first(tag="div", id_exact=f"post_{target_pid}")
            if target_div is None:
                raise ForumParseError(f"页面中未找到指定楼层 (#post_{target_pid})")
            rep = self._extract_reply_div(doc, target_div, op_uid=op_uid)
            score, reactions = _parse_likes_reactions(raw_html, target_pid)
            return {
                "tid": tid,
                "pid": rep["pid"],
                "floor_label": rep["floor_label"],
                "is_main_post": False,
                "is_thread_author": rep["is_thread_author"],
                "title": title,
                "category": category,
                "forum_name": forum_name
                or ("条目讨论" if sub_type == "subject_topic" else "小组讨论"),
                "group_icon": group_icon,
                "author": rep["author"],
                "author_id": rep["author_id"],
                "author_avatar": rep["author_avatar"],
                "author_sign": rep["author_sign"],
                "created_at": rep["created_at"],
                "reading_time": "",
                "replies": replies_count,
                "score": score,
                "reactions": reactions,
                "related_subjects": related_subjects,
                "raw_html": rep["raw_html"],
            }

        # 提取主楼
        re_info = post_topic.find_first(class_name="re_info")
        small_el = re_info.find_first(tag="small") if re_info else None
        floor_txt, created_at = _split_floor_and_time(small_el.inner_text() if small_el else "")

        author = "匿名"
        author_id = ""
        inner = post_topic.find_first(class_name="inner")
        if inner:
            author_a = inner.find_first(tag="a", class_name="l")
            if author_a:
                author = author_a.inner_text().strip() or author
                m_uid = USER_ID_RE.search(author_a.get_attr("href"))
                if m_uid:
                    author_id = m_uid.group(1)
        if not author_id:
            author_id = op_uid

        sign_el = post_topic.find_first(class_name="sign")
        author_sign = sign_el.inner_text().strip() if sign_el else ""
        if author_sign.startswith("(") and author_sign.endswith(")"):
            author_sign = author_sign[1:-1].strip()

        avatar_url = ""
        av_span = post_topic.find_first(class_name="avatarNeue")
        if av_span:
            avatar_url = _extract_bg_image_url(av_span.get_attr("style"))

        topic_content = post_topic.find_first(class_name="topic_content")
        content_html = "".join(c.to_html() for c in topic_content.children) if topic_content else ""
        score, reactions = _parse_likes_reactions(raw_html, op_pid)

        return {
            "tid": tid,
            "pid": op_pid,
            "floor_label": f"楼主 · {floor_txt}" if floor_txt else "楼主 · #1",
            "is_main_post": True,
            "is_thread_author": True,
            "title": title,
            "category": category,
            "forum_name": forum_name or ("条目讨论" if sub_type == "subject_topic" else "小组讨论"),
            "group_icon": group_icon,
            "author": author,
            "author_id": author_id,
            "author_avatar": avatar_url,
            "author_sign": author_sign,
            "created_at": created_at,
            "reading_time": "",
            "replies": replies_count,
            "score": score,
            "reactions": reactions,
            "related_subjects": related_subjects,
            "raw_html": content_html,
        }

    def _parse_blog_html(
        self,
        raw_html: str,
        *,
        tid: str,
        target_pid: str,
    ) -> dict[str, Any]:
        doc = parse_html(raw_html)
        notice_err = self._extract_notice_error(doc)
        if notice_err:
            raise ForumParseError(notice_err)

        entry = doc.find_first(id_exact="viewEntry")
        if entry is None:
            raise ForumParseError("未能从 Bangumi 页面提取到日志内容")

        h1_title = entry.find_first(tag="h1", class_name="title")
        raw_title = h1_title.inner_text().strip() if h1_title else f"Bangumi 日志 #{tid}"
        category, title = _extract_leading_category(raw_title)

        op_author = "匿名"
        op_uid = ""
        op_avatar = ""
        user_card = entry.find_first(class_name="user-card")
        if user_card:
            av_img = user_card.find_first(tag="img")
            if av_img:
                op_avatar = _resolve_bgm_url(av_img.get_attr("src"))
            title_box = user_card.find_first(class_name="title")
            if title_box:
                a_el = title_box.find_first(tag="a")
                if a_el:
                    op_author = a_el.inner_text().strip() or op_author
                    m_uid = USER_ID_RE.search(a_el.get_attr("href"))
                    if m_uid:
                        op_uid = m_uid.group(1)

        created_at = ""
        reading_time = ""
        time_div = entry.find_first(class_name="time")
        if time_div:
            raw_time = re.sub(r"\s+", " ", time_div.inner_text()).strip()
            parts = [p.strip() for p in raw_time.split("·") if p.strip()]
            if parts:
                created_at = parts[0]
            for p in parts[1:]:
                if "阅读" in p:
                    reading_time = p
                    break

        cmt_list = doc.find_first(id_exact="comment_list")
        reply_divs = cmt_list.find_all(tag="div", id_prefix="post_") if cmt_list is not None else []
        replies_count = len(reply_divs)

        # 解析关联作品条目卡片
        related_subjects: list[RelatedSubject] = []
        rel_box = doc.find_first(class_name="entry-related-subjects")
        if rel_box:
            for card in rel_box.find_all(class_name="subject-card")[:6]:
                cov_img = card.find_first(tag="img")
                cover = _resolve_bgm_url(cov_img.get_attr("src")) if cov_img else ""
                p_title = card.find_first(tag="p", class_name="title")
                t_a = p_title.find_first(tag="a") if p_title else None
                if not t_a:
                    continue
                m_sid = SUBJECT_ID_RE.search(t_a.get_attr("href"))
                sid = m_sid.group(1) if m_sid else ""
                s_title = re.sub(r"\s+", " ", t_a.inner_text()).strip()
                p_tip = card.find_first(tag="p", class_name="tip")
                s_sub = re.sub(r"\s+", " ", p_tip.inner_text()).strip() if p_tip else ""
                if s_title:
                    related_subjects.append(
                        RelatedSubject(subject_id=sid, title=s_title, subtitle=s_sub, cover=cover)
                    )

        # 若指定具体评论楼层 #post_...
        if target_pid and cmt_list is not None:
            target_div = cmt_list.find_first(tag="div", id_exact=f"post_{target_pid}")
            if target_div is None:
                raise ForumParseError(f"日志页面中未找到指定评论 (#post_{target_pid})")
            rep = self._extract_reply_div(doc, target_div, op_uid=op_uid)
            # 对评论中的“作者”再结合 op_author 二次核对
            if not rep["is_thread_author"] and rep["author"] == op_author:
                rep["is_thread_author"] = True
                if rep["floor_label"] and not rep["floor_label"].startswith("楼主"):
                    rep["floor_label"] = f"楼主 · {rep['floor_label']}"
            score, reactions = _parse_likes_reactions(raw_html, target_pid)
            return {
                "tid": tid,
                "pid": rep["pid"],
                "floor_label": rep["floor_label"],
                "is_main_post": False,
                "is_thread_author": rep["is_thread_author"],
                "title": title,
                "category": category,
                "forum_name": "日志 · 长评",
                "group_icon": "",
                "author": rep["author"],
                "author_id": rep["author_id"],
                "author_avatar": rep["author_avatar"],
                "author_sign": rep["author_sign"],
                "created_at": rep["created_at"],
                "reading_time": "",
                "replies": replies_count,
                "score": score,
                "reactions": reactions,
                "related_subjects": related_subjects,
                "raw_html": rep["raw_html"],
            }

        content_div = doc.find_first(id_exact="entry_content")
        content_html = "".join(c.to_html() for c in content_div.children) if content_div else ""
        score, reactions = _parse_likes_reactions(raw_html, tid)

        return {
            "tid": tid,
            "pid": "",
            "floor_label": "日志全文",
            "is_main_post": True,
            "is_thread_author": True,
            "title": title,
            "category": category,
            "forum_name": "日志 · 长评",
            "group_icon": "",
            "author": op_author,
            "author_id": op_uid,
            "author_avatar": op_avatar,
            "author_sign": "",
            "created_at": created_at,
            "reading_time": reading_time,
            "replies": replies_count,
            "score": score,
            "reactions": reactions,
            "related_subjects": related_subjects,
            "raw_html": content_html,
        }

    async def _fetch_via_next_api(
        self, sub_type: str, tid: str, target_pid: str
    ) -> dict[str, Any] | None:
        if sub_type == "group_topic":
            data, _ = await self._fetch_next_api_json(f"groups/-/topics/{tid}")
            if not isinstance(data, dict) or not data.get("title"):
                return None
            replies = data.get("replies") or []
            grp = data.get("group") or {}
            icon_obj = grp.get("icon") or {}
            group_icon = _resolve_bgm_url(icon_obj.get("medium") or icon_obj.get("large") or "")
            category, title = _extract_leading_category(str(data.get("title") or ""))

            target_post = replies[0] if replies else {}
            is_main = True
            floor_lbl = "楼主 · #1"
            if target_pid:
                found = next(
                    (
                        r
                        for r in replies
                        if isinstance(r, dict) and str(r.get("id") or "") == target_pid
                    ),
                    None,
                )
                if found is None:
                    return None
                target_post = found
                idx = replies.index(found) + 1
                is_main = idx == 1 and str(found.get("creatorID") or "") == str(
                    data.get("creatorID") or ""
                )
                floor_lbl = "楼主 · #1" if is_main else f"#{idx}"

            creator = target_post.get("creator") or {}
            creator_id = str(
                creator.get("username")
                or target_post.get("creatorID")
                or data.get("creatorID")
                or ""
            )
            av_obj = creator.get("avatar") or {}
            raw_bb = str(target_post.get("content") or "")
            return {
                "tid": tid,
                "pid": str(target_post.get("id") or target_pid),
                "floor_label": floor_lbl,
                "is_main_post": is_main,
                "is_thread_author": str(target_post.get("creatorID") or "")
                == str(data.get("creatorID") or ""),
                "title": title,
                "category": category,
                "forum_name": str(grp.get("title") or "小组讨论"),
                "group_icon": group_icon,
                "author": str(creator.get("nickname") or creator_id or "Bangumi 用户"),
                "author_id": creator_id,
                "author_avatar": _resolve_bgm_url(
                    av_obj.get("medium") or av_obj.get("large") or ""
                ),
                "author_sign": str(creator.get("sign") or ""),
                "created_at": _format_ts_cn(target_post.get("createdAt") or data.get("createdAt")),
                "reading_time": "",
                "replies": data.get("replyCount"),
                "score": None,
                "reactions": [],
                "related_subjects": [],
                "raw_html": convert_bgm_bbcode_to_html(raw_bb),
            }

        if sub_type == "blog" and not target_pid:
            data, _ = await self._fetch_next_api_json(f"blogs/{tid}")
            if not isinstance(data, dict) or not data.get("title"):
                return None
            subj_list, _ = await self._fetch_next_api_json(f"blogs/{tid}/subjects")
            related_subjects: list[RelatedSubject] = []
            if isinstance(subj_list, list):
                for item in subj_list[:6]:
                    if not isinstance(item, dict):
                        continue
                    imgs = item.get("images") or {}
                    related_subjects.append(
                        RelatedSubject(
                            subject_id=str(item.get("id") or ""),
                            title=str(item.get("name") or item.get("nameCN") or ""),
                            subtitle=str(item.get("nameCN") or ""),
                            cover=_resolve_bgm_url(imgs.get("medium") or imgs.get("common") or ""),
                        )
                    )
            user_obj = data.get("user") or {}
            av_obj = user_obj.get("avatar") or {}
            category, title = _extract_leading_category(str(data.get("title") or ""))
            return {
                "tid": tid,
                "pid": "",
                "floor_label": "日志全文",
                "is_main_post": True,
                "is_thread_author": True,
                "title": title,
                "category": category,
                "forum_name": "日志 · 长评",
                "group_icon": "",
                "author": str(
                    user_obj.get("nickname") or user_obj.get("username") or "Bangumi 用户"
                ),
                "author_id": str(user_obj.get("username") or user_obj.get("id") or ""),
                "author_avatar": _resolve_bgm_url(
                    av_obj.get("medium") or av_obj.get("large") or ""
                ),
                "author_sign": str(user_obj.get("sign") or ""),
                "created_at": _format_ts_cn(data.get("createdAt")),
                "reading_time": "",
                "replies": data.get("replies"),
                "score": None,
                "reactions": [],
                "related_subjects": related_subjects,
                "raw_html": convert_bgm_bbcode_to_html(str(data.get("content") or "")),
            }

        return None

    async def _inline_auxiliary_images(
        self,
        *,
        referer: str,
        cookies: dict[str, str] | None,
        author_avatar: str,
        group_icon: str,
        reactions: list[ReactionItem],
        related_subjects: list[RelatedSubject],
    ) -> tuple[str, str]:
        """并发预拉取头像、小组徽标、贴贴表情及关联作品封面为 Data URI，提升截图清晰度与加载成功率。"""
        url_tasks: dict[str, asyncio.Task[str | None]] = {}

        def _schedule(u: str, max_bytes: int = 512 * 1024) -> None:
            if u and u.startswith("http") and u not in url_tasks:
                url_tasks[u] = asyncio.create_task(
                    _fetch_image_as_data_uri(
                        self.session,
                        u,
                        referer=referer,
                        cookies=cookies,
                        same_site_hosts=BGM_SAME_SITE_HOSTS,
                        max_bytes=max_bytes,
                    )
                )

        _schedule(author_avatar)
        _schedule(group_icon)
        for rx in reactions[:10]:
            _schedule(rx.emoji_url, max_bytes=128 * 1024)
        for rs in related_subjects[:6]:
            _schedule(rs.cover)

        if not url_tasks:
            return author_avatar, group_icon

        resolved_map: dict[str, str] = {}
        results = await asyncio.gather(*url_tasks.values(), return_exceptions=True)
        for orig_u, res in zip(url_tasks.keys(), results, strict=True):
            if isinstance(res, str) and res:
                resolved_map[orig_u] = res

        for rx in reactions:
            if rx.emoji_url in resolved_map:
                rx.emoji_url = resolved_map[rx.emoji_url]
        for rs in related_subjects:
            if rs.cover in resolved_map:
                rs.cover = resolved_map[rs.cover]

        return (
            resolved_map.get(author_avatar, ""),
            resolved_map.get(group_icon, ""),
        )

    @staticmethod
    def _decode_bmo_needed_srcs(manifest: dict[str, Any], codes: list[str]) -> set[str]:
        """根据 bmo.js 的 versionOrder + compactReverse 规则，按需解析出 Bmoji 所需 PNG 路径。"""
        version_buckets: dict[int, list[tuple[str, str]]] = {}
        item_index: dict[str, str] = {}
        for cat_key, cat in manifest.items():
            if not isinstance(cat, dict) or not isinstance(cat.get("items"), list):
                continue
            cat_code = str(cat.get("id") or cat_key)
            for item in cat["items"]:
                if not isinstance(item, dict) or not item.get("id") or not item.get("src"):
                    continue
                item_id = str(item["id"])
                code_id = (
                    f"{cat_code}{item_id}"
                    if (cat_code and item_id.isdigit() and not item.get("custom"))
                    else item_id
                )
                src = str(item["src"])
                alias = str(item["alias"]) if item.get("alias") else ""
                item_index.setdefault(item_id, src)
                if code_id:
                    item_index.setdefault(code_id, src)
                if alias:
                    item_index.setdefault(alias, src)
                try:
                    ver = max(1, int(item.get("version") or 1))
                except (TypeError, ValueError):
                    ver = 1
                version_buckets.setdefault(ver, []).append((code_id, src))

        compact_reverse: list[str] = []
        seen_primary: set[str] = set()
        for ver in sorted(version_buckets):
            for primary_key, src in version_buckets[ver]:
                if primary_key and primary_key not in seen_primary:
                    seen_primary.add(primary_key)
                    compact_reverse.append(src)

        needed: set[str] = set()
        for raw_code in codes:
            trimmed = raw_code.strip()
            if trimmed.startswith("(") and trimmed.endswith(")"):
                trimmed = trimmed[1:-1]
            if not trimmed:
                continue
            if trimmed.startswith("bmoC"):
                payload = trimmed[4:].lstrip("_:-")
                if not payload:
                    continue
                pad = "=" * ((4 - len(payload) % 4) % 4)
                try:
                    raw_bytes = base64.urlsafe_b64decode(payload + pad)
                except Exception:
                    continue
                pos = 0

                def _read_varuint() -> int | None:
                    nonlocal pos
                    val = 0
                    shift = 0
                    while pos < len(raw_bytes):
                        b = raw_bytes[pos]
                        pos += 1
                        val |= (b & 0x7F) << shift
                        if (b & 0x80) == 0:
                            return val
                        shift += 7
                        if shift > 35:
                            return None
                    return None

                while pos < len(raw_bytes):
                    combined = _read_varuint()
                    if combined is None:
                        break
                    cid = combined >> 7
                    flags = combined & 127
                    if 0 <= cid < len(compact_reverse):
                        needed.add(compact_reverse[cid])
                    for bit in (1, 2, 4, 8, 16, 32):
                        if flags & bit and _read_varuint() is None:
                            pos = len(raw_bytes)
                            break
                    if flags & 64:
                        elen = _read_varuint()
                        if elen is None:
                            break
                        pos += elen
            elif trimmed.lower().startswith("bmo"):
                payload = trimmed[3:].lstrip("_:-")
                parts = [p for p in payload.split("_") if p]
                idx = 0
                while idx < len(parts):
                    matched = False
                    for seg_len in range(min(3, len(parts) - idx), 0, -1):
                        cand = "_".join(parts[idx : idx + seg_len]).split(":", 1)[0]
                        if cand in item_index:
                            needed.add(item_index[cand])
                            idx += seg_len
                            matched = True
                            break
                    if not matched:
                        idx += 1
        return needed

    async def _ensure_bmo_base_assets(self) -> tuple[str | None, dict[str, Any] | None]:
        if self._cached_bmo_js is None:
            try:
                async with self.session.get(
                    BGM_BMO_JS_URL,
                    headers={"User-Agent": DEFAULT_BROWSER_UA, "Referer": BGM_WEB_BASE + "/"},
                    timeout=BGM_REQUEST_TIMEOUT,
                ) as resp:
                    if resp.status == 200:
                        raw = await read_response_bounded(resp)
                        self._cached_bmo_js = raw.decode("utf-8", errors="replace")
            except Exception as e:
                logger.debug(f"拉取 Bangumi Bmoji 运行时脚本失败: {e}")

        if self._cached_bmo_manifest is None:
            try:
                async with self.session.get(
                    BGM_BMO_MANIFEST_URL,
                    headers={"User-Agent": DEFAULT_BROWSER_UA, "Referer": BGM_WEB_BASE + "/"},
                    timeout=BGM_REQUEST_TIMEOUT,
                ) as resp:
                    if resp.status == 200:
                        raw = await read_response_bounded(resp)
                        obj = json.loads(raw.decode("utf-8", errors="replace"))
                        if isinstance(obj, dict):
                            self._cached_bmo_manifest = obj
            except Exception as e:
                logger.debug(f"拉取 Bangumi Bmoji 资源清单失败: {e}")

        return self._cached_bmo_js, self._cached_bmo_manifest

    async def _build_bmo_script_html(self, clean_html: str, *, referer: str) -> tuple[str, str]:
        """当正文含 Bangumi `<span class="bmo" data-code="...">` 时按需打包离线 Bmoji 脚本。"""
        bmo_codes = BMO_SPAN_RE.findall(clean_html or "")
        if not bmo_codes:
            return clean_html, ""

        bmo_js, manifest = await self._ensure_bmo_base_assets()
        if not bmo_js or not manifest:
            fallback_html = BMO_SPAN_FALLBACK_RE.sub(
                '<span class="forum-emote">[bmo表情]</span>', clean_html
            )
            return fallback_html, ""

        needed_srcs = self._decode_bmo_needed_srcs(manifest, bmo_codes)
        missing_srcs = [s for s in sorted(needed_srcs) if s not in self._bmo_png_cache]
        if missing_srcs:
            sem = asyncio.Semaphore(4)

            async def _fetch_part(rel_src: str) -> tuple[str, str | None]:
                full_u = f"{BGM_WEB_BASE}/js/lib/bmo/{rel_src.lstrip('./')}"
                async with sem:
                    uri = await _fetch_image_as_data_uri(
                        self.session,
                        full_u,
                        referer=referer,
                        max_bytes=512 * 1024,
                    )
                    return rel_src, uri

            fetched = await asyncio.gather(*(_fetch_part(s) for s in missing_srcs))
            for rel_src, uri in fetched:
                if uri:
                    self._bmo_png_cache[rel_src] = uri
                    while len(self._bmo_png_cache) > 160:
                        self._bmo_png_cache.popitem(last=False)

        inlined_manifest = copy.deepcopy(manifest)
        for cat in inlined_manifest.values():
            if not isinstance(cat, dict) or not isinstance(cat.get("items"), list):
                continue
            for item in cat["items"]:
                if not isinstance(item, dict):
                    continue
                src = str(item.get("src") or "")
                if src in self._bmo_png_cache:
                    item["src"] = self._bmo_png_cache[src]

        patched_js = bmo_js.replace(
            "DEFAULT_ASSTET_PATH + item.src",
            "(String(item.src).indexOf('data:') === 0 ? item.src : DEFAULT_ASSTET_PATH + item.src)",
        )
        manifest_json = json.dumps(inlined_manifest, ensure_ascii=False).replace("</", "<\\/")
        script_html = (
            f"<script>\nwindow.__BMOJI_ASSETS__ = {manifest_json};\n"
            f"{patched_js}\n"
            "if (window.Bmoji) { window.Bmoji.configure({ width: 42, height: 42 }); "
            "window.Bmoji.renderAll(); }\n</script>"
        )
        return clean_html, script_html

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
        sub_type = link.sub_type or "group_topic"
        tid = link.tid
        target_pid = link.pid
        cookies, has_auth = self._build_cookies()
        req_cookies = cookies if has_auth else None

        if sub_type == "blog":
            base_page = f"{BGM_WEB_BASE}/blog/{tid}"
        elif sub_type == "subject_topic":
            base_page = f"{BGM_WEB_BASE}/subject/topic/{tid}"
        else:
            base_page = f"{BGM_WEB_BASE}/group/topic/{tid}"
        canonical_url = f"{base_page}#post_{target_pid}" if target_pid else base_page

        html_text, web_err = await self._fetch_web_html(base_page, req_cookies)
        extracted: dict[str, Any] | None = None

        if html_text is not None:
            try:
                if sub_type == "blog":
                    extracted = self._parse_blog_html(html_text, tid=tid, target_pid=target_pid)
                else:
                    extracted = self._parse_topic_html(
                        html_text, sub_type=sub_type, tid=tid, target_pid=target_pid
                    )
            except ForumParseError as web_parse_err:
                err_s = str(web_parse_err)
                if not has_auth and any(
                    k in err_s for k in ("登录", "权限", "私有", "好友", "小组成员")
                ):
                    raise ForumLoginRequiredError(
                        f"Bangumi 内容需要登录或权限访问（{err_s}），请在插件设置中配置 bangumi.cookie。"
                    ) from web_parse_err
                # 尝试通过 next.bgm.tv 公开接口兜底核实
                extracted = await self._fetch_via_next_api(sub_type, tid, target_pid)
                if extracted is None:
                    raise ForumParseError(
                        f"无法解析 Bangumi 内容 ({sub_type} #{tid})：{err_s}"
                    ) from web_parse_err
        else:
            extracted = await self._fetch_via_next_api(sub_type, tid, target_pid)
            if extracted is None:
                raise ForumParseError(
                    f"无法获取 Bangumi 页面 ({sub_type} #{tid})：{web_err or '网络请求失败'}"
                )

        post_image_urls: list[str] = []
        pre_downloaded_files: dict[int, str] = {}
        uninlined_flags: list[bool] = []
        size_kwargs: dict[str, int] = {}
        if max_inline_single_bytes is not None:
            size_kwargs["max_inline_single_bytes"] = max_inline_single_bytes
        if max_inline_total_bytes is not None:
            size_kwargs["max_inline_total_bytes"] = max_inline_total_bytes
        if max_download_single_bytes is not None:
            size_kwargs["max_download_single_bytes"] = max_download_single_bytes
        if max_download_total_bytes is not None:
            size_kwargs["max_download_total_bytes"] = max_download_total_bytes

        clean_html, is_truncated, char_count, omitted_imgs = await process_post_html(
            extracted["raw_html"],
            base_url=BGM_WEB_BASE + "/",
            referer=canonical_url,
            session=self.session,
            cookies=req_cookies,
            same_site_hosts=BGM_SAME_SITE_HOSTS,
            embed_images=embed_images,
            prefetch_data_uri=prefetch_data_uri,
            max_inline_images=max_inline_images,
            enable_length_limit=enable_length_limit,
            max_content_chars=max_content_chars,
            out_post_image_urls=post_image_urls,
            temp_scope=temp_scope,
            rehost_oversize_fn=rehost_oversize_fn,
            out_downloaded_files=pre_downloaded_files,
            out_has_uninlined_images=uninlined_flags,
            **size_kwargs,
        )

        bmo_script_html = ""
        if BMO_SPAN_RE.search(clean_html):
            if embed_images and prefetch_data_uri:
                clean_html, bmo_script_html = await self._build_bmo_script_html(
                    clean_html, referer=canonical_url
                )
            else:
                clean_html = BMO_SPAN_FALLBACK_RE.sub(
                    '<span class="forum-emote">[bmo表情]</span>', clean_html
                )

        author_avatar = extracted["author_avatar"]
        group_icon = extracted["group_icon"]
        reactions: list[ReactionItem] = extracted["reactions"]
        related_subjects: list[RelatedSubject] = extracted["related_subjects"]

        if embed_images and prefetch_data_uri:
            author_avatar, group_icon = await self._inline_auxiliary_images(
                referer=canonical_url,
                cookies=req_cookies,
                author_avatar=author_avatar,
                group_icon=group_icon,
                reactions=reactions,
                related_subjects=related_subjects,
            )
        else:
            author_avatar = ""
            group_icon = ""

        return ParsedThread(
            forum=ForumType.BANGUMI,
            tid=extracted["tid"],
            pid=extracted["pid"],
            floor_label=extracted["floor_label"],
            is_main_post=extracted["is_main_post"],
            is_thread_author=extracted["is_thread_author"],
            url=canonical_url,
            title=extracted["title"],
            author=extracted["author"],
            author_id=extracted["author_id"],
            author_avatar=author_avatar,
            forum_name=extracted["forum_name"],
            category=extracted["category"],
            created_at=extracted["created_at"],
            replies=extracted["replies"],
            score=extracted["score"],
            content_html=clean_html,
            is_truncated=is_truncated,
            truncated_at_chars=char_count if is_truncated else 0,
            omitted_images_count=omitted_imgs,
            post_image_urls=post_image_urls,
            sub_type=sub_type,
            group_icon=group_icon,
            author_sign=extracted["author_sign"],
            reading_time=extracted["reading_time"],
            reactions=reactions,
            related_subjects=related_subjects,
            pre_downloaded_files=pre_downloaded_files,
            has_uninlined_images=bool(uninlined_flags),
            bmo_script_html=bmo_script_html,
        )
