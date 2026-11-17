from __future__ import annotations

import json
from pathlib import Path

import aiohttp
import pytest
from conftest import MockHttpRouter

from core.models import ForumType, ParsedThread, RateItem
from core.telegraph_publisher import (
    TelegraphPublisher,
    _split_nodes_by_byte_budget,
    build_full_telegraph_document,
)
from core.temp_manager import TempFileManager

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_html_to_telegraph_dom_conversion_and_chunking() -> None:
    sample_html = (
        '<div class="forum-edit-note">本帖最后编辑于今天</div>'
        '<div class="forum-quote"><blockquote>前层引用文字</blockquote></div>'
        '<p>正文包含 <strong>加粗</strong>、<del class="forum-del">删除线</del>、'
        '<span class="forum-spoiler">反白剧透真相</span> 与麻将脸 '
        '<img class="forum-smiley" src="https://static.stage1st.com/image/smiley/face2017/067.png" alt="s1" />。</p>'
        '<details class="forum-collapse"><summary>折叠标题</summary><div class="forum-collapse-body"><p>折叠内部详情</p></div></details>'
        "<table><tr><th>项目</th><th>数值</th></tr><tr><td>攻击力</td><td>999</td></tr></table>"
        '<img class="forum-img" src="https://img.stage1st.com/forum/a.png" data-idx="0" />'
    )
    thread = ParsedThread(
        forum=ForumType.STAGE1ST,
        tid="2196337",
        pid="70514830",
        floor_label="239楼",
        is_main_post=False,
        url="https://stage1st.com/2b/forum.php?mod=redirect&goto=findpost&ptid=2196337&pid=70514830",
        title="野生的最终BOSS出现了！",
        category="漫区",
        forum_name="动漫论坛",
        author="eilot",
        author_group="婆罗门",
        created_at="2026-11-16 00:32",
        is_unblocked_invisible=True,
        invisible_reason="作者被禁言/内容屏蔽",
        content_html=sample_html,
        rate_summary="1 人参与 · 战斗力 +2",
        rates=[RateItem(user="坛友甲", score="+2", reason="到位")],
        post_image_urls=["https://img.stage1st.com/forum/a.png"],
    )

    nodes = build_full_telegraph_document(
        thread,
        idx_url_map={0: "https://p.sda1.dev/36/hash123/stage1st_2196337_1.png"},
    )
    serialized = json.dumps(nodes, ensure_ascii=False)

    # 验证所有顶层节点都是合法的 Telegraph 块级元素
    allowed_top_tags = {"p", "blockquote", "aside", "figure", "h3", "h4", "hr", "ul", "ol", "pre"}
    for n in nodes:
        assert n["tag"] in allowed_top_tags

    assert "239楼" in serialized
    assert "🔓 已通过官方接口解锁不可见内容" in serialized
    assert "【反白：反白剧透真相】" in serialized
    assert "[s1:067]" in serialized
    assert "📂 折叠标题" in serialized
    assert "折叠内部详情" in serialized
    assert "攻击力" in serialized and "999" in serialized
    assert "https://p.sda1.dev/36/hash123/stage1st_2196337_1.png" in serialized
    assert "坛友甲" in serialized

    # 验证超长节点自动切分分页
    many_nodes = [{"tag": "p", "children": ["超长段落测试" * 200]} for _ in range(40)]
    pages = _split_nodes_by_byte_budget(many_nodes, max_bytes=10 * 1024)
    assert len(pages) >= 4


@pytest.mark.asyncio
async def test_telegraph_publisher_uploads_binary_to_sda1_and_cleans_temp(tmp_path: Path) -> None:
    kv: dict[str, object] = {}

    async def kv_get(k: str, d: object = None) -> object:
        return kv.get(k, d)

    async def kv_set(k: str, v: object) -> None:
        kv[k] = v

    thread = ParsedThread(
        forum=ForumType.STAGE1ST,
        tid="2200123",
        url="https://stage1st.com/2b/thread-2200123-1-1.html",
        title="图床转存与Telegraph发布测试",
        author="楼主测试",
        content_html='<p>第一段</p><img class="forum-img" src="https://img.stage1st.com/forum/pic.png" data-idx="0" />',
        post_image_urls=["https://img.stage1st.com/forum/pic.png"],
    )

    mocked = MockHttpRouter()
    mocked.get(
        "https://img.stage1st.com/forum/pic.png",
        status=200,
        body=PNG_1X1,
        headers={"Content-Type": "image/png"},
    )
    mocked.post(
        "https://p.sda1.dev/api/v1/upload_external_noform?filename=stage1st_2200123_1.png",
        status=200,
        payload={
            "success": True,
            "code": "success",
            "data": {"url": "https://p.sda1.dev/36/mockhash/stage1st_2200123_1.png"},
        },
    )
    mocked.get(
        "https://p.sda1.dev/36/mockhash/stage1st_2200123_1.png",
        status=200,
        body=PNG_1X1,
        headers={"Content-Type": "image/png", "CF-Cache-Status": "MISS"},
    )
    mocked.post(
        "https://api.telegra.ph/createAccount",
        status=200,
        payload={"ok": True, "result": {"access_token": "persisted_token_001"}},
    )
    mocked.post(
        "https://api.telegra.ph/createPage",
        status=200,
        payload={"ok": True, "result": {"url": "https://telegra.ph/Test-S1-Post-11-18"}},
    )

    temp_mgr = TempFileManager(tmp_path / "tg_pub_temp")
    scope = temp_mgr.create_scope()

    async with aiohttp.ClientSession(middlewares=[mocked.middleware]) as session:
        pub = TelegraphPublisher(session, kv_getter=kv_get, kv_setter=kv_set)
        page_url = await pub.publish_thread(
            thread,
            scope,
            cookies={"B7Y9_auth": "1"},
            same_site_hosts={"img.stage1st.com"},
        )

    await scope.cleanup()

    assert page_url == "https://telegra.ph/Test-S1-Post-11-18"
    assert kv["telegraph_access_token"] == "persisted_token_001"
    # 验证请求了 p.sda1.dev Binary 上传接口且在 createPage 前完成了 GET 边缘缓存预热
    sda1_posts = [r for r in mocked.requests if r[0] == "POST" and "p.sda1.dev" in r[1]]
    sda1_gets = [r for r in mocked.requests if r[0] == "GET" and "p.sda1.dev" in r[1]]
    assert len(sda1_posts) == 1
    assert "filename=stage1st_2200123_1.png" in sda1_posts[0][1]
    assert sda1_posts[0][2].get("Content-Type") == "application/octet-stream"
    assert len(sda1_gets) == 1
    assert sda1_gets[0][1] == "https://p.sda1.dev/36/mockhash/stage1st_2200123_1.png"
    # 预热必须发生在 createPage 之前
    req_urls = [r[1] for r in mocked.requests]
    assert req_urls.index("https://p.sda1.dev/36/mockhash/stage1st_2200123_1.png") < req_urls.index(
        "https://api.telegra.ph/createPage"
    )
    # 临时文件与目录均已彻底清空无残留
    assert not (tmp_path / "tg_pub_temp").exists()


@pytest.mark.asyncio
async def test_s1_discuz_signed_attr_and_403_fallback_plus_sda1_reuse_and_prewarm(
    tmp_path: Path,
) -> None:
    """验证：
    1. Discuz <img> 同时存在无签名 zoomfile 与带签名 file(?a=...) 时，优先选用带签名地址；
    2. 当主地址返回 403 且备选地址首次跨境超时时，自动回退备选地址并换新连接重试拉取上传图床；
    3. 原帖本身已在 p.sda1.dev 上的图片直接复用原链不重复重传，且与新传图一起按序串行预热边缘缓存。
    """
    from core.html_sanitizer import process_post_html

    raw_s1_html = (
        "<p>多图测试</p>"
        '<img id="aimg_1" src="https://static.stage1st.com/image/common/none.gif" '
        'zoomfile="https://img.stage1st.com/forum/unsigned1.png" '
        'file="https://img.stage1st.com/forum/unsigned1.png?a=sigtoken1" />'
        '<img id="aimg_2" src="https://static.stage1st.com/image/common/none.gif" '
        'zoomfile="https://img.stage1st.com/forum/orig403.png" '
        'file="https://img.stage1st.com/forum/orig403.png.thumb.jpg" />'
        '<img id="aimg_3" file="http://p.sda1.dev/19/existinghash/already_hosted.jpg" />'
    )
    post_urls: list[str] = []
    clean_html, _, _, _ = await process_post_html(
        raw_s1_html,
        base_url="https://stage1st.com/2b/",
        referer="https://stage1st.com/2b/thread-2294225-1-1.html",
        embed_images=True,
        prefetch_data_uri=False,
        max_inline_images=25,
        out_post_image_urls=post_urls,
    )

    # 第 1 张图应优先选中带 ?a=sigtoken1 的鉴权地址
    assert post_urls[0] == "https://img.stage1st.com/forum/unsigned1.png?a=sigtoken1"
    # 第 2 张图保留原图为主地址，同时在 HTML 上记录 .thumb.jpg 为 data-fallback-src
    assert post_urls[1] == "https://img.stage1st.com/forum/orig403.png"
    assert 'data-fallback-src="https://img.stage1st.com/forum/orig403.png.thumb.jpg"' in clean_html
    assert post_urls[2] == "http://p.sda1.dev/19/existinghash/already_hosted.jpg"

    thread = ParsedThread(
        forum=ForumType.STAGE1ST,
        tid="2294225",
        url="https://stage1st.com/2b/thread-2294225-1-1.html",
        title="S1附件防盗链与图床预热测试",
        author="lymm2003",
        content_html=clean_html,
        post_image_urls=post_urls,
    )

    mocked = MockHttpRouter()
    # 图 1：带签名地址直接 200
    mocked.get(
        "https://img.stage1st.com/forum/unsigned1.png?a=sigtoken1",
        status=200,
        body=PNG_1X1,
        headers={"Content-Type": "image/png"},
    )
    mocked.post(
        "https://p.sda1.dev/api/v1/upload_external_noform?filename=stage1st_2294225_1.png",
        status=200,
        payload={
            "success": True,
            "data": {"url": "https://p.sda1.dev/36/h1/stage1st_2294225_1.png"},
        },
    )
    # 图 2：主地址 403 -> 自动回退到 thumb 备选地址（第 1 次跨境超时，第 2 次重试 200）
    mocked.get("https://img.stage1st.com/forum/orig403.png", status=403, body="Forbidden")
    mocked.get("https://img.stage1st.com/forum/orig403.png.thumb.jpg", exc=TimeoutError())
    mocked.get(
        "https://img.stage1st.com/forum/orig403.png.thumb.jpg",
        status=200,
        body=PNG_1X1,
        headers={"Content-Type": "image/png"},
    )
    mocked.post(
        "https://p.sda1.dev/api/v1/upload_external_noform?filename=stage1st_2294225_2.png",
        status=200,
        payload={
            "success": True,
            "data": {"url": "https://p.sda1.dev/36/h2/stage1st_2294225_2.png"},
        },
    )

    # 预热阶段：模拟第 2 张冷缓存首次断流异常，自动带 Connection: close 重试成功
    mocked.get(
        "https://p.sda1.dev/36/h1/stage1st_2294225_1.png",
        status=200,
        body=PNG_1X1,
        headers={"Content-Type": "image/png"},
    )
    mocked.get(
        "https://p.sda1.dev/36/h2/stage1st_2294225_2.png",
        exc=aiohttp.ClientPayloadError("Response payload is not completed"),
    )
    mocked.get(
        "https://p.sda1.dev/36/h2/stage1st_2294225_2.png",
        status=200,
        body=PNG_1X1,
        headers={"Content-Type": "image/png"},
    )
    mocked.get(
        "https://p.sda1.dev/19/existinghash/already_hosted.jpg",
        status=200,
        body=PNG_1X1,
        headers={"Content-Type": "image/jpeg"},
    )

    captured_content: list[str] = []

    async def _capture_middleware(req, handler):
        if str(req.url) == "https://api.telegra.ph/createPage":
            # 提取提交给 createPage 的表单内容
            raw_data = req._body
            captured_content.append(str(raw_data))
        return await mocked.middleware(req, handler)

    mocked.post(
        "https://api.telegra.ph/createAccount",
        status=200,
        payload={"ok": True, "result": {"access_token": "tok_xyz"}},
    )
    mocked.post(
        "https://api.telegra.ph/createPage",
        status=200,
        payload={"ok": True, "result": {"url": "https://telegra.ph/S1-Test-Page-11-18"}},
    )

    temp_mgr = TempFileManager(tmp_path / "tg_pub_s1")
    scope = temp_mgr.create_scope()
    async with aiohttp.ClientSession(middlewares=[_capture_middleware]) as session:
        pub = TelegraphPublisher(session)
        out_url = await pub.publish_thread(
            thread,
            scope,
            cookies={"B7Y9_2f85_auth": "ok"},
            same_site_hosts={"img.stage1st.com"},
        )
    await scope.cleanup()

    assert out_url == "https://telegra.ph/S1-Test-Page-11-18"
    # 仅对 2 张 S1 站内图调用了 POST 上传，第 3 张已有 p.sda1.dev 外链未重复上传
    upload_posts = [r for r in mocked.requests if r[0] == "POST" and "p.sda1.dev" in r[1]]
    assert len(upload_posts) == 2

    # 3 张图床地址均按序完成了 GET 预热（其中第 2 张断流重试共 2 次 GET）
    warm_gets = [r[1] for r in mocked.requests if r[0] == "GET" and "p.sda1.dev" in r[1]]
    assert warm_gets == [
        "https://p.sda1.dev/36/h1/stage1st_2294225_1.png",
        "https://p.sda1.dev/36/h2/stage1st_2294225_2.png",
        "https://p.sda1.dev/36/h2/stage1st_2294225_2.png",
        "https://p.sda1.dev/19/existinghash/already_hosted.jpg",
    ]
    assert not (tmp_path / "tg_pub_s1").exists()
