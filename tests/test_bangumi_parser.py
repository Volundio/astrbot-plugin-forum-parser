from __future__ import annotations

import json
import struct
from contextlib import aclosing
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context
from conftest import MockHttpRouter

from core.models import ForumType
from core.parsers.bangumi import BangumiParser, convert_bgm_bbcode_to_html
from core.renderer import (
    ForumCardRenderer,
    build_template_payload,
    load_bangumi_card_template,
)
from core.temp_manager import TempFileManager
from core.url_extractor import extract_forum_links_from_event, parse_forum_url


def _make_png_bytes(width: int = 200, height: int = 200) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\x00\x00\x00\x00"


PNG_1X1 = _make_png_bytes(1, 1)


SAMPLE_GROUP_TOPIC_HTML = """
<!DOCTYPE html>
<html>
<head><title>[客户端] Chogumi：Android 原生客户端</title></head>
<body>
<div id="pageHeader">
  <h1><span><a href="/group/dev" class="avatar"><img src="//lain.bgm.tv/pic/icon/l/000/00/41/4180.jpg" class="avatar ll" width="32" />番组开发</a> &raquo; <a href="/group/dev/forum">讨论</a></span><br />[客户端] Chogumi：Android 原生客户端</h1>
</div>
<div id="post_4102908" class="postTopic light_odd clearit" data-item-user="550799">
  <div class="post_actions re_info">
    <div class="action"><small>#1 - 2026-11-8 10:51</small></div>
  </div>
  <a href="/user/550799" class="avatar"><span class="avatarNeue avatarSize48 ll" style="background-image:url('//lain.bgm.tv/pic/user/l/000/55/07/550799.jpg')"></span></a>
  <div class="inner">
    <strong><a href="/user/550799" class="l">没事了</a></strong>
    <span class="sign tip_j">(我是恋爱高手)</span>
    <div class="topic_content">
      <p>正文展示 <span style="background-color:#555;color:#555;border:1px solid #555;">剧透反白内容</span> <span style="text-decoration:line-through;">旧版本</span>。</p>
      <img src="https://raw.githubusercontent.com/TouhouGO/Chogumi/main/banner.png" class="code" />
      <img src="/img/smiles/tv/01.gif" smileid="40" class="smile" alt="(bgm24)" />
    </div>
  </div>
</div>
<div id="comment_list">
  <div id="post_4102966" class="light_odd row row_reply clearit" data-item-user="xunlan">
    <div class="post_actions re_info"><div class="action"><small><a href="#post_4102966" class="floor-anchor">#2</a> - 2026-11-8 11:49</small></div></div>
    <a href="/user/xunlan" class="avatar"><span class="avatarNeue" style="background-image:url('//lain.bgm.tv/pic/user/l/000/57/62/576295.jpg')"></span></a>
    <div class="inner">
      <span class="userInfo"><strong><a href="/user/xunlan" class="l post_author_4102966">洵烂</a></strong> <span class="sign tip_j">(签名测试)</span></span>
      <div class="reply_content">
        <div class="message">感觉还是不错的，建议专注模式补充说明。</div>
        <div class="topic_sub_reply" id="topic_reply_4102966">
          <div id="post_4102983" class="sub_reply_bg clearit" data-item-user="550799">
            <div class="post_actions re_info"><div class="action"><small><a href="#post_4102983" class="floor-anchor">#2-1</a> - 2026-11-8 11:58</small></div></div>
            <a href="/user/550799" class="avatar"><span class="avatarNeue" style="background-image:url('//lain.bgm.tv/pic/user/l/000/55/07/550799.jpg')"></span></a>
            <div class="inner">
              <strong class="userName"><a id="4102983" href="/user/550799" class="l">没事了</a></strong>
              <div class="cmt_sub_content">好的 下个版本加上说明<img src="/img/smiles/tv_vs/bgm_201.png" class="smile" smileid="201" alt="(bgm201)" /></div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>
<script>
var data_likes_list = {"4102908":{"80":{"type":8,"main_id":475273,"value":"80","total":7,"emoji":"41"},"122":{"type":8,"main_id":475273,"value":"122","total":2,"emoji":"83"}}};
</script>
</body>
</html>
"""


SAMPLE_BLOG_HTML = """
<!DOCTYPE html>
<html>
<head><title>漫谈汽车在影视中的作用</title></head>
<body>
<div id="columnA" class="column column-main">
  <div id="viewEntry" class="entry-container">
    <div class="author user-card">
      <a href="/user/garfield4odie" class="avatar l"><img src="//lain.bgm.tv/pic/user/l/000/57/29/572941.jpg" class="avatar" width="32" /></a>
      <div class="title"><p><a href="/user/garfield4odie" class="avatar l">卡林のZ</a></p></div>
    </div>
    <div class="header">
      <h1 class="title">漫谈汽车在影视中的作用</h1>
      <div class="tools"><div class="time">2024-4-13 10:15 · 43 分钟阅读</div></div>
    </div>
    <div id="entry_content" class="content">
      <p>　　距离卡尔·本茨申请专利已经过去近140年。</p>
      <img src="https://s2.loli.net/2024/04/18/demo.png" class="code" />
      <p>　　汽车本身就是演员。</p>
    </div>
  </div>
  <div id="comment_list">
    <div id="post_272332" class="light_odd row row_reply clearit" data-item-user="hugo2233">
      <div class="post_actions re_info"><div class="action"><small><a href="#post_272332">#1</a> - 2024-7-7 13:56</small></div></div>
      <div class="inner"><strong><a href="/user/hugo2233" class="l">hugo2233</a></strong>
        <div class="reply_content"><div class="message">好棒</div></div>
      </div>
    </div>
  </div>
</div>
<div id="columnB">
  <div class="entry-related-subjects">
    <div class="card subject-card">
      <a href="/subject/506"><span class="cover"><img src="//lain.bgm.tv/r/200/pic/cover/l/506.jpg" /></span></a>
      <div class="inner">
        <p class="title"><a href="/subject/506">猫の恩返し <small></small></a></p>
        <p class="info tip">猫的报恩</p>
      </div>
    </div>
  </div>
</div>
<script>
var data_likes_list = {"337198":{"40":{"total":12,"emoji":"41"}}};
</script>
</body>
</html>
"""


def test_parse_bangumi_urls_all_domains_and_exclusions() -> None:
    # 三大域名小组话题
    for host in ("bangumi.tv", "bgm.tv", "chii.in"):
        res = parse_forum_url(f"https://{host}/group/topic/475273")
        assert res is not None
        assert res.forum == ForumType.BANGUMI
        assert res.sub_type == "group_topic"
        assert res.tid == "475273"
        assert res.canonical_url == "https://bangumi.tv/group/topic/475273"

    # Rakuen 链接与楼层锚点
    rakuen = parse_forum_url("https://bgm.tv/rakuen/topic/group/475273#post_4102983")
    assert rakuen is not None
    assert rakuen.sub_type == "group_topic"
    assert rakuen.pid == "4102983"
    assert rakuen.canonical_url == "https://bangumi.tv/group/topic/475273#post_4102983"
    assert rakuen.target_id == "group_topic:475273:pid:4102983"

    # 条目讨论
    subj_topic = parse_forum_url("https://chii.in/subject/topic/32800")
    assert subj_topic is not None
    assert subj_topic.sub_type == "subject_topic"
    assert subj_topic.canonical_url == "https://bangumi.tv/subject/topic/32800"

    # 日志 / 长评
    blog = parse_forum_url("https://bangumi.tv/blog/337198")
    assert blog is not None
    assert blog.sub_type == "blog"
    assert blog.tid == "337198"
    assert blog.target_id == "blog:337198"
    assert blog.canonical_url == "https://bangumi.tv/blog/337198"

    # 普通条目详情页、章节页、用户主页不应触发解析
    assert parse_forum_url("https://bangumi.tv/subject/506") is None
    assert parse_forum_url("https://bgm.tv/ep/12345") is None
    assert parse_forum_url("https://chii.in/user/550799") is None


@pytest.mark.asyncio
async def test_bangumi_parser_group_topic_subreply_and_blog(tmp_path: Path) -> None:
    mocked = MockHttpRouter()
    mocked.get("https://bangumi.tv/group/topic/475273", status=200, body=SAMPLE_GROUP_TOPIC_HTML)
    mocked.get("https://bangumi.tv/blog/337198", status=200, body=SAMPLE_BLOG_HTML)
    mocked.get(
        "https://raw.githubusercontent.com/TouhouGO/Chogumi/main/banner.png",
        status=200,
        body=PNG_1X1,
        headers={"Content-Type": "image/png"},
    )
    mocked.get(
        "https://s2.loli.net/2024/04/18/demo.png",
        status=200,
        body=PNG_1X1,
        headers={"Content-Type": "image/png"},
    )
    # 其余头像/封面/表情统一给 PNG_1X1 便于测试内联
    for u in (
        "https://lain.bgm.tv/pic/user/l/000/55/07/550799.jpg",
        "https://lain.bgm.tv/pic/icon/l/000/00/41/4180.jpg",
        "https://lain.bgm.tv/pic/user/l/000/57/29/572941.jpg",
        "https://lain.bgm.tv/r/200/pic/cover/l/506.jpg",
        "https://bangumi.tv/img/smiles/tv/01.gif",
        "https://bangumi.tv/img/smiles/tv/41.gif",
        "https://bangumi.tv/img/smiles/tv/83.gif",
        "https://bangumi.tv/img/smiles/tv_vs/bgm_201.png",
    ):
        mocked.get(u, status=200, body=PNG_1X1, headers={"Content-Type": "image/png"})

    temp_mgr = TempFileManager(tmp_path / "bgm_temp")
    scope = temp_mgr.create_scope()

    async with aiohttp.ClientSession(middlewares=[mocked.middleware]) as session:
        parser = BangumiParser(session)

        # 1. 小组话题主楼
        link_op = parse_forum_url("https://bgm.tv/group/topic/475273")
        assert link_op is not None
        op_res = await parser.parse_thread(link_op, temp_scope=scope)
        assert op_res.forum == ForumType.BANGUMI
        assert op_res.sub_type == "group_topic"
        assert op_res.category == "客户端"
        assert op_res.title == "Chogumi：Android 原生客户端"
        assert op_res.forum_name == "番组开发"
        assert op_res.author == "没事了"
        assert op_res.author_id == "550799"
        assert op_res.author_sign == "我是恋爱高手"
        assert op_res.score == 9
        assert op_res.replies == 2
        assert len(op_res.reactions) == 2
        assert 'class="forum-spoiler"' in op_res.content_html
        assert op_res.group_icon.startswith("data:image/png;base64,")

        # 2. 楼中楼指定回复 (#post_4102983)
        link_sub = parse_forum_url("https://chii.in/group/topic/475273#post_4102983")
        assert link_sub is not None
        sub_res = await parser.parse_thread(link_sub, temp_scope=scope)
        assert sub_res.is_main_post is False
        assert sub_res.is_thread_author is True
        assert "楼主 · #2-1" in sub_res.floor_label
        assert "@洵烂 (#2)" in sub_res.content_html
        assert "专注模式补充说明" in sub_res.content_html

        # 3. 日志 / 长评 + 关联条目 + 专属 Bangumi 模板选型
        link_blog = parse_forum_url("https://bangumi.tv/blog/337198")
        assert link_blog is not None
        blog_res = await parser.parse_thread(link_blog, temp_scope=scope)
        assert blog_res.sub_type == "blog"
        assert blog_res.title == "漫谈汽车在影视中的作用"
        assert blog_res.reading_time == "43 分钟阅读"
        assert blog_res.created_at == "2024-4-13 10:15"
        assert blog_res.score == 12
        assert len(blog_res.related_subjects) == 1
        assert blog_res.related_subjects[0].title == "猫の恩返し"
        assert blog_res.related_subjects[0].subtitle == "猫的报恩"
        assert blog_res.related_subjects[0].cover.startswith("data:image/png;base64,")

        # 校验渲染负载与 Bangumi 专属模板选用
        tmpl = load_bangumi_card_template()
        assert 'body[data-theme="bgm"]' in tmpl
        assert "related_subjects" in tmpl
        payload, _ = build_template_payload(blog_res, {"theme": "auto"})
        assert payload["theme_key"] == "bgm"
        assert payload["sub_type_badge"] == "日志 · 长评"
        assert payload["reading_time"] == "43 分钟阅读"

        captured_templates: list[str] = []

        class FakeHtmlRenderer:
            async def render_custom_template(
                self, tmpl_str: str, tmpl_data: dict[str, Any], return_url: bool = False, **_: Any
            ) -> str:
                captured_templates.append(tmpl_str)
                out_file = tmp_path / "bgm_card.jpg"
                out_file.write_bytes(_make_png_bytes(780, 1200))
                return str(out_file)

        renderer = ForumCardRenderer(
            context=Context(html_renderer=FakeHtmlRenderer()),
            session=session,
            temp_dir=tmp_path,
            temp_manager=temp_mgr,
        )
        render_res = await renderer.render_thread_card(blog_res, temp_scope=scope)
        assert render_res.file_size > 0
        assert captured_templates and captured_templates[0] == tmpl

    await scope.cleanup()
    assert not (tmp_path / "bgm_temp").exists()


def test_bangumi_bbcode_conversion_and_next_api_fallback() -> None:
    raw = (
        "[b]加粗[/b] [mask]剧透[/mask] (bgm01) (bgm11) (bgm24) (bgm124) "
        "(bgm201) (musume_63) (blake_14) (bmoCAIBzgEqALA) [url=https://bgm.tv]BGM[/url]"
    )
    out = convert_bgm_bbcode_to_html(raw)
    assert "<strong>加粗</strong>" in out
    assert '<span class="forum-spoiler">剧透</span>' in out
    assert "https://bangumi.tv/img/smiles/bgm/01.png" in out
    assert "https://bangumi.tv/img/smiles/bgm/11.gif" in out
    assert "https://bangumi.tv/img/smiles/tv/01.gif" in out
    assert "https://bangumi.tv/img/smiles/tv/101.gif" in out
    assert "https://bangumi.tv/img/smiles/tv_vs/bgm_201.png" in out
    assert "https://lain.bgm.tv/img/smiles/musume/musume_63.gif" in out
    assert "https://lain.bgm.tv/img/smiles/blake/blake_14.gif" in out
    assert 'class="bmo" data-code="(bmoCAIBzgEqALA)"' in out


@pytest.mark.asyncio
async def test_bangumi_list_reactions_dynamic_smiley_and_bmoji_bundling() -> None:
    """验证 Bangumi 列表格式贴贴、>256KB 动态大表情、以及 Bmoji (<span class=bmo>) 离线内联脚本打包。"""
    sample_bmo_html = """
    <!DOCTYPE html><html><body>
      <div id="pageHeader"><h1><span><a class="avatar">番组测试组</a></span>[提问] BMO 与贴贴测试</h1></div>
      <div id="post_4112908" class="postTopic" data-item-user="bgsld">
        <div class="re_info"><small>#1 - 2026-11-18 01:00</small></div>
        <div class="inner"><a href="/user/bgsld" class="l">测试作者</a></div>
        <div class="topic_content">
          来个组合表情 <span class="bmo" data-code="(bmoCAIBzgEqALA)"></span>
          再来个大图表情 <img src="//lain.bgm.tv/img/smiles/blake/blake_14.gif" class="smile smile-dynamic" smileid="blake_14" alt="(blake_14)" />
        </div>
      </div>
      <div id="comment_list"></div>
      <script>
        var data_likes_list = {"4112908":[{"type":8,"main_id":475911,"value":"0","total":3,"emoji":"44","users":[]}]};
      </script>
    </body></html>
    """
    fake_gif = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
    # 超过 256KB 但低于 2MB 的动态大表情（如 blake_14.gif 为 347KB）
    large_dynamic_gif = fake_gif + b"\x00" * (360 * 1024)
    fake_manifest = {
        "face": {
            "id": "f",
            "layer": 1,
            "items": [{"id": "1", "src": "./assets/f1.png", "layer": 1}],
        },
        "mouth": {
            "id": "m",
            "layer": 2,
            "items": [{"id": "1", "src": "./assets/m1.png", "layer": 2}],
        },
    }
    fake_bmo_js = "var src = DEFAULT_ASSTET_PATH + item.src; window.Bmoji = { configure: function(){}, renderAll: function(){} };"

    mocked = MockHttpRouter()
    mocked.get("https://bangumi.tv/group/topic/475911", status=200, body=sample_bmo_html)
    mocked.get(
        "https://lain.bgm.tv/img/smiles/blake/blake_14.gif",
        status=200,
        body=large_dynamic_gif,
        headers={"Content-Type": "image/gif"},
    )
    mocked.get(
        "https://bangumi.tv/img/smiles/tv/44.gif",
        status=200,
        body=PNG_1X1,
        headers={"Content-Type": "image/gif"},
    )
    mocked.get(
        "https://bangumi.tv/js/lib/bmo/bmo.js",
        status=200,
        body=fake_bmo_js,
        headers={"Content-Type": "application/javascript"},
    )
    mocked.get(
        "https://bangumi.tv/js/lib/bmo/assets/manifest.local.json",
        status=200,
        payload=fake_manifest,
    )
    mocked.get(
        "https://bangumi.tv/js/lib/bmo/assets/f1.png",
        status=200,
        body=PNG_1X1,
        headers={"Content-Type": "image/png"},
    )
    mocked.get(
        "https://bangumi.tv/js/lib/bmo/assets/m1.png",
        status=200,
        body=PNG_1X1,
        headers={"Content-Type": "image/png"},
    )

    async with aiohttp.ClientSession(middlewares=[mocked.middleware]) as sess:
        parser = BangumiParser(sess)
        link = parse_forum_url("https://bangumi.tv/group/topic/475911")
        assert link is not None
        res = await parser.parse_thread(link, embed_images=True, prefetch_data_uri=True)

    # list 格式的 data_likes_list 成功提取
    assert res.score == 3
    assert len(res.reactions) == 1
    assert res.reactions[0].emoji_url.startswith("data:image/png;base64,")
    # >256KB 动态大表情成功保留为 Data URI + forum-smiley-dynamic，未降级为文字
    assert "forum-smiley-dynamic" in res.content_html
    assert "data:image/gif;base64," in res.content_html
    assert "[(blake_14)]" not in res.content_html
    # Bmoji span 与内联离线打包脚本均注入成功
    assert 'class="bmo" data-code="(bmoCAIBzgEqALA)"' in res.content_html
    assert "window.__BMOJI_ASSETS__" in res.bmo_script_html
    payload, _ = build_template_payload(res, {"theme": "auto"})
    assert payload["bmo_script_html"] == res.bmo_script_html


@pytest.mark.asyncio
async def test_bangumi_next_api_fallback_when_web_fails() -> None:
    mocked = MockHttpRouter()
    mocked.get("https://bangumi.tv/group/topic/475273", exc=TimeoutError())
    mocked.get("https://bangumi.tv/group/topic/475273", exc=TimeoutError())
    mocked.get(
        "https://next.bgm.tv/p1/groups/-/topics/475273",
        status=200,
        payload={
            "id": 475273,
            "title": "[开发] API 兜底话题",
            "creatorID": 550799,
            "replyCount": 5,
            "createdAt": 1794106260,
            "group": {"id": 4180, "title": "番组开发"},
            "replies": [
                {
                    "id": 4102908,
                    "creatorID": 550799,
                    "createdAt": 1794106260,
                    "content": "[b]兜底正文[/b] [mask]隐藏文本[/mask]",
                    "creator": {"username": "550799", "nickname": "没事了", "sign": "测试签名"},
                }
            ],
        },
    )
    async with aiohttp.ClientSession(middlewares=[mocked.middleware]) as session:
        parser = BangumiParser(session)
        link = parse_forum_url("https://bangumi.tv/group/topic/475273")
        assert link is not None
        res = await parser.parse_thread(link, embed_images=False)
        assert res.title == "API 兜底话题"
        assert res.category == "开发"
        assert res.author == "没事了"
        assert "<strong>兜底正文</strong>" in res.content_html
        assert 'class="forum-spoiler"' in res.content_html


@pytest.mark.asyncio
async def test_bangumi_platform_restriction_onebot_only_never_telegram(tmp_path: Path) -> None:
    """严格验证：Bangumi 链接仅在 QQ OneBot (`aiocqhttp`) 平台触发，在 Telegram 平台完全不触发解析。"""
    import main as plugin_pkg

    mocked = MockHttpRouter()
    mocked.get("https://bangumi.tv/group/topic/475273", status=200, body=SAMPLE_GROUP_TOPIC_HTML)
    mocked.get(
        "https://raw.githubusercontent.com/TouhouGO/Chogumi/main/banner.png",
        status=200,
        body=PNG_1X1,
        headers={"Content-Type": "image/png"},
    )
    for u in (
        "https://lain.bgm.tv/pic/user/l/000/55/07/550799.jpg",
        "https://lain.bgm.tv/pic/icon/l/000/00/41/4180.jpg",
        "https://bangumi.tv/img/smiles/tv/01.gif",
        "https://bangumi.tv/img/smiles/tv/41.gif",
        "https://bangumi.tv/img/smiles/tv/83.gif",
    ):
        mocked.get(u, status=200, body=PNG_1X1, headers={"Content-Type": "image/png"})

    class DummyRenderer:
        async def render_custom_template(
            self, tmpl_str: str, tmpl_data: dict[str, Any], return_url: bool = False, **_: Any
        ) -> str:
            p = tmp_path / "rendered_bgm.jpg"
            p.write_bytes(_make_png_bytes(780, 900))
            return str(p)

    ctx = Context(html_renderer=DummyRenderer())
    plugin = plugin_pkg.ForumParserPlugin(
        ctx,
        config={"parse_options": {"cooldown_seconds": 0}},
    )
    plugin._temp_mgr = TempFileManager(tmp_path / "bgm_e2e_temp")
    plugin._http_session = aiohttp.ClientSession(middlewares=[mocked.middleware])
    plugin._ensure_services()

    flt = plugin_pkg.ForumAutoLinkFilter()

    # 1. Telegram 平台收到 Bangumi 链接：过滤器直接拒绝，手动指令提示不支持，且零网络请求发出
    tg_ev = AstrMessageEvent("https://bangumi.tv/group/topic/475273", platform_name="telegram")
    assert flt.filter(tg_ev, {}) is False
    async with aclosing(plugin.on_forum_link_message(tg_ev)) as agen:
        tg_auto_out = [r async for r in agen]
    assert tg_auto_out == []

    async with aclosing(plugin.cmd_parse_forum(tg_ev, "https://bgm.tv/blog/337198")) as agen:
        tg_cmd_out = [r async for r in agen]
    assert len(tg_cmd_out) == 1
    assert tg_cmd_out[0].kind == "plain"
    assert "未识别到受支持的论坛帖子链接" in tg_cmd_out[0].payload
    assert len(mocked.requests) == 0

    # Telegram 收到纯文本 + S1 + Bangumi 混合消息时：仅提取 S1，忽略 Bangumi
    mixed_tg_ev = AstrMessageEvent(
        "https://bangumi.tv/blog/337198 https://stage1st.com/2b/thread-100-1-1.html",
        platform_name="telegram",
    )
    assert flt.filter(mixed_tg_ev, {}) is True
    extracted_tg = extract_forum_links_from_event(mixed_tg_ev, enable_bangumi=False)
    assert len(extracted_tg) == 1 and extracted_tg[0].forum == ForumType.STAGE1ST

    # 2. QQ OneBot (aiocqhttp) 平台收到 Bangumi 链接（含 chii.in QQ JSON 卡片）：正常触发解析并输出合并转发
    qq_card = json.dumps(
        {
            "app": "com.tencent.structmsg",
            "meta": {"news": {"jumpUrl": "https://chii.in/group/topic/475273"}},
        }
    )
    import astrbot.api.message_components as Comp

    qq_ev = AstrMessageEvent(
        "",
        platform_name="aiocqhttp",
        messages=[Comp.Json(data=qq_card)],
    )
    assert flt.filter(qq_ev, {}) is True
    async with aclosing(plugin.on_forum_link_message(qq_ev)) as agen:
        qq_out = [r async for r in agen]
    assert len(qq_out) == 1
    assert qq_out[0].kind == "chain"
    nodes = qq_out[0].payload[0].nodes
    assert len(nodes) == 3  # 摘要 + 渲染长图 + 配图原图
    assert "番组开发" in nodes[0].content[0].text

    await plugin.terminate()
