from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp

from ..html_sanitizer import (
    _fetch_image_as_data_uri,
    process_post_html,
    rewrite_legacy_media_url,
)
from ..io_guard import read_response_bounded
from ..models import (
    ForumLink,
    ForumLoginRequiredError,
    ForumParseError,
    ForumType,
    ParsedThread,
)
from ..temp_manager import TempScope
from .base import DEFAULT_BROWSER_UA, BaseForumParser, parse_cookie_string

NGA_WEB_BASE = "https://bbs.nga.cn/"
# NGA 官方已下线旧域名 img.nga.178.com，切换为新图床域名 img.nga.cn
NGA_ATTACH_BASE = "https://img.nga.cn/attachments"
NGA_APP_UA = "NGA_skull/6.0.5(iPhone10,3;iOS 12.0.1)"
NGA_READ_UA = "NGA_WP_JW(;WINDOWS)"

NGA_SAME_SITE_HOSTS = {
    "bbs.nga.cn",
    "ngabbs.com",
    "img.nga.cn",
    "img4.nga.cn",
    "img8.nga.cn",
}

CHINA_TZ = timezone(timedelta(hours=8))

# NGA 已知成对 BBCode 标签
KNOWN_PAIRED_BBCODE_TAGS = frozenset(
    {
        "b",
        "i",
        "u",
        "del",
        "s",
        "quote",
        "collapse",
        "color",
        "size",
        "align",
        "font",
        "h",
        "code",
        "list",
        "table",
        "tr",
        "td",
        "img",
        "attach",
        "url",
        "tid",
        "pid",
        "uid",
        "dice",
        "flash",
        "album",
    }
)

# NGA 颜色名映射为现代清晰可读色值
NGA_COLOR_MAP = {
    "skyblue": "#0284c7",
    "royalblue": "#2563eb",
    "blue": "#1d4ed8",
    "darkblue": "#1e3a8a",
    "orange": "#d97706",
    "orangered": "#ea580c",
    "crimson": "#dc2626",
    "red": "#dc2626",
    "firebrick": "#b91c1c",
    "darkred": "#991b1b",
    "green": "#15803d",
    "limegreen": "#16a34a",
    "seagreen": "#0d9488",
    "teal": "#0f766e",
    "deeppink": "#db2777",
    "tomato": "#e11d48",
    "coral": "#ea580c",
    "purple": "#7e22ce",
    "indigo": "#4f46e5",
    "burlywood": "#a16207",
    "sandybrown": "#b45309",
    "sienna": "#92400e",
    "chocolate": "#9a3412",
    "gray": "#6b7280",
}

# NGA 表情组友好名称映射
NGA_STICKER_GROUP_NAMES = {
    "ac": "AC娘",
    "a2": "AC娘",
    "ng": "NGA",
    "pst": "潘斯特",
    "dt": "外域",
    "pg": "企鹅",
}

BR_TAG_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
HEADING_EQ_RE = re.compile(r"(?:^|\n)={3,}\s*(.+?)\s*={3,}(?=\n|$)")
STICKER_CODE_RE = re.compile(r"\[s:([a-zA-Z0-9_]+):([^\[\]]{1,24})\]")
NUMERIC_STICKER_RE = re.compile(r"\[s:(\d{1,3})\]")
MENTION_TAG_RE = re.compile(r"\[@([^\[\]]{1,40})\]")
BBCODE_TOKEN_RE = re.compile(
    r"\[(?P<slash>/)?(?P<tag>[a-zA-Z]+\d*|\*)(?:=(?P<eq_val>[^\[\]]*))?(?P<sp_attrs>(?:\s+[a-zA-Z]+=[^\[\]\s]+)*)\]"
)


def _format_nga_time(val: Any) -> str:
    if val is None or val == "":
        return ""
    s = str(val).strip()
    if s.isdigit():
        try:
            ts = int(s)
            if ts > 0:
                return datetime.fromtimestamp(ts, tz=CHINA_TZ).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            pass
    return s


def _resolve_nga_img_src(raw_src: str, attach_base: str = NGA_ATTACH_BASE) -> str:
    s = (raw_src or "").strip()
    if not s:
        return ""
    base = attach_base.rstrip("/")
    if s.startswith("./"):
        return rewrite_legacy_media_url(f"{base}/{s[2:].lstrip('/')}")
    if s.startswith("//"):
        return rewrite_legacy_media_url("https:" + s)
    if s.startswith(("http://", "https://")):
        return rewrite_legacy_media_url(s)
    if s.startswith("mon_"):
        return rewrite_legacy_media_url(f"{base}/{s}")
    return ""


def _canonical_bb_tag(tag_raw: str) -> tuple[str, str]:
    """规范化 NGA BBCode 标签名，如 [td25] -> ('td', '25')。"""
    low = tag_raw.lower()
    if low == "*":
        return "*", ""
    if low.startswith("td") and len(low) > 2 and low[2:].isdigit():
        return "td", low[2:]
    return low, ""


def convert_nga_bbcode_to_html(
    raw_content: str,
    attach_url_map: dict[str, str] | None = None,
) -> tuple[str, set[str]]:
    """将 NGA 主楼 BBCode 富文本安全转换为语义化 HTML，并自动展开折叠内容。

    Returns:
        (html_string, referenced_attachment_urls)
    """
    if not raw_content:
        return "", set()

    attach_map = attach_url_map or {}
    used_attach_urls: set[str] = set()

    # 1. 统一换行、兼容 app_api.php 预转换的 <del> 标签并反转义 HTML 实体
    text = BR_TAG_RE.sub("\n", raw_content)
    text = re.sub(
        r"<del\b[^>]*>(.*?)</del>", r"[del]\1[/del]", text, flags=re.IGNORECASE | re.DOTALL
    )
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = html.unescape(text)

    # 2. 扫描成对标签，避免把游戏技能按键如 [Q]、[E]、[1] 误识别为未闭合标签
    tokens = list(BBCODE_TOKEN_RE.finditer(text))
    open_positions_by_tag: dict[str, list[int]] = {}
    matched_positions: set[int] = set()

    for m in tokens:
        is_close = bool(m.group("slash"))
        canon, _ = _canonical_bb_tag(m.group("tag"))
        if canon == "*":
            matched_positions.add(m.start())
            continue
        if canon not in KNOWN_PAIRED_BBCODE_TAGS:
            continue
        if not is_close:
            open_positions_by_tag.setdefault(canon, []).append(m.start())
        else:
            stack_pos = open_positions_by_tag.get(canon)
            if stack_pos:
                open_pos = stack_pos.pop()
                matched_positions.add(open_pos)
                matched_positions.add(m.start())

    # 即使在截断引用里缺少闭合，也允许常见块级/媒体标签解析并在末尾自动闭合
    always_allow_open = {"quote", "collapse", "img", "b", "color", "del", "h", "list"}

    def _render_plain_segment(seg: str) -> str:
        if not seg:
            return ""
        # 先转义 HTML 特殊字符
        escaped = html.escape(seg, quote=False)

        # 处理 === 章节小标题 ===
        escaped = HEADING_EQ_RE.sub(
            r'\n<h3 class="forum-heading">\1</h3>\n',
            escaped,
        )

        # 处理 [@用户名] 提及
        escaped = MENTION_TAG_RE.sub(
            r'<span class="forum-mention">@\1</span>',
            escaped,
        )

        # 处理 [s:ac:哭笑] 等论坛表情
        def _repl_sticker(sm: re.Match[str]) -> str:
            grp = sm.group(1).lower()
            name = sm.group(2)
            grp_label = NGA_STICKER_GROUP_NAMES.get(grp, grp.upper())
            return f'<span class="forum-emote">[{grp_label}·{name}]</span>'

        escaped = STICKER_CODE_RE.sub(_repl_sticker, escaped)
        escaped = NUMERIC_STICKER_RE.sub(r'<span class="forum-emote">[表情\1]</span>', escaped)
        return escaped

    out: list[str] = []
    open_html_stack: list[tuple[str, str]] = []  # (bb_canon, closing_html)
    in_list_item = False
    cursor = 0

    def _close_list_item_if_needed() -> None:
        nonlocal in_list_item
        if in_list_item:
            out.append("</li>")
            in_list_item = False

    i = 0
    while i < len(tokens):
        m = tokens[i]
        start, end = m.span()
        is_close = bool(m.group("slash"))
        canon, td_width = _canonical_bb_tag(m.group("tag"))
        eq_val = (m.group("eq_val") or "").strip()
        sp_attrs = (m.group("sp_attrs") or "").strip()

        is_valid_tag = (
            canon == "*"
            or start in matched_positions
            or (not is_close and canon in always_allow_open)
        )
        if not is_valid_tag:
            i += 1
            continue

        # 输出当前标签前的普通文本片段
        if start > cursor:
            out.append(_render_plain_segment(text[cursor:start]))
        cursor = end

        # 处理自包含内容型标签：[img]...[/img], [attach]...[/attach], [code]...[/code], [flash]...[/flash]
        if not is_close and canon in {"img", "attach", "code", "flash"}:
            # 寻找对应的闭合标签
            close_idx = None
            for j in range(i + 1, len(tokens)):
                mj = tokens[j]
                cj, _ = _canonical_bb_tag(mj.group("tag"))
                if mj.group("slash") and cj == canon:
                    close_idx = j
                    break
            if close_idx is not None:
                inner_raw = text[end : tokens[close_idx].start()].strip()
                cursor = tokens[close_idx].end()
                i = close_idx + 1

                if canon == "img":
                    resolved = _resolve_nga_img_src(inner_raw)
                    if resolved:
                        used_attach_urls.add(resolved)
                        esc_u = html.escape(resolved, quote=True)
                        out.append(f'<img class="forum-img" src="{esc_u}" alt="NGA图片" />')
                elif canon == "attach":
                    resolved = attach_map.get(inner_raw) or _resolve_nga_img_src(inner_raw)
                    if resolved:
                        used_attach_urls.add(resolved)
                        esc_u = html.escape(resolved, quote=True)
                        out.append(f'<img class="forum-img" src="{esc_u}" alt="NGA附件" />')
                elif canon == "code":
                    out.append(
                        f'<pre class="forum-code"><code>{html.escape(inner_raw, quote=False)}</code></pre>'
                    )
                elif canon == "flash":
                    media_u = _resolve_nga_img_src(inner_raw) or inner_raw
                    out.append(
                        f'<div class="forum-img-placeholder">🎬 [内嵌视频：{html.escape(media_u[:100], quote=False)}]</div>'
                    )
                continue

        if canon == "*":
            _close_list_item_if_needed()
            # 如果外层未显式写 [list]，自动补一层 <ul>
            if not any(b == "list" for b, _ in open_html_stack):
                out.append('<ul class="forum-list">')
                open_html_stack.append(("list", "</ul>"))
            out.append("<li>")
            in_list_item = True
            i += 1
            continue

        if not is_close:
            if canon == "b":
                out.append("<strong>")
                open_html_stack.append((canon, "</strong>"))
            elif canon == "i":
                out.append("<em>")
                open_html_stack.append((canon, "</em>"))
            elif canon == "u":
                out.append("<u>")
                open_html_stack.append((canon, "</u>"))
            elif canon in {"del", "s"}:
                out.append('<del class="forum-del">')
                open_html_stack.append((canon, "</del>"))
            elif canon == "quote":
                out.append('<blockquote class="forum-quote">')
                open_html_stack.append((canon, "</blockquote>"))
            elif canon == "collapse":
                title_label = html.escape(eq_val or "点击展开的内容", quote=False)
                out.append(
                    f'<div class="forum-collapse">'
                    f'<div class="forum-collapse-header">📂 折叠内容（已自动展开）：{title_label}</div>'
                    f'<div class="forum-collapse-body">'
                )
                open_html_stack.append((canon, "</div></div>"))
            elif canon == "color":
                c_key = eq_val.lower().strip()
                if c_key in {"silver", "white", "#fff", "#ffffff"}:
                    out.append('<span class="forum-spoiler">')
                else:
                    mapped = NGA_COLOR_MAP.get(c_key, c_key)
                    if re.match(r"^#[0-9a-fA-F]{3,6}$", mapped):
                        out.append(f'<span style="color:{mapped}">')
                    else:
                        out.append("<span>")
                open_html_stack.append((canon, "</span>"))
            elif canon == "size":
                m_pct = re.match(r"^(\d{2,3})%$", eq_val)
                if m_pct:
                    pct = max(85, min(160, int(m_pct.group(1))))
                    if pct >= 125:
                        out.append('<span style="font-weight:bold;color:inherit">')
                    else:
                        out.append("<span>")
                else:
                    out.append("<span>")
                open_html_stack.append((canon, "</span>"))
            elif canon == "align":
                al = eq_val.lower() if eq_val.lower() in {"left", "center", "right"} else "left"
                out.append(f'<div style="text-align:{al}">')
                open_html_stack.append((canon, "</div>"))
            elif canon == "h":
                if eq_val:
                    out.append(f'<h3 class="forum-heading">{html.escape(eq_val)}</h3><div>')
                    open_html_stack.append((canon, "</div>"))
                else:
                    out.append('<h3 class="forum-heading">')
                    open_html_stack.append((canon, "</h3>"))
            elif canon == "list":
                out.append('<ul class="forum-list">')
                open_html_stack.append((canon, "</ul>"))
            elif canon == "table":
                out.append('<table class="forum-table"><tbody>')
                open_html_stack.append((canon, "</tbody></table>"))
            elif canon == "tr":
                out.append("<tr>")
                open_html_stack.append((canon, "</tr>"))
            elif canon == "td":
                td_attrs: list[str] = []
                if td_width and td_width.isdigit() and 1 <= int(td_width) <= 100:
                    td_attrs.append(f'width="{td_width}%"')
                elif eq_val.isdigit() and 1 <= int(eq_val) <= 100:
                    td_attrs.append(f'width="{eq_val}%"')
                for attr_m in re.finditer(r"(colspan|rowspan)=(\d+)", sp_attrs, re.I):
                    td_attrs.append(f'{attr_m.group(1).lower()}="{attr_m.group(2)}"')
                attr_s = (" " + " ".join(td_attrs)) if td_attrs else ""
                out.append(f"<td{attr_s}>")
                open_html_stack.append((canon, "</td>"))
            elif canon == "url":
                if eq_val and eq_val.startswith(("http://", "https://")):
                    esc_u = html.escape(eq_val, quote=True)
                    out.append(f'<a class="forum-link" href="{esc_u}">')
                    open_html_stack.append((canon, "</a>"))
                else:
                    out.append('<span class="forum-link">')
                    open_html_stack.append((canon, "</span>"))
            elif canon == "tid":
                href = (
                    f"https://bbs.nga.cn/read.php?tid={eq_val}"
                    if eq_val.isdigit()
                    else NGA_WEB_BASE
                )
                out.append(f'<a class="forum-link" href="{html.escape(href, quote=True)}">🔗 ')
                open_html_stack.append((canon, "</a>"))
            elif canon == "pid":
                out.append('<span class="forum-mention">💬 ')
                open_html_stack.append((canon, "</span>"))
            elif canon == "uid":
                out.append('<span class="forum-mention">@')
                open_html_stack.append((canon, "</span>"))
            elif canon == "dice":
                out.append('<span class="forum-emote">🎲 ROLL: ')
                open_html_stack.append((canon, "</span>"))
            else:
                out.append("<span>")
                open_html_stack.append((canon, "</span>"))
        else:
            if canon == "list":
                _close_list_item_if_needed()
            # 寻找栈内最近同名标签并有序闭合
            for s_idx in range(len(open_html_stack) - 1, -1, -1):
                if open_html_stack[s_idx][0] == canon:
                    while len(open_html_stack) > s_idx:
                        popped_tag, close_str = open_html_stack.pop()
                        if popped_tag == "list":
                            _close_list_item_if_needed()
                        out.append(close_str)
                    break

        i += 1

    if cursor < len(text):
        out.append(_render_plain_segment(text[cursor:]))

    _close_list_item_if_needed()
    while open_html_stack:
        _, close_str = open_html_stack.pop()
        out.append(close_str)

    combined = "".join(out)

    # 将普通段落换行转换为 <br />（避免在块级标签紧邻处制造多余空行）
    combined = re.sub(
        r"(</(?:blockquote|div|h3|pre|table|ul|li|tr|td)>)\n+",
        r"\1",
        combined,
    )
    combined = re.sub(
        r"\n+(<(?:blockquote|div|h3|pre|table|ul|li|tr|td)\b)",
        r"\1",
        combined,
    )
    combined = combined.replace("\n", "<br />")
    return combined, used_attach_urls


class NgaParser(BaseForumParser):
    """NGA 玩家社区 (bbs.nga.cn) 帖子主楼解析器。"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(session, **kwargs)
        self.cfg: dict[str, Any] = config or {}

    def update_config(self, config: dict[str, Any] | None) -> None:
        self.cfg = config or {}

    def _build_cookies(self) -> tuple[dict[str, str], bool]:
        """构造 NGA 请求 Cookie，返回 (cookies_dict, has_user_auth)。"""
        cookies: dict[str, str] = {}
        raw_cookie = str(self.cfg.get("cookie") or "").strip()
        if raw_cookie:
            cookies.update(parse_cookie_string(raw_cookie))

        uid = str(self.cfg.get("passport_uid") or "").strip()
        cid = str(self.cfg.get("passport_cid") or "").strip()
        if uid:
            cookies["ngaPassportUid"] = uid
        if cid:
            cookies["ngaPassportCid"] = cid

        has_auth = bool(cookies.get("ngaPassportUid") and cookies.get("ngaPassportCid"))
        if not has_auth and "guestJs" not in cookies:
            cookies["guestJs"] = str(int(time.time()))
        return cookies, has_auth

    @staticmethod
    def _decode_nga_json(raw_bytes: bytes) -> dict[str, Any] | None:
        """解码 NGA 返回的 JSON（兼容 UTF-8 / GB18030 及原始制表符控制字符）。"""
        text = ""
        for enc in ("utf-8", "gb18030"):
            try:
                text = raw_bytes.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if not text:
            text = raw_bytes.decode("utf-8", errors="replace")

        text = text.strip()
        if text.startswith("window.script_muti_get_var_store="):
            text = text.removeprefix("window.script_muti_get_var_store=").rstrip(";")

        try:
            obj = json.loads(text, strict=False)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None

    async def _resolve_pid_page_via_redirect(
        self, pid: str, cookies: dict[str, str]
    ) -> tuple[str, int]:
        """对纯 pid 链接请求 read.php?pid=...&to=1 的 302 Location 头，反查所属 tid 与真实所在页码。"""
        url = f"https://bbs.nga.cn/read.php?pid={pid}&to=1"
        headers = {
            "User-Agent": NGA_READ_UA,
            "Referer": f"https://bbs.nga.cn/read.php?pid={pid}",
        }
        timeout = aiohttp.ClientTimeout(total=6, connect=3, sock_connect=3, sock_read=4)
        try:
            async with self.session.get(
                url,
                headers=headers,
                cookies=cookies or None,
                timeout=timeout,
                allow_redirects=False,
            ) as resp:
                loc = resp.headers.get("Location") or ""
                if loc:
                    parsed = urlparse(loc)
                    qs = parse_qs(parsed.query)
                    r_tid = (qs.get("tid") or [""])[0]
                    r_page = (qs.get("page") or ["1"])[0]
                    if r_tid.isdigit() and int(r_tid) > 0:
                        p_num = int(r_page) if r_page.isdigit() and int(r_page) > 0 else 1
                        return r_tid, p_num
        except Exception:
            pass
        return "", 0

    async def _fetch_via_read_php(
        self,
        tid: str,
        cookies: dict[str, str],
        *,
        pid: str = "",
        page: int = 1,
    ) -> tuple[dict[str, Any] | None, str]:
        """通道一：调用 read.php?__output=11 获取完整 JSON（支持 tid 主楼或 pid/page 指定楼层）。"""
        if pid:
            url = f"https://bbs.nga.cn/read.php?pid={pid}&__output=11"
            ref = f"https://bbs.nga.cn/read.php?pid={pid}"
        else:
            url = f"https://bbs.nga.cn/read.php?tid={tid}&page={max(1, page)}&__output=11"
            ref = f"https://bbs.nga.cn/read.php?tid={tid}"

        headers = {
            "User-Agent": NGA_READ_UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": ref,
        }
        req_cookies = dict(cookies)
        timeout = aiohttp.ClientTimeout(total=12, connect=4, sock_connect=4, sock_read=8)
        last_msg = ""

        for attempt in range(2):
            req_headers = dict(headers)
            if attempt > 0:
                req_headers["Connection"] = "close"
            try:
                async with self.session.get(
                    url,
                    headers=req_headers,
                    cookies=req_cookies,
                    timeout=timeout,
                ) as resp:
                    # 处理游客首次访问 guestJs 握手跳转页
                    for k, morsel in resp.cookies.items():
                        req_cookies[k] = morsel.value
                    raw = await read_response_bounded(resp)
                    payload = self._decode_nga_json(raw)
                    if payload and isinstance(payload.get("data"), dict):
                        data = payload["data"]
                        if data.get("__T") or data.get("__R"):
                            return data, ""
                        msg_dict = data.get("__MESSAGE")
                        if isinstance(msg_dict, dict):
                            last_msg = str(msg_dict.get("1") or msg_dict)
                    elif payload and payload.get("msg"):
                        last_msg = str(payload.get("msg"))
                    else:
                        decoded_preview = raw.decode("gb18030", errors="ignore")
                        if "guestJs" in decoded_preview and attempt == 0:
                            req_cookies["guestJs"] = str(int(time.time()))
                            continue
                        if resp.status in {401, 403}:
                            last_msg = f"HTTP {resp.status} 权限不足或需要登录"
            except Exception as e:
                last_msg = str(e).strip() or type(e).__name__
                if attempt == 0:
                    continue
            break

        return None, last_msg

    async def _fetch_via_app_api(
        self,
        tid: str,
        cookies: dict[str, str],
        *,
        pid: str = "",
        page: int = 1,
    ) -> tuple[dict[str, Any] | None, str]:
        """通道二（兜底）：调用 NGA 移动端 app_api.php?__lib=post&__act=list。"""
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers = {
            "User-Agent": DEFAULT_BROWSER_UA,
            "X-User-Agent": NGA_APP_UA,
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": cookie_header,
        }
        req_form: dict[str, str] = {"page": str(max(1, page))}
        if tid:
            req_form["tid"] = tid
        if pid:
            req_form["pid"] = pid

        timeout = aiohttp.ClientTimeout(total=12, connect=4, sock_connect=4, sock_read=8)
        last_msg = ""

        for idx, host in enumerate(("https://bbs.nga.cn", "https://ngabbs.com")):
            req_headers = dict(headers)
            if idx > 0:
                req_headers["Connection"] = "close"
            try:
                async with self.session.post(
                    f"{host}/app_api.php?__lib=post&__act=list",
                    data=req_form,
                    headers=req_headers,
                    timeout=timeout,
                ) as resp:
                    raw = await read_response_bounded(resp)
                    payload = self._decode_nga_json(raw)
                    if not payload:
                        continue
                    code = payload.get("code")
                    if code == 0 and isinstance(payload.get("result"), list) and payload["result"]:
                        return payload, ""
                    last_msg = str(payload.get("msg") or f"code={code}")
            except Exception as e:
                last_msg = str(e).strip() or type(e).__name__
                continue

        return None, last_msg

    @staticmethod
    def _extract_from_read_data(
        tid: str,
        data: dict[str, Any],
        *,
        target_pid: str = "",
        target_floor: int = 0,
    ) -> dict[str, Any]:
        """从 read.php?__output=11 的 data 字典提取主楼或指定楼层结构化字段。"""
        t_info = data.get("__T") or {}
        f_info = data.get("__F") or {}
        u_map = data.get("__U") or {}
        r_map = data.get("__R") or {}

        resolved_tid = str(t_info.get("tid") or tid or "")
        r_items: list[dict[str, Any]] = []
        if isinstance(r_map, dict):
            r_items = [v for v in r_map.values() if isinstance(v, dict)]
        elif isinstance(r_map, list):
            r_items = [v for v in r_map if isinstance(v, dict)]

        target_post: dict[str, Any] = {}
        if target_pid:
            for v in r_items:
                if str(v.get("pid") or "") == target_pid:
                    target_post = v
                    break
            if not target_post and len(r_items) == 1:
                target_post = r_items[0]
            if not target_post:
                raise ForumParseError(f"未找到指定的 NGA 回复楼层 (pid={target_pid})")
        elif target_floor > 0:
            for v in r_items:
                if str(v.get("lou", "")) == str(target_floor):
                    target_post = v
                    break
            if not target_post:
                raise ForumParseError(f"未找到指定的 NGA 楼层 ({target_floor}楼)")
        else:
            for v in r_items:
                if str(v.get("lou", "")) == "0":
                    target_post = v
                    break
            if not target_post and isinstance(r_map, dict) and isinstance(r_map.get("0"), dict):
                target_post = r_map["0"]
            if not target_post and r_items:
                target_post = r_items[0]

        post_pid = str(target_post.get("pid") or target_pid or "")
        lou_raw = target_post.get("lou")
        lou_num = int(lou_raw) if (lou_raw is not None and str(lou_raw).isdigit()) else 0
        op_author_id = str(t_info.get("authorid") or "")
        author_id = str(target_post.get("authorid") or op_author_id or "")
        is_non_main_pid = bool(post_pid.isdigit() and int(post_pid) > 0)
        is_op_reply = bool(
            (lou_num > 0 or is_non_main_pid) and op_author_id and author_id == op_author_id
        )

        if lou_num > 0:
            is_main_post = False
            floor_label = f"楼主 · {lou_num}楼" if is_op_reply else f"{lou_num}楼"
        elif is_non_main_pid or target_pid:
            # NGA 的单回复接口 read.php?pid=... 会固定把单条结果的 lou 填为 0，而主楼真实 pid 恒为 0
            is_main_post = False
            floor_label = f"楼主 · PID #{post_pid}" if is_op_reply else f"PID #{post_pid}"
        else:
            is_main_post = True
            floor_label = "楼主 · 1L"

        title = html.unescape(
            str(
                t_info.get("subject")
                or target_post.get("subject")
                or f"NGA 主题 #{resolved_tid or post_pid}"
            )
        ).strip()

        # 提取主题分类头，例如 [讨论] 标题 -> category="讨论"
        category = ""
        m_cat = re.match(r"^\[([^\[\]]{1,12})\]\s*(.+)$", title)
        if m_cat:
            category = m_cat.group(1).strip()
            title = m_cat.group(2).strip()

        forum_name = str(f_info.get("name") or "").strip()
        if not forum_name and isinstance(t_info.get("parent"), dict):
            forum_name = str(t_info["parent"].get("2") or "").strip()

        author = str(t_info.get("author") or "") if is_main_post else ""
        author_avatar = ""
        author_group = ""
        u_obj = None
        if isinstance(u_map, dict):
            cand = u_map.get(author_id)
            if not isinstance(cand, dict) and author_id.isdigit():
                cand = u_map.get(int(author_id))
            if isinstance(cand, dict):
                u_obj = cand

        if u_obj is not None:
            author = str(u_obj.get("username") or author)
            av = u_obj.get("avatar")
            if isinstance(av, str) and av.startswith("http"):
                author_avatar = av
            elif isinstance(av, dict):
                for av_v in av.values():
                    if isinstance(av_v, str) and av_v.startswith("http"):
                        author_avatar = av_v
                        break
            groups_map = u_map.get("__GROUPS") if isinstance(u_map, dict) else None
            member_id = str(u_obj.get("memberid") or "")
            if isinstance(groups_map, dict) and member_id in groups_map:
                g_entry = groups_map[member_id]
                if isinstance(g_entry, list) and g_entry and g_entry[0]:
                    author_group = str(g_entry[0])
        if not author and author_id:
            author = f"UID:{author_id}"

        # 匿名发帖用户名美化
        if author.startswith("#anony_"):
            author = f"匿名用户 ({author[-6:]})"

        created_at = _format_nga_time(
            target_post.get("postdate")
            or target_post.get("postdatetimestamp")
            or t_info.get("postdate")
        )

        replies = None
        if t_info.get("replies") is not None:
            try:
                replies = int(t_info["replies"])
            except (TypeError, ValueError):
                pass

        score = None
        if target_post.get("score") is not None:
            try:
                score = int(target_post["score"])
            except (TypeError, ValueError):
                pass

        # 兼容动态 _ATTACH_BASE_VIEW（自动重写废弃的 178.com 域名至 img.nga.cn）
        g_info = data.get("__GLOBAL") or {}
        dyn_attach_base = str(g_info.get("_ATTACH_BASE_VIEW") or "").strip().strip("/")
        if dyn_attach_base:
            if not dyn_attach_base.startswith(("http://", "https://")):
                dyn_attach_base = f"https://{dyn_attach_base}"
            attach_base = rewrite_legacy_media_url(dyn_attach_base)
        else:
            attach_base = NGA_ATTACH_BASE

        # 提取附件映射（处理未通过 [img] 插入正文的底部图片附件）
        attach_map: dict[str, str] = {}
        orphan_img_urls: list[str] = []
        attachs = target_post.get("attachs")
        att_iter = (
            attachs.values()
            if isinstance(attachs, dict)
            else (attachs if isinstance(attachs, list) else [])
        )
        for att in att_iter:
            if not isinstance(att, dict):
                continue
            att_url_rel = str(att.get("attachurl") or "").strip()
            if not att_url_rel:
                continue
            full_u = _resolve_nga_img_src("./" + att_url_rel.lstrip("/"), attach_base=attach_base)
            if not full_u:
                continue
            aid = str(att.get("aid") or att.get("name") or "")
            if aid:
                attach_map[aid] = full_u
            att_type = str(att.get("type") or "").lower()
            if att_type == "img" or full_u.lower().endswith(
                (".png", ".jpg", ".jpeg", ".gif", ".webp")
            ):
                orphan_img_urls.append(full_u)

        return {
            "tid": resolved_tid,
            "pid": post_pid,
            "floor_label": floor_label,
            "is_main_post": is_main_post,
            "is_thread_author": is_op_reply,
            "title": title,
            "category": category,
            "forum_name": forum_name or "NGA 玩家社区",
            "author": author or "未知作者",
            "author_id": author_id,
            "author_avatar": author_avatar,
            "author_group": author_group,
            "created_at": created_at,
            "replies": replies,
            "score": score,
            "raw_bbcode": str(target_post.get("content") or ""),
            "attach_map": attach_map,
            "orphan_img_urls": orphan_img_urls,
        }

    @staticmethod
    def _extract_from_app_data(
        tid: str,
        payload: dict[str, Any],
        *,
        target_pid: str = "",
        target_floor: int = 0,
    ) -> dict[str, Any]:
        """从 app_api.php 返回的数据字典提取结构化字段。"""
        results = [item for item in (payload.get("result") or []) if isinstance(item, dict)]
        target_post: dict[str, Any] = {}
        if target_pid:
            for item in results:
                if str(item.get("pid") or "") == target_pid:
                    target_post = item
                    break
            if not target_post and len(results) == 1:
                target_post = results[0]
            if not target_post:
                raise ForumParseError(f"未找到指定的 NGA 回复楼层 (pid={target_pid})")
        elif target_floor > 0:
            for item in results:
                if str(item.get("lou", "")) == str(target_floor):
                    target_post = item
                    break
            if not target_post:
                raise ForumParseError(f"未找到指定的 NGA 楼层 ({target_floor}楼)")
        else:
            target_post = results[0] if results else {}
            for item in results:
                if item.get("lou") in (0, "0"):
                    target_post = item
                    break

        resolved_tid = str(payload.get("tid") or target_post.get("tid") or tid or "")
        post_pid = str(target_post.get("pid") or target_pid or "")
        lou_raw = target_post.get("lou")
        lou_num = int(lou_raw) if (lou_raw is not None and str(lou_raw).isdigit()) else 0

        author_obj = (
            target_post.get("author") if isinstance(target_post.get("author"), dict) else {}
        )
        op_uid = str(payload.get("tauthorid") or "")
        author_id = str((author_obj or {}).get("uid") or (op_uid if lou_num == 0 else ""))
        is_non_main_pid = bool(post_pid.isdigit() and int(post_pid) > 0)
        is_op_reply = bool((lou_num > 0 or is_non_main_pid) and op_uid and author_id == op_uid)

        if lou_num > 0:
            is_main_post = False
            floor_label = f"楼主 · {lou_num}楼" if is_op_reply else f"{lou_num}楼"
        elif is_non_main_pid or target_pid:
            is_main_post = False
            floor_label = f"楼主 · PID #{post_pid}" if is_op_reply else f"PID #{post_pid}"
        else:
            is_main_post = True
            floor_label = "楼主 · 1L"

        title = html.unescape(
            str(
                payload.get("tsubject")
                or target_post.get("subject")
                or f"NGA 主题 #{resolved_tid or post_pid}"
            )
        ).strip()
        category = ""
        m_cat = re.match(r"^\[([^\[\]]{1,12})\]\s*(.+)$", title)
        if m_cat:
            category = m_cat.group(1).strip()
            title = m_cat.group(2).strip()

        author = str(
            (author_obj or {}).get("username")
            or (payload.get("tauthor") if is_main_post else "")
            or "未知作者"
        )
        if author.startswith("#anony_"):
            author = f"匿名用户 ({author[-6:]})"

        author_avatar = str((author_obj or {}).get("avatar") or "")
        if not author_avatar.startswith("http"):
            author_avatar = ""

        created_at = _format_nga_time(
            target_post.get("postdatetimestamp") or target_post.get("postdate")
        )

        score = None
        if target_post.get("vote_good") is not None:
            try:
                score = int(target_post["vote_good"])
            except (TypeError, ValueError):
                pass

        attach_map: dict[str, str] = {}
        orphan_img_urls: list[str] = []
        for att in target_post.get("attachs") or []:
            if not isinstance(att, dict):
                continue
            att_u = str(att.get("attachurl") or "").strip()
            if not att_u:
                continue
            full_u = (
                att_u
                if att_u.startswith("http")
                else _resolve_nga_img_src("./" + att_u.lstrip("/"))
            )
            if full_u and full_u.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                orphan_img_urls.append(full_u)

        return {
            "tid": resolved_tid,
            "pid": post_pid,
            "floor_label": floor_label,
            "is_main_post": is_main_post,
            "is_thread_author": is_op_reply,
            "title": title,
            "category": category,
            "forum_name": str(payload.get("forum_name") or payload.get("fname") or "NGA 玩家社区"),
            "author": author,
            "author_id": author_id,
            "author_avatar": author_avatar,
            "author_group": str((author_obj or {}).get("member") or ""),
            "created_at": created_at,
            "replies": None,
            "score": score,
            "raw_bbcode": str(target_post.get("content") or ""),
            "attach_map": attach_map,
            "orphan_img_urls": orphan_img_urls,
        }

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
    ) -> ParsedThread:
        tid = link.tid
        target_pid = link.pid
        target_floor = link.floor_hint
        req_page = (
            (target_floor // 20) + 1 if (target_floor > 0 and link.page <= 1) else max(1, link.page)
        )

        cookies, has_auth = self._build_cookies()
        extracted: dict[str, Any] | None = None
        err_read = ""
        err_app = ""

        # 若为指定 pid 链接，优先按所在页整页拉取以拿到 NGA 真实楼层号 (lou)；若无页码则通过 to=1 302 定位页码
        if target_pid:
            loc_tid = tid
            loc_page = link.page if (tid and link.page > 1) else 0
            if not loc_tid or loc_page <= 0:
                r_tid, r_page = await self._resolve_pid_page_via_redirect(target_pid, cookies)
                if r_tid:
                    loc_tid = r_tid
                    loc_page = r_page

            if loc_tid and loc_page >= 1:
                page_data, _ = await self._fetch_via_read_php(
                    loc_tid, cookies, pid="", page=loc_page
                )
                if page_data is not None:
                    try:
                        cand = self._extract_from_read_data(
                            loc_tid, page_data, target_pid=target_pid
                        )
                        if cand.get("pid") == target_pid and "楼" in str(
                            cand.get("floor_label") or ""
                        ):
                            extracted = cand
                            tid = loc_tid
                            req_page = loc_page
                    except ForumParseError:
                        pass

        if extracted is None:
            read_data, err_read = await self._fetch_via_read_php(
                tid, cookies, pid=target_pid, page=req_page
            )
            if read_data is not None:
                extracted = self._extract_from_read_data(
                    tid, read_data, target_pid=target_pid, target_floor=target_floor
                )
            else:
                app_data, err_app = await self._fetch_via_app_api(
                    tid, cookies, pid=target_pid, page=req_page
                )
                if app_data is not None:
                    extracted = self._extract_from_app_data(
                        tid, app_data, target_pid=target_pid, target_floor=target_floor
                    )
                else:
                    combined_err = err_read or err_app or "未知错误"
                    if not has_auth and any(
                        k in combined_err for k in ("登录", "权限", "403", "401", "隐藏", "访客")
                    ):
                        raise ForumLoginRequiredError(
                            f"NGA 帖子访问受限（{combined_err}），请在插件设置中配置 ngaPassportUid 与 ngaPassportCid。"
                        )
                    id_desc = f"pid={target_pid}" if target_pid else f"tid={tid}"
                    raise ForumParseError(f"无法解析 NGA 帖子 ({id_desc})：{combined_err}")

        resolved_tid = extracted["tid"] or tid
        resolved_pid = extracted["pid"] or target_pid
        if target_pid:
            canonical_url = (
                f"https://bbs.nga.cn/read.php?tid={resolved_tid}&pid={resolved_pid}"
                if resolved_tid
                else f"https://bbs.nga.cn/read.php?pid={resolved_pid}"
            )
        elif target_floor > 0 and resolved_tid:
            canonical_url = (
                f"https://bbs.nga.cn/read.php?tid={resolved_tid}&page={req_page}#l{target_floor}"
            )
        else:
            canonical_url = f"https://bbs.nga.cn/read.php?tid={resolved_tid}"

        bb_html, used_att_urls = convert_nga_bbcode_to_html(
            extracted["raw_bbcode"],
            attach_url_map=extracted["attach_map"],
        )

        # 追加未在正文中显式通过 [img] 引用的图片附件
        extra_imgs: list[str] = []
        for u in extracted["orphan_img_urls"]:
            if u not in used_att_urls:
                used_att_urls.add(u)
                esc_u = html.escape(u, quote=True)
                extra_imgs.append(f'<img class="forum-img" src="{esc_u}" alt="NGA附件图片" />')
        if extra_imgs:
            bb_html += '<div class="forum-attachments">' + "".join(extra_imgs) + "</div>"

        post_image_urls: list[str] = []
        pre_downloaded_files: dict[int, str] = {}
        uninlined_flags: list[bool] = []
        clean_html, is_truncated, char_count, omitted_imgs = await process_post_html(
            bb_html,
            base_url=NGA_WEB_BASE,
            referer=canonical_url,
            session=self.session,
            cookies=cookies if has_auth else None,
            same_site_hosts=NGA_SAME_SITE_HOSTS,
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
        )

        author_avatar = rewrite_legacy_media_url(extracted["author_avatar"])
        if embed_images and prefetch_data_uri and author_avatar.startswith("http"):
            av_data = await _fetch_image_as_data_uri(
                self.session,
                author_avatar,
                referer=NGA_WEB_BASE,
                max_bytes=512 * 1024,
            )
            author_avatar = av_data or ""
        else:
            author_avatar = ""

        return ParsedThread(
            forum=ForumType.NGA,
            tid=resolved_tid or resolved_pid,
            pid=resolved_pid,
            floor_label=extracted["floor_label"],
            is_main_post=extracted["is_main_post"],
            is_thread_author=extracted["is_thread_author"],
            url=canonical_url,
            title=extracted["title"],
            author=extracted["author"],
            author_id=extracted["author_id"],
            author_avatar=author_avatar,
            author_group=extracted.get("author_group", ""),
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
            pre_downloaded_files=pre_downloaded_files,
            has_uninlined_images=bool(uninlined_flags),
        )

    def get_media_auth_context(self) -> tuple[dict[str, str] | None, set[str]]:
        """返回用于拉取帖子附件图片的登录 Cookie 与同源域名白名单。"""
        cookies, has_auth = self._build_cookies()
        return (cookies if has_auth else None), NGA_SAME_SITE_HOSTS
