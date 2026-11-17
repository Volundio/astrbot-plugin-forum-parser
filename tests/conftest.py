from __future__ import annotations

import atexit
import enum
import json as _json
import shutil
import sys
import tempfile
import types
from collections import defaultdict, deque
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

from multidict import CIMultiDict

_TEST_DATA_TMPDIR = tempfile.mkdtemp(prefix="astrbot_forum_parser_ut_")
atexit.register(shutil.rmtree, _TEST_DATA_TMPDIR, True)


class _FakeStreamContent:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def iter_chunked(self, n: int):
        for i in range(0, len(self._data), n):
            yield self._data[i : i + n]


class _FakeAiohttpResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self.headers = CIMultiDict(headers or {})
        self._raw_cookie_headers = tuple(self.headers.getall("Set-Cookie", []))
        self.cookies = SimpleCookie()
        for sc in self._raw_cookie_headers:
            self.cookies.load(sc)
        self.content = _FakeStreamContent(body)
        self.url: Any = None
        self.connection = None
        self._history = ()

    def raise_for_status(self) -> None:
        return None

    async def read(self) -> bytes:
        return self._body

    async def text(self, encoding: str = "utf-8", errors: str = "strict") -> str:
        return self._body.decode(encoding, errors=errors)

    async def json(self, content_type: str | None = "application/json") -> Any:
        return _json.loads(self._body.decode("utf-8"))

    async def release(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def __aenter__(self) -> _FakeAiohttpResponse:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False


class MockHttpRouter:
    """兼容 aiohttp 3.14 ClientSession(middlewares=[...]) 的轻量级测试桩路由器。"""

    def __init__(self) -> None:
        self._queues: dict[tuple[str, str], deque[_FakeAiohttpResponse | BaseException]] = (
            defaultdict(deque)
        )
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    def add(
        self,
        method: str,
        url: str,
        *,
        status: int = 200,
        body: bytes | str | None = None,
        payload: Any = None,
        headers: dict[str, str] | None = None,
        exc: BaseException | None = None,
    ) -> None:
        if exc is not None:
            self._queues[(method.upper(), url)].append(exc)
            return
        hdrs = dict(headers or {})
        if payload is not None:
            raw_body = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json; charset=utf-8")
        elif isinstance(body, str):
            raw_body = body.encode("utf-8")
        elif isinstance(body, bytes):
            raw_body = body
        else:
            raw_body = b""
        self._queues[(method.upper(), url)].append(
            _FakeAiohttpResponse(status=status, body=raw_body, headers=hdrs)
        )

    def get(self, url: str, **kwargs: Any) -> None:
        self.add("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> None:
        self.add("POST", url, **kwargs)

    async def middleware(self, req: Any, handler: Any) -> Any:
        method = req.method.upper()
        url = str(req.url)
        self.requests.append((method, url, dict(req.headers)))
        q = self._queues.get((method, url))
        if q:
            item = q.popleft() if len(q) > 1 else q[0]
            if isinstance(item, BaseException):
                raise item
            item.url = req.url
            return item
        err_resp = _FakeAiohttpResponse(status=404, body=f"Unmocked {method} {url}".encode())
        err_resp.url = req.url
        return err_resp


def _install_astrbot_mock() -> None:
    """在未安装 AstrBot 本体的纯测试环境中注入最小兼容桩模块，以便对 main.py 进行端到端测试。"""
    if "astrbot" in sys.modules:
        return

    import logging

    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    filter_mod = types.ModuleType("astrbot.api.event.filter")
    star_mod = types.ModuleType("astrbot.api.star")
    comp_mod = types.ModuleType("astrbot.api.message_components")

    api_mod.logger = logging.getLogger("astrbot_test")
    api_mod.AstrBotConfig = dict

    class EventMessageType(enum.Enum):
        PRIVATE_MESSAGE = "private"
        GROUP_MESSAGE = "group"
        ALL = "all"

    class PlatformAdapterType(enum.Flag):
        AIOCQHTTP = enum.auto()
        TELEGRAM = enum.auto()
        DISCORD = enum.auto()
        ALL = enum.auto()

    class CustomFilter:
        def __init__(self, raise_error: bool = True, **kwargs: Any) -> None:
            self.raise_error = raise_error

        def filter(self, event: Any, cfg: Any) -> bool:
            raise NotImplementedError

    def _noop_decorator(*args: Any, **kwargs: Any):
        def _wrap(fn):
            return fn

        return _wrap

    filter_mod.EventMessageType = EventMessageType
    filter_mod.PlatformAdapterType = PlatformAdapterType
    filter_mod.CustomFilter = CustomFilter
    filter_mod.command = _noop_decorator
    filter_mod.platform_adapter_type = _noop_decorator
    filter_mod.event_message_type = _noop_decorator
    filter_mod.custom_filter = _noop_decorator
    filter_mod.after_message_sent = _noop_decorator

    @dataclass
    class Plain:
        text: str
        type: str = "Plain"

    @dataclass
    class Image:
        file: str
        path: str = ""
        type: str = "Image"

        @staticmethod
        def fromFileSystem(path: str | Path, **_: Any) -> Image:
            fp = Path(path).resolve(strict=False)
            return Image(file=fp.as_uri(), path=str(fp))

        @staticmethod
        def fromBase64(b64: str, **_: Any) -> Image:
            return Image(file=f"base64://{b64}")

        @staticmethod
        def fromURL(url: str, **_: Any) -> Image:
            return Image(file=url)

    @dataclass
    class File:
        name: str
        file: str = ""
        url: str = ""
        type: str = "File"

    @dataclass
    class Node:
        content: list[Any]
        uin: str = "0"
        name: str = ""
        type: str = "Node"

    @dataclass
    class Nodes:
        nodes: list[Node]
        type: str = "Nodes"

    @dataclass
    class Json:
        data: Any
        type: str = "Json"

    @dataclass
    class Share:
        url: str
        title: str = ""
        type: str = "Share"

    comp_mod.Plain = Plain
    comp_mod.Image = Image
    comp_mod.File = File
    comp_mod.Node = Node
    comp_mod.Nodes = Nodes
    comp_mod.Json = Json
    comp_mod.Share = Share

    @dataclass
    class FakeResult:
        kind: str
        payload: Any

    @dataclass
    class FakeMessageObj:
        message: list[Any] = field(default_factory=list)
        raw_message: Any = None

    class AstrMessageEvent:
        def __init__(
            self,
            message_str: str = "",
            *,
            platform_name: str = "aiocqhttp",
            sender_id: str = "user_1",
            self_id: str = "bot_1",
            unified_msg_origin: str = "aiocqhttp:GroupMessage:10001",
            messages: list[Any] | None = None,
            raw_message: Any = None,
        ) -> None:
            self.message_str = message_str
            self._platform_name = platform_name
            self._sender_id = sender_id
            self._self_id = self_id
            self.unified_msg_origin = unified_msg_origin
            self._extras: dict[str, Any] = {}
            self.message_obj = FakeMessageObj(
                message=(
                    messages
                    if messages is not None
                    else ([Plain(message_str)] if message_str else [])
                ),
                raw_message=raw_message,
            )

        def get_platform_name(self) -> str:
            return self._platform_name

        def get_sender_id(self) -> str:
            return self._sender_id

        def get_self_id(self) -> str:
            return self._self_id

        def get_messages(self) -> list[Any]:
            return self.message_obj.message

        def plain_result(self, text: str) -> FakeResult:
            return FakeResult("plain", text)

        def image_result(self, url_or_path: str) -> FakeResult:
            return FakeResult("image", url_or_path)

        def chain_result(self, chain: list[Any]) -> FakeResult:
            return FakeResult("chain", chain)

        def set_extra(self, key: str, val: Any) -> None:
            self._extras[key] = val

        def get_extra(self, key: str, default: Any = None) -> Any:
            return self._extras.get(key, default)

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = filter_mod

    class Context:
        def __init__(self, html_renderer: Any = None, config: dict[str, Any] | None = None) -> None:
            self.html_renderer = html_renderer
            self._cfg = config or {}

        def get_config(self, umo: str | None = None) -> dict[str, Any]:
            return self._cfg

    class Star:
        def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
            self.context = context
            self._kv: dict[str, Any] = {}

        async def get_kv_data(self, key: str, default: Any = None) -> Any:
            return self._kv.get(key, default)

        async def put_kv_data(self, key: str, value: Any) -> None:
            self._kv[key] = value

    class StarTools:
        _base_dir: Path = Path(_TEST_DATA_TMPDIR)

        @classmethod
        def get_data_dir(cls, plugin_name: str = "astrbot_plugin_forum_parser") -> Path:
            p = cls._base_dir / plugin_name
            p.mkdir(parents=True, exist_ok=True)
            return p

    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.StarTools = StarTools

    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = api_mod
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.event.filter"] = filter_mod
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.api.message_components"] = comp_mod


_install_astrbot_mock()
