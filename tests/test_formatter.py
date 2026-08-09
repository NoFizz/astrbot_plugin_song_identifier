import io

import pytest
from PIL import Image, ImageFont

from astrbot_plugin_song_identifier.main import ResultFormatter, SongInfo


def make_cfg(title=True, artist=True, link=True, fmt="text"):
    return {"output": {"title": title, "artist": artist, "link": link, "format": fmt}}


def test_text_with_all_switches():
    fmt = ResultFormatter(make_cfg())
    song = SongInfo(
        title="晴天", artist="周杰伦", audio_url="http://audio/1.mp3", song_id="001"
    )
    text = fmt.format_text(song)
    assert "晴天" in text and "周杰伦" in text
    # 链接不再内嵌于文本（独立消息发送，见 format_link）
    assert "http://" not in text
    assert fmt.format_link(song) == "🔗 http://audio/1.mp3"


def test_text_title_only():
    fmt = ResultFormatter(make_cfg(title=True, artist=False, link=False))
    song = SongInfo(title="晴天", artist="周杰伦", audio_url="http://a.mp3")
    text = fmt.format_text(song)
    assert "晴天" in text
    assert "周杰伦" not in text
    assert "http://" not in text


def test_format_link_fallback_to_qq_page():
    fmt = ResultFormatter(make_cfg(title=False, artist=False))
    song = SongInfo(title="晴天", artist="周杰伦", song_id="001", source="qq")
    assert fmt.format_link(song) == "🔗 https://y.qq.com/n/ryqq/songDetail/001"


def test_format_link_xfyun_source_falls_back_qq_page():
    fmt = ResultFormatter(make_cfg(title=False, artist=False))
    song = SongInfo(title="晴天", artist="周杰伦", song_id="001", source="xfyun")
    assert fmt.format_link(song) == "🔗 https://y.qq.com/n/ryqq/songDetail/001"


def test_format_link_netease_source_uses_music163_page():
    fmt = ResultFormatter(make_cfg(title=False, artist=False))
    song = SongInfo(title="晴天", artist="周杰伦", song_id="001", source="netease")
    assert fmt.format_link(song) == "🔗 https://music.163.com/song/001"


def test_text_empty_song_falls_back():
    fmt = ResultFormatter(make_cfg(title=False, artist=False, link=False))
    assert fmt.format_text(SongInfo()) == "未能获取歌曲信息"


def test_format_link_none_without_id():
    fmt = ResultFormatter(make_cfg())
    assert fmt.format_link(SongInfo(title="晴天")) is None


def test_cjk_font_loader():
    import os

    from astrbot_plugin_song_identifier.main import _load_cjk_font

    candidates = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ]
    if not any(os.path.exists(p) for p in candidates):
        pytest.skip("no system CJK font available")
    assert isinstance(_load_cjk_font(), ImageFont.FreeTypeFont)


@pytest.mark.asyncio
async def test_build_image_returns_jpeg_bytes():
    fmt = ResultFormatter(make_cfg(fmt="image"))
    song = SongInfo(title="晴天", artist="周杰伦")
    data = await fmt.build_image(song)
    assert data is not None
    img = Image.open(io.BytesIO(data))
    assert img.format == "JPEG"
    assert img.width > 100


def test_build_card_payload():
    fmt = ResultFormatter({})
    song = SongInfo(
        title="晴天",
        artist="周杰伦",
        audio_url="http://audio/1.mp3",
        cover_url="http://cover/1.jpg",
    )
    payload = fmt.build_card_payload(song, "123456")
    message = payload["message"][0]
    assert message["type"] == "music"
    assert message["data"]["type"] == "custom"
    assert message["data"]["title"] == "晴天"
    assert message["data"]["singer"] == "周杰伦"
    assert message["data"]["audio"] == "http://audio/1.mp3"
    assert message["data"]["image"] == "http://cover/1.jpg"


def test_build_card_payload_prefers_netease_163():
    """有网易云歌曲 ID 时优先构造 163 卡片（仅需 id，不需要音频直链）。"""
    fmt = ResultFormatter({})
    song = SongInfo(title="晴天", artist="周杰伦", song_id="487527980", source="netease")
    payload = fmt.build_card_payload(song, "123456")
    message = payload["message"][0]
    assert message["type"] == "music"
    assert message["data"]["type"] == "163"
    assert message["data"]["id"] == "487527980"


def test_build_custom_card_payload_uses_netease_outer_url():
    """custom 兜底卡片：有 song_id 时用网易云官方外链试听作为音频地址。"""
    fmt = ResultFormatter({})
    song = SongInfo(title="晴天", artist="周杰伦", song_id="487527980", source="netease")
    payload = fmt.build_custom_card_payload(song, "123456")
    message = payload["message"][0]
    assert message["data"]["type"] == "custom"
    assert (
        message["data"]["audio"]
        == "https://music.163.com/song/media/outer/url?id=487527980.mp3"
    )
    assert message["data"]["url"] == message["data"]["audio"]
