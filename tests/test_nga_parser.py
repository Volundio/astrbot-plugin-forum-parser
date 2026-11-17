from __future__ import annotations

import aiohttp
import pytest
from conftest import MockHttpRouter

from core.models import ForumLink, ForumType
from core.parsers.nga import NgaParser, convert_nga_bbcode_to_html

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_nga_bbcode_conversion_and_xss_escape() -> None:
    raw_bb = (
        "[quote]引用前情提要[/quote]<br/>"
        "=== 一、技能机制简评 ===<br/>"
        "普通技能按键 [Q] 与 [E] 不应被误判为标签。<br/>"
        "[b][color=royalblue]核心结论[/color][/b]：[del]其实就是数值怪[/del][s:ac:哭笑]<br/>"
        "[collapse=点击查看详细伤害数据]折叠内隐藏文本123<br/>[img]./mon_202611/17/chart.png[/img][/collapse]"
        "[list][*]条目A[*]条目B[/list]"
        "[table][tr][td25]列1[/td][td colspan=2]列2[/td][/tr][/table]"
        "[@测试水友] <script>alert('xss')</script>"
    )
    html_out, used_imgs = convert_nga_bbcode_to_html(raw_bb)

    assert "forum-quote" in html_out
    assert '<h3 class="forum-heading">一、技能机制简评</h3>' in html_out
    assert "[Q]" in html_out and "[E]" in html_out
    assert "forum-del" in html_out
    assert "[AC娘·哭笑]" in html_out
    # 折叠块自动展开
    assert "折叠内容（已自动展开）：点击查看详细伤害数据" in html_out
    assert "折叠内隐藏文本123" in html_out
    # 相对附件图片路径被补全为 NGA 当前有效图床地址 img.nga.cn
    assert "https://img.nga.cn/attachments/mon_202611/17/chart.png" in html_out
    assert "https://img.nga.cn/attachments/mon_202611/17/chart.png" in used_imgs
    # 列表与表格
    assert '<ul class="forum-list"><li>条目A</li><li>条目B</li></ul>' in html_out
    assert '<td width="25%">列1</td><td colspan="2">列2</td>' in html_out
    # XSS 脚本被转义
    assert "<script>" not in html_out
    assert "&lt;script&gt;" in html_out


@pytest.mark.asyncio
async def test_nga_read_php_json_parse_with_raw_tab_and_orphan_attach() -> None:
    link = ForumLink(
        forum=ForumType.NGA,
        tid="42396123",
        original_url="https://bbs.nga.cn/read.php?tid=42396123",
        canonical_url="https://bbs.nga.cn/read.php?tid=42396123",
    )

    # 包含 NGA 经典的未转义 \t 制表符以及未写进 [img] 的尾部 attachs 附件
    raw_nga_json = (
        '{"data":{'
        '"__F":{"name":"晴风村"},'
        '"__T":{"tid":42396123,"subject":"[树洞] 泥潭主楼完整解析测试","authorid":66859487,"replies":48,"postdate":1763376000},'
        '"__U":{"66859487":{"username":"泥潭老哥","avatar":"https://img.nga.178.com/avatars/u1.png"}},'
        '"__R":{"0":{"lou":0,"authorid":66859487,"postdate":"2026-11-17 18:40","score":128,'
        '"alterinfo":"E1763376100\t0\t0",'
        '"content":"主楼第一段内容[s:a2:doge]",'
        '"attachs":{"0":{"attachurl":"mon_202611/17/extra_att.png","type":"img"}}'
        "}}}}"
    )

    mocked = MockHttpRouter()
    mocked.get(
        "https://bbs.nga.cn/read.php?tid=42396123&page=1&__output=11",
        status=200,
        body=raw_nga_json.encode("utf-8"),
    )
    mocked.get(
        "https://img.nga.cn/attachments/mon_202611/17/extra_att.png",
        status=200,
        body=PNG_1X1,
        headers={"Content-Type": "image/png"},
    )
    # 即使原 JSON 返回了旧版 img.nga.178.com 头像域名，也会被自动迁移请求至 img.nga.cn
    mocked.get(
        "https://img.nga.cn/avatars/u1.png",
        status=200,
        body=PNG_1X1,
        headers={"Content-Type": "image/png"},
    )

    async with aiohttp.ClientSession(middlewares=[mocked.middleware]) as session:
        parser = NgaParser(
            session,
            config={
                "enabled": True,
                "passport_uid": "66859487",
                "passport_cid": "secret_cid_token",
            },
        )
        res = await parser.parse_thread(link)

    assert res.forum == ForumType.NGA
    assert res.category == "树洞"
    assert res.title == "泥潭主楼完整解析测试"
    assert res.forum_name == "晴风村"
    assert res.author == "泥潭老哥"
    assert res.replies == 48
    assert res.score == 128
    assert "[AC娘·doge]" in res.content_html
    # 尾部未显式 [img] 的附件也被自动追加并内联为 Data URI
    assert "data:image/png;base64," in res.content_html
    assert res.author_avatar.startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_nga_fallback_to_app_api() -> None:
    link = ForumLink(
        forum=ForumType.NGA,
        tid="40009999",
        original_url="https://bbs.nga.cn/read.php?tid=40009999",
        canonical_url="https://bbs.nga.cn/read.php?tid=40009999",
    )

    mocked = MockHttpRouter()
    # read.php 返回非 JSON 访客拦截页
    mocked.get(
        "https://bbs.nga.cn/read.php?tid=40009999&page=1&__output=11",
        status=403,
        body=b"<html>guest check</html>",
    )
    # 自动回退到 app_api.php 成功获取主楼
    mocked.post(
        "https://bbs.nga.cn/app_api.php?__lib=post&__act=list",
        status=200,
        payload={
            "code": 0,
            "tsubject": "[攻略] 移动端接口兜底成功",
            "fname": "综合讨论",
            "result": [
                {
                    "pid": 1,
                    "lou": 0,
                    "postdatetimestamp": 1763376000,
                    "vote_good": 66,
                    "author": {"uid": 10086, "username": "#anony_abcdef123456"},
                    "content": "来自 app_api 兜底通道的主楼正文",
                }
            ],
        },
    )

    async with aiohttp.ClientSession(middlewares=[mocked.middleware]) as session:
        parser = NgaParser(session, config={"enabled": True})
        res = await parser.parse_thread(link, embed_images=False)

    assert res.category == "攻略"
    assert res.title == "移动端接口兜底成功"
    assert res.author == "匿名用户 (123456)"
    assert res.score == 66
    assert "app_api 兜底通道" in res.content_html


@pytest.mark.asyncio
async def test_nga_specific_pid_floor_parse() -> None:
    """验证 NGA 带 pid 的单回复链接解析对应楼层而非主楼。"""
    from core.url_extractor import parse_forum_url

    link = parse_forum_url("https://bbs.nga.cn/read.php?pid=795123456")
    assert link is not None

    nga_pid_json = (
        '{"data":{'
        '"__F":{"name":"猴区"},'
        '"__T":{"tid":42396123,"subject":"NGA单楼层解析测试","authorid":10001,"replies":30},'
        '"__U":{"20002":{"username":"回帖水友"}},'
        '"__R":{"0":{"pid":795123456,"lou":15,"authorid":20002,"postdate":1763376500,"score":9,"content":"这是第 15 楼的单独回复内容"}}'
        "}}"
    )
    mocked = MockHttpRouter()
    mocked.get(
        "https://bbs.nga.cn/read.php?pid=795123456&__output=11",
        status=200,
        body=nga_pid_json.encode("utf-8"),
    )

    async with aiohttp.ClientSession(middlewares=[mocked.middleware]) as session:
        parser = NgaParser(session, config={"enabled": True})
        res = await parser.parse_thread(link, embed_images=False)

    assert res.tid == "42396123"
    assert res.pid == "795123456"
    assert res.is_main_post is False
    assert res.floor_label == "15楼"
    assert res.author == "回帖水友"
    assert "第 15 楼的单独回复内容" in res.content_html
