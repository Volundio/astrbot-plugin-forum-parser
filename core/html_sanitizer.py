from __future__ import annotations

import asyncio
import base64
import html
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import aiohttp

from .io_guard import mime_to_ext, sniff_image_mime, stream_response_to_file
from .temp_manager import TempScope

# HTML 渲染卡片内联图片内存保护预算（单图上限 5MB，整帖原始图片合计上限 10MB；超出部分保留外链或交由磁盘流式转发）
MAX_CARD_INLINE_SINGLE_BYTES = 5 * 1024 * 1024
MAX_CARD_INLINE_TOTAL_BYTES = 10 * 1024 * 1024
MAX_CARD_INLINE_TOTAL_B64_BYTES = MAX_CARD_INLINE_TOTAL_BYTES

# OneBot11 合并转发原图流式落盘预算（单图上限 10MB，单次合并消息本地图片总预算 50MB）
MAX_FORWARD_SINGLE_IMAGE_BYTES = 10 * 1024 * 1024
MAX_FORWARD_TOTAL_FILE_BYTES = 50 * 1024 * 1024

INLINED_FORUM_IMG_RE = re.compile(
    r'<img\b[^>]*?\bsrc="(data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+)"[^>]*?\bdata-idx="(\d+)"'
    r'|<img\b[^>]*?\bdata-idx="(\d+)"[^>]*?\bsrc="(data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+)"',
    re.IGNORECASE,
)

VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

# 完全丢弃其子树的不安全或噪音标签
DROP_SUBTREE_TAGS = frozenset(
    {
        "script",
        "style",
        "iframe",
        "frame",
        "frameset",
        "object",
        "embed",
        "applet",
        "form",
        "input",
        "button",
        "select",
        "textarea",
        "meta",
        "link",
        "base",
        "noscript",
        "svg",
        "math",
        "canvas",
        "audio",
        "video",
    }
)

# 允许输出的最终 HTML 标签集合（其余未知容器标签保留子节点并剥离外壳）
ALLOWED_TAGS = frozenset(
    {
        "div",
        "p",
        "span",
        "br",
        "hr",
        "strong",
        "b",
        "em",
        "i",
        "u",
        "s",
        "strike",
        "del",
        "ins",
        "sub",
        "sup",
        "blockquote",
        "pre",
        "code",
        "ul",
        "ol",
        "li",
        "dl",
        "dt",
        "dd",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "th",
        "td",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "a",
        "img",
        "details",
        "summary",
    }
)

# Discuz 论坛常见防复制干扰与附件浮层噪音 class
DISCUZ_NOISE_CLASSES = frozenset(
    {
        "jammer",
        "a_pr",
        "a_pt",
        "a_pb",
        "a_mu",
        "tip",
        "tip_4",
        "cronclosethread_getbox",
    }
)

# Stage1st / 浅底论坛常见反白剧透颜色
LIGHT_SPOILER_COLORS = frozenset(
    {
        "white",
        "#fff",
        "#ffffff",
        "#fefefe",
        "#fafafa",
        "#f6f7eb",
        "#f5f6eb",
        "#f1f2e6",
        "#fff8e7",
        "#fff9eb",
        "lemonchiffon",
        "#fffacd",
        "ivory",
        "floralwhite",
        "cornsilk",
        "lightyellow",
        "beige",
        "snow",
        "ghostwhite",
        "aliceblue",
        "mintcream",
        "linen",
        "oldlace",
        "seashell",
        "silver",
        "#c0c0c0",
        "transparent",
    }
)

HIDDEN_STYLE_RE = re.compile(
    r"(?:display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?:px|pt|em|rem|%)?\b)",
    re.IGNORECASE,
)

STYLE_COLOR_RE = re.compile(r"(?:^|;)\s*color\s*:\s*([^;]+)", re.IGNORECASE)
STYLE_BG_COLOR_RE = re.compile(r"(?:^|;)\s*background(?:-color)?\s*:\s*([^;]+)", re.IGNORECASE)
STYLE_TEXT_ALIGN_RE = re.compile(
    r"(?:^|;)\s*text-align\s*:\s*(left|center|right|justify)\b", re.IGNORECASE
)
STYLE_FONT_WEIGHT_RE = re.compile(r"(?:^|;)\s*font-weight\s*:\s*(bold|[6-9]00)\b", re.IGNORECASE)
SAFE_CSS_COLOR_RE = re.compile(
    r"^(?:#[0-9a-fA-F]{3,6}|[a-zA-Z]{3,20}|rgb\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*\))$"
)


@dataclass
class MiniNode:
    """轻量级 HTML DOM 节点（基于标准库 html.parser 构建，零外部依赖）。"""

    tag: str  # "#text" 表示文本节点，"#root" 表示虚拟根节点
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[MiniNode] = field(default_factory=list)
    text: str = ""
    parent: MiniNode | None = field(default=None, repr=False)

    @property
    def is_text(self) -> bool:
        return self.tag == "#text"

    @property
    def classes(self) -> set[str]:
        cls_str = self.attrs.get("class", "")
        return set(cls_str.split()) if cls_str else set()

    def get_attr(self, name: str, default: str = "") -> str:
        return self.attrs.get(name.lower(), default)

    def inner_text(self, separator: str = "") -> str:
        parts: list[str] = []
        self._collect_text(parts)
        return separator.join(parts)

    def _collect_text(self, out: list[str]) -> None:
        if self.is_text:
            out.append(self.text)
            return
        if self.tag in DROP_SUBTREE_TAGS:
            return
        if self.tag == "br":
            out.append("\n")
            return
        for child in self.children:
            child._collect_text(out)
        if self.tag in {"p", "div", "tr", "li", "blockquote", "h1", "h2", "h3", "h4"}:
            out.append("\n")

    def find_all(
        self,
        *,
        tag: str | set[str] | None = None,
        class_name: str | None = None,
        id_exact: str | None = None,
        id_prefix: str | None = None,
        predicate: Callable[[MiniNode], bool] | None = None,
    ) -> list[MiniNode]:
        results: list[MiniNode] = []
        tag_set = {tag.lower()} if isinstance(tag, str) else tag

        def _walk(node: MiniNode) -> None:
            for child in node.children:
                if child.is_text:
                    continue
                matched = True
                if tag_set is not None and child.tag not in tag_set:
                    matched = False
                if matched and class_name is not None and class_name not in child.classes:
                    matched = False
                if matched and id_exact is not None and child.get_attr("id") != id_exact:
                    matched = False
                if (
                    matched
                    and id_prefix is not None
                    and not child.get_attr("id").startswith(id_prefix)
                ):
                    matched = False
                if matched and predicate is not None and not predicate(child):
                    matched = False
                if matched:
                    results.append(child)
                _walk(child)

        _walk(self)
        return results

    def find_first(
        self,
        *,
        tag: str | set[str] | None = None,
        class_name: str | None = None,
        id_exact: str | None = None,
        id_prefix: str | None = None,
        predicate: Callable[[MiniNode], bool] | None = None,
    ) -> MiniNode | None:
        matches = self.find_all(
            tag=tag,
            class_name=class_name,
            id_exact=id_exact,
            id_prefix=id_prefix,
            predicate=predicate,
        )
        return matches[0] if matches else None

    def to_html(self) -> str:
        if self.is_text:
            return html.escape(self.text, quote=False)
        if self.tag == "#root":
            return "".join(c.to_html() for c in self.children)

        attr_parts: list[str] = []
        for k, v in self.attrs.items():
            escaped_v = html.escape(str(v), quote=True)
            attr_parts.append(f'{k}="{escaped_v}"')
        attr_str = (" " + " ".join(attr_parts)) if attr_parts else ""

        if self.tag in VOID_ELEMENTS:
            return f"<{self.tag}{attr_str} />"

        inner = "".join(c.to_html() for c in self.children)
        return f"<{self.tag}{attr_str}>{inner}</{self.tag}>"


class _MiniTreeBuilder(HTMLParser):
    """宽容型 HTML 解析器，支持真实论坛页面中常见的未闭合标签。"""

    # 同级自动闭合标签规则
    AUTO_CLOSE_ON_SAME = frozenset({"li", "dt", "dd", "p", "tr", "td", "th", "option"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = MiniNode(tag="#root")
        self._stack: list[MiniNode] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in self.AUTO_CLOSE_ON_SAME and len(self._stack) > 1:
            if self._stack[-1].tag == t:
                self._stack.pop()

        attr_dict: dict[str, str] = {}
        for k, v in attrs:
            if k:
                attr_dict[k.lower()] = v if v is not None else ""

        parent = self._stack[-1]
        node = MiniNode(tag=t, attrs=attr_dict, parent=parent)
        parent.children.append(node)

        if t not in VOID_ELEMENTS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attr_dict = {k.lower(): (v if v is not None else "") for k, v in attrs if k}
        parent = self._stack[-1]
        node = MiniNode(tag=t, attrs=attr_dict, parent=parent)
        parent.children.append(node)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in VOID_ELEMENTS:
            return
        # 向上回溯最近匹配的开始标签（最多回溯 8 层防止异常闭合破坏全局树）
        for idx in range(len(self._stack) - 1, 0, -1):
            if self._stack[idx].tag == t:
                del self._stack[idx:]
                return

    def handle_data(self, data: str) -> None:
        if not data:
            return
        parent = self._stack[-1]
        if parent.children and parent.children[-1].is_text:
            parent.children[-1].text += data
        else:
            parent.children.append(MiniNode(tag="#text", text=data, parent=parent))


def parse_html(raw_html: str) -> MiniNode:
    """将 HTML 字符串解析为 MiniNode 树根节点。"""
    builder = _MiniTreeBuilder()
    builder.feed(raw_html or "")
    builder.close()
    return builder.root


def _normalize_color_token(raw_color: str) -> str:
    return re.sub(r"\s+", "", (raw_color or "").strip().lower())


def _is_spoiler_color(fg_color: str, bg_color: str = "") -> bool:
    fg = _normalize_color_token(fg_color)
    bg = _normalize_color_token(bg_color)
    if not fg:
        return False
    if bg and fg == bg:
        return True
    return fg in LIGHT_SPOILER_COLORS


def _is_safe_http_url(url: str) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


# 旧版已下线图床/附件域名自动迁移映射（NGA 178.com -> nga.cn，S1 saraba1st.com -> stage1st.com）
LEGACY_MEDIA_HOST_MAP = {
    "img.nga.178.com": "img.nga.cn",
    "img4.nga.178.com": "img4.nga.cn",
    "img8.nga.178.com": "img8.nga.cn",
    "nga.178.com": "bbs.nga.cn",
    "img.saraba1st.com": "img.stage1st.com",
    "avatar.saraba1st.com": "avatar.stage1st.com",
    "static.saraba1st.com": "static.stage1st.com",
    "bbs.saraba1st.com": "stage1st.com",
    "www.saraba1st.com": "stage1st.com",
    "saraba1st.com": "stage1st.com",
}


def rewrite_legacy_media_url(url: str) -> str:
    """将论坛历史遗留或已停解析的旧 CDN 域名自动重写为当前有效域名。"""
    if not url or not url.startswith(("http://", "https://")):
        return url
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    host = (parsed.hostname or "").lower()
    new_host = LEGACY_MEDIA_HOST_MAP.get(host)
    if not new_host:
        return url
    netloc = f"{new_host}:{parsed.port}" if parsed.port else new_host
    scheme = "https" if parsed.scheme.lower() == "http" else parsed.scheme
    return urlunparse((scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def _resolve_url(raw_url: str, base_url: str) -> str:
    u = (raw_url or "").strip()
    if not u:
        return ""
    if u.startswith("//"):
        return rewrite_legacy_media_url("https:" + u)
    if u.startswith(("http://", "https://")):
        return rewrite_legacy_media_url(u)
    if u.startswith(("javascript:", "vbscript:", "data:", "file:", "mailto:")):
        return ""
    return rewrite_legacy_media_url(urljoin(base_url, u))


def _is_smiley_image(node: MiniNode, resolved_src: str) -> bool:
    if node.get_attr("smilieid"):
        return True
    if "forum-smiley" in node.classes or "nga-smiley" in node.classes:
        return True
    low = resolved_src.lower()
    return any(
        marker in low
        for marker in (
            "/image/smiley/",
            "/static/image/smiley/",
            "/ngabbs/post/smile/",
            "acniang",
        )
    )


def _extract_discuz_img_url(node: MiniNode, base_url: str) -> str:
    """提取 Discuz / NGA <img> 真实图片地址（兼容懒加载 none.gif 与 zoomfile/file）。"""
    for attr_name in ("zoomfile", "file", "data-original", "data-src", "src"):
        candidate = node.get_attr(attr_name).strip()
        if not candidate:
            continue
        low = candidate.lower()
        if low.endswith(("/none.gif", "/transparent.gif", "common/none.gif")):
            continue
        resolved = _resolve_url(candidate, base_url)
        if _is_safe_http_url(resolved):
            return resolved
    return ""


def _sanitize_node_tree(node: MiniNode, base_url: str) -> list[MiniNode]:
    """递归清洗并增强单个节点，返回替代该节点的安全节点列表。"""
    if node.is_text:
        return [MiniNode(tag="#text", text=node.text)]

    tag = node.tag.lower()
    # 将论坛帖常见的内嵌视频 iframe（如 Bilibili 播放器）转换为安全的可见视频提示块
    if tag == "iframe":
        raw_iframe_src = _resolve_url(node.get_attr("src"), base_url)
        if _is_safe_http_url(raw_iframe_src):
            bvid_m = re.search(r"(BV[0-9A-Za-z]{10})", raw_iframe_src)
            aid_m = re.search(r"[?&]aid=(\d+)", raw_iframe_src)
            if bvid_m:
                vid_label = f"https://www.bilibili.com/video/{bvid_m.group(1)}"
            elif aid_m:
                vid_label = f"https://www.bilibili.com/video/av{aid_m.group(1)}"
            else:
                vid_label = raw_iframe_src[:100]
            # 仅对白名单视频源保留可见提示，拒绝任意未知/内网 iframe
            parsed_if = urlparse(raw_iframe_src)
            if (
                (parsed_if.hostname or "")
                .lower()
                .endswith(("bilibili.com", "hdslb.com", "music.163.com", "youtube.com", "youtu.be"))
            ):
                return [
                    MiniNode(
                        tag="div",
                        attrs={"class": "forum-img-placeholder"},
                        children=[MiniNode(tag="#text", text=f"🎬 [内嵌媒体：{vid_label}]")],
                    )
                ]
        return []

    if tag in DROP_SUBTREE_TAGS:
        return []

    node_classes = node.classes
    if node_classes & DISCUZ_NOISE_CLASSES:
        return []

    raw_style = node.get_attr("style")
    if raw_style and HIDDEN_STYLE_RE.search(raw_style):
        return []

    # 处理 Discuz 特殊编辑提示 <i class="pstatus"> 本帖最后由 xxx 编辑 </i>
    if tag == "i" and "pstatus" in node_classes:
        txt = node.inner_text().strip()
        if txt:
            p_node = MiniNode(tag="div", attrs={"class": "forum-edit-note"})
            p_node.children.append(MiniNode(tag="#text", text=txt))
            return [p_node]
        return []

    # 处理 Discuz 引用块 <blockquote><div class="quote">...</div></blockquote>
    # 递归清洗子节点
    clean_children: list[MiniNode] = []
    for child in node.children:
        clean_children.extend(_sanitize_node_tree(child, base_url))

    # 处理图片节点
    if tag == "img":
        src = _extract_discuz_img_url(node, base_url)
        if not src:
            return []
        is_smiley = _is_smiley_image(node, src)
        alt = node.get_attr("alt") or ("表情" if is_smiley else "图片")
        if alt.lower() == "attachimg":
            alt = "帖子图片"
        img_attrs = {
            "src": src,
            "alt": alt[:60],
            "class": "forum-smiley" if is_smiley else "forum-img",
            "referrerpolicy": "no-referrer",
        }
        return [MiniNode(tag="img", attrs=img_attrs)]

    # 处理超链接
    if tag == "a":
        href = _resolve_url(node.get_attr("href"), base_url)
        if not clean_children:
            return []
        # 如果链接内部只包含单张帖子大图，直接保留图片本身避免多余下划线框
        if (
            len(clean_children) == 1
            and clean_children[0].tag == "img"
            and "forum-img" in clean_children[0].classes
        ):
            return clean_children
        if _is_safe_http_url(href):
            return [
                MiniNode(
                    tag="a",
                    attrs={"href": href, "class": "forum-link"},
                    children=clean_children,
                )
            ]
        return [MiniNode(tag="span", children=clean_children)]

    # 处理 <font> 或带颜色样式的 <span>（自动识别反白剧透文字并高亮展开）
    fg_color = node.get_attr("color")
    bg_color = ""
    if raw_style:
        m_fg = STYLE_COLOR_RE.search(raw_style)
        if m_fg and not fg_color:
            fg_color = m_fg.group(1).strip()
        m_bg = STYLE_BG_COLOR_RE.search(raw_style)
        if m_bg:
            bg_color = m_bg.group(1).strip()

    if tag == "font" or (tag == "span" and (fg_color or bg_color)):
        if _is_spoiler_color(fg_color, bg_color):
            return [
                MiniNode(
                    tag="span",
                    attrs={"class": "forum-spoiler"},
                    children=clean_children,
                )
            ]
        safe_styles: list[str] = []
        if fg_color and SAFE_CSS_COLOR_RE.match(fg_color.strip()):
            safe_styles.append(f"color:{fg_color.strip()}")
        if raw_style:
            m_fw = STYLE_FONT_WEIGHT_RE.search(raw_style)
            if m_fw:
                safe_styles.append("font-weight:bold")
        attrs: dict[str, str] = {}
        if "forum-spoiler" in node_classes:
            attrs["class"] = "forum-spoiler"
        elif "forum-emote" in node_classes:
            attrs["class"] = "forum-emote"
        elif "forum-mention" in node_classes:
            attrs["class"] = "forum-mention"
        if safe_styles:
            attrs["style"] = ";".join(safe_styles)
        return [MiniNode(tag="span", attrs=attrs, children=clean_children)]

    # 展开 <details> 折叠块以便静态截图完整可见
    if tag == "details":
        return [
            MiniNode(
                tag="details",
                attrs={"open": "open", "class": "forum-collapse"},
                children=clean_children,
            )
        ]

    if tag not in ALLOWED_TAGS:
        # 未知标签剥离外壳，保留清洗后的内部节点
        return clean_children

    # 保留受信任的自定义语义 class 与基本对齐/表格属性
    safe_attrs: dict[str, str] = {}
    preserved_classes = [
        c for c in node.attrs.get("class", "").split() if c.startswith("forum-") or c in {"quote"}
    ]
    if preserved_classes:
        safe_attrs["class"] = " ".join(preserved_classes)

    if tag in {"td", "th"}:
        for tbl_attr in ("colspan", "rowspan"):
            val = node.get_attr(tbl_attr)
            if val.isdigit() and 1 <= int(val) <= 20:
                safe_attrs[tbl_attr] = val
        w_val = node.get_attr("width")
        if re.match(r"^\d{1,3}%?$", w_val):
            safe_attrs["width"] = w_val

    if raw_style and tag in {"div", "p", "td", "th"}:
        m_align = STYLE_TEXT_ALIGN_RE.search(raw_style)
        if m_align:
            safe_attrs["style"] = f"text-align:{m_align.group(1).lower()}"

    return [MiniNode(tag=tag, attrs=safe_attrs, children=clean_children)]


def truncate_dom_tree(root: MiniNode, max_chars: int) -> tuple[bool, int]:
    """在不破坏 HTML 标签闭合的前提下，按可见字符数截断 DOM 树。

    Returns:
        (是否发生了截断, 实际保留的字符数)
    """
    if max_chars <= 0:
        return False, len(root.inner_text())

    state = {"count": 0, "truncated": False}

    def _walk_and_trim(node: MiniNode) -> bool:
        """返回 True 表示当前节点应保留，False 表示整棵子树已被裁掉。"""
        if state["truncated"]:
            return False

        if node.is_text:
            text_len = len(node.text)
            if state["count"] + text_len <= max_chars:
                state["count"] += text_len
                return True
            remain = max(0, max_chars - state["count"])
            node.text = node.text[:remain].rstrip() + "……"
            state["count"] = max_chars
            state["truncated"] = True
            return True

        kept_children: list[MiniNode] = []
        for child in node.children:
            if _walk_and_trim(child):
                kept_children.append(child)
            if state["truncated"]:
                break
        node.children = kept_children
        return bool(kept_children) or node.tag in VOID_ELEMENTS or node.tag == "#root"

    _walk_and_trim(root)
    return state["truncated"], state["count"]


async def _fetch_image_as_data_uri(
    session: aiohttp.ClientSession,
    url: str,
    *,
    referer: str,
    cookies: dict[str, str] | None = None,
    same_site_hosts: set[str] | None = None,
    max_bytes: int = MAX_CARD_INLINE_SINGLE_BYTES,
) -> str | None:
    """异步下载单张图片并转换为 Base64 Data URI（带 Content-Length 预检与分块上限防 OOM）。"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Referer": referer,
    }
    host = (urlparse(url).hostname or "").lower()
    req_cookies = cookies if (cookies and same_site_hosts and host in same_site_hosts) else None

    try:
        timeout = aiohttp.ClientTimeout(total=12, connect=5)
        async with session.get(
            url,
            headers=headers,
            cookies=req_cookies,
            timeout=timeout,
            allow_redirects=True,
        ) as resp:
            if resp.status != 200:
                return None
            cl_hdr = resp.headers.get("Content-Length")
            if cl_hdr and cl_hdr.isdigit() and int(cl_hdr) > max_bytes:
                return None

            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype and not ctype.startswith("image/"):
                return None

            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.content.iter_chunked(32768):
                total += len(chunk)
                if total > max_bytes:
                    chunks.clear()
                    return None
                chunks.append(chunk)

            raw = b"".join(chunks)
            chunks.clear()
            if not raw:
                return None

            mime = sniff_image_mime(raw[:16], fallback=ctype or "image/png")
            if mime == "image/svg+xml":
                return None

            b64 = base64.b64encode(raw).decode("ascii")
            return f"data:{mime};base64,{b64}"
    except Exception:
        return None


def _data_uri_raw_byte_len(data_uri: str) -> int:
    """精确计算 Base64 Data URI 对应的原始图片二进制字节数（不含头部与 Base64 编码膨胀）。"""
    sep = ";base64,"
    pos = data_uri.find(sep)
    if pos < 0:
        return len(data_uri)
    b64_len = len(data_uri) - (pos + len(sep))
    if b64_len <= 0:
        return 0
    pad = 2 if data_uri.endswith("==") else (1 if data_uri.endswith("=") else 0)
    return max(0, (b64_len * 3) // 4 - pad)


async def process_post_html(
    raw_html: str,
    *,
    base_url: str,
    referer: str,
    session: aiohttp.ClientSession | None = None,
    cookies: dict[str, str] | None = None,
    same_site_hosts: set[str] | None = None,
    embed_images: bool = True,
    max_inline_images: int = 15,
    enable_length_limit: bool = False,
    max_content_chars: int = 15000,
    out_post_image_urls: list[str] | None = None,
) -> tuple[str, bool, int, int]:
    """执行完整的主楼 HTML 清洗、反白展开、长度保护与图片内嵌流水线。

    Returns:
        (clean_html, is_truncated, char_count, omitted_images_count)
    """
    parsed_root = parse_html(raw_html)
    sanitized_root = MiniNode(tag="#root")
    for child in parsed_root.children:
        sanitized_root.children.extend(_sanitize_node_tree(child, base_url))

    # 预先提取主楼全量正文图片 URL（不含表情），并标记唯一索引以便合并转发复用内存数据
    url_to_idx: dict[str, int] = {}
    ordered_post_urls: list[str] = []
    for img_node in sanitized_root.find_all(tag="img"):
        if "forum-smiley" in img_node.classes:
            continue
        u = img_node.get_attr("src")
        if not u:
            continue
        if u not in url_to_idx:
            url_to_idx[u] = len(ordered_post_urls)
            ordered_post_urls.append(u)
        img_node.attrs["data-idx"] = str(url_to_idx[u])

    if out_post_image_urls is not None:
        out_post_image_urls.clear()
        out_post_image_urls.extend(ordered_post_urls)

    # 1. 可选字数截断保护
    is_truncated = False
    char_count = 0
    if enable_length_limit and max_content_chars > 0:
        is_truncated, char_count = truncate_dom_tree(sanitized_root, max_content_chars)
    else:
        char_count = len(sanitized_root.inner_text())

    # 2. 处理内嵌图片与论坛表情
    all_imgs = sanitized_root.find_all(tag="img")
    post_imgs: list[MiniNode] = []
    smiley_imgs: list[MiniNode] = []
    for img_node in all_imgs:
        if "forum-smiley" in img_node.classes:
            smiley_imgs.append(img_node)
        else:
            post_imgs.append(img_node)

    omitted_images_count = 0
    if not embed_images:
        omitted_images_count = len(post_imgs)
        for img_node in post_imgs:
            alt = img_node.get_attr("alt") or "图片"
            img_node.tag = "div"
            img_node.attrs = {"class": "forum-img-placeholder"}
            img_node.children = [MiniNode(tag="#text", text=f"🖼️ [{alt} · 已关闭图片加载]")]
    else:
        allowed_post_imgs = post_imgs[:max_inline_images] if max_inline_images >= 0 else post_imgs
        overflow_post_imgs = post_imgs[max_inline_images:] if max_inline_images >= 0 else []
        omitted_images_count = len(overflow_post_imgs)

        for idx, img_node in enumerate(overflow_post_imgs):
            img_node.tag = "div"
            img_node.attrs = {"class": "forum-img-placeholder"}
            if idx == 0:
                img_node.children = [
                    MiniNode(
                        tag="#text",
                        text=f"🖼️ [其余 {omitted_images_count} 张图片已省略，请访问原帖查看]",
                    )
                ]
            else:
                # 其余溢出图片合并为首条提示，清空冗余节点
                img_node.tag = "span"
                img_node.attrs = {}
                img_node.children = []

        if session is not None:
            # 预下载表情（最多 30 个唯一 URL）与主楼图片，解决防盗链与需登录附件问题
            targets: list[MiniNode] = allowed_post_imgs + smiley_imgs[:40]
            unique_urls: list[str] = []
            seen_u: set[str] = set()
            for n in targets:
                u = n.get_attr("src")
                if u and u not in seen_u:
                    seen_u.add(u)
                    unique_urls.append(u)

            sem = asyncio.Semaphore(3)
            total_inline_budget = MAX_CARD_INLINE_TOTAL_BYTES
            used_inline_bytes = 0
            url_to_data_uri: dict[str, str] = {}

            # 按序分组拉取并在预算耗尽后立即跳过后续大图拉取，按原始图片字节计量防 OOM
            async def _worker(u: str) -> tuple[str, str | None]:
                async with sem:
                    remain = total_inline_budget - used_inline_bytes
                    if remain <= 0:
                        return u, None
                    uri = await _fetch_image_as_data_uri(
                        session,
                        u,
                        referer=referer,
                        cookies=cookies,
                        same_site_hosts=same_site_hosts,
                        max_bytes=min(MAX_CARD_INLINE_SINGLE_BYTES, remain),
                    )
                    return u, uri

            for batch_start in range(0, len(unique_urls), 4):
                if used_inline_bytes >= total_inline_budget:
                    break
                batch = unique_urls[batch_start : batch_start + 4]
                fetched = await asyncio.gather(*(_worker(u) for u in batch))
                for u, uri in fetched:
                    if uri is not None:
                        raw_sz = _data_uri_raw_byte_len(uri)
                        if (
                            raw_sz <= MAX_CARD_INLINE_SINGLE_BYTES
                            and (used_inline_bytes + raw_sz) <= total_inline_budget
                        ):
                            url_to_data_uri[u] = uri
                            used_inline_bytes += raw_sz

            for n in targets:
                u = n.get_attr("src")
                if u in url_to_data_uri:
                    n.attrs["src"] = url_to_data_uri[u]
                    n.attrs.pop("referrerpolicy", None)
                elif "forum-smiley" in n.classes:
                    # 表情如果无法下载，优雅降级为内联文字表情标签，绝不展示裂图图标
                    alt = n.get_attr("alt") or "表情"
                    n.tag = "span"
                    n.attrs = {"class": "forum-emote"}
                    n.children = [MiniNode(tag="#text", text=f"[{alt}]")]

    return sanitized_root.to_html(), is_truncated, char_count, omitted_images_count


def _decode_data_uri_to_file_sync(data_uri: str, dest: Path) -> int:
    """将单个 data:image/...;base64,... 解码原子落盘，释放内存字符串引用。"""
    _, b64_part = data_uri.split(";base64,", 1)
    raw = base64.b64decode(b64_part)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        tmp.write_bytes(raw)
        os.replace(tmp, dest)
        return len(raw)
    except BaseException:
        tmp.unlink(missing_ok=True)
        dest.unlink(missing_ok=True)
        raise


async def _stream_single_image_to_scope(
    session: aiohttp.ClientSession,
    url: str,
    scope: TempScope,
    *,
    referer: str,
    cookies: dict[str, str] | None,
    same_site_hosts: set[str] | None,
    max_bytes: int,
) -> tuple[Path | None, int]:
    """将单张论坛原图按 64KB 分块直接流式写入请求私有临时目录（峰值内存仅 64KB）。"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Referer": referer,
    }
    host = (urlparse(url).hostname or "").lower()
    req_cookies = cookies if (cookies and same_site_hosts and host in same_site_hosts) else None

    try:
        timeout = aiohttp.ClientTimeout(total=18, connect=5)
        async with session.get(
            url,
            headers=headers,
            cookies=req_cookies,
            timeout=timeout,
            allow_redirects=True,
        ) as resp:
            if resp.status != 200:
                return None, 0
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype and (not ctype.startswith("image/") or ctype == "image/svg+xml"):
                return None, 0

            ext = mime_to_ext(ctype)
            dest = scope.alloc_file(prefix="post_img", ext=ext)
            written, detected_mime = await stream_response_to_file(resp, dest, max_bytes=max_bytes)
            if detected_mime == "image/svg+xml":
                await asyncio.to_thread(dest.unlink, True)
                return None, 0
            return dest, written
    except Exception:
        return None, 0


async def prepare_post_images_for_forward(
    content_html: str,
    post_image_urls: list[str],
    scope: TempScope,
    *,
    max_images: int = 20,
    session: aiohttp.ClientSession | None = None,
    referer: str = "",
    cookies: dict[str, str] | None = None,
    same_site_hosts: set[str] | None = None,
    max_single_bytes: int = MAX_FORWARD_SINGLE_IMAGE_BYTES,
    max_total_bytes: int = MAX_FORWARD_TOTAL_FILE_BYTES,
) -> tuple[list[str], int]:
    """准备用于 OneBot11 合并转发的帖子原图本地文件列表（流式落盘防 OOM，发送后由 TempScope 立即清空）。

    注意：AstrBot 底层 Node.to_dict() 对 Image.fromURL 仍会在无 Cookie/Referer 且无异常捕获的情况下发起下载，
    一旦单张外链失效或超限会导致整条合并转发发送失败。因此此处仅返回已校验落盘成功的本地文件路径列表，
    并统计未附带（超限或源站失效）的图片数量供上层展示纯文本提示，确保合并转发 100% 稳定发出。

    Returns:
        (local_file_paths, skipped_images_count)
    """
    if not post_image_urls:
        return [], 0
    if max_images <= 0:
        return [], len(post_image_urls)

    target_urls = post_image_urls[:max_images]
    overflow_by_count = max(0, len(post_image_urls) - len(target_urls))
    idx_to_file: dict[int, Path] = {}
    used_file_bytes = 0

    # 1. 逐个扫描已内嵌 Data URI 并逐张解码落盘（不重新构建 DOM，也不把多张图同时堆在字典内存里）
    if content_html and "data:image/" in content_html:
        for m in INLINED_FORUM_IMG_RE.finditer(content_html):
            uri = m.group(1) or m.group(4) or ""
            idx_s = m.group(2) or m.group(3) or ""
            if not uri or not idx_s.isdigit():
                continue
            idx = int(idx_s)
            if idx >= len(target_urls) or idx in idx_to_file:
                continue
            est_raw_len = _data_uri_raw_byte_len(uri)
            if est_raw_len > max_single_bytes or (used_file_bytes + est_raw_len) > max_total_bytes:
                continue
            mime_part = uri.split(";", 1)[0].removeprefix("data:")
            dest = scope.alloc_file(prefix=f"post_{idx}", ext=mime_to_ext(mime_part))
            try:
                written = await asyncio.to_thread(_decode_data_uri_to_file_sync, uri, dest)
                idx_to_file[idx] = dest
                used_file_bytes += written
            except Exception:
                continue

    # 2. 对未内嵌的图片按低并发（2 路）64KB 分块直接流式落盘
    if session is not None and used_file_bytes < max_total_bytes:
        sem = asyncio.Semaphore(2)
        missing = [i for i in range(len(target_urls)) if i not in idx_to_file]

        for batch_start in range(0, len(missing), 2):
            if used_file_bytes >= max_total_bytes:
                break
            batch = missing[batch_start : batch_start + 2]

            async def _dl_one(idx: int) -> tuple[int, Path | None, int]:
                async with sem:
                    remain_budget = max_total_bytes - used_file_bytes
                    if remain_budget <= 0:
                        return idx, None, 0
                    eff_cap = min(max_single_bytes, remain_budget)
                    p, sz = await _stream_single_image_to_scope(
                        session,
                        rewrite_legacy_media_url(target_urls[idx]),
                        scope,
                        referer=referer,
                        cookies=cookies,
                        same_site_hosts=same_site_hosts,
                        max_bytes=eff_cap,
                    )
                    return idx, p, sz

            batch_res = await asyncio.gather(*(_dl_one(i) for i in batch))
            for idx, p, sz in batch_res:
                if p is not None and sz > 0:
                    if (used_file_bytes + sz) <= max_total_bytes:
                        idx_to_file[idx] = p
                        used_file_bytes += sz
                    else:
                        await asyncio.to_thread(p.unlink, True)

    local_files: list[str] = []
    skipped_in_target = 0
    for idx in range(len(target_urls)):
        if idx in idx_to_file:
            local_files.append(str(idx_to_file[idx]))
        else:
            skipped_in_target += 1

    return local_files, (overflow_by_count + skipped_in_target)
