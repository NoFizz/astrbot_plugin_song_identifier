import io

import pytest
from PIL import Image, ImageFont

from astrbot_plugin_song_identifier.main import ResultFormatter, SongInfo


def make_cfg(title=True, artist=True, link=True, fmt="文本", text_template=None):
    cfg = {"output": {"title": title, "artist": artist, "link": link, "format": fmt}}
    if text_template:
        cfg["output"]["text_template"] = text_template
    return cfg


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


def test_text_template_with_album():
    """自定义模板：album 占位符在 ACRCloud 系引擎（有专辑名）时生效。"""
    fmt = ResultFormatter(
        make_cfg(text_template="{title} - {artist} - {album}")
    )
    song = SongInfo(title="晴天", artist="周杰伦", album="叶惠美", source="acrcloud")
    assert fmt.format_text(song) == "晴天 - 周杰伦 - 叶惠美"


def test_text_template_ignores_empty_album():
    """非 ACRCloud 系引擎 album 为空：album 占位符被忽略且不留分隔符残留。"""
    fmt = ResultFormatter(
        make_cfg(text_template="{title} - {artist} - {album}")
    )
    song = SongInfo(title="晴天", artist="周杰伦", source="shazam")
    assert fmt.format_text(song) == "晴天 - 周杰伦"


def test_text_template_respects_switches():
    """输出歌名开关关闭时 {title} 占位符被忽略。"""
    fmt = ResultFormatter(
        make_cfg(title=False, text_template="{title} - {artist}")
    )
    song = SongInfo(title="晴天", artist="周杰伦", source="acrcloud")
    assert fmt.format_text(song) == "周杰伦"


def test_text_template_preserves_crlf_newlines():
    """Windows 编辑器保存的 \\r\\n 换行原样保留，不被折叠成空格。"""
    fmt = ResultFormatter(
        make_cfg(text_template="歌名：{title}\r\n歌手：{artist}")
    )
    song = SongInfo(title="晴天", artist="周杰伦", source="acrcloud")
    assert fmt.format_text(song) == "歌名：晴天\r\n歌手：周杰伦"


def test_text_template_middle_placeholder_cleanup():
    """中间的 {album} 为空时，残留的 " - " 分隔符被清理且不破坏其他内容。"""
    fmt = ResultFormatter(
        make_cfg(text_template="{title} - {album} - {artist}")
    )
    song = SongInfo(title="晴天", artist="周杰伦", source="shazam")  # 无专辑
    assert fmt.format_text(song) == "晴天 - 周杰伦"


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
