"""输出层测试：文本/图片/卡片与平台链接隔离。"""

import pytest
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
    return EnrichedSong(song=song, netease_id=netease_id, qq_songmid=qq_songmid)


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
