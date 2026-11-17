from __future__ import annotations

from contextlib import aclosing
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context
from conftest import MockHttpRouter

from core.temp_manager import BoundedTTLCache, TempFileManager

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.mark.asyncio
async def test_immediate_temp_file_cleanup_during_and_after_send(tmp_path: Path) -> None:
    """验证渲染临时图片在发送期间可读，一旦发送完毕（生成器恢复）即刻删除且空目录一并清理。"""
    import main as plugin_pkg

    nga_json = (
        '{"data":{"__F":{"name":"水区"},'
        '"__T":{"tid":88888,"subject":"清理测试帖","authorid":1,"replies":1,"postdate":1763376000},'
        '"__U":{"1":{"username":"测试员"}},'
        '"__R":{"0":{"lou":0,"authorid":1,"content":"测试正文内容"}}}}'
    )

    mocked = MockHttpRouter()
    mocked.get(
        "https://bbs.nga.cn/read.php?tid=88888&page=1&__output=11",
        status=200,
        body=nga_json.encode("utf-8"),
    )
    mocked.post(
        "http://mock-t2i.local:8999/text2img/generate",
        status=200,
        body=PNG_1X1,
        headers={"Content-Type": "image/png"},
    )

    ctx = Context(html_renderer=None)
    plugin = plugin_pkg.ForumParserPlugin(
        ctx,
        config={
            "t2i_endpoint": "http://mock-t2i.local:8999",
            "render_options": {"embed_images": False, "image_format": "png"},
            "parse_options": {"cooldown_seconds": 0},
        },
    )
    temp_render_dir = tmp_path / "temp_render"
    plugin._temp_dir = temp_render_dir
    plugin._temp_mgr = TempFileManager(temp_render_dir)
    plugin._http_session = aiohttp.ClientSession(middlewares=[mocked.middleware])
    plugin._s1_parser = plugin_pkg.Stage1stParser(plugin._http_session, config={"enabled": True})
    plugin._nga_parser = plugin_pkg.NgaParser(plugin._http_session, config={"enabled": True})
    plugin._renderer = plugin_pkg.ForumCardRenderer(
        context=ctx,
        session=plugin._http_session,
        temp_dir=temp_render_dir,
        temp_manager=plugin._temp_mgr,
    )

    ev = AstrMessageEvent("https://bbs.nga.cn/read.php?tid=88888", platform_name="aiocqhttp")
    observed_paths: list[Path] = []

    async with aclosing(plugin.on_forum_link_message(ev)) as agen:
        async for res in agen:
            # OneBot11 默认返回合并转发 Nodes，第 2 个节点承载本地渲染长图
            nodes_comp = res.payload[0]
            img_p = Path(nodes_comp.nodes[1].content[0].path)
            observed_paths.append(img_p)
            # 在 RespondStage 发送阶段（yield 挂起期间），图片文件必须存在且可读
            assert img_p.is_file()

    # 发送阶段结束退出后，文件与空的 temp_render 目录必须已被立即删除，零残留
    assert len(observed_paths) == 1
    assert not observed_paths[0].exists()
    assert not temp_render_dir.exists()
    assert plugin._temp_mgr.tracked_count == 0

    await plugin.terminate()


@pytest.mark.asyncio
async def test_astrbot_core_temp_img_and_after_message_sent_hook(tmp_path: Path) -> None:
    """验证由 AstrBot 内置 html_renderer 生成在 data/temp/io_temp_img_* 的文件同样会被立即清理。"""
    import main as plugin_pkg

    astrbot_temp_dir = tmp_path / "data" / "temp"
    astrbot_temp_dir.mkdir(parents=True)
    astrbot_io_img = astrbot_temp_dir / "io_temp_img_1763376000_abcd1234.jpg"
    astrbot_io_img.write_bytes(PNG_1X1)

    class FakeCoreRenderer:
        async def render_custom_template(
            self,
            tmpl_str: str,
            tmpl_data: dict[str, Any],
            return_url: bool = False,
            options: dict[str, Any] | None = None,
            endpoint: str | None = None,
        ) -> str:
            return str(astrbot_io_img)

    nga_json = (
        '{"data":{"__F":{"name":"水区"},'
        '"__T":{"tid":99999,"subject":"Core临时文件清理测试","authorid":1,"replies":1,"postdate":1763376000},'
        '"__U":{"1":{"username":"测试员"}},'
        '"__R":{"0":{"lou":0,"authorid":1,"content":"测试"}}}}'
    )
    mocked = MockHttpRouter()
    mocked.get(
        "https://bbs.nga.cn/read.php?tid=99999&page=1&__output=11",
        status=200,
        body=nga_json.encode("utf-8"),
    )

    ctx = Context(html_renderer=FakeCoreRenderer())
    plugin = plugin_pkg.ForumParserPlugin(
        ctx,
        config={
            "render_options": {"embed_images": False},
            "parse_options": {"cooldown_seconds": 0},
        },
    )
    plugin._http_session = aiohttp.ClientSession(middlewares=[mocked.middleware])
    plugin._s1_parser = plugin_pkg.Stage1stParser(plugin._http_session, config={"enabled": True})
    plugin._nga_parser = plugin_pkg.NgaParser(plugin._http_session, config={"enabled": True})
    plugin._renderer = plugin_pkg.ForumCardRenderer(
        context=ctx,
        session=plugin._http_session,
        temp_dir=tmp_path / "plugin_temp",
        temp_manager=plugin._temp_mgr,
    )

    ev = AstrMessageEvent("https://bbs.nga.cn/read.php?tid=99999", platform_name="aiocqhttp")
    agen = plugin.on_forum_link_message(ev)
    res = await agen.__anext__()
    nodes_comp = res.payload[0]
    assert nodes_comp.nodes[1].content[0].path == str(astrbot_io_img)
    assert astrbot_io_img.exists()

    # 模拟 RespondStage 触发 after_message_sent 钩子（即使下游插件调用了 stop_event 未继续驱动生成器）
    await plugin.on_after_message_sent_cleanup(ev)
    assert not astrbot_io_img.exists()

    await agen.aclose()
    await plugin.terminate()


@pytest.mark.asyncio
async def test_startup_purge_legacy_and_orphan_files(tmp_path: Path) -> None:
    """验证启动与巡检会自动清空崩溃遗留的孤立文件、.tmp 半成品及旧版 render_cache 目录。"""
    temp_dir = tmp_path / "temp_render"
    legacy_dir = tmp_path / "render_cache"
    temp_dir.mkdir()
    legacy_dir.mkdir()

    orphan_1 = temp_dir / "forum_orphan.jpg"
    orphan_2 = temp_dir / "forum_partial.jpg.tmp"
    legacy_file = legacy_dir / "forum_old.png"
    orphan_1.write_bytes(b"123")
    orphan_2.write_bytes(b"456")
    legacy_file.write_bytes(b"789")

    mgr = TempFileManager(temp_dir, legacy_dirs=[legacy_dir], ttl_seconds=60.0)
    await mgr.purge_all()

    assert not orphan_1.exists()
    assert not orphan_2.exists()
    assert not legacy_file.exists()
    assert not legacy_dir.exists()
    assert not temp_dir.exists()


def test_bounded_ttl_cache_eviction_and_expiry() -> None:
    cache: BoundedTTLCache[str, str] = BoundedTTLCache(
        max_entries=2,
        ttl_seconds=0.01,
        size_estimator=len,
        max_total_bytes=100,
    )
    cache.set("a", "hello")
    cache.set("b", "world")
    cache.set("c", "!")  # 超出 max_entries=2，淘汰最早的 "a"
    assert cache.get("a") is None
    assert cache.get("b") == "world"
    assert cache.get("c") == "!"

    # 超大单项（> max_total_bytes // 2）拒绝入缓存
    cache.set("huge", "x" * 80)
    assert cache.get("huge") is None

    import time

    time.sleep(0.02)
    assert cache.prune_expired() == 2
    assert len(cache) == 0


@pytest.mark.asyncio
async def test_oom_guards_stream_limit_and_forward_budget_and_concurrency(tmp_path: Path) -> None:
    """验证流式下载体积超限中止防 OOM、合并转发字节预算超额自动降级 URL、并发排队背压与磁盘配额淘汰。"""
    import asyncio

    from core.html_sanitizer import prepare_post_images_for_forward
    from core.io_guard import (
        BoundedTaskLimiter,
        ConcurrencyLimitError,
        ResponseTooLargeError,
        stream_response_to_file,
    )

    # 1. 流式落盘遇到超大响应立即抛错中止，且不遗留 .tmp 或残缺文件
    mocked = MockHttpRouter()
    mocked.get(
        "https://example.com/huge.png",
        status=200,
        body=b"A" * 4096,
    )
    dest = tmp_path / "stream_out.png"
    async with aiohttp.ClientSession(middlewares=[mocked.middleware]) as sess:
        async with sess.get("https://example.com/huge.png") as resp:
            with pytest.raises(ResponseTooLargeError):
                await stream_response_to_file(resp, dest, max_bytes=1024)
    assert not dest.exists()
    assert not dest.with_suffix(".png.tmp").exists()

    # 2. 合并转发原图达到 max_total_bytes 预算后，后续图片自动降级为 ("url", http_url) 零内存引用
    mgr = TempFileManager(tmp_path / "fwd_quota", max_dir_bytes=1024 * 1024)
    scope = mgr.create_scope()
    img_payload = PNG_1X1 * 10  # ~670 bytes
    mocked.get(
        "https://img.nga.178.com/attachments/1.png",
        status=200,
        body=img_payload,
        headers={"Content-Type": "image/png"},
    )
    mocked.get(
        "https://img.nga.178.com/attachments/2.png",
        status=200,
        body=img_payload,
        headers={"Content-Type": "image/png"},
    )
    async with aiohttp.ClientSession(middlewares=[mocked.middleware]) as sess:
        fwd_items = await prepare_post_images_for_forward(
            "",
            [
                "https://img.nga.178.com/attachments/1.png",
                "https://img.nga.178.com/attachments/2.png",
            ],
            scope,
            session=sess,
            max_total_bytes=800,  # 仅够容纳第 1 张落盘，第 2 张必须自动退化为 URL
        )
    assert len(fwd_items) == 2
    assert fwd_items[0][0] == "file" and Path(fwd_items[0][1]).is_file()
    assert fwd_items[1] == ("url", "https://img.nga.178.com/attachments/2.png")
    await scope.cleanup()
    assert not Path(fwd_items[0][1]).exists()
    assert not (tmp_path / "fwd_quota").exists()

    # 3. 全局并发信号量与排队背压保护
    limiter = BoundedTaskLimiter(max_concurrent=1, max_waiting=1)
    hold_ev = asyncio.Event()

    async def _slow_worker() -> None:
        async with limiter.acquire():
            await hold_ev.wait()

    t1 = asyncio.create_task(_slow_worker())
    await asyncio.sleep(0.01)
    t2 = asyncio.create_task(_slow_worker())  # 占用唯一 waiting 槽位
    await asyncio.sleep(0.01)

    with pytest.raises(ConcurrencyLimitError):
        async with limiter.acquire():
            pass

    hold_ev.set()
    await asyncio.gather(t1, t2)
