from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ForumType(StrEnum):
    """支持的论坛类型枚举。"""

    STAGE1ST = "stage1st"
    NGA = "nga"

    @property
    def display_name(self) -> str:
        if self == ForumType.STAGE1ST:
            return "Stage1st"
        if self == ForumType.NGA:
            return "NGA 玩家社区"
        return self.value


@dataclass(frozen=True)
class ForumLink:
    """从消息中识别出的论坛帖子链接信息。"""

    forum: ForumType
    tid: str
    original_url: str
    canonical_url: str


@dataclass
class RateItem:
    """主楼评分条目（如 S1 鹅评）。"""

    user: str
    score: str
    reason: str = ""


@dataclass
class ParsedThread:
    """解析后的论坛帖子主楼结构化数据。"""

    forum: ForumType
    tid: str
    url: str
    title: str
    author: str
    content_html: str
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
