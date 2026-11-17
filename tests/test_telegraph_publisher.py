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
    # 验证请求了 p.sda1.dev Binary 上传接口且携带了 filename 参数
    sda1_reqs = [r for r in mocked.requests if "p.sda1.dev" in r[1]]
    assert len(sda1_reqs) == 1
    assert sda1_reqs[0][0] == "POST"
    assert "filename=stage1st_2200123_1.png" in sda1_reqs[0][1]
    assert sda1_reqs[0][2].get("Content-Type") == "application/octet-stream"
    # 临时文件与目录均已彻底清空无残留
    assert not (tmp_path / "tg_pub_temp").exists()
