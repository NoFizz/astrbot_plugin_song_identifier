from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register


@register("astrbot_plugin_song_identifier", "NoFizz", "引用语音/视频消息识曲插件", "1.0.0")
class SongIdentifierPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
