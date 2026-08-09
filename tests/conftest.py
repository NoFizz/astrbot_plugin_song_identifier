"""
pytest 配置文件
提供路径注入、astrbot 真实导入（失败时回退 stub）与 mock_event 夹具。
"""

import sys
import types
from enum import Flag
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = Path(__file__).resolve().parents[2]
ASTRBOT_ROOT = Path(__file__).resolve().parents[4]

for candidate in (PROJECT_ROOT, PLUGINS_DIR, ASTRBOT_ROOT):
    candidate_str = str(candidate)
    if candidate_str not in sys.path:
        sys.path.insert(0, candidate_str)


def _package(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []
    return module


def _install_astrbot_stubs() -> None:
    """安装满足单元测试所需的最小 AstrBot 桩模块。"""

    class _Filter:
        def command_group(self, *args, **kwargs):
            class _CommandGroup:
                def __call__(self, func):
                    return self

                def command(self, *cmd_args, **cmd_kwargs):
                    return lambda func: func

            return _CommandGroup()

        def command(self, *args, **kwargs):
            return lambda func: func

        def custom_filter(self, *args, **kwargs):
            return lambda func: func

        def event_message_type(self, *args, **kwargs):
            return lambda func: func

        def permission_type(self, *args, **kwargs):
            return lambda func: func

        def platform_adapter_type(self, *args, **kwargs):
            return lambda func: func

        def regex(self, *args, **kwargs):
            return lambda func: func

        def on_llm_request(self):
            return lambda func: func

        def on_llm_response(self):
            return lambda func: func

        def after_message_sent(self):
            return lambda func: func

    class AstrMessageEvent:
        pass

    class Context:
        pass

    class Star:
        pass

    def register(*args, **kwargs):
        return lambda obj: obj

    class _BaseComponent:
        type = "component"

        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    class Plain(_BaseComponent):
        type = "plain"

        def __init__(self, text=""):
            super().__init__(text=text)

    class Image(_BaseComponent):
        type = "image"

        def __init__(self, file=None, **kwargs):
            super().__init__(file=file, **kwargs)

    class Record(_BaseComponent):
        type = "record"

        def __init__(self, file=None, **kwargs):
            super().__init__(file=file, **kwargs)

    class Video(_BaseComponent):
        type = "video"

        def __init__(self, file=None, **kwargs):
            super().__init__(file=file, **kwargs)

    class File(_BaseComponent):
        type = "file"

        def __init__(self, name="", file="", url=""):
            super().__init__(name=name, file_=file, url=url)

        @property
        def file(self):
            return self.file_

    class At(_BaseComponent):
        type = "at"

        def __init__(self, qq="", name=""):
            super().__init__(qq=qq, name=name)

    class Reply(_BaseComponent):
        type = "reply"

        def __init__(self, id="", chain=None, **kwargs):
            super().__init__(id=id, chain=chain if chain is not None else [], **kwargs)

    class EventMessageType(Flag):
        GROUP_MESSAGE = 1
        PRIVATE_MESSAGE = 2
        OTHER_MESSAGE = 4
        ALL = GROUP_MESSAGE | PRIVATE_MESSAGE | OTHER_MESSAGE

    astrbot_mod = _package("astrbot")

    api_mod = _package("astrbot.api")
    astrbot_mod.api = api_mod

    event_mod = _package("astrbot.api.event")
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = _Filter()
    api_mod.event = event_mod

    message_components_mod = types.ModuleType("astrbot.api.message_components")
    message_components_mod.Plain = Plain
    message_components_mod.Image = Image
    message_components_mod.Record = Record
    message_components_mod.Video = Video
    message_components_mod.File = File
    message_components_mod.At = At
    message_components_mod.Reply = Reply

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.register = register
    api_mod.star = star_mod

    core_mod = _package("astrbot.core")
    astrbot_mod.core = core_mod

    core_star_mod = _package("astrbot.core.star")
    core_mod.star = core_star_mod

    core_star_filter_mod = _package("astrbot.core.star.filter")
    core_star_mod.filter = core_star_filter_mod

    event_message_type_mod = types.ModuleType(
        "astrbot.core.star.filter.event_message_type"
    )
    event_message_type_mod.EventMessageType = EventMessageType

    sys.modules.update(
        {
            "astrbot": astrbot_mod,
            "astrbot.api": api_mod,
            "astrbot.api.event": event_mod,
            "astrbot.api.message_components": message_components_mod,
            "astrbot.api.star": star_mod,
            "astrbot.core": core_mod,
            "astrbot.core.star": core_star_mod,
            "astrbot.core.star.filter": core_star_filter_mod,
            "astrbot.core.star.filter.event_message_type": event_message_type_mod,
        }
    )


try:
    import astrbot.api  # type: ignore  # noqa: F401
    from astrbot.core.star.filter.event_message_type import (  # type: ignore  # noqa: F401
        EventMessageType,
    )
except Exception:
    _install_astrbot_stubs()


@pytest.fixture
def mock_event():
    class _Event:
        def __init__(
            self, messages=None, message_str="", self_id="bot-1", group_id="g1"
        ):
            self._messages = messages or []
            self.message_str = message_str
            self._self_id = self_id
            self._group_id = group_id
            self.sent = []
            self.stopped = False

        def get_messages(self):
            return self._messages

        def get_self_id(self):
            return self._self_id

        def get_group_id(self):
            return self._group_id

        def get_sender_id(self):
            return "user-1"

        def is_private_chat(self):
            return not self._group_id

        @property
        def message_str(self):
            if self._message_str:
                return self._message_str
            parts = []
            for comp in self._messages:
                if type(comp).__name__ == "Plain":
                    parts.append(getattr(comp, "text", "") or "")
            return "".join(parts)

        @message_str.setter
        def message_str(self, value):
            self._message_str = value

        def plain_result(self, text):
            return {"type": "plain", "text": text}

        def chain_result(self, chain):
            return {"type": "chain", "chain": chain}

        def image_result(self, url_or_bytes):
            return {"type": "image", "url": url_or_bytes}

        async def send(self, result):
            self.sent.append(result)

        def stop_event(self):
            self.stopped = True

    return _Event
