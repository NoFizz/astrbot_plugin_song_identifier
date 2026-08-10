"""结果输出层：文本 / 图片 / OneBot 音乐卡片。

所有输出基于 EnrichedSong；平台链接与卡片只使用对应平台的 ID，
不得交叉构造（网易云 ID 不生成 QQ 链接，反之亦然）。
"""

import io
import os
import re

import aiohttp
from PIL import Image, ImageDraw, ImageFont

from .enrichment import EnrichedSong


def _cfg(config: dict, *keys, default=None):
    node = config
    for key in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(key)
        if node is None:
            return default
    return node


def _load_cjk_font(size: int = 20):
    """加载系统中文字体，失败时回退 Pillow 默认字体。"""
    candidates = [
        "C:/Windows/Fonts/msyh.ttc",  # 微软雅黑
        "C:/Windows/Fonts/simhei.ttf",  # 黑体
        "C:/Windows/Fonts/simsun.ttc",  # 宋体
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",  # Linux
        "/System/Library/Fonts/PingFang.ttc",  # macOS
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


class ResultFormatter:
    """将识别结果格式化为文本或图片。"""

    CARD_WIDTH = 500
    CARD_HEIGHT = 240
    THUMB_SIZE = 240

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def format_text(self, enriched: EnrichedSong) -> str:
        """按用户模板格式化歌曲文本。

        Args:
            enriched: 识别 + 增强结果。

        Returns:
            格式化后的文本；结果为空时返回兜底提示。
        """
        song = enriched.song
        template = (
            _cfg(self.cfg, "output", "text_template", default="{title} - {artist}")
            or "{title} - {artist}"
        )
        title_enabled = _cfg(self.cfg, "output", "title", default=True)
        artist_enabled = _cfg(self.cfg, "output", "artist", default=True)
        values = {
            "title": song.title if (title_enabled and song.title) else "",
            "artist": song.artist if (artist_enabled and song.artist) else "",
            "album": song.album or "",
        }
        text = (
            template.replace("\\r\\n", "\r\n").replace("\\n", "\n").replace("\\t", "\t")
        )
        for key, value in values.items():
            text = text.replace("{" + key + "}", value)
        # 清理空占位符残留的空格与连续 " - "，保留换行
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"(?: ?- ){2,}", " - ", text)
        return text.strip(" -") or "未能获取歌曲信息"

    def format_link(self, enriched: EnrichedSong) -> str | None:
        """生成带图标的试听链接；无可用链接时返回 None。

        Args:
            enriched: 识别 + 增强结果。

        Returns:
            "🔗 <url>" 形式的文本；无链接时返回 None。
        """
        url = enriched.netease_url or enriched.qq_url
        return f"🔗 {url}" if url else None

    async def build_image(self, enriched: EnrichedSong) -> bytes | None:
        """绘制音乐卡片图（封面 + 歌名 + 歌手），失败返回 None。"""
        try:
            canvas = Image.new("RGB", (self.CARD_WIDTH, self.CARD_HEIGHT), "#1a1a2e")
            cover = None
            if enriched.cover_url:
                cover = await self._load_cover(enriched.cover_url)
            if cover is not None:
                cover = cover.resize((self.THUMB_SIZE, self.THUMB_SIZE))
                canvas.paste(cover, (0, 0))
                overlay = Image.new(
                    "RGB",
                    (self.CARD_WIDTH - self.THUMB_SIZE, self.THUMB_SIZE),
                    "#2d2d44",
                )
                canvas.paste(overlay, (self.THUMB_SIZE, 0))
            draw = ImageDraw.Draw(canvas)
            font = _load_cjk_font()
            title = enriched.song.title or "未知歌曲"
            draw.text((self.THUMB_SIZE + 20, 40), title, fill="#ffffff", font=font)
            if enriched.song.artist:
                draw.text(
                    (self.THUMB_SIZE + 20, 100),
                    enriched.song.artist,
                    fill="#bbbbbb",
                    font=font,
                )
            buffer = io.BytesIO()
            canvas.save(buffer, format="JPEG", quality=85)
            return buffer.getvalue()
        except Exception:
            return None

    async def _load_cover(self, url: str):
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
        return Image.open(io.BytesIO(data)).convert("RGB")


class NeteaseCardProvider:
    """网易云音乐卡片：163 卡片（仅需歌曲 ID）。"""

    async def build_music_segment(self, enriched: EnrichedSong) -> dict | None:
        if not enriched.netease_id:
            return None
        return {"type": "music", "data": {"type": "163", "id": enriched.netease_id}}


class QQMusicCardProvider:
    """QQ 音乐卡片：原生 qq 卡片（songmid）。"""

    async def build_music_segment(self, enriched: EnrichedSong) -> dict | None:
        if not enriched.qq_songmid:
            return None
        return {"type": "music", "data": {"type": "qq", "id": enriched.qq_songmid}}


# 配置中文标签 → 卡片 provider
PLATFORM_PROVIDERS = {
    "网易云音乐": NeteaseCardProvider(),
    "QQ音乐": QQMusicCardProvider(),
}
