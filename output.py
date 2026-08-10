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

    CARD_WIDTH = 600
    CARD_HEIGHT = 300
    THUMB_SIZE = 260  # 封面尺寸（与设计稿一致）
    TEXT_MARGIN = 34  # 文字区与封面间距
    FALLBACK_COLORS = ((110, 150, 255), (190, 120, 255))  # 蓝紫兜底

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
        """绘制展示用音乐卡片（封面 + 歌名 + 歌手），失败返回 None。"""
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
        thumb = None

        # 1) 背景：封面放大模糊铺满；无封面时深色渐变兜底
        if cover is not None:
            bg = self._crop_fill(cover.copy(), W, H).filter(
                Image.GaussianBlur(radius=30)
            )
            canvas = bg.convert("RGBA")
        else:
            canvas = self._vertical_gradient(W, H, (30, 30, 52), (14, 14, 26)).convert(
                "RGBA"
            )

        # 2) 左浅右深的暗色叠加，保证右侧文字清晰
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        for x in range(W):
            od.line([(x, 0), (x, H)], fill=(10, 10, 22, int(80 + 110 * x / W)))
        canvas = Image.alpha_composite(canvas, overlay)

        margin = (H - self.THUMB_SIZE) // 2
        tx = ty = margin

        # 3) 封面：圆角 + 极柔和投影（无描边）
        if cover is not None:
            thumb = cover.convert("RGB").resize((self.THUMB_SIZE, self.THUMB_SIZE))
            self._paste_cover(canvas, thumb, tx, ty, radius=16)
        else:
            d0 = ImageDraw.Draw(canvas)
            d0.rounded_rectangle(
                (tx, ty, tx + self.THUMB_SIZE, ty + self.THUMB_SIZE),
                radius=16,
                fill=(255, 255, 255, 16),
            )
            note_font = _load_cjk_font(72)
            bbox = d0.textbbox((0, 0), "♪", font=note_font)
            d0.text(
                (
                    tx + (self.THUMB_SIZE - (bbox[2] - bbox[0])) / 2 - bbox[0],
                    ty + (self.THUMB_SIZE - (bbox[3] - bbox[1])) / 2 - bbox[1],
                ),
                "♪",
                fill=(255, 255, 255, 90),
                font=note_font,
            )

        # 4) 文字区：粗体歌名 + 封面取色渐变装饰线 + 灰色歌手
        draw = ImageDraw.Draw(canvas)
        font_title = _load_cjk_font(30, bold=True)
        font_artist = _load_cjk_font(19)

        text_x = tx + self.THUMB_SIZE + self.TEXT_MARGIN
        max_w = W - text_x - self.TEXT_MARGIN

        title = self._fit_text(
            draw, enriched.song.title or "未知歌曲", font_title, max_w
        )
        draw.text((text_x, 100), title, fill=(255, 255, 255, 245), font=font_title)
        tb = draw.textbbox((0, 0), title, font=font_title)
        line_y = 100 + (tb[3] - tb[1]) + 22

        # 渐变装饰线：颜色直接从封面提取（无封面时用蓝紫兜底）
        accent = self._extract_accent_colors(thumb)
        line_len = min(max_w, 240)
        for i in range(line_len):
            t = i / line_len
            color = self._gradient_pixel(accent, t)
            draw.line(
                [(text_x + i, line_y), (text_x + i, line_y + 2)],
                fill=color,
            )

        if enriched.song.artist:
            artist = self._fit_text(draw, enriched.song.artist, font_artist, max_w)
            draw.text(
                (text_x, line_y + 18),
                artist,
                fill=(255, 255, 255, 160),
                font=font_artist,
            )

        return canvas.convert("RGB")

    # ---------------- 取色与渐变 ----------------

    def _extract_accent_colors(self, cover: Image.Image | None) -> tuple[tuple, tuple]:
        """从封面提取两个高鲜艳度颜色（与 HTML 设计稿同一策略）。

        24×24 缩略图取色 → 按鲜艳度降序 → 第二个颜色要求与第一个色差 > 100
        → 每个颜色提亮 25%。失败或无封面时返回蓝紫兜底。

        Args:
            cover: 封面图像（可为 None）。

        Returns:
            两个 RGB 颜色元组。
        """
        if cover is None:
            return self.FALLBACK_COLORS
        try:
            small = cover.convert("RGB").resize((24, 24))
            if hasattr(small, "get_flattened_data"):
                pixels = list(small.get_flattened_data())
            else:  # Pillow < 12 兼容
                pixels = list(small.getdata())

            def _score(p):
                mx, mn = max(p), min(p)
                return mx * 0.4 + (mx - mn) * 0.6

            def _dist(a, b):
                return sum(abs(x - y) for x, y in zip(a, b))

            def _lift(c, f):
                return tuple(int(v + (255 - v) * f) for v in c)

            pixels.sort(key=_score, reverse=True)
            picked = []
            for p in pixels:
                if all(_dist(p, q) > 100 for q in picked):
                    picked.append(_lift(p, 0.25))
                if len(picked) == 2:
                    break
            if len(picked) == 1:
                picked.append(_lift(picked[0], 0.5))
            return tuple(picked) if len(picked) == 2 else self.FALLBACK_COLORS
        except Exception:
            return self.FALLBACK_COLORS

    @staticmethod
    def _gradient_pixel(accent: tuple[tuple, tuple], t: float) -> tuple:
        """渐变线第 t（0..1）处的 RGBA 颜色。

        0 → 第一色（不透明）；过渡到第二色 85% 不透明度；末尾渐隐为透明。
        """
        c1, c2 = accent
        if t <= 0.55:
            tt = t / 0.55
            r = int(c1[0] + (c2[0] - c1[0]) * tt)
            g = int(c1[1] + (c2[1] - c1[1]) * tt)
            b = int(c1[2] + (c2[2] - c1[2]) * tt)
            a = int(255 - (255 - 217) * tt)  # 第一色 255 → 第二色 217
        else:
            tt = (t - 0.55) / 0.45
            r, g, b = c2
            a = int(217 * (1 - tt))  # 217 → 0 渐隐
        return (r, g, b, max(0, min(255, a)))

    # ---------------- 绘制辅助 ----------------

    @staticmethod
    def _crop_fill(img: Image.Image, w: int, h: int) -> Image.Image:
        """按目标比例居中裁切。"""
        if img.width / img.height > w / h:
            new_w = int(img.height * w / h)
            left = (img.width - new_w) // 2
            return img.crop((left, 0, left + new_w, img.height))
        new_h = int(img.width * h / w)
        top = (img.height - new_h) // 2
        return img.crop((0, top, img.width, top + new_h))

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
        """圆角封面 + 极柔和投影（弱化阴影，无描边）。"""
        s = thumb.size[0]
        pad = 18
        shadow = Image.new("RGBA", (s + pad * 2, s + pad * 2), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (pad, pad, pad + s, pad + s),
            radius=radius,
            fill=(0, 0, 0, 80),
        )
        shadow = shadow.filter(Image.GaussianBlur(16))
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
