from __future__ import annotations

import aiohttp
import pytest
from conftest import MockHttpRouter

from core.models import ForumLink, ForumType
from core.parsers.stage1st import Stage1stParser

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

SAMPLE_S1_NORMAL_HTML = """
<!DOCTYPE html>
<html>
<body>
  <div id="pt">
    <div class="z">
      <a href="forum.php">论坛</a> <em>&rsaquo;</em>
      <a href="forum-75-1.html">卓明谷</a> <em>&rsaquo;</em>
      <a href="thread-2200001-1-1.html">测试主题</a>
    </div>
  </div>
  <div id="postlist">
    <table>
      <tr>
        <td class="pls">
          <div class="hm ptn">
            <span class="xg1">查看:</span> <span class="xi1">12580</span>
            <span class="pipe">|</span>
            <span class="xg1">回复:</span> <span class="xi1">89</span>
          </div>
        </td>
        <td class="plc">
          <h1 class="ts">
            <a href="forum.php?mod=forumdisplay&fid=75&filter=typeid&typeid=312">[欢乐]</a>
            <span id="thread_subject">Stage1st 正常主楼解析与反白附件测试</span>
          </h1>
        </td>
      </tr>
    </table>
    <div id="post_66000001">
      <table>
        <tr>
          <td class="pls">
            <div class="avatar"><img src="https://avatar.saraba1st.com/test_avatar.jpg" /></div>
            <p><em><a href="home.php?mod=spacecp&ac=usergroup&gid=30">圣者</a></em></p>
          </td>
          <td class="plc">
            <div class="pi">
              <strong><a id="postnum66000001">楼主</a></strong>
              <div class="authi">
                <a href="space-uid-458227.html" class="xw1">测试坛友</a>
                <em id="authorposton66000001">发表于 <span title="2026-11-17 18:30:00">半小时前</span></em>
              </div>
            </div>
            <div class="pcb">
              <div class="t_fsz">
                <table><tr><td class="t_f" id="postmessage_66000001">
                  <i class="pstatus"> 本帖最后由 测试坛友 于 2026-11-17 18:35 编辑 </i><br />
                  这是主楼第一段可见正文。<font class="jammer">这段防复制干扰码必须被清除12345</font>
                  <span style="display:none">隐藏干扰码abcdef</span><br />
                  剧透反白测试：<font color="LemonChiffon">凶手其实是管家！</font><br />
                  <img id="aimg_999" aid="999" src="static/image/common/none.gif" zoomfile="https://img.saraba1st.com/forum/202611/17/pic1.png" />
                </td></tr></table>
              </div>
              <dl id="ratelog_66000001">
                <dd>
                  <table class="ratl">
                    <tbody>
                      <tr>
                        <th><span>3</span> 人参与</th>
                        <th>战斗力 <i><span>+5</span></i></th>
                      </tr>
                    </tbody>
                    <tbody class="ratl_l">
                      <tr>
                        <td><a href="space-uid-100.html">路人甲</a></td>
                        <td>+2</td>
                        <td>欢乐多</td>
                      </tr>
                    </tbody>
                  </table>
                </dd>
              </dl>
            </div>
          </td>
        </tr>
      </table>
    </div>
  </div>
</body>
</html>
"""

SAMPLE_S1_MODERATED_HTML = """
<!DOCTYPE html>
<html>
<body>
  <div id="messagetext" class="alert_info">
    <p>内容审核中，即将开放</p>
  </div>
</body>
</html>
"""

SAMPLE_S1_BANNED_AUTHOR_HTML = """
<!DOCTYPE html>
<html>
<body>
  <div id="pt"><div class="z"><a href="forum-4-1.html">游戏论坛</a></div></div>
  <div id="postlist">
    <h1 class="ts"><span id="thread_subject">被禁言作者的主楼标题</span></h1>
    <div id="post_66000099">
      <div class="pi">
        <strong><a id="postnum66000099">楼主</a></strong>
        <div class="authi"><a href="space-uid-555555.html" class="xw1">小黑屋坛友</a></div>
      </div>
      <div class="plhin">
        <div class="pcb"><div class="locked">提示: <em>作者被禁止或删除 内容自动屏蔽</em></div></div>
      </div>
    </div>
  </div>
</body>
</html>
"""


@pytest.mark.asyncio
async def test_s1_normal_thread_parse() -> None:
    link = ForumLink(
        forum=ForumType.STAGE1ST,
        tid="2200001",
        original_url="https://stage1st.com/2b/thread-2200001-1-1.html",
        canonical_url="https://stage1st.com/2b/thread-2200001-1-1.html",
    )

    mocked = MockHttpRouter()
    mocked.get(
        "https://stage1st.com/2b/thread-2200001-1-1.html",
        status=200,
        body=SAMPLE_S1_NORMAL_HTML.encode("utf-8"),
        headers={"Content-Type": "text/html; charset=utf-8"},
    )
    # 即使旧帖 HTML 中残留了 saraba1st.com 图床与头像地址，也会自动重写到 stage1st.com 请求
    mocked.get(
        "https://img.stage1st.com/forum/202611/17/pic1.png",
        status=200,
        body=PNG_1X1,
        headers={"Content-Type": "image/png"},
    )
    mocked.get(
        "https://avatar.stage1st.com/test_avatar.jpg",
        status=200,
        body=PNG_1X1,
        headers={"Content-Type": "image/png"},
    )

    async with aiohttp.ClientSession(middlewares=[mocked.middleware]) as session:
        parser = Stage1stParser(session, config={"enabled": True})
        res = await parser.parse_thread(link)

    assert res.title == "Stage1st 正常主楼解析与反白附件测试"
    assert res.category == "欢乐"
    assert res.forum_name == "卓明谷"
    assert res.author == "测试坛友"
    assert res.author_id == "458227"
    assert res.author_group == "圣者"
    assert res.created_at == "2026-11-17 18:30:00"
    assert res.views == 12580
    assert res.replies == 89
    assert res.is_unblocked_invisible is False
    # 防复制干扰码已被完全清除
    assert "干扰码" not in res.content_html
    # 反白文字被转为可见的 forum-spoiler
    assert "forum-spoiler" in res.content_html
    assert "凶手其实是管家" in res.content_html
    # 懒加载 zoomfile 图片被预下载并内联为 data:image/png;base64
    assert "data:image/png;base64," in res.content_html
    # 评分表提取正常
    assert len(res.rates) == 1
    assert res.rates[0].user == "路人甲"
    assert res.rates[0].score == "+2"


@pytest.mark.asyncio
async def test_s1_moderated_invisible_thread_unblock_via_app_api() -> None:
    """测试“内容审核中，即将开放”不可见帖自动登录 S1 App 接口获取主楼。"""
    kv_store: dict[str, object] = {}

    async def kv_get(k: str, default: object = None) -> object:
        return kv_store.get(k, default)

    async def kv_set(k: str, val: object) -> None:
        kv_store[k] = val

    link = ForumLink(
        forum=ForumType.STAGE1ST,
        tid="2200002",
        original_url="https://stage1st.com/2b/thread-2200002-1-1.html",
        canonical_url="https://stage1st.com/2b/thread-2200002-1-1.html",
    )

    mocked = MockHttpRouter()
    # 移动端网页登录
    mocked.post(
        "https://stage1st.com/2b/api/mobile/index.php?version=4&module=login&loginsubmit=yes&loginfield=auto&cookietime=2592000",
        status=200,
        body='{"Message":{"messageval":"login_succeed"}}',
        headers={"Set-Cookie": "B7Y9_2132_auth=fakeauth; Path=/"},
    )
    # 网页返回“内容审核中，即将开放”
    mocked.get(
        "https://stage1st.com/2b/thread-2200002-1-1.html",
        status=200,
        body=SAMPLE_S1_MODERATED_HTML.encode("utf-8"),
    )
    # S1 官方 App 接口登录
    mocked.post(
        "https://app.stage1st.com/2b/api/app/user/login",
        status=200,
        payload={
            "code": 200,
            "success": True,
            "data": {"uid": "12345", "sid": "fresh_sid_token_001", "username": "s1user"},
        },
    )
    # S1 App /thread 主题元信息
    mocked.post(
        "https://app.stage1st.com/2b/api/app/thread",
        status=200,
        payload={
            "code": 200,
            "data": {
                "tid": 2200002,
                "fid": 75,
                "fname": "外野",
                "type": "数码",
                "subject": "审核中的新帖标题",
                "views": 42,
                "replies": 3,
                "pid": 66000050,
            },
        },
    )
    # S1 App /thread/page 第一页楼层列表
    mocked.post(
        "https://app.stage1st.com/2b/api/app/thread/page",
        status=200,
        payload={
            "code": 200,
            "data": {
                "pageNo": 1,
                "pageSize": 40,
                "totalCount": 4,
                "list": [
                    {
                        "pid": 66000050,
                        "position": 1,
                        "author": "发帖楼主",
                        "authorid": 88888,
                        "grouptitle": "大贤者",
                        "dateline": 1763376000,
                        "message": "<p>这是通过官方 App 接口成功拿到的审核中主楼完整正文！</p>",
                    }
                ],
            },
        },
    )

    async with aiohttp.ClientSession(middlewares=[mocked.middleware]) as session:
        parser = Stage1stParser(
            session,
            config={
                "enabled": True,
                "username": "s1user",
                "password": "s1password",
                "question_id": 1,
                "answer": "mom",
                "enable_invisible_parse": True,
            },
            kv_getter=kv_get,
            kv_setter=kv_set,
        )
        res = await parser.parse_thread(link, embed_images=False)

    assert res.is_unblocked_invisible is True
    assert "审核" in res.invisible_reason
    assert res.title == "审核中的新帖标题"
    assert res.forum_name == "外野"
    assert res.category == "数码"
    assert res.author == "发帖楼主"
    assert "通过官方 App 接口成功拿到" in res.content_html
    assert kv_store.get("s1_app_sid") == "fresh_sid_token_001"


@pytest.mark.asyncio
async def test_s1_banned_author_and_expired_sid_auto_relogin() -> None:
    """测试“作者被禁止或删除 内容自动屏蔽”以及旧 sid 501 失效自动重登重试。"""
    kv_store: dict[str, object] = {
        "s1_app_sid": "expired_old_sid",
        "s1_app_username": "s1user",
        "s1_web_cookies": {"B7Y9_2132_auth": "ok"},
        "s1_web_username": "s1user",
    }

    async def kv_get(k: str, default: object = None) -> object:
        return kv_store.get(k, default)

    async def kv_set(k: str, val: object) -> None:
        kv_store[k] = val

    link = ForumLink(
        forum=ForumType.STAGE1ST,
        tid="2200099",
        original_url="https://stage1st.com/2b/thread-2200099-1-1.html",
        canonical_url="https://stage1st.com/2b/thread-2200099-1-1.html",
    )

    mocked = MockHttpRouter()
    mocked.get(
        "https://stage1st.com/2b/thread-2200099-1-1.html",
        status=200,
        body=SAMPLE_S1_BANNED_AUTHOR_HTML.encode("utf-8"),
    )
    # 第一次使用旧 sid 返回 501 登录失效
    mocked.post(
        "https://app.stage1st.com/2b/api/app/thread",
        status=200,
        payload={"code": 501, "message": "请重新登录"},
    )
    mocked.post(
        "https://app.stage1st.com/2b/api/app/thread/page",
        status=200,
        payload={"code": 501, "message": "请重新登录"},
    )
    # 自动触发重登
    mocked.post(
        "https://app.stage1st.com/2b/api/app/user/login",
        status=200,
        payload={"code": 200, "data": {"sid": "renewed_sid_999"}},
    )
    # 重试成功
    mocked.post(
        "https://app.stage1st.com/2b/api/app/thread",
        status=200,
        payload={"code": 200, "data": {"tid": 2200099, "subject": "被禁言作者的主楼标题"}},
    )
    mocked.post(
        "https://app.stage1st.com/2b/api/app/thread/page",
        status=200,
        payload={
            "code": 200,
            "data": {
                "list": [
                    {
                        "pid": 66000099,
                        "position": 1,
                        "author": "小黑屋坛友",
                        "authorid": 555555,
                        "dateline": 1763376100,
                        "message": "被塞抹布前的真实主楼发言内容！",
                    }
                ]
            },
        },
    )

    async with aiohttp.ClientSession(middlewares=[mocked.middleware]) as session:
        parser = Stage1stParser(
            session,
            config={
                "enabled": True,
                "username": "s1user",
                "password": "s1password",
                "enable_invisible_parse": True,
            },
            kv_getter=kv_get,
            kv_setter=kv_set,
        )
        res = await parser.parse_thread(link, embed_images=False)

    assert res.is_unblocked_invisible is True
    assert "禁言" in res.invisible_reason or "屏蔽" in res.invisible_reason
    assert "被塞抹布前的真实主楼发言内容" in res.content_html
    assert kv_store["s1_app_sid"] == "renewed_sid_999"


@pytest.mark.asyncio
async def test_s1_cross_border_timeout_retry_and_app_permission_error() -> None:
    """验证跨境连接 TimeoutError 自动换新连接重试、网页全挂时并发走 App 兜底不误标徽章，以及权限不足清晰报错。"""

    from core.models import ForumParseError

    kv_store: dict[str, object] = {
        "s1_app_sid": "valid_sid",
        "s1_app_username": "s1user",
        "s1_web_cookies": {"B7Y9_2132_auth": "ok"},
        "s1_web_username": "s1user",
    }

    async def kv_get(k: str, default: object = None) -> object:
        return kv_store.get(k, default)

    async def kv_set(k: str, val: object) -> None:
        kv_store[k] = val

    # 场景 1：网页端连续两次超时，App 接口第 1 次抖动超时、第 2 次换新连接 (Connection: close) 重试成功
    link1 = ForumLink(
        forum=ForumType.STAGE1ST,
        tid="2136103",
        original_url="https://stage1st.com/2b/thread-2136103-1-1.html",
        canonical_url="https://stage1st.com/2b/thread-2136103-1-1.html",
    )
    mocked1 = MockHttpRouter()
    mocked1.get("https://stage1st.com/2b/thread-2136103-1-1.html", exc=TimeoutError())
    mocked1.get("https://stage1st.com/2b/thread-2136103-1-1.html", exc=TimeoutError())
    mocked1.post("https://app.stage1st.com/2b/api/app/thread", exc=TimeoutError())
    mocked1.post(
        "https://app.stage1st.com/2b/api/app/thread",
        status=200,
        payload={
            "code": 200,
            "data": {"tid": 2136103, "subject": "跨境容灾测试帖", "fname": "漫区"},
        },
    )
    mocked1.post(
        "https://app.stage1st.com/2b/api/app/thread/page",
        status=200,
        payload={
            "code": 200,
            "data": {
                "list": [
                    {
                        "pid": 1,
                        "position": 1,
                        "author": "漫区楼主",
                        "authorid": 1001,
                        "dateline": 1763376200,
                        "message": "<p>跨境重试后成功拿回的主楼内容</p>",
                    }
                ]
            },
        },
    )

    async with aiohttp.ClientSession(middlewares=[mocked1.middleware]) as session:
        parser = Stage1stParser(
            session,
            config={"enabled": True, "username": "s1user", "password": "s1password"},
            kv_getter=kv_get,
            kv_setter=kv_set,
        )
        res1 = await parser.parse_thread(link1, embed_images=False)

    assert res1.title == "跨境容灾测试帖"
    assert "跨境重试后成功拿回" in res1.content_html
    # 纯网页超时走 App 容灾恢复的正常帖，不应误标为“不可见内容”
    assert res1.is_unblocked_invisible is False
    # 验证第 2 次重试请求自动带上了 Connection: close 强制换新连接
    web_reqs = [r for r in mocked1.requests if "thread-2136103" in r[1]]
    assert len(web_reqs) == 2
    assert web_reqs[1][2].get("Connection") == "close"

    # 场景 2：不可见帖且 App 接口返回明确业务权限限制（如阅读权限不足），报错应清晰包含 App 返回原因
    link2 = ForumLink(
        forum=ForumType.STAGE1ST,
        tid="2200999",
        original_url="https://stage1st.com/2b/thread-2200999-1-1.html",
        canonical_url="https://stage1st.com/2b/thread-2200999-1-1.html",
    )
    mocked2 = MockHttpRouter()
    mocked2.get(
        "https://stage1st.com/2b/thread-2200999-1-1.html",
        status=200,
        body=SAMPLE_S1_MODERATED_HTML.encode("utf-8"),
    )
    mocked2.post(
        "https://app.stage1st.com/2b/api/app/thread",
        status=200,
        payload={"code": 403, "message": "本帖要求阅读权限高于 100"},
    )
    mocked2.post(
        "https://app.stage1st.com/2b/api/app/thread/page",
        status=200,
        payload={"code": 403, "message": "本帖要求阅读权限高于 100"},
    )

    async with aiohttp.ClientSession(middlewares=[mocked2.middleware]) as session:
        parser2 = Stage1stParser(
            session,
            config={"enabled": True, "username": "s1user", "password": "s1password"},
            kv_getter=kv_get,
            kv_setter=kv_set,
        )
        with pytest.raises(ForumParseError) as exc_info:
            await parser2.parse_thread(link2, embed_images=False)
        assert "本帖要求阅读权限高于 100" in str(exc_info.value)


@pytest.mark.asyncio
async def test_s1_specific_floor_findpost_parse_and_invisible_floor_unblock() -> None:
    """验证带 pid 的指定楼层链接仅解析对应楼层（不解析主楼），且支持解锁被禁言的指定楼层并正确渲染楼层标签。"""
    from core.renderer import build_template_payload
    from core.url_extractor import parse_forum_url

    floor_page_html = """
    <html><body>
      <div id="pt"><div class="z"><a href="forum-6-1.html">动漫论坛</a></div></div>
      <div id="postlist">
        <h1 class="ts"><span id="thread_subject">野生的最终BOSS出现了！ TV动画化</span></h1>
        <div id="post_70514001">
          <div class="pi"><strong><a id="postnum70514001"><em>201</em><sup>#</sup></a></strong>
            <div class="authi"><a href="space-uid-111.html" class="xw1">路人层主</a></div>
          </div>
          <td id="postmessage_70514001">这是同一页上的其他楼层，绝不能误解析这一层！</td>
        </div>
        <div id="post_70514830">
          <td class="pls"><p><em><a href="home.php?mod=spacecp&ac=usergroup&gid=54">婆罗门</a></em></p></td>
          <div class="pi">
            <strong><a id="postnum70514830"><em>239</em><sup>#</sup></a></strong>
            <div class="pti"><div class="authi">
              <a href="space-uid-36160.html" class="xw1">eilot</a>
              <em id="authorposton70514830">发表于 2026-11-16 00:32</em>
            </div></div>
          </div>
          <td id="postmessage_70514830">
            <div class="quote"><blockquote>引用前一层讨论</blockquote></div>
            这是 239 楼指定楼层的正文，<font color="LemonChiffon">反白剧透内容</font>
          </td>
        </div>
      </div>
    </body></html>
    """

    link = parse_forum_url(
        "https://stage1st.com/2b/forum.php?mod=redirect&goto=findpost&ptid=2196337&pid=70514830"
    )
    assert link is not None

    mocked = MockHttpRouter()
    mocked.get(
        "https://stage1st.com/2b/forum.php?mod=redirect&goto=findpost&ptid=2196337&pid=70514830",
        status=200,
        body=floor_page_html.encode("utf-8"),
    )

    async with aiohttp.ClientSession(middlewares=[mocked.middleware]) as session:
        parser = Stage1stParser(session, config={"enabled": True})
        res = await parser.parse_thread(link, embed_images=False)

    assert res.tid == "2196337"
    assert res.pid == "70514830"
    assert res.is_main_post is False
    assert res.floor_label == "239楼"
    assert res.display_floor_badge == "239楼"
    assert res.author == "eilot"
    assert res.author_id == "36160"
    assert res.author_group == "婆罗门"
    assert "239 楼指定楼层的正文" in res.content_html
    assert "绝不能误解析这一层" not in res.content_html
    assert "forum-spoiler" in res.content_html

    tmpl_data, _ = build_template_payload(res)
    assert tmpl_data["floor_badge"] == "239楼"
    assert tmpl_data["is_main_post"] is False
    assert tmpl_data["pid"] == "70514830"

    # 验证当指定楼层被禁言屏蔽时，App 接口精准解锁该 pid 楼层（不误返回 1L 主楼）
    blocked_floor_html = """
    <html><body>
      <div id="postlist">
        <h1 class="ts"><span id="thread_subject">测试禁言楼层解锁</span></h1>
        <div id="post_70514830">
          <div class="pi"><strong><a id="postnum70514830"><em>88</em><sup>#</sup></a></strong>
            <div class="authi"><a href="space-uid-36160.html" class="xw1">被塞抹布层主</a></div>
          </div>
          <div class="plhin"><div class="pcb"><div class="locked">提示: <em>作者被禁止或删除 内容自动屏蔽</em></div></div></div>
        </div>
      </div>
    </body></html>
    """
    link_banned_floor = parse_forum_url(
        "https://stage1st.com/2b/thread-2196337-3-1.html#pid70514830"
    )
    assert link_banned_floor is not None

    mocked_b = MockHttpRouter()
    mocked_b.get(
        "https://stage1st.com/2b/thread-2196337-3-1.html",
        status=200,
        body=blocked_floor_html.encode("utf-8"),
    )
    mocked_b.post(
        "https://app.stage1st.com/2b/api/app/thread",
        status=200,
        payload={
            "code": 200,
            "data": {"tid": 2196337, "authorid": 999, "subject": "测试禁言楼层解锁"},
        },
    )
    mocked_b.post(
        "https://app.stage1st.com/2b/api/app/thread/page",
        status=200,
        payload={
            "code": 200,
            "data": {
                "pageNo": 3,
                "pageSize": 40,
                "totalCount": 90,
                "list": [
                    {
                        "pid": 70514800,
                        "position": 81,
                        "author": "其他人",
                        "message": "81楼无关内容",
                    },
                    {
                        "pid": 70514830,
                        "position": 88,
                        "author": "被塞抹布层主",
                        "authorid": 36160,
                        "dateline": 1763376300,
                        "message": "成功解锁出的第 88 楼被禁言回复原文！",
                    },
                ],
            },
        },
    )

    async with aiohttp.ClientSession(middlewares=[mocked_b.middleware]) as session:
        parser_b = Stage1stParser(
            session,
            config={
                "enabled": True,
                "app_sid": "preset_sid",
                "enable_invisible_parse": True,
            },
        )
        res_b = await parser_b.parse_thread(link_banned_floor, embed_images=False)

    assert res_b.is_main_post is False
    assert res_b.floor_label == "88楼"
    assert res_b.is_unblocked_invisible is True
    assert "第 88 楼被禁言回复原文" in res_b.content_html
    assert "81楼无关内容" not in res_b.content_html


@pytest.mark.asyncio
async def test_s1_invite_auth_deleted_rejected_and_two_step_login_heals_403() -> None:
    """回归测试：
    1. 历史缓存中误存的 `B7Y9_2f85_invite_auth=deleted` 会被识别为无效态并清除；
    2. 登录流程先 GET 预取 saltkey 与 formhash，再 POST 提交；仅带 `invite_auth=deleted` 的失败响应不会被误判登录成功；
    3. 页面出现 `attach_nopermission` 或站内附件图首次返回 403 时，自动刷新登录并用新 Cookie 成功拉取附件图。
    """
    kv_store: dict[str, object] = {
        "s1_web_cookies": {"B7Y9_2f85_invite_auth": "deleted", "B7Y9_2f85_saltkey": "stale"},
        "s1_web_username": "s1user",
    }

    async def kv_get(k: str, default: object = None) -> object:
        return kv_store.get(k, default)

    async def kv_set(k: str, val: object) -> None:
        kv_store[k] = val

    mocked = MockHttpRouter()
    # 第一轮校验：验证登录失败仅返回 invite_auth=deleted 时绝不误判为登录成功
    mocked.get(
        "https://stage1st.com/2b/api/mobile/index.php?version=4&module=login",
        status=200,
        payload={"Variables": {"formhash": "fh_bad"}},
        headers={"Set-Cookie": "B7Y9_2f85_saltkey=s0"},
    )
    mocked.post(
        "https://stage1st.com/2b/api/mobile/index.php?version=4&module=login&loginsubmit=yes&loginfield=auto&cookietime=2592000",
        status=200,
        body='{"Message":{"messageval":"login_invalid"}}',
        headers={"Set-Cookie": "B7Y9_2f85_invite_auth=deleted; Max-Age=0"},
    )
    async with aiohttp.ClientSession(middlewares=[mocked.middleware]) as sess:
        p_bad = Stage1stParser(
            sess,
            config={"enabled": True, "username": "s1user", "password": "wrong"},
            kv_getter=kv_get,
            kv_setter=kv_set,
        )
        # _get_web_cookies 应主动作废历史只有 invite_auth=deleted 的假阳性缓存
        assert await p_bad._get_web_cookies() == {}
        assert kv_store["s1_web_cookies"] == {}
        assert await p_bad._login_web_session() == {}

    # 第二轮校验：真实两步登录成功 -> attach_nopermission 页面重取 + img.stage1st.com 携带 Cookie 200 内联成功
    link = ForumLink(
        forum=ForumType.STAGE1ST,
        tid="2294225",
        original_url="https://stage1st.com/2b/thread-2294225-1-1.html",
        canonical_url="https://stage1st.com/2b/thread-2294225-1-1.html",
    )
    kv_good: dict[str, object] = {
        "s1_web_cookies": {"B7Y9_2f85_auth": "expired_token", "B7Y9_2f85_saltkey": "old_salt"},
        "s1_web_username": "s1user",
    }

    async def kv_get_g(k: str, default: object = None) -> object:
        return kv_good.get(k, default)

    async def kv_set_g(k: str, val: object) -> None:
        kv_good[k] = val

    mocked2 = MockHttpRouter()
    guest_html = """
    <html><body>
      <div id="postlist">
        <h1 class="ts"><span id="thread_subject">盒马帖子</span></h1>
        <div id="post_70521258">
          <div class="pi"><strong><a id="postnum70521258">楼主</a></strong>
            <div class="authi"><a href="space-uid-1.html" class="xw1">lymm2003</a></div>
          </div>
          <td id="postmessage_70521258">正文文本</td>
          <div class="attach_nopermission">需要登录</div>
        </div>
      </div>
    </body></html>
    """
    authed_html = """
    <html><body>
      <div id="postlist">
        <h1 class="ts"><span id="thread_subject">盒马帖子</span></h1>
        <div id="post_70521258">
          <div class="pi"><strong><a id="postnum70521258">楼主</a></strong>
            <div class="authi"><a href="space-uid-1.html" class="xw1">lymm2003</a></div>
          </div>
          <td id="postmessage_70521258">正文文本 <img id="aimg_1" file="https://img.stage1st.com/forum/202611/17/att1.png" /></td>
        </div>
      </div>
    </body></html>
    """
    mocked2.get("https://stage1st.com/2b/thread-2294225-1-1.html", status=200, body=guest_html)
    mocked2.get(
        "https://stage1st.com/2b/api/mobile/index.php?version=4&module=login",
        status=200,
        payload={"Variables": {"formhash": "fh_9988"}},
        headers={"Set-Cookie": "B7Y9_2f85_saltkey=fresh_salt"},
    )
    mocked2.post(
        "https://stage1st.com/2b/api/mobile/index.php?version=4&module=login&loginsubmit=yes&loginfield=auto&cookietime=2592000",
        status=200,
        body='{"Message":{"messageval":"login_succeed"}}',
        headers={"Set-Cookie": "B7Y9_2f85_auth=fresh%2Bauth%2Fval%3D%3D; Path=/"},
    )
    mocked2.get("https://stage1st.com/2b/thread-2294225-1-1.html", status=200, body=authed_html)
    mocked2.get(
        "https://img.stage1st.com/forum/202611/17/att1.png",
        status=200,
        body=PNG_1X1,
        headers={"Content-Type": "image/png"},
    )

    async with aiohttp.ClientSession(middlewares=[mocked2.middleware]) as sess2:
        p_good = Stage1stParser(
            sess2,
            config={"enabled": True, "username": "s1user", "password": "secret"},
            kv_getter=kv_get_g,
            kv_setter=kv_set_g,
        )
        res = await p_good.parse_thread(link)

    assert "data:image/png;base64," in res.content_html
    assert not res.has_uninlined_images
    # 验证图片请求显式携带未被 SimpleCookie 加引号的新 auth Cookie
    img_reqs = [r for r in mocked2.requests if "img.stage1st.com" in r[1]]
    assert len(img_reqs) == 1
    assert "B7Y9_2f85_auth=fresh%2Bauth%2Fval%3D%3D" in img_reqs[0][2].get("Cookie", "")
