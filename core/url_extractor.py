from __future__ import annotations

import html
import json
import re
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .models import ForumLink, ForumType

# Stage1st 域名集合
S1_PRIMARY_HOSTS = {"stage1st.com", "www.stage1st.com"}
S1_ALIAS_HOSTS = {
    "bbs.saraba1st.com",
    "saraba1st.com",
    "www.saraba1st.com",
    "app.stage1st.com",
    "app.saraba1st.com",
}

# NGA 域名集合
NGA_PRIMARY_HOSTS = {"bbs.nga.cn"}
NGA_ALIAS_HOSTS = {
    "ngabbs.com",
    "www.ngabbs.com",
    "nga.178.com",
    "g.nga.cn",
    "bbs.ngacn.cc",
}

# 常见跳转包装域名（如 QQ 链接安全跳转）
REDIRECT_WRAPPER_HOSTS = {
    "c.pc.qq.com",
    "weixin110.qq.com",
}

# 匹配文本中潜在的 HTTP(S) URL
URL_CANDIDATE_PATTERN = re.compile(
    r"https?://[^\s<>\"'`\u3000\uff08\uff09\u3010\u3011\u300a\u300b]+",
    re.IGNORECASE,
)

# URL 末尾常见粘连标点
TRAILING_PUNCTUATION = ".,;:!?)]}>'\"，。；：！？）、》】」』"

# S1 静态伪静态路径匹配
S1_THREAD_HTML_PATTERN = re.compile(r"/thread-(\d+)-(\d+)-\d+\.html", re.IGNORECASE)
S1_ARCHIVER_PATTERN = re.compile(r"/archiver/tid-(\d+)\.html", re.IGNORECASE)
S1_FRAGMENT_PID_PATTERN = re.compile(r"^(?:pid|post_)(\d+)$", re.IGNORECASE)

# NGA 锚点匹配（如 #pid795123456Anchor 或 #l25）
NGA_FRAGMENT_PID_PATTERN = re.compile(r"^pid(\d+)(?:anchor)?$", re.IGNORECASE)
NGA_FRAGMENT_FLOOR_PATTERN = re.compile(r"^l(\d+)$", re.IGNORECASE)


def _strip_trailing_punctuation(raw_url: str) -> str:
    """剥离聊天消息中粘连在 URL 末尾的中英文标点，同时保留平衡括号。"""
    url = html.unescape(raw_url.strip())
    while url and url[-1] in TRAILING_PUNCTUATION:
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        url = url[:-1]
    return url


def _unwrap_redirect_url(url: str) -> str:
    """解包如 QQ 安全中心 c.pc.qq.com/middlem.html?pfurl=... 的嵌套目标 URL。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url

    host = (parsed.hostname or "").lower()
    if host not in REDIRECT_WRAPPER_HOSTS:
        return url

    qs = parse_qs(parsed.query)
    for key in ("pfurl", "url", "target", "dest"):
        vals = qs.get(key)
        if vals and vals[0]:
            decoded = unquote(vals[0]).strip()
            if decoded.startswith(("http://", "https://")):
                return decoded
    return url


def parse_forum_url(
    raw_url: str,
    *,
    allow_alias_domains: bool = True,
    enable_s1: bool = True,
    enable_nga: bool = True,
) -> ForumLink | None:
    """尝试将单个 URL 解析为受支持的论坛主楼链接。"""
    if not raw_url:
        return None

    cleaned = _strip_trailing_punctuation(raw_url)
    if not cleaned.lower().startswith(("http://", "https://")):
        extracted = extract_urls_from_text(cleaned)
        if not extracted:
            return None
        cleaned = _strip_trailing_punctuation(extracted[0])
    cleaned = _unwrap_redirect_url(cleaned)
    cleaned = _strip_trailing_punctuation(cleaned)

    try:
        parsed = urlparse(cleaned)
    except ValueError:
        return None

    if parsed.scheme.lower() not in {"http", "https"}:
        return None

    host = (parsed.hostname or "").lower()
    if not host:
        return None

    # 1. Stage1st 识别
    s1_hosts = S1_PRIMARY_HOSTS | S1_ALIAS_HOSTS if allow_alias_domains else S1_PRIMARY_HOSTS
    if enable_s1 and host in s1_hosts:
        path = parsed.path or ""
        # 主域名下要求位于 /2b/ 路径下（兼容 /2b/thread-...、/2b/forum.php?...、/2b/archiver/...）
        if not path.startswith("/2b/"):
            return None

        qs = parse_qs(parsed.query)
        frag = (parsed.fragment or "").strip()

        tid = ""
        pid = ""
        page = 1

        raw_page = qs.get("page", [None])[0]
        if raw_page and str(raw_page).isdigit() and int(raw_page) > 0:
            page = int(raw_page)

        m_frag_pid = S1_FRAGMENT_PID_PATTERN.match(frag)
        if m_frag_pid and int(m_frag_pid.group(1)) > 0:
            pid = m_frag_pid.group(1)

        m_thread = S1_THREAD_HTML_PATTERN.search(path)
        if m_thread:
            tid = m_thread.group(1)
            if int(m_thread.group(2)) > 0:
                page = int(m_thread.group(2))
        else:
            m_arch = S1_ARCHIVER_PATTERN.search(path)
            if m_arch:
                tid = m_arch.group(1)
            elif path.lower().endswith("/forum.php"):
                mod = (qs.get("mod", [""])[0] or "").lower()
                goto = (qs.get("goto", [""])[0] or "").lower()
                raw_tid = qs.get("tid", [None])[0] or (
                    qs.get("ptid", [None])[0] if mod == "redirect" else None
                )
                if raw_tid and raw_tid.isdigit() and int(raw_tid) > 0:
                    tid = raw_tid

                raw_pid = qs.get("pid", [None])[0]
                if (
                    raw_pid
                    and raw_pid.isdigit()
                    and int(raw_pid) > 0
                    and (mod in {"redirect", "viewthread"} or goto == "findpost")
                ):
                    pid = raw_pid

        has_valid_tid = bool(tid and tid.isdigit() and int(tid) > 0)
        has_valid_pid = bool(pid and pid.isdigit() and int(pid) > 0)

        if has_valid_pid:
            canonical = (
                f"https://stage1st.com/2b/forum.php?mod=redirect&goto=findpost&ptid={tid}&pid={pid}"
                if has_valid_tid
                else f"https://stage1st.com/2b/forum.php?mod=redirect&goto=findpost&pid={pid}"
            )
            return ForumLink(
                forum=ForumType.STAGE1ST,
                tid=tid if has_valid_tid else "",
                original_url=cleaned,
                canonical_url=canonical,
                pid=pid,
                page=page,
            )

        if has_valid_tid:
            return ForumLink(
                forum=ForumType.STAGE1ST,
                tid=tid,
                original_url=cleaned,
                canonical_url=f"https://stage1st.com/2b/thread-{tid}-1-1.html",
                page=1,
            )
        return None

    # 2. NGA 识别（支持主楼链接与带 pid / #l楼层 的单楼层链接）
    nga_hosts = NGA_PRIMARY_HOSTS | NGA_ALIAS_HOSTS if allow_alias_domains else NGA_PRIMARY_HOSTS
    if enable_nga and host in nga_hosts:
        path = (parsed.path or "").lower()
        if path in {"/read.php", "/nuke.php"} or path.endswith("/read.php"):
            qs = parse_qs(parsed.query)
            frag = (parsed.fragment or "").strip()

            raw_tid = qs.get("tid", [None])[0]
            tid = raw_tid if (raw_tid and raw_tid.isdigit() and int(raw_tid) > 0) else ""

            raw_pid = qs.get("pid", [None])[0]
            pid = raw_pid if (raw_pid and raw_pid.isdigit() and int(raw_pid) > 0) else ""
            if not pid:
                m_npid = NGA_FRAGMENT_PID_PATTERN.match(frag)
                if m_npid and int(m_npid.group(1)) > 0:
                    pid = m_npid.group(1)

            floor_hint = 0
            if not pid:
                m_nfloor = NGA_FRAGMENT_FLOOR_PATTERN.match(frag)
                if m_nfloor and int(m_nfloor.group(1)) > 0:
                    floor_hint = int(m_nfloor.group(1))

            raw_page = qs.get("page", [None])[0]
            page = int(raw_page) if (raw_page and raw_page.isdigit() and int(raw_page) > 0) else 1

            if pid:
                canonical = (
                    f"https://bbs.nga.cn/read.php?tid={tid}&pid={pid}"
                    if tid
                    else f"https://bbs.nga.cn/read.php?pid={pid}"
                )
                return ForumLink(
                    forum=ForumType.NGA,
                    tid=tid,
                    original_url=cleaned,
                    canonical_url=canonical,
                    pid=pid,
                    page=page,
                )
            if tid and floor_hint > 0:
                return ForumLink(
                    forum=ForumType.NGA,
                    tid=tid,
                    original_url=cleaned,
                    canonical_url=f"https://bbs.nga.cn/read.php?tid={tid}&page={page}#l{floor_hint}",
                    page=page,
                    floor_hint=floor_hint,
                )
            if tid:
                return ForumLink(
                    forum=ForumType.NGA,
                    tid=tid,
                    original_url=cleaned,
                    canonical_url=f"https://bbs.nga.cn/read.php?tid={tid}",
                )
        return None

    return None


def extract_urls_from_text(text: str) -> list[str]:
    """从任意字符串中提取所有候选 HTTP(S) URL。"""
    if not text:
        return []
    normalized = html.unescape(text.replace(r"\/", "/"))
    return [m.group(0) for m in URL_CANDIDATE_PATTERN.finditer(normalized)]


def _walk_json_strings(obj: Any, out: list[str], depth: int = 0) -> None:
    """递归遍历 JSON 结构收集字符串字段中的 URL。"""
    if depth > 12 or obj is None:
        return
    if isinstance(obj, str):
        if "http://" in obj or "https://" in obj or r"http:\/\/" in obj or r"https:\/\/" in obj:
            out.extend(extract_urls_from_text(obj))
        elif obj.startswith(("{", "[")):
            try:
                nested = json.loads(obj)
                _walk_json_strings(nested, out, depth + 1)
            except Exception:
                pass
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk_json_strings(v, out, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _walk_json_strings(item, out, depth + 1)


def _extract_telegram_entity_urls(raw_message: Any) -> list[str]:
    """从 Telegram Update / Message 对象的 entities 与 caption_entities 中提取隐藏链接。"""
    urls: list[str] = []
    if raw_message is None:
        return urls

    # 兼容 python-telegram-bot Update 对象或消息对象或 dict
    msg_candidates: list[Any] = []
    if isinstance(raw_message, dict):
        for key in ("message", "edited_message", "channel_post"):
            if isinstance(raw_message.get(key), dict):
                msg_candidates.append(raw_message[key])
        if not msg_candidates:
            msg_candidates.append(raw_message)
    else:
        for attr in ("message", "edited_message", "channel_post"):
            sub = getattr(raw_message, attr, None)
            if sub is not None:
                msg_candidates.append(sub)
        if not msg_candidates:
            msg_candidates.append(raw_message)

    for msg in msg_candidates:
        if isinstance(msg, dict):
            for ent_key in ("entities", "caption_entities"):
                for ent in msg.get(ent_key) or ():
                    if isinstance(ent, dict) and ent.get("url"):
                        urls.append(str(ent["url"]))
            for txt_key in ("text", "caption"):
                if isinstance(msg.get(txt_key), str):
                    urls.extend(extract_urls_from_text(msg[txt_key]))
        else:
            for ent_attr in ("entities", "caption_entities"):
                entities = getattr(msg, ent_attr, None) or ()
                for ent in entities:
                    ent_url = getattr(ent, "url", None)
                    if ent_url:
                        urls.append(str(ent_url))
            for txt_attr in ("text", "caption"):
                txt_val = getattr(msg, txt_attr, None)
                if isinstance(txt_val, str):
                    urls.extend(extract_urls_from_text(txt_val))

    return urls


def extract_forum_links_from_event(
    event: Any,
    *,
    allow_alias_domains: bool = True,
    parse_share_cards: bool = True,
    enable_s1: bool = True,
    enable_nga: bool = True,
) -> list[ForumLink]:
    """从 AstrBot 消息事件（支持 OneBot11 与 Telegram）提取去重后的论坛帖子链接列表。"""
    candidate_urls: list[str] = []

    # 1. 纯文本内容
    msg_str = getattr(event, "message_str", "") or ""
    if msg_str:
        candidate_urls.extend(extract_urls_from_text(msg_str))

    # 2. 消息链组件（Plain, Json 卡片, Share 卡片）
    messages: list[Any] = []
    get_messages_fn = getattr(event, "get_messages", None)
    if callable(get_messages_fn):
        try:
            messages = list(get_messages_fn() or [])
        except Exception:
            messages = []
    elif getattr(event, "message_obj", None) is not None:
        messages = list(getattr(event.message_obj, "message", None) or [])

    for comp in messages:
        comp_type = getattr(comp, "type", None)
        comp_type_str = str(getattr(comp_type, "value", comp_type) or type(comp).__name__)

        if comp_type_str == "Plain":
            txt = getattr(comp, "text", "")
            if txt:
                candidate_urls.extend(extract_urls_from_text(txt))
        elif parse_share_cards and comp_type_str == "Share":
            share_url = getattr(comp, "url", "")
            if share_url:
                candidate_urls.append(str(share_url))
        elif parse_share_cards and comp_type_str == "Json":
            json_data = getattr(comp, "data", None)
            _walk_json_strings(json_data, candidate_urls)

    # 3. Telegram 原生消息实体（text_link 等）
    if parse_share_cards:
        msg_obj = getattr(event, "message_obj", None)
        raw_msg = getattr(msg_obj, "raw_message", None) if msg_obj is not None else None
        if raw_msg is not None:
            candidate_urls.extend(_extract_telegram_entity_urls(raw_msg))

    # 4. 归一化并按 (forum, target_id) 去重保序（同帖不同楼层互不覆盖）
    results: list[ForumLink] = []
    seen: set[tuple[ForumType, str]] = set()
    for raw_u in candidate_urls:
        link = parse_forum_url(
            raw_u,
            allow_alias_domains=allow_alias_domains,
            enable_s1=enable_s1,
            enable_nga=enable_nga,
        )
        if link is None:
            continue
        key = (link.forum, link.target_id)
        if key not in seen:
            seen.add(key)
            results.append(link)

    return results


class CooldownTracker:
    """会话内同帖重复解析冷却管理器（带自动过期清扫，避免内存累积）。"""

    def __init__(self, max_entries: int = 512) -> None:
        self._records: dict[tuple[str, str, str], tuple[float, float]] = {}
        self._max_entries = max_entries
        self._last_prune: float = 0.0

    def prune_expired(self) -> int:
        """清理所有已过期的冷却记录，返回清理条数。"""
        now = time.monotonic()
        removed = 0
        for k in list(self._records.keys()):
            _, expires_at = self._records[k]
            if now >= expires_at:
                self._records.pop(k, None)
                removed += 1
        self._last_prune = now
        return removed

    def clear(self) -> None:
        self._records.clear()

    def check_and_mark(
        self,
        session_id: str,
        forum: ForumType,
        tid: str,
        cooldown_seconds: int,
    ) -> bool:
        """若未处于冷却期则记录当前时间并返回 True；若仍在冷却期则返回 False。"""
        if cooldown_seconds <= 0:
            return True
        now = time.monotonic()
        if (now - self._last_prune) >= 60.0 or len(self._records) >= self._max_entries:
            self.prune_expired()

        key = (session_id, forum.value, tid)
        entry = self._records.get(key)
        if entry is not None:
            _, expires_at = entry
            if now < expires_at:
                return False
            self._records.pop(key, None)

        if len(self._records) >= self._max_entries:
            oldest_key = min(self._records, key=lambda item_k: self._records[item_k][1])
            self._records.pop(oldest_key, None)

        self._records[key] = (now, now + cooldown_seconds)
        return True
