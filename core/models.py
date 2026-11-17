from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ForumType(StrEnum):
    """支持的论坛类型枚举。"""

    STAGE1ST = "stage1st"
    NGA = "nga"
    BANGUMI = "bangumi"

    @property
    def display_name(self) -> str:
        if self == ForumType.STAGE1ST:
            return "Stage1st"
        if self == ForumType.NGA:
            return "NGA 玩家社区"
        if self == ForumType.BANGUMI:
            return "Bangumi 番组计划"
        return self.value


@dataclass(frozen=True)
class ForumLink:
    """从消息中识别出的论坛帖子或指定楼层链接信息。"""

    forum: ForumType
    tid: str
    original_url: str
    canonical_url: str
    pid: str = ""
    page: int = 1
    floor_hint: int = 0
    sub_type: str = ""

    @property
    def is_floor_link(self) -> bool:
        """是否为指向特定回复楼层的链接。"""
        return bool(self.pid) or self.floor_hint > 0

    @property
    def target_id(self) -> str:
        """用于去重、冷却与缓存的唯一目标标识（区分同帖主楼与不同回复楼层）。"""
        if self.pid:
            base = f"{self.tid}:pid:{self.pid}" if self.tid else f"pid:{self.pid}"
        elif self.floor_hint > 0:
            base = f"{self.tid}:floor:{self.floor_hint}"
        else:
            base = self.tid
        return f"{self.sub_type}:{base}" if self.sub_type else base


@dataclass
class RateItem:
    """楼层评分条目（如 S1 鹅评）。"""

    user: str
    score: str
    reason: str = ""


@dataclass
class ReactionItem:
    """贴贴表态条目（如 Bangumi 贴贴表情计数）。"""

    emoji_url: str = ""
    label: str = ""
    count: int = 0


@dataclass
class RelatedSubject:
    """关联作品条目（用于 Bangumi 日志/条目讨论展示）。"""

    subject_id: str
    title: str
    subtitle: str = ""
    cover: str = ""


@dataclass
class ParsedThread:
    """解析后的论坛帖子（主楼或指定楼层）结构化数据。"""

    forum: ForumType
    tid: str
    url: str
    title: str
    author: str
    content_html: str
    pid: str = ""
    floor_label: str = ""
    is_main_post: bool = True
    is_thread_author: bool = False
    author_id: str = ""
    author_avatar: str = ""
    author_group: str = ""
    forum_name: str = ""
    category: str = ""
    created_at: str = ""
    views: int | None = None
    replies: int | None = None
    score: int | None = None
    alter_info: str = ""
    is_unblocked_invisible: bool = False
    invisible_reason: str = ""
    is_truncated: bool = False
    truncated_at_chars: int = 0
    omitted_images_count: int = 0
    rate_summary: str = ""
    rates: list[RateItem] = field(default_factory=list)
    post_image_urls: list[str] = field(default_factory=list)
    sub_type: str = ""
    group_icon: str = ""
    author_sign: str = ""
    reading_time: str = ""
    reactions: list[ReactionItem] = field(default_factory=list)
    related_subjects: list[RelatedSubject] = field(default_factory=list)
    pre_downloaded_files: dict[int, str] = field(default_factory=dict)
    has_uninlined_images: bool = False
    bmo_script_html: str = ""

    @property
    def display_floor_badge(self) -> str:
        """返回卡片与转发节点中展示的楼层标签文案。"""
        if self.floor_label:
            return self.floor_label
        return "楼主 · 1L" if self.is_main_post else "指定楼层"


@dataclass
class RenderResult:
    """远程渲染结果。"""

    image_path: str
    width: int = 0
    height: int = 0
    file_size: int = 0
    mime_type: str = "image/jpeg"

    @property
    def exceeds_telegram_photo_limit(self) -> bool:
        """检查图片是否超过 Telegram sendPhoto 接口限制（宽高和 <= 10000px 且文件 <= 10MB）。"""
        if self.file_size > 9_800_000:
            return True
        if self.width > 0 and self.height > 0 and (self.width + self.height) > 9800:
            return True
        return False


class ForumParseError(Exception):
    """论坛解析异常基类。"""


class ForumLoginRequiredError(ForumParseError):
    """需要论坛登录态或凭证无效异常。"""
