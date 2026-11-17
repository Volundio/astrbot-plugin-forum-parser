from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import aiohttp
import astrbot.api.message_components as Comp
import pytest
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context
from conftest import MockHttpRouter

from core.html_sanitizer import process_post_html
from core.models import ForumType, ParsedThread
from core.renderer import (
    ForumCardRenderer,
    build_template_payload,
    inspect_image_dimensions,
    load_forum_card_template,
    normalize_t2i_endpoint,
)


def _make_png_bytes(width: int, height: int) -> bytes:
    """生成具有指定 IHDR 宽高的最小合法 PNG 头用于尺寸检测测试。"""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\x00\x00\x00\x00"


@pytest.mark.asyncio
async def test_html_security_sanitization_and_truncation() -> None:
    malicious_html = (
        "<p>安全段落</p>"
        '<script>fetch("file:///etc/passwd")</script>'
        '<iframe src="file:///etc/shadow"></iframe>'
        '<img src="file:///etc/hosts" onerror="alert(1)" />'
        '<a href="javascript:alert(1)">恶意链接</a>'
        "<p>" + ("超长内容测试字" * 200) + "</p>"
    )
    clean_html, is_truncated, char_count, _ = await process_post_html(
        malicious_html,
        base_url="https://stage1st.com/2b/",
        referer="https://stage1st.com/2b/",
        embed_images=False,
        enable_length_limit=True,
        max_content_chars=100,
    )

    assert "<script" not in clean_html
    assert "<iframe" not in clean_html
    assert "file://" not in clean_html
    assert "javascript:" not in clean_html
    assert "onerror" not in clean_html
    assert is_truncated is True
    assert char_count == 100
    assert clean_html.endswith("</p>")


def test_template_structure_and_endpoint_normalization() -> None:
    assert normalize_t2i_endpoint("http://127.0.0.1:8999") == "http://127.0.0.1:8999/text2img"
    assert (
        normalize_t2i_endpoint("http://127.0.0.1:8999/text2img/")
        == "http://127.0.0.1:8999/text2img"
    )
    assert (
        normalize_t2i_endpoint("http://127.0.0.1:8999/text2img/generate")
        == "http://127.0.0.1:8999/text2img"
    )

    tmpl = load_forum_card_template()
    # 包含 astrbot-t2i-shiki-runtime 标记以避免无意义的 1.2MB JS 注入
    assert 'id="astrbot-t2i-shiki-runtime"' in tmpl
    # 验证模板支持 s1 / nga / light / dark 主题样式
    assert 'body[data-theme="s1"]' in tmpl
    assert 'body[data-theme="nga"]' in tmpl
    assert 'body[data-theme="dark"]' in tmpl

    sample_thread = ParsedThread(
        forum=ForumType.STAGE1ST,
        tid="2200001",
        url="https://stage1st.com/2b/thread-2200001-1-1.html",
        title="测试标题",
        author="测试楼主",
        content_html="<p>正文</p>",
    )
    data, opts = build_template_payload(sample_thread, {"theme": "auto", "viewport_width": 800})
    assert data["theme_key"] == "s1"
    assert opts["viewport_width"] == 800
    assert opts["full_page"] is True


@pytest.mark.asyncio
async def test_renderer_prefers_astrbot_html_renderer_with_custom_endpoint(tmp_path: Path) -> None:
    captured_calls: list[dict[str, Any]] = []
    out_img = tmp_path / "rendered.png"
    out_img.write_bytes(_make_png_bytes(800, 1200))

    class FakeHtmlRenderer:
        async def render_custom_template(
            self,
            tmpl_str: str,
            tmpl_data: dict[str, Any],
            return_url: bool = False,
            options: dict[str, Any] | None = None,
            endpoint: str | None = None,
        ) -> str:
            captured_calls.append(
                {
                    "tmpl_len": len(tmpl_str),
                    "tmpl_data": tmpl_data,
                    "return_url": return_url,
                    "options": options,
                    "endpoint": endpoint,
                }
            )
            return str(out_img)

    ctx = Context(html_renderer=FakeHtmlRenderer())
    renderer = ForumCardRenderer(context=ctx, temp_dir=tmp_path)

    thread = ParsedThread(
        forum=ForumType.NGA,
        tid="42396123",
        url="https://bbs.nga.cn/read.php?tid=42396123",
        title="渲染测试",
        author="泥潭用户",
        content_html="<p>测试</p>",
    )
    res = await renderer.render_thread_card(
        thread,
        plugin_t2i_endpoint="http://10.0.0.5:8999",
    )

    assert len(captured_calls) == 1
    assert captured_calls[0]["endpoint"] == "http://10.0.0.5:8999"
    assert captured_calls[0]["return_url"] is False
    assert captured_calls[0]["tmpl_data"]["theme_key"] == "nga"
    assert res.width == 800
    assert res.height == 1200
    assert res.exceeds_telegram_photo_limit is False


@pytest.mark.asyncio
async def test_plugin_end_to_end_aiocqhttp_and_telegram_tall_image(tmp_path: Path) -> None:
    import main as plugin_pkg

    ForumParserPlugin = plugin_pkg.ForumParserPlugin
    ForumAutoLinkFilter = plugin_pkg.ForumAutoLinkFilter

    # 1. 验证过滤器不会在手动指令上重复触发，也不会在普通闲聊上触发
    flt = ForumAutoLinkFilter()
    assert flt.filter(AstrMessageEvent("今天天气真不错"), {}) is False
    assert (
        flt.filter(AstrMessageEvent("forum_parse https://bbs.nga.cn/read.php?tid=12345"), {})
        is False
    )
    assert flt.filter(AstrMessageEvent("快看 https://bbs.nga.cn/read.php?tid=12345"), {}) is True

    # 2. 构造超过 Telegram 10000px 限制的超长截图，验证 OneBot11 发图片而 Telegram 自动降级发原图文件
    tall_png = _make_png_bytes(1040, 9500)  # width + height = 10540 > 10000
    w, h, mime = inspect_image_dimensions_from_bytes(tall_png, tmp_path)
    assert (w, h, mime) == (1040, 9500, "image/png")

    nga_json = (
        '{"data":{"__F":{"name":"大漩涡"},'
        '"__T":{"tid":12345,"subject":"端到端测试帖","authorid":1,"replies":5,"postdate":1763376000},'
        '"__U":{"1":{"username":"楼主A"}},'
        '"__R":{"0":{"lou":0,"authorid":1,"content":"端到端主楼正文[img]./mon_202611/17/p1.png[/img][img]./mon_202611/17/p2.png[/img]"}}}}'
    )

    ctx = Context(html_renderer=None)
    plugin = ForumParserPlugin(
        ctx,
        config={
            "t2i_endpoint": "http://mock-t2i.local:8999",
            "render_options": {"embed_images": True, "image_format": "png"},
            "parse_options": {"cooldown_seconds": 0},
            "stage1st": {"enabled": True},
            "nga": {"enabled": True},
        },
    )

    mocked = MockHttpRouter()
    mocked.get(
        "https://bbs.nga.cn/read.php?tid=12345&page=1&__output=11",
        status=200,
        body=nga_json.encode("utf-8"),
    )
    mocked.get(
        "https://img.nga.cn/attachments/mon_202611/17/p1.png",
        status=200,
        body=tall_png,
        headers={"Content-Type": "image/png"},
    )
    mocked.get(
        "https://img.nga.cn/attachments/mon_202611/17/p2.png",
        status=200,
        body=tall_png,
        headers={"Content-Type": "image/png"},
    )
    mocked.post(
        "http://mock-t2i.local:8999/text2img/generate",
        status=200,
        body=tall_png,
        headers={"Content-Type": "image/png"},
    )

    plugin._http_session = aiohttp.ClientSession(middlewares=[mocked.middleware])
    plugin._s1_parser = plugin_pkg.Stage1stParser(plugin._http_session, config={"enabled": True})
    plugin._nga_parser = plugin_pkg.NgaParser(plugin._http_session, config={"enabled": True})
    plugin._renderer = ForumCardRenderer(
        context=ctx,
        session=plugin._http_session,
        temp_dir=tmp_path / "qq_tg_temp",
        temp_manager=plugin._temp_mgr,
    )

    # OneBot11 默认发送合并转发消息（节点1: 摘要文本，节点2: 渲染长图，节点3~4: 帖子内 2 张原图）
    from contextlib import aclosing

    ev_qq = AstrMessageEvent(
        "分享个帖 https://bbs.nga.cn/read.php?tid=12345",
        platform_name="aiocqhttp",
        self_id="2854196310",
    )
    spilled_paths: list[Path] = []
    async with aclosing(plugin.on_forum_link_message(ev_qq)) as agen:
        async for r in agen:
            assert r.kind == "chain"
            assert isinstance(r.payload[0], Comp.Nodes)
            forward_nodes = r.payload[0].nodes
            assert len(forward_nodes) == 4
            assert forward_nodes[0].uin == "2854196310"
            assert isinstance(forward_nodes[0].content[0], Comp.Plain)
            assert "端到端测试帖" in forward_nodes[0].content[0].text
            assert isinstance(forward_nodes[1].content[0], Comp.Image)
            # 原图节点采用流式落盘文件引用（file://）以防内存堆积，且在发送阶段文件真实存在
            assert forward_nodes[2].name == "帖子配图 (1/2)"
            assert forward_nodes[2].content[0].file.startswith("file://")
            assert Path(forward_nodes[2].content[0].path).is_file()
            assert forward_nodes[3].name == "帖子配图 (2/2)"
            assert forward_nodes[3].content[0].file.startswith("file://")
            assert Path(forward_nodes[3].content[0].path).is_file()
            spilled_paths.extend(
                [
                    Path(forward_nodes[1].content[0].path),
                    Path(forward_nodes[2].content[0].path),
                    Path(forward_nodes[3].content[0].path),
                ]
            )

    # 发送完毕退出生成器后，渲染长图与 2 张落盘配图均已被立即销毁，零垃圾残留
    assert len(spilled_paths) == 3
    for p in spilled_paths:
        assert not p.exists()

    # 若在设置中显式关闭 OneBot 合并转发，则直接回退为单张图片发送
    plugin.config["onebot_options"] = {"use_forward": False}
    qq_direct_outputs = [r async for r in plugin.on_forum_link_message(ev_qq)]
    assert len(qq_direct_outputs) == 1
    assert qq_direct_outputs[0].kind == "image"

    # Telegram 平台始终仅推送自定义模板渲染的主楼完整长图，并固定以 File (send as file) 方式发送
    ev_tg = AstrMessageEvent(
        "分享个帖 https://bbs.nga.cn/read.php?tid=12345",
        platform_name="telegram",
    )
    tg_outputs = [r async for r in plugin.on_forum_link_message(ev_tg)]
    assert len(tg_outputs) == 1
    assert tg_outputs[0].kind == "chain"
    assert len(tg_outputs[0].payload) == 1
    assert isinstance(tg_outputs[0].payload[0], Comp.File)
    assert tg_outputs[0].payload[0].name == "nga_12345.png"

    # 即使渲染图尺寸很小（未超 10000px 普通图片限制），Telegram 也依然固定走 Comp.File 且不附带额外节点
    small_png = _make_png_bytes(780, 900)
    mocked.post(
        "http://mock-t2i.local:8999/text2img/generate",
        status=200,
        body=small_png,
        headers={"Content-Type": "image/png"},
    )
    # 清空队列使新注册的 small_png 生效
    mocked._queues[("POST", "http://mock-t2i.local:8999/text2img/generate")].popleft()
    tg_small_outputs = [r async for r in plugin.on_forum_link_message(ev_tg)]
    assert len(tg_small_outputs) == 1
    assert tg_small_outputs[0].kind == "chain"
    assert len(tg_small_outputs[0].payload) == 1
    assert isinstance(tg_small_outputs[0].payload[0], Comp.File)
    assert tg_small_outputs[0].payload[0].name == "nga_12345.png"

    await plugin.terminate()


def inspect_image_dimensions_from_bytes(data: bytes, tmp_path: Path) -> tuple[int, int, str]:
    p = tmp_path / "probe.bin"
    p.write_bytes(data)
    return inspect_image_dimensions(str(p))


def test_jpeg_sof_dimension_inspection(tmp_path: Path) -> None:
    # 构造包含 APP0 + SOF0 (高 1850, 宽 780) 的最小合法 JPEG 头
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, 1850, 780, 1) + b"\x01\x11\x00"
    jpeg_bytes = b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"
    w, h, mime = inspect_image_dimensions_from_bytes(jpeg_bytes, tmp_path)
    assert (w, h, mime) == (780, 1850, "image/jpeg")


@pytest.mark.asyncio
async def test_bilibili_iframe_and_manual_command(tmp_path: Path) -> None:
    # B站播放器 iframe 应转为可见提示块，而任意未知 iframe 应被安全丢弃
    raw_html = (
        '<iframe src="//player.bilibili.com/player.html?bvid=BV1xx411c7mD&page=1"></iframe>'
        '<iframe src="https://evil.example.com/phish"></iframe>'
    )
    clean_html, _, _, _ = await process_post_html(
        raw_html,
        base_url="https://stage1st.com/2b/",
        referer="https://stage1st.com/2b/",
        embed_images=False,
    )
    assert "BV1xx411c7mD" in clean_html
    assert "evil.example.com" not in clean_html

    # 测试手动指令对无效链接的友好提示
    import main as plugin_pkg

    ctx = Context(html_renderer=None)
    plugin = plugin_pkg.ForumParserPlugin(ctx, config={})
    ev = AstrMessageEvent("/forum_parse https://example.com/not-a-forum")
    outputs = [r async for r in plugin.cmd_parse_forum(ev, "https://example.com/not-a-forum")]
    assert len(outputs) == 1
    assert outputs[0].kind == "plain"
    assert "未识别到受支持的论坛帖子链接" in outputs[0].payload
    await plugin.terminate()


@pytest.mark.asyncio
async def test_onebot_forward_fetches_post_images_even_when_embed_disabled_and_skips_smileys(
    tmp_path: Path,
) -> None:
    """即使关闭了卡片图片内嵌或内容被字数截断，QQ 合并转发仍应附带正文图片并自动排除小表情。"""
    import main as plugin_pkg

    s1_html = """
    <html><body><div id="postlist">
      <h1 class="ts"><span id="thread_subject">合并转发排除表情测试</span></h1>
      <div id="post_1">
        <div class="pi"><strong><a id="postnum1">楼主</a></strong>
          <div class="authi"><a href="space-uid-100.html" class="xw1">S1楼主</a></div>
        </div>
        <td id="postmessage_1">
          这是一段很长的文字用于触发字数截断测试，确保后面的图片即使被卡片截断也能进入合并转发。
          <img smilieid="88" src="https://static.stage1st.com/image/smiley/face2017/067.png" />
          <img src="https://img.saraba1st.com/forum/202611/17/real_photo.png" />
        </td>
      </div>
    </div></body></html>
    """
    png_bytes = _make_png_bytes(400, 300)
    mocked = MockHttpRouter()
    mocked.get(
        "https://stage1st.com/2b/thread-3300001-1-1.html",
        status=200,
        body=s1_html.encode("utf-8"),
    )
    mocked.get(
        "https://img.stage1st.com/forum/202611/17/real_photo.png",
        status=200,
        body=png_bytes,
        headers={"Content-Type": "image/png"},
    )
    mocked.post(
        "http://mock-t2i.local:8999/text2img/generate",
        status=200,
        body=png_bytes,
        headers={"Content-Type": "image/png"},
    )

    ctx = Context(html_renderer=None)
    plugin = plugin_pkg.ForumParserPlugin(
        ctx,
        config={
            "t2i_endpoint": "http://mock-t2i.local:8999",
            "render_options": {
                "embed_images": False,
                "enable_length_limit": True,
                "max_content_chars": 10,
                "image_format": "png",
            },
            "parse_options": {"cooldown_seconds": 0},
        },
    )
    from core.temp_manager import TempFileManager

    plugin._temp_mgr = TempFileManager(tmp_path / "s1_fwd_temp")
    plugin._http_session = aiohttp.ClientSession(middlewares=[mocked.middleware])
    plugin._s1_parser = plugin_pkg.Stage1stParser(plugin._http_session, config={"enabled": True})
    plugin._nga_parser = plugin_pkg.NgaParser(plugin._http_session, config={"enabled": True})
    plugin._renderer = ForumCardRenderer(
        context=ctx,
        session=plugin._http_session,
        temp_dir=tmp_path / "s1_fwd_temp",
        temp_manager=plugin._temp_mgr,
    )

    ev = AstrMessageEvent(
        "https://stage1st.com/2b/thread-3300001-1-1.html",
        platform_name="aiocqhttp",
    )
    from contextlib import aclosing

    spilled_img: Path | None = None
    async with aclosing(plugin.on_forum_link_message(ev)) as agen:
        async for r in agen:
            nodes = r.payload[0].nodes
            # 节点1(摘要) + 节点2(渲染长图) + 节点3(唯一正文大图，麻将脸表情已被过滤) = 3 个节点
            assert len(nodes) == 3
            assert nodes[2].name == "帖子配图 (1/1)"
            assert nodes[2].content[0].file.startswith("file://")
            spilled_img = Path(nodes[2].content[0].path)
            assert spilled_img.is_file()

    assert spilled_img is not None and not spilled_img.exists()
    assert not (tmp_path / "s1_fwd_temp").exists()

    await plugin.terminate()
