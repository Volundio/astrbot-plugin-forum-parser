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
