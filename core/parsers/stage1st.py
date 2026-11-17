from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from ..html_sanitizer import (
    MiniNode,
    _fetch_image_as_data_uri,
    parse_html,
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
    RateItem,
)
from .base import DEFAULT_BROWSER_UA, BaseForumParser, parse_cookie_string

logger = logging.getLogger("astrbot")

S1_WEB_BASE = "https://stage1st.com/2b/"
S1_MOBILE_LOGIN_URL = (
    "https://stage1st.com/2b/api/mobile/index.php"
    "?version=4&module=login&loginsubmit=yes&loginfield=auto&cookietime=2592000"
)
# 参考 GreasyFork 419494 脚本：S1 官方 App 接口根路径（旧域名 saraba1st.com 已下线）
S1_APP_API_BASES = ("https://app.stage1st.com/2b/api/app",)

S1_SAME_SITE_HOSTS = {
    "stage1st.com",
    "www.stage1st.com",
    "static.stage1st.com",
    "img.stage1st.com",
    "avatar.stage1st.com",
}

# 网页端不可见/受限提示关键字
WEB_BLOCKED_POST_HINTS = (
    "作者被禁止或删除 内容自动屏蔽",
    "该帖被管理员或版主屏蔽",
    "内容审核中，即将开放",
    "内容审核中",
)

WEB_MODERATED_THREAD_HINTS = (
    "内容审核中，即将开放",
    "正在被审核",
    "指定的主题不存在或已被删除或正在被审核",
)

WEB_LOGIN_OR_PERM_HINTS = (
    "您需要登录才可以下载或查看",
    "您需要登录",
    "请先登录",
    "无权访问该版块",
    "本帖要求阅读权限高于",
    "抱歉，只有特定用户可以访问",
)

UID_IN_HREF_RE = re.compile(r"(?:space-uid-|uid=)(\d+)")
CHINA_TZ = timezone(timedelta(hours=8))

# 针对跨境（如香港/海外直连内地阿里云源站）优化的短握手/读取超时与重试次数，避免丢包死连接傻等 15 秒
S1_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=12, connect=4, sock_connect=4, sock_read=8)
S1_MAX_ATTEMPTS = 2


def format_network_exc(exc: BaseException) -> str:
    """格式化异常信息，防止 asyncio.TimeoutError 的 str(e) 为空字符串导致日志原因空白。"""
    msg = str(exc).strip()
    if isinstance(exc, TimeoutError | asyncio.TimeoutError):
        return f"连接或读取超时 ({msg})" if msg else "连接或读取超时 (TimeoutError)"
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def build_s1_avatar_url(uid: str | int | None) -> str:
    """根据 Discuz UID 规则构造 Stage1st 用户头像地址。"""
    if not uid:
        return ""
    s = str(uid).strip()
    if not s.isdigit() or int(s) <= 0:
        return ""
    padded = f"{int(s):09d}"
    path = f"{padded[0:3]}/{padded[3:5]}/{padded[5:7]}/{padded[7:9]}"
    return f"https://avatar.stage1st.com/{path}_avatar_middle.jpg"


def format_timestamp_cn(ts: int | float | str | None) -> str:
    """将 Unix 秒或毫秒时间戳格式化为东八区时间字符串。"""
    if ts is None or ts == "":
        return ""
    try:
        val = float(ts)
        if val > 1e11:
            val /= 1000.0
        if val <= 0:
            return ""
        dt = datetime.fromtimestamp(val, tz=CHINA_TZ)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OSError):
        return str(ts)


class Stage1stParser(BaseForumParser):
    """Stage1st (S1) 论坛主楼解析器。

    同时支持：
    1. Discuz 网页端解析（支持配置账号密码自动登录网页会话或手动配置 Cookie）；
    2. 官方 App 接口解析（参考 GreasyFork 脚本 419494，解锁“内容审核中”及“作者被禁止/屏蔽”不可见主楼，支持 SID 持久化与过期自动重登）。
    """

    KV_APP_SID = "s1_app_sid"
    KV_APP_USER = "s1_app_username"
    KV_WEB_COOKIES = "s1_web_cookies"
    KV_WEB_USER = "s1_web_username"

    def __init__(
        self,
        session: aiohttp.ClientSession,
        config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(session, **kwargs)
        self.cfg: dict[str, Any] = config or {}
        self._mem_app_sid: str = ""
        self._mem_web_cookies: dict[str, str] = {}

    def update_config(self, config: dict[str, Any] | None) -> None:
        self.cfg = config or {}

    @property
    def _username(self) -> str:
        return str(self.cfg.get("username") or "").strip()

    @property
    def _password(self) -> str:
        return str(self.cfg.get("password") or "")

    @property
    def _question_id(self) -> int:
        try:
            return int(self.cfg.get("question_id") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def _answer(self) -> str:
        return str(self.cfg.get("answer") or "").strip()

    @property
    def _enable_invisible_parse(self) -> bool:
        return bool(self.cfg.get("enable_invisible_parse", True))

    def _has_credentials(self) -> bool:
        return bool(self._username and self._password)

    async def _get_web_cookies(self) -> dict[str, str]:
        """获取网页端请求可用的 Cookie。"""
        manual_cookie = str(self.cfg.get("cookie") or "").strip()
        if manual_cookie:
            return parse_cookie_string(manual_cookie)

        if self._mem_web_cookies:
            return dict(self._mem_web_cookies)

        saved_user = await self.get_state(self.KV_WEB_USER, "")
        saved_cookies = await self.get_state(self.KV_WEB_COOKIES, None)
        if (
            isinstance(saved_cookies, dict)
            and saved_cookies
            and (not self._username or saved_user == self._username)
        ):
            self._mem_web_cookies = {str(k): str(v) for k, v in saved_cookies.items()}
            return dict(self._mem_web_cookies)
        return {}

    async def _login_web_session(self) -> dict[str, str]:
        """使用账号密码登录 Stage1st 网页/移动端会话并缓存 Cookie。"""
        if not self._has_credentials():
            return {}

        form: dict[str, str] = {
            "username": self._username,
            "password": self._password,
        }
        if self._question_id > 0:
            form["questionid"] = str(self._question_id)
            form["answer"] = self._answer

        headers = {
            "User-Agent": DEFAULT_BROWSER_UA,
            "Referer": S1_WEB_BASE,
        }
        last_err = ""
        for attempt in range(S1_MAX_ATTEMPTS):
            req_headers = dict(headers)
            if attempt > 0:
                req_headers["Connection"] = "close"
            try:
                async with self.session.post(
                    S1_MOBILE_LOGIN_URL,
                    data=form,
                    headers=req_headers,
                    timeout=S1_REQUEST_TIMEOUT,
                ) as resp:
                    cookies: dict[str, str] = {}
                    for k, morsel in resp.cookies.items():
                        cookies[k] = morsel.value
                    # 兼容从 Set-Cookie 头提取所有键值
                    for raw_sc in resp.headers.getall("Set-Cookie", []):
                        first_pair = raw_sc.split(";", 1)[0].strip()
                        if "=" in first_pair:
                            ck, cv = first_pair.split("=", 1)
                            cookies[ck.strip()] = cv.strip()

                    body_text = await resp.text(errors="replace")
                    if cookies and (
                        "login_succeed" in body_text or any("auth" in k for k in cookies)
                    ):
                        self._mem_web_cookies = cookies
                        await self.set_state(self.KV_WEB_USER, self._username)
                        await self.set_state(self.KV_WEB_COOKIES, cookies)
                        logger.info("Stage1st 网页会话登录成功。")
                        return dict(cookies)
                    logger.warning("Stage1st 网页登录未返回有效鉴权 Cookie。")
                    return {}
            except Exception as e:
                last_err = format_network_exc(e)
                if attempt + 1 < S1_MAX_ATTEMPTS:
                    logger.debug(
                        f"Stage1st 网页登录第 {attempt + 1} 次超时/异常，正在换新连接重试: {last_err}"
                    )
                    continue
        logger.warning(f"Stage1st 网页登录请求失败: {last_err}")
        return {}

    async def _get_app_sid(self, *, force_relogin: bool = False) -> str:
        """获取 Stage1st 官方 App 接口的会话凭证 sid。"""
        if not force_relogin:
            if self._mem_app_sid:
                return self._mem_app_sid

            saved_user = await self.get_state(self.KV_APP_USER, "")
            saved_sid = await self.get_state(self.KV_APP_SID, "")
            if saved_sid and (not self._username or saved_user == self._username):
                self._mem_app_sid = str(saved_sid)
                return self._mem_app_sid

            manual_sid = str(self.cfg.get("app_sid") or "").strip()
            if manual_sid:
                self._mem_app_sid = manual_sid
                return manual_sid

        if not self._has_credentials():
            manual_sid = str(self.cfg.get("app_sid") or "").strip()
            if manual_sid and not force_relogin:
                return manual_sid
            return ""

        return await self._login_app_api()

    async def _login_app_api(self) -> str:
        """参考脚本 419494 调用 POST /user/login 获取 App 接口 sid。"""
        form: dict[str, str] = {
            "username": self._username,
            "password": self._password,
        }
        if self._question_id > 0:
            form["questionid"] = str(self._question_id)
            form["answer"] = self._answer

        headers = {
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "User-Agent": DEFAULT_BROWSER_UA,
        }
        last_err: str = ""

        for api_base in S1_APP_API_BASES:
            for attempt in range(S1_MAX_ATTEMPTS):
                req_headers = dict(headers)
                if attempt > 0:
                    req_headers["Connection"] = "close"
                try:
                    async with self.session.post(
                        f"{api_base}/user/login",
                        data=form,
                        headers=req_headers,
                        timeout=S1_REQUEST_TIMEOUT,
                    ) as resp:
                        if resp.status != 200:
                            last_err = f"HTTP {resp.status}"
                            continue
                        payload = await resp.json(content_type=None)
                        code = str(payload.get("code") or "")
                        sid = (payload.get("data") or {}).get("sid")
                        if code and not code.startswith("50") and sid:
                            self._mem_app_sid = str(sid)
                            await self.set_state(self.KV_APP_USER, self._username)
                            await self.set_state(self.KV_APP_SID, self._mem_app_sid)
                            logger.info("Stage1st 官方 App 接口登录成功，已缓存 sid。")
                            return self._mem_app_sid
                        last_err = str(payload.get("message") or f"code={code}")
                        break
                except Exception as e:
                    last_err = format_network_exc(e)
                    if attempt + 1 < S1_MAX_ATTEMPTS:
                        logger.debug(
                            f"Stage1st App 登录第 {attempt + 1} 次超时/异常，正在换新连接重试: {last_err}"
                        )
                        continue

        logger.warning(f"Stage1st 官方 App 接口登录失败: {last_err}")
        return ""

    async def _post_app_endpoint(
        self,
        route: str,
        params: dict[str, str],
        sid: str,
    ) -> tuple[dict[str, Any] | None, bool, str]:
        """请求 S1 官方 App 接口路由（支持跨境丢包快速断连换新连接重试）。

        Returns:
            (data_dict, is_login_invalid, error_message)
        """
        req_data = dict(params)
        if sid:
            req_data["sid"] = sid

        headers = {
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "User-Agent": DEFAULT_BROWSER_UA,
        }
        if sid:
            headers["authorization"] = sid

        last_err = ""
        clean_route = route.lstrip("/")
        for api_base in S1_APP_API_BASES:
            for attempt in range(S1_MAX_ATTEMPTS):
                req_headers = dict(headers)
                if attempt > 0:
                    req_headers["Connection"] = "close"
                try:
                    async with self.session.post(
                        f"{api_base}/{clean_route}",
                        data=req_data,
                        headers=req_headers,
                        timeout=S1_REQUEST_TIMEOUT,
                    ) as resp:
                        if resp.status != 200:
                            last_err = f"HTTP {resp.status}"
                            continue
                        payload = await resp.json(content_type=None)
                        if not isinstance(payload, dict):
                            last_err = "响应格式非预期 JSON"
                            continue
                        code = str(payload.get("code") or "")
                        msg = str(payload.get("message") or "").strip()
                        if code.startswith("50") or "重新登录" in msg or msg.startswith("请先登录"):
                            return None, True, (msg or f"登录态失效(code={code})")
                        data = payload.get("data")
                        if isinstance(data, dict):
                            return data, False, ""
                        return None, False, (msg or (f"code={code}" if code else "无可见数据"))
                except Exception as e:
                    last_err = format_network_exc(e)
                    if attempt + 1 < S1_MAX_ATTEMPTS:
                        logger.debug(
                            f"S1 App 接口 /{clean_route} 第 {attempt + 1} 次异常，换新连接重试: {last_err}"
                        )
                        continue
            if last_err:
                logger.warning(
                    f"Stage1st App 接口 /{clean_route} 请求失败 (tid={params.get('tid', '')}): {last_err}"
                )
        return None, False, (f"App 接口请求失败（{last_err}）" if last_err else "App 接口无响应")

    async def _fetch_via_app_api(
        self, tid: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
        """通过 S1 官方 App 接口并发获取主题元信息 (/thread) 与主楼 (/thread/page, pageSize=2)。

        仅拉取前 2 条楼层以大幅缩减跨境数据包体积，并在检测到 sid 失效（50x 状态码）时自动重登重试。
        """
        logger.info(f"Stage1st 正在通过官方 App 接口获取帖子主楼 (tid={tid})...")
        sid = await self._get_app_sid(force_relogin=False)
        last_err = ""

        for attempt in range(2):
            (thread_info, invalid_1, err_1), (page_data, invalid_2, err_2) = await asyncio.gather(
                self._post_app_endpoint("thread", {"tid": tid}, sid),
                self._post_app_endpoint(
                    "thread/page",
                    {"tid": tid, "pageNo": "1", "pageSize": "2"},
                    sid,
                ),
            )
            last_err = err_2 or err_1

            if (invalid_1 or invalid_2) and attempt == 0:
                logger.info("Stage1st App sid 失效或为空，尝试自动登录刷新 sid...")
                self._mem_app_sid = ""
                await self.set_state(self.KV_APP_SID, "")
                new_sid = await self._get_app_sid(force_relogin=True)
                if new_sid:
                    sid = new_sid
                    continue
                last_err = "App 接口登录获取 sid 失败"
                break

            post_list = (page_data or {}).get("list") if isinstance(page_data, dict) else None
            main_post: dict[str, Any] | None = None
            if isinstance(post_list, list) and post_list:
                target_pid = (thread_info or {}).get("pid")
                for p in post_list:
                    if not isinstance(p, dict):
                        continue
                    if p.get("position") == 1 or (
                        target_pid and str(p.get("pid")) == str(target_pid)
                    ):
                        main_post = p
                        break
                if main_post is None and isinstance(post_list[0], dict):
                    main_post = post_list[0]

            if thread_info or main_post:
                return thread_info, main_post, last_err
            break

        return None, None, (last_err or "未获取到主楼内容")

    async def _fetch_web_page(self, tid: str, cookies: dict[str, str]) -> tuple[str | None, str]:
        url = f"{S1_WEB_BASE}thread-{tid}-1-1.html"
        headers = {
            "User-Agent": DEFAULT_BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": S1_WEB_BASE,
        }
        last_err = ""
        for attempt in range(S1_MAX_ATTEMPTS):
            req_headers = dict(headers)
            if attempt > 0:
                req_headers["Connection"] = "close"
            try:
                async with self.session.get(
                    url,
                    headers=req_headers,
                    cookies=cookies or None,
                    timeout=S1_REQUEST_TIMEOUT,
                ) as resp:
                    if resp.status != 200:
                        last_err = f"HTTP {resp.status}"
                        if resp.status >= 500 and attempt + 1 < S1_MAX_ATTEMPTS:
                            continue
                        break
                    raw_bytes = await read_response_bounded(resp)
                    return raw_bytes.decode("utf-8", errors="replace"), ""
            except Exception as e:
                last_err = format_network_exc(e)
                if attempt + 1 < S1_MAX_ATTEMPTS:
                    logger.debug(
                        f"Stage1st 网页请求第 {attempt + 1} 次异常 (tid={tid})，换新连接重试: {last_err}"
                    )
                    continue
        logger.warning(f"Stage1st 网页请求失败 (tid={tid}): {last_err}")
        return None, f"网页请求失败（{last_err or '未知错误'}）"

    @staticmethod
    def _extract_web_metadata(doc: MiniNode) -> dict[str, Any]:
        """从 S1 网页 DOM 提取标题、分类、版块、浏览/回复数。"""
        meta: dict[str, Any] = {
            "title": "",
            "category": "",
            "forum_name": "",
            "views": None,
            "replies": None,
        }
        subj_el = doc.find_first(id_exact="thread_subject")
        if subj_el:
            meta["title"] = subj_el.inner_text().strip()

        # 主题分类，例如 [泥潭] / [讨论]
        ts_h1 = doc.find_first(tag="h1", class_name="ts")
        if ts_h1:
            for a_el in ts_h1.find_all(tag="a"):
                if "typeid" in a_el.get_attr("href"):
                    cat_txt = a_el.inner_text().strip().strip("[]【】")
                    if cat_txt:
                        meta["category"] = cat_txt
                        break

        # 面包屑导航提取版块名
        pt_div = doc.find_first(id_exact="pt")
        if pt_div:
            forum_links = [
                a.inner_text().strip()
                for a in pt_div.find_all(tag="a")
                if ("forum-" in a.get_attr("href") or "mod=forumdisplay" in a.get_attr("href"))
                and a.inner_text().strip()
            ]
            if forum_links:
                meta["forum_name"] = forum_links[-1]

        # 查看数与回复数
        postlist = doc.find_first(id_exact="postlist")
        if postlist:
            hm_div = postlist.find_first(class_name="hm")
            if hm_div:
                nums = [
                    s.inner_text().strip()
                    for s in hm_div.find_all(tag="span", class_name="xi1")
                    if s.inner_text().strip().isdigit()
                ]
                if len(nums) >= 2:
                    meta["views"] = int(nums[0])
                    meta["replies"] = int(nums[1])

        return meta

    @staticmethod
    def _extract_web_first_post(doc: MiniNode) -> tuple[MiniNode | None, str]:
        """查找网页中的主楼节点，并检测是否存在审核/屏蔽/登录限制。

        Returns:
            (post_div_or_none, block_reason_if_any)
        """
        msg_box = doc.find_first(id_exact="messagetext")
        if msg_box:
            msg_txt = msg_box.inner_text().strip()
            for hint in WEB_MODERATED_THREAD_HINTS:
                if hint in msg_txt:
                    return None, hint
            for hint in WEB_LOGIN_OR_PERM_HINTS:
                if hint in msg_txt:
                    return None, msg_txt

        postlist = doc.find_first(id_exact="postlist")
        if not postlist:
            return None, ""

        post_divs = [
            c
            for c in postlist.children
            if not c.is_text and c.tag == "div" and c.get_attr("id").startswith("post_")
        ]
        if not post_divs:
            post_divs = postlist.find_all(tag="div", id_prefix="post_")
        if not post_divs:
            return None, ""

        first_post = post_divs[0]
        for p in post_divs:
            floor_el = p.find_first(tag="a", id_prefix="postnum")
            if floor_el:
                fl_txt = floor_el.inner_text().strip()
                if "楼主" in fl_txt or fl_txt in {"1#", "1 #", "1"}:
                    first_post = p
                    break

        plhin = first_post.find_first(class_name="plhin") or first_post
        plhin_text = plhin.inner_text()
        for hint in WEB_BLOCKED_POST_HINTS:
            if hint in plhin_text:
                return first_post, hint

        msg_td = first_post.find_first(tag="td", id_prefix="postmessage_")
        if msg_td is None:
            pcb = first_post.find_first(class_name="pcb")
            pcb_text = pcb.inner_text().strip() if pcb else ""
            if pcb_text:
                return first_post, pcb_text[:60]
            return first_post, "主楼内容不可见"

        return first_post, ""

    @staticmethod
    def _extract_post_details_from_div(post_div: MiniNode) -> dict[str, Any]:
        """从正常可见的 S1 主楼 DOM 节点提取作者、时间、正文与评分。"""
        author = "匿名"
        author_id = ""
        authi_links = post_div.find_all(tag="a", predicate=lambda n: "xw1" in n.classes)
        for a in authi_links:
            name = a.inner_text().strip()
            if name:
                author = name
                m_uid = UID_IN_HREF_RE.search(a.get_attr("href"))
                if m_uid:
                    author_id = m_uid.group(1)
                break

        avatar_url = ""
        avatar_div = post_div.find_first(class_name="avatar")
        if avatar_div:
            av_img = avatar_div.find_first(tag="img")
            if av_img and av_img.get_attr("src").startswith("http"):
                avatar_url = av_img.get_attr("src")
        if not avatar_url and author_id:
            avatar_url = build_s1_avatar_url(author_id)

        author_group = ""
        pls_div = post_div.find_first(class_name="pls")
        if pls_div:
            grp_link = pls_div.find_first(
                tag="a", predicate=lambda n: "usergroup" in n.get_attr("href")
            )
            if grp_link:
                author_group = grp_link.inner_text().strip()

        created_at = ""
        poston_em = post_div.find_first(tag="em", id_prefix="authorposton")
        if poston_em:
            span_title = poston_em.find_first(
                tag="span", predicate=lambda n: bool(n.get_attr("title"))
            )
            if span_title:
                created_at = span_title.get_attr("title").strip()
            else:
                created_at = poston_em.inner_text().replace("发表于", "").strip()

        # 正文 HTML + 未内嵌的底部图片附件列表 (.pattl)
        msg_td = post_div.find_first(tag="td", id_prefix="postmessage_")
        raw_html = "".join(c.to_html() for c in msg_td.children) if msg_td else ""

        referenced_aids: set[str] = set()
        if msg_td:
            for img in msg_td.find_all(tag="img"):
                aid = img.get_attr("aid") or img.get_attr("id").removeprefix("aimg_")
                if aid:
                    referenced_aids.add(aid)

        pattl = post_div.find_first(class_name="pattl")
        extra_attach_imgs: list[str] = []
        if pattl:
            for img in pattl.find_all(tag="img"):
                aid = img.get_attr("aid") or img.get_attr("id").removeprefix("aimg_")
                if aid and aid in referenced_aids:
                    continue
                extra_attach_imgs.append(img.to_html())
        if extra_attach_imgs:
            raw_html = (
                raw_html + '<div class="forum-attachments">' + "".join(extra_attach_imgs) + "</div>"
            )

        # 鹅评提取
        rate_summary = ""
        rates: list[RateItem] = []
        ratelog = post_div.find_first(tag="dl", id_prefix="ratelog_")
        if ratelog:
            ratl_table = ratelog.find_first(tag="table", class_name="ratl")
            if ratl_table:
                ths = ratl_table.find_all(tag="th")
                if len(ths) >= 2:
                    p_cnt = ths[0].inner_text().strip()
                    s_sum = ths[1].inner_text().strip()
                    rate_summary = f"{p_cnt} · {s_sum}".strip(" ·")
                tbody_l = ratl_table.find_first(tag="tbody", class_name="ratl_l")
                if tbody_l:
                    for tr in tbody_l.find_all(tag="tr")[:8]:
                        tds = [c for c in tr.children if not c.is_text and c.tag == "td"]
                        if len(tds) >= 2:
                            u_links = tds[0].find_all(tag="a")
                            u_name = (
                                u_links[-1].inner_text().strip()
                                if u_links
                                else tds[0].inner_text().strip()
                            )
                            sc_txt = tds[1].inner_text().strip()
                            rsn_txt = tds[2].inner_text().strip() if len(tds) >= 3 else ""
                            rates.append(RateItem(user=u_name, score=sc_txt, reason=rsn_txt))

        return {
            "author": author,
            "author_id": author_id,
            "author_avatar": avatar_url,
            "author_group": author_group,
            "created_at": created_at,
            "raw_html": raw_html,
            "rate_summary": rate_summary,
            "rates": rates,
        }

    async def parse_thread(
        self,
        link: ForumLink,
        *,
        embed_images: bool = True,
        max_inline_images: int = 15,
        enable_length_limit: bool = False,
        max_content_chars: int = 15000,
    ) -> ParsedThread:
        tid = link.tid
        canonical_url = f"{S1_WEB_BASE}thread-{tid}-1-1.html"

        web_cookies = await self._get_web_cookies()
        if not web_cookies and self._has_credentials():
            web_cookies = await self._login_web_session()

        html_text, web_err = await self._fetch_web_page(tid, web_cookies)
        doc = parse_html(html_text) if html_text else None
        web_meta = self._extract_web_metadata(doc) if doc else {}
        first_post_div, block_reason = (
            self._extract_web_first_post(doc) if doc else (None, web_err or "网页请求失败")
        )

        # 若因未登录受限且配置了账号密码但此前使用的是缓存旧 Cookie，尝试刷新一次网页登录
        if (
            doc is not None
            and block_reason
            and any(h in block_reason for h in WEB_LOGIN_OR_PERM_HINTS)
            and self._has_credentials()
            and not str(self.cfg.get("cookie") or "").strip()
        ):
            fresh_cookies = await self._login_web_session()
            if fresh_cookies:
                web_cookies = fresh_cookies
                html_text, web_err = await self._fetch_web_page(tid, web_cookies)
                if html_text:
                    doc = parse_html(html_text)
                    web_meta = self._extract_web_metadata(doc)
                    first_post_div, block_reason = self._extract_web_first_post(doc)

        is_web_network_failure = html_text is None
        need_app_unblock = first_post_div is None or bool(block_reason)
        is_unblocked_invisible = False
        invisible_reason = ""

        title = web_meta.get("title") or ""
        category = web_meta.get("category") or ""
        forum_name = web_meta.get("forum_name") or ""
        views = web_meta.get("views")
        replies = web_meta.get("replies")
        author = ""
        author_id = ""
        author_avatar = ""
        author_group = ""
        created_at = ""
        raw_post_html = ""
        rate_summary = ""
        rates: list[RateItem] = []

        if first_post_div is not None and not block_reason:
            details = self._extract_post_details_from_div(first_post_div)
            author = details["author"]
            author_id = details["author_id"]
            author_avatar = details["author_avatar"]
            author_group = details["author_group"]
            created_at = details["created_at"]
            raw_post_html = details["raw_html"]
            rate_summary = details["rate_summary"]
            rates = details["rates"]
        elif first_post_div is not None and block_reason:
            # 即使主楼正文被屏蔽（如作者被禁言），仍可先提取网页上的楼层外围信息作为补充
            details = self._extract_post_details_from_div(first_post_div)
            author = details["author"]
            author_id = details["author_id"]
            author_avatar = details["author_avatar"]
            author_group = details["author_group"]
            created_at = details["created_at"]
            rate_summary = details["rate_summary"]
            rates = details["rates"]

        # 当主楼不可见（审核中 / 禁言屏蔽 / 权限限制）或网页端请求超时时，调用 S1 官方 App API 解锁/兜底
        if need_app_unblock:
            if not self._enable_invisible_parse and not is_web_network_failure:
                raise ForumParseError(
                    f"Stage1st 帖子主楼当前不可见（{block_reason or '受限'}），且未开启不可见内容解析。"
                )

            app_thread, app_post, app_err = await self._fetch_via_app_api(tid)
            if app_post and app_post.get("message"):
                raw_post_html = str(app_post.get("message") or "")
                # 若仅因网页网络超时走 App 兜底成功，不误标“不可见内容”徽章；仅在网页确有屏蔽/审核提示时标注
                if not is_web_network_failure:
                    is_unblocked_invisible = True
                    if "审核" in (block_reason or ""):
                        invisible_reason = "内容审核中"
                    elif "禁止或删除" in (block_reason or "") or "屏蔽" in (block_reason or ""):
                        invisible_reason = "作者被禁言/内容屏蔽"
                    else:
                        invisible_reason = block_reason or "不可见/受限内容"

                if app_thread:
                    title = title or str(app_thread.get("subject") or "")
                    forum_name = forum_name or str(app_thread.get("fname") or "")
                    category = category or str(app_thread.get("type") or "")
                    if views is None and app_thread.get("views") is not None:
                        try:
                            views = int(app_thread["views"])
                        except (TypeError, ValueError):
                            pass
                    if replies is None and app_thread.get("replies") is not None:
                        try:
                            replies = int(app_thread["replies"])
                        except (TypeError, ValueError):
                            pass

                author = str(app_post.get("author") or author or "未知作者")
                author_id = str(app_post.get("authorid") or author_id or "")
                author_group = str(app_post.get("grouptitle") or author_group or "")
                created_at = format_timestamp_cn(app_post.get("dateline")) or created_at
                app_av = str(app_post.get("avatarurl") or "").strip()
                if app_av.startswith("http"):
                    author_avatar = app_av
                elif not author_avatar and author_id:
                    author_avatar = build_s1_avatar_url(author_id)
            else:
                if not self._has_credentials() and not str(self.cfg.get("app_sid") or "").strip():
                    raise ForumLoginRequiredError(
                        f"Stage1st 帖子不可见（{block_reason or '审核中/需要权限'}），请在插件设置中配置 Stage1st 账号密码以通过 App 接口解锁。"
                    )
                reason_parts: list[str] = []
                if block_reason:
                    reason_parts.append(block_reason)
                if app_err and app_err not in (block_reason or ""):
                    if app_err.startswith("App 接口"):
                        reason_parts.append(app_err)
                    else:
                        reason_parts.append(f"App 接口返回：{app_err}")
                combined_reason = "；".join(reason_parts) or "帖子不存在或已彻底删除"
                raise ForumParseError(f"无法解析 Stage1st 帖子 (tid={tid})：{combined_reason}")

        if not title:
            title = f"Stage1st 主题 #{tid}"

        post_image_urls: list[str] = []
        clean_html, is_truncated, char_count, omitted_imgs = await process_post_html(
            raw_post_html,
            base_url=S1_WEB_BASE,
            referer=canonical_url,
            session=self.session,
            cookies=web_cookies,
            same_site_hosts=S1_SAME_SITE_HOSTS,
            embed_images=embed_images,
            max_inline_images=max_inline_images,
            enable_length_limit=enable_length_limit,
            max_content_chars=max_content_chars,
            out_post_image_urls=post_image_urls,
        )

        # 预下载楼主头像为 Data URI 防止远端渲染器无法加载防盗链头像
        author_avatar = rewrite_legacy_media_url(author_avatar)
        if embed_images and author_avatar.startswith("http"):
            av_data = await _fetch_image_as_data_uri(
                self.session,
                author_avatar,
                referer=S1_WEB_BASE,
                max_bytes=512 * 1024,
            )
            author_avatar = av_data or ""
        else:
            author_avatar = ""

        return ParsedThread(
            forum=ForumType.STAGE1ST,
            tid=tid,
            url=canonical_url,
            title=title,
            author=author or "未知作者",
            author_id=author_id,
            author_avatar=author_avatar,
            author_group=author_group,
            forum_name=forum_name or "Stage1st",
            category=category,
            created_at=created_at,
            views=views,
            replies=replies,
            content_html=clean_html,
            is_unblocked_invisible=is_unblocked_invisible,
            invisible_reason=invisible_reason,
            is_truncated=is_truncated,
            truncated_at_chars=char_count if is_truncated else 0,
            omitted_images_count=omitted_imgs,
            rate_summary=rate_summary,
            rates=rates,
            post_image_urls=post_image_urls,
        )

    async def get_media_auth_context(self) -> tuple[dict[str, str] | None, set[str]]:
        """返回用于拉取帖子附件图片的登录 Cookie 与同源域名白名单。"""
        cookies = await self._get_web_cookies()
        return (cookies or None), S1_SAME_SITE_HOSTS
