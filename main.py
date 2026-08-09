from dataclasses import dataclass

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import At, Reply
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_song_identifier", "NoFizz", "引用语音/视频消息识曲插件", "1.0.0"
)
class SongIdentifierPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config


@dataclass
class SongInfo:
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    cover_url: str | None = None
    audio_url: str | None = None
    song_id: str | None = None
    source: str = ""

    def is_valid(self) -> bool:
        return bool(self.title and self.title.strip())


class TriggerDetector:
    """判断消息触发模式：群聊需 @bot + 关键词 + 引用；私聊只需关键词 + 引用。

    Returns:
        "music"（识曲）/"humming"（哼唱）/ None（不触发）
    """

    def __init__(self, keyword: str, humming_keyword: str = "哼唱"):
        self.keyword = keyword
        self.humming_keyword = humming_keyword

    def check(self, event) -> str | None:
        messages = event.get_messages()
        if not messages:
            return None
        text = event.message_str or ""
        mode = None
        if self.keyword in text:
            mode = "music"
        elif self.humming_keyword in text:
            mode = "humming"
        if mode is None:
            return None
        has_reply = any(isinstance(comp, Reply) for comp in messages)
        if not has_reply:
            return None
        if event.is_private_chat():
            return mode
        for comp in messages:
            if isinstance(comp, At) and str(comp.qq) == str(event.get_self_id()):
                return mode
        return None
