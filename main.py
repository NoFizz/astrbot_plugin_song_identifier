import asyncio
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from astrbot.api.message_components import At, File, Record, Reply, Video
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


class MediaExtractor:
    """从引用消息中提取第一个可识别的媒体段。"""

    MEDIA_TYPES = (Record, Video, File)

    @staticmethod
    def extract_media(event):
        """在事件的第一个 Reply 段的 chain 中查找第一个媒体段。

        Args:
            event: AstrBot 消息事件（需提供 get_messages()）。

        Returns:
            找到的第一个 Record/Video/File 段；若无引用消息或链中无媒体段，返回 None。
        """
        messages = event.get_messages() or []
        for comp in messages:
            if not isinstance(comp, Reply):
                continue
            for seg in comp.chain or []:
                if isinstance(seg, MediaExtractor.MEDIA_TYPES):
                    return seg
        return None


class MediaMaterializer:
    """将媒体段落地为本地音频文件（语音→wav，视频→抽音轨wav，文件→原格式）。"""

    async def materialize(self, component) -> str | None:
        """把媒体段落地为本地音频文件路径。

        Args:
            component: Record/Video/File 消息段。

        Returns:
            本地音频文件路径；无法落地时返回 None。
        """
        if isinstance(component, Record):
            return await component.convert_to_file_path()
        if isinstance(component, Video):
            video_path = await component.convert_to_file_path()
            if not video_path:
                return None
            out_path = str(
                Path(tempfile.gettempdir())
                / f"songid_{os.getpid()}_{abs(hash(video_path))}.wav"
            )
            return await self._extract_audio_from_video(video_path, out_path)
        if isinstance(component, File):
            return await component.get_file(allow_return_url=False)
        return None

    async def _extract_audio_from_video(
        self, video_path: str, out_path: str
    ) -> str | None:
        """用 ffmpeg 从视频中抽取音轨为单声道 44.1kHz wav。

        Args:
            video_path: 本地视频文件路径。
            out_path: 输出 wav 文件路径。

        Returns:
            成功时返回 out_path；ffmpeg 失败或输出不存在时返回 None。
        """
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            video_path,
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "44100",
            "-ac",
            "1",
            out_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
        if proc.returncode != 0 or not os.path.exists(out_path):
            return None
        return out_path
