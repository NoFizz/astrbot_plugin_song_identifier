"""结果输出层：文本 / 图片 / OneBot 音乐卡片。

所有输出基于 EnrichedSong；平台链接与卡片只使用对应平台的 ID，
不得交叉构造（网易云 ID 不生成 QQ 链接，反之亦然）。
"""

import io
import os
import re

import aiohttp
from PIL import Image, ImageDraw, ImageFilter, ImageFont

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


# 思源黑体（Source Han Sans / Noto Sans CJK）优先，微软雅黑回退
_CJK_FONT_BOLD = [
    "C:/Windows/Fonts/SourceHanSansSC-Bold.otf",
    "C:/Windows/Fonts/NotoSansSC-Bold.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Bold.otf",
    "C:/Windows/Fonts/msyhbd.ttc",  # 微软雅黑 粗体（回退）
    "C:/Windows/Fonts/msyh.ttc",
]
_CJK_FONT_REGULAR = [
    "C:/Windows/Fonts/SourceHanSansSC-Regular.otf",
    "C:/Windows/Fonts/NotoSansSC-Regular.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf",
    "C:/Windows/Fonts/msyh.ttc",  # 微软雅黑（回退）
    "C:/Windows/Fonts/simhei.ttf",  # 黑体（最后兜底）
]


def _load_cjk_font(size: int = 20, bold: bool = False):
    """加载中文字体：思源黑体优先，微软雅黑回退，失败时用 Pillow 默认字体。"""
    candidates = _CJK_FONT_BOLD if bold else _CJK_FONT_REGULAR
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


class ResultFormatter:
    """将识别结果格式化为文本或图片。"""

    CARD_WIDTH = 480
    CARD_HEIGHT = 576  # 压缩底部空白，封面占比升至 ~80%
    THUMB_SIZE = 440  # 封面尺寸：上方约占 3/4 区域
    COVER_MARGIN = 20  # 封面留白

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
        """绘制展示用音乐卡片（上封面 + 下歌名/歌手），失败返回 None。"""
        from . import log

        try:
            cover = None
            if enriched.cover_url:
                log.debug(f"图片: 下载封面 {enriched.cover_url[:60]}")
                cover = await self._load_cover(enriched.cover_url)
                log.debug(f"图片: 封面加载{'成功' if cover else '失败'}")

            canvas = self._render_card(enriched, cover)

            buffer = io.BytesIO()
            canvas.save(buffer, format="JPEG", quality=92)
            log.debug(f"图片: 生成完成 {len(buffer.getvalue())} bytes")
            return buffer.getvalue()
        except Exception as error:
            log.warning(f"图片生成失败: {error}")
            return None

    # ---------------- 卡片绘制 ----------------

    def _render_card(
        self, enriched: EnrichedSong, cover: Image.Image | None
    ) -> Image.Image:
        W, H = self.CARD_WIDTH, self.CARD_HEIGHT

        # 1) 背景：封面放大模糊铺满；无封面时深色渐变兜底
        if cover is not None:
            bg = self._crop_fill(cover.copy(), W, H).filter(
                ImageFilter.GaussianBlur(radius=30)
            )
            canvas = bg.convert("RGBA")
        else:
            canvas = self._vertical_gradient(W, H, (30, 30, 52), (14, 14, 26)).convert(
                "RGBA"
            )

        # 2) 上浅下深的暗色叠加，保证底部文字清晰
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        for y in range(H):
            od.line([(0, y), (W, y)], fill=(10, 10, 22, int(60 + 120 * y / H)))
        canvas = Image.alpha_composite(canvas, overlay)

        cx = (W - self.THUMB_SIZE) // 2
        cy = self.COVER_MARGIN

        # 3) 封面：圆角 + 极柔和投影；无封面画占位块
        if cover is not None:
            thumb = cover.convert("RGB").resize((self.THUMB_SIZE, self.THUMB_SIZE))
            self._paste_cover(canvas, thumb, cx, cy, radius=24)
        else:
            d0 = ImageDraw.Draw(canvas)
            d0.rounded_rectangle(
                (cx, cy, cx + self.THUMB_SIZE, cy + self.THUMB_SIZE),
                radius=24,
                fill=(255, 255, 255, 16),
            )
            note_font = _load_cjk_font(96)
            bbox = d0.textbbox((0, 0), "♪", font=note_font)
            d0.text(
                (
                    cx + (self.THUMB_SIZE - (bbox[2] - bbox[0])) / 2 - bbox[0],
                    cy + (self.THUMB_SIZE - (bbox[3] - bbox[1])) / 2 - bbox[1],
                ),
                "♪",
                fill=(255, 255, 255, 90),
                font=note_font,
            )

        # 4) 底部文字区：歌名（较大）+ 歌手（较小），水平居中、垂直居中于剩余区域
        draw = ImageDraw.Draw(canvas)
        font_title = _load_cjk_font(36, bold=True)
        font_artist = _load_cjk_font(20)

        max_w = W - 80
        title = self._fit_text(
            draw, enriched.song.title or "未知歌曲", font_title, max_w
        )
        artist = (
            self._fit_text(draw, enriched.song.artist, font_artist, max_w)
            if enriched.song.artist
            else None
        )

        tb = draw.textbbox((0, 0), title, font=font_title)
        title_h = tb[3] - tb[1]
        gap = 12
        if artist:
            ab = draw.textbbox((0, 0), artist, font=font_artist)
            artist_h = ab[3] - ab[1]
            total = title_h + gap + artist_h
        else:
            total = title_h

        area_top = cy + self.THUMB_SIZE
        # -2：光学居中，避免小区域里文字显得偏下
        top = area_top + (H - area_top - total) // 2 - 2

        title_w = draw.textlength(title, font=font_title)
        draw.text(
            ((W - title_w) / 2 - tb[0], top - tb[1]),
            title,
            fill=(255, 255, 255, 245),
            font=font_title,
        )

        if artist:
            artist_w = draw.textlength(artist, font=font_artist)
            draw.text(
                ((W - artist_w) / 2 - ab[0], top + title_h + gap - ab[1]),
                artist,
                fill=(255, 255, 255, 160),
                font=font_artist,
            )

        return canvas.convert("RGB")

    # ---------------- 绘制辅助 ----------------

    @staticmethod
    def _crop_fill(img: Image.Image, w: int, h: int) -> Image.Image:
        """按目标比例居中裁切并缩放铺满（cover 语义）。"""
        if img.width / img.height > w / h:
            new_w = int(img.height * w / h)
            left = (img.width - new_w) // 2
            cropped = img.crop((left, 0, left + new_w, img.height))
        else:
            new_h = int(img.width * h / w)
            top = (img.height - new_h) // 2
            cropped = img.crop((0, top, img.width, top + new_h))
        return cropped.resize((w, h), Image.LANCZOS)

    @staticmethod
    def _vertical_gradient(w: int, h: int, top: tuple, bottom: tuple) -> Image.Image:
        img = Image.new("RGB", (w, h))
        d = ImageDraw.Draw(img)
        for y in range(h):
            t = y / h
            d.line(
                [(0, y), (w, y)],
                fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)),
            )
        return img

    @staticmethod
    def _paste_cover(
        canvas: Image.Image, thumb: Image.Image, x: int, y: int, radius: int
    ):
        """圆角封面 + 极柔和投影（无描边）。"""
        s = thumb.size[0]
        pad = 18
        shadow = Image.new("RGBA", (s + pad * 2, s + pad * 2), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (pad, pad, pad + s, pad + s),
            radius=radius,
            fill=(0, 0, 0, 80),
        )
        shadow = shadow.filter(ImageFilter.GaussianBlur(16))
        canvas.alpha_composite(shadow, (x - pad + 4, y - pad + 8))
        mask = Image.new("L", (s, s), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, s - 1, s - 1), radius=radius, fill=255
        )
        canvas.paste(thumb.convert("RGBA"), (x, y), mask)

    @staticmethod
    def _fit_text(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
        """超宽文本截断加省略号，防止溢出卡片。"""
        if draw.textlength(text, font=font) <= max_w:
            return text
        while text and draw.textlength(text + "…", font=font) > max_w:
            text = text[:-1]
        return text + "…"

    async def _load_cover(self, url: str):
        from . import log

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        log.warning(f"图片: 封面 HTTP {resp.status}")
                        return None
                    data = await resp.read()
                    log.debug(f"图片: 封面下载完成 {len(data)} bytes")
            return Image.open(io.BytesIO(data)).convert("RGB")
        except Exception as error:
            log.warning(f"图片: 封面加载异常 {error}")
            return None


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
