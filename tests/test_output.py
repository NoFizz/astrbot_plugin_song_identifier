"""输出层测试：文本/图片/卡片与平台链接隔离。"""

import io

import pytest
from PIL import Image
from astrbot_plugin_song_identifier.enrichment import EnrichedSong
from astrbot_plugin_song_identifier.models import SongInfo
from astrbot_plugin_song_identifier.output import (
    NeteaseCardProvider,
    QQMusicCardProvider,
    ResultFormatter,
)


def _enriched(netease_id=None, qq_songmid=None, cover=None):
    song = SongInfo(
        title="花の塔",
        artist="さユり",
        album="花の塔",
        provider="acrcloud",
        mode="music",
        cover_url=cover,
    )
    return EnrichedSong(song=song, netease_id=netease_id, qq_songmid=qq_songmid, cover_url=cover)


def _formatter(cfg=None):
    base = {
        "output": {
            "format": "文本",
            "text_template": "{title} - {artist}",
            "title": True,
            "artist": True,
        }
    }
    if cfg:
        base["output"].update(cfg)
    return ResultFormatter(base)


def test_format_text_uses_template():
    fmt = _formatter()
    text = fmt.format_text(_enriched())

    assert text == "花の塔 - さユり"


def test_format_text_respects_title_artist_switches():
    fmt = _formatter({"title": False, "artist": False})
    assert fmt.format_text(_enriched()) == "未能获取歌曲信息"


def test_format_text_cleanup_empty_album():
    fmt = _formatter({"text_template": "{title} - {album} - {artist}"})
    # album 为空（如 Shazam 结果）时，残留的 " - " 应被清理
    song = SongInfo(title="花の塔", artist="さユり", provider="shazam", mode="music")
    text = fmt.format_text(EnrichedSong(song=song))

    assert text == "花の塔 - さユり"


def test_link_uses_only_corresponding_platform():
    fmt = _formatter()

    assert fmt.format_link(_enriched(netease_id="123")) == (
        "🔗 https://music.163.com/song/123"
    )
    assert fmt.format_link(_enriched(qq_songmid="qq")) == (
        "🔗 https://y.qq.com/n/ryqq/songDetail/qq"
    )
    # 只有网易云 ID 时，不得生成 QQ 链接
    assert "y.qq.com" not in fmt.format_link(_enriched(netease_id="123"))
    assert fmt.format_link(_enriched()) is None


@pytest.mark.asyncio
async def test_netease_card_uses_netease_id_only():
    provider = NeteaseCardProvider()
    assert await provider.build_music_segment(_enriched(netease_id="123")) == {
        "type": "music",
        "data": {"type": "163", "id": "123"},
    }
    # 只有 QQ ID 时无法构建网易云卡片
    assert await provider.build_music_segment(_enriched(qq_songmid="qq")) is None


@pytest.mark.asyncio
async def test_qq_card_uses_qq_songmid_only():
    provider = QQMusicCardProvider()
    assert await provider.build_music_segment(_enriched(qq_songmid="qq")) == {
        "type": "music",
        "data": {"type": "qq", "id": "qq"},
    }
    assert await provider.build_music_segment(_enriched(netease_id="123")) is None


@pytest.mark.asyncio
async def test_build_image_returns_jpeg_bytes():
    fmt = _formatter()
    image = await fmt.build_image(_enriched(cover=None))

    assert image is not None
    assert image[:2] == b"\xff\xd8"  # JPEG 文件头


@pytest.mark.asyncio
async def test_build_image_with_cover_succeeds():
    """有封面时图片生成必须成功（回归：GaussianBlur 误用 Image. 前缀导致失败）。"""
    fmt = _formatter()
    enriched = _enriched(cover="https://example.com/cover.jpg")

    # 拦截 _load_cover 返回本地生成封面，避免真实网络请求
    from PIL import Image

    async def fake_load_cover(url):
        return Image.new("RGB", (300, 300), (200, 80, 40))

    fmt._load_cover = fake_load_cover  # type: ignore[assignment]

    image = await fmt.build_image(enriched)

    assert image is not None
    assert image[:2] == b"\xff\xd8"


def test_extract_accent_colors_returns_two_vivid_colors():
    """取色函数从纯色封面提取两个高鲜艳度颜色。"""
    fmt = _formatter()

    # 纯红色封面
    img = Image.new("RGB", (100, 100), (200, 30, 30))
    colors = fmt._extract_accent_colors(img)
    assert len(colors) == 2
    assert all(len(c) == 3 for c in colors)
    # 第一色应为红色系（R 通道显著高于 G/B）
    assert colors[0][0] > colors[0][1] + 50


def test_extract_accent_colors_lifts_dark_colors():
    """暗色封面提取后颜色应被提亮（避免渐变线过暗）。"""
    fmt = _formatter()
    img = Image.new("RGB", (100, 100), (40, 30, 20))
    colors = fmt._extract_accent_colors(img)
    # 提亮后至少一个通道明显大于原色
    assert any(c[0] > 60 for c in colors) or any(c[1] > 60 for c in colors)


def test_extract_accent_colors_fallback_on_failure():
    """异常输入（如空图像）回退到默认蓝紫色。"""
    fmt = _formatter()
    colors = fmt._extract_accent_colors(None)
    assert len(colors) == 2
    assert colors[0][0] >= 100  # 蓝色系兜底


def test_crop_fill_scales_to_target_size():
    """裁切后必须缩放到目标尺寸（cover 语义），保证合成尺寸一致。"""
    img = Image.new("RGB", (300, 300), (200, 80, 40))
    result = ResultFormatter._crop_fill(img, 600, 300)
    assert result.size == (600, 300)
