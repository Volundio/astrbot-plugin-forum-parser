from __future__ import annotations

from dataclasses import dataclass

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent

from core.models import ForumType
from core.url_extractor import (
    CooldownTracker,
    extract_forum_links_from_event,
    parse_forum_url,
)


def test_parse_stage1st_urls() -> None:
    # 标准伪静态链接
    l1 = parse_forum_url("https://stage1st.com/2b/thread-2209876-1-1.html")
    assert l1 is not None
    assert l1.forum == ForumType.STAGE1ST
    assert l1.tid == "2209876"
    assert l1.canonical_url == "https://stage1st.com/2b/thread-2209876-1-1.html"

    # forum.php?mod=viewthread&tid=...
    l2 = parse_forum_url(
        "https://stage1st.com/2b/forum.php?mod=viewthread&tid=2209876&extra=page%3D1"
    )
    assert l2 is not None
    assert l2.tid == "2209876"

    # redirect findpost ptid=...
    l3 = parse_forum_url(
        "https://stage1st.com/2b/forum.php?mod=redirect&goto=findpost&ptid=2209876&pid=654321"
    )
    assert l3 is not None
    assert l3.tid == "2209876"

    # 同源旧域名 saraba1st.com 开关控制
    l4_on = parse_forum_url(
        "https://bbs.saraba1st.com/2b/thread-1999999-3-1.html",
        allow_alias_domains=True,
    )
    assert l4_on is not None
    assert l4_on.tid == "1999999"

    l4_off = parse_forum_url(
        "https://bbs.saraba1st.com/2b/thread-1999999-3-1.html",
        allow_alias_domains=False,
    )
    assert l4_off is None

    # 版块列表链接不应误触发为帖子
    assert parse_forum_url("https://stage1st.com/2b/forum-75-1.html") is None


def test_parse_nga_urls_and_punctuation() -> None:
    l1 = parse_forum_url("（看看这个帖子：https://bbs.nga.cn/read.php?tid=42396123&page=2。）")
    assert l1 is not None
    assert l1.forum == ForumType.NGA
    assert l1.tid == "42396123"
    assert l1.canonical_url == "https://bbs.nga.cn/read.php?tid=42396123"

    # 同源域名 ngabbs.com 与 nga.178.com
    l2 = parse_forum_url("https://ngabbs.com/read.php?tid=41112222&_fp=2")
    assert l2 is not None
    assert l2.forum == ForumType.NGA
    assert l2.tid == "41112222"

    assert (
        parse_forum_url("https://ngabbs.com/read.php?tid=41112222", allow_alias_domains=False)
        is None
    )


def test_onebot11_json_share_card_and_qq_redirect() -> None:
    qq_json_card = {
        "app": "com.tencent.miniapp_01",
        "meta": {
            "detail_1": {
                "title": "NGA玩家社区",
                "qqdocurl": (
                    r"https:\/\/c.pc.qq.com\/middlem.html?pfurl="
                    r"https%3A%2F%2Fngabbs.com%2Fread.php%3Ftid%3D43001122"
                ),
            }
        },
    }
    ev = AstrMessageEvent(
        message_str="",
        platform_name="aiocqhttp",
        messages=[Comp.Json(data=qq_json_card)],
    )
    links = extract_forum_links_from_event(ev)
    assert len(links) == 1
    assert links[0].forum == ForumType.NGA
    assert links[0].tid == "43001122"


def test_telegram_text_link_entities_and_dedup() -> None:
    @dataclass
    class FakeEntity:
        type: str
        url: str = ""

    @dataclass
    class FakeTgMsg:
        text: str
        entities: list[FakeEntity]

    @dataclass
    class FakeTgUpdate:
        message: FakeTgMsg

    tg_update = FakeTgUpdate(
        message=FakeTgMsg(
            text="点这里看S1神帖 和 NGA原帖",
            entities=[
                FakeEntity(type="text_link", url="https://stage1st.com/2b/thread-2211000-1-1.html"),
                FakeEntity(type="text_link", url="https://bbs.nga.cn/read.php?tid=39887766"),
                # 重复同帖不同页码应自动去重
                FakeEntity(type="text_link", url="https://stage1st.com/2b/thread-2211000-5-1.html"),
            ],
        )
    )
    ev = AstrMessageEvent(
        message_str="点这里看S1神帖 和 NGA原帖",
        platform_name="telegram",
        raw_message=tg_update,
    )
    links = extract_forum_links_from_event(ev)
    assert [(x.forum, x.tid) for x in links] == [
        (ForumType.STAGE1ST, "2211000"),
        (ForumType.NGA, "39887766"),
    ]


def test_cooldown_tracker() -> None:
    tracker = CooldownTracker()
    assert tracker.check_and_mark("sess1", ForumType.NGA, "100", 30) is True
    assert tracker.check_and_mark("sess1", ForumType.NGA, "100", 30) is False
    assert tracker.check_and_mark("sess2", ForumType.NGA, "100", 30) is True
    assert tracker.check_and_mark("sess1", ForumType.STAGE1ST, "100", 30) is True
