"""输出层测试：文本/图片/卡片与平台链接隔离。"""

import pytest
from astrbot_plugin_song_identifier.enrichment import EnrichedSong
from astrbot_plugin_song_identifier.models import SongInfo
from astrbot_plugin_song_identifier.output import (
    NeteaseCardProvider,
    QQMusicCardProvider,
    ResultFormatter,
)
from PIL import Image


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


@pytest.mark.asyncio
async def test_load_cover_passes_proxy(monkeypatch):
    """配置代理时封面下载必须传 proxy；未配置时 proxy 为 None（回退环境变量）。"""
    from io import BytesIO

    from PIL import Image

    captured = {}

    class _FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def read(self):
            buf = BytesIO()
            Image.new("RGB", (10, 10), (1, 2, 3)).save(buf, format="JPEG")
            return buf.getvalue()

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self, url, **kwargs):
            captured["kwargs"] = kwargs
            return _FakeResponse()

    import astrbot_plugin_song_identifier.output as output_module

    monkeypatch.setattr(
        output_module.aiohttp, "ClientSession", lambda **kw: _FakeSession()
    )

    fmt = ResultFormatter({"advanced": {"proxy": "http://127.0.0.1:7890"}})
    await fmt._load_cover("https://example.com/cover.jpg")
    assert captured["kwargs"].get("proxy") == "http://127.0.0.1:7890"

    fmt2 = ResultFormatter({"advanced": {}})
    await fmt2._load_cover("https://example.com/cover.jpg")
    assert captured["kwargs"].get("proxy") is None


def test_crop_fill_scales_to_target_size():
    """裁切后必须缩放到目标尺寸（cover 语义），保证合成尺寸一致。"""
    img = Image.new("RGB", (300, 300), (200, 80, 40))
    result = ResultFormatter._crop_fill(img, 600, 300)
    assert result.size == (600, 300)


@pytest.mark.asyncio
async def test_load_cover_sends_referer(monkeypatch):
    """封面下载必须带网易云 Referer，避免 CDN 403。"""
    from io import BytesIO

    from PIL import Image

    captured = {}

    class _FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def read(self):
            buf = BytesIO()
            Image.new("RGB", (10, 10), (1, 2, 3)).save(buf, format="JPEG")
            return buf.getvalue()

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self, url, **kwargs):
            captured["headers"] = kwargs.get("headers", {})
            return _FakeResponse()

    import astrbot_plugin_song_identifier.output as output_module

    monkeypatch.setattr(
        output_module.aiohttp, "ClientSession", lambda **kw: _FakeSession()
    )

    fmt = _formatter()
    cover = await fmt._load_cover("https://example.com/cover.jpg")

    assert cover is not None
    assert captured["headers"].get("Referer") == "https://music.163.com/"


@pytest.mark.asyncio
async def test_load_cover_retries_transient_errors(monkeypatch):
    """封面下载网络异常自动重试（默认 2 次，间隔 2 秒）。"""
    import asyncio
    from io import BytesIO

    from PIL import Image

    real_sleep = asyncio.sleep
    state = {"calls": 0}
    sleeps = []
    monkeypatch.setattr(
        "astrbot_plugin_song_identifier.output.asyncio.sleep",
        lambda s: sleeps.append(s) or real_sleep(0),
    )

    class _FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def read(self):
            buf = BytesIO()
            Image.new("RGB", (10, 10), (1, 2, 3)).save(buf, format="JPEG")
            return buf.getvalue()

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self, url, **kwargs):
            state["calls"] += 1
            if state["calls"] < 3:
                raise ConnectionError("Cannot connect")
            return _FakeResponse()

    import astrbot_plugin_song_identifier.output as output_module

    monkeypatch.setattr(
        output_module.aiohttp, "ClientSession", lambda **kw: _FakeSession()
    )

    fmt = ResultFormatter({"advanced": {}})
    cover = await fmt._load_cover("https://example.com/cover.jpg")

    assert cover is not None
    assert state["calls"] == 3  # 1 次 + 重试 2 次
    assert sleeps == [2.0, 2.0]


@pytest.mark.asyncio
async def test_load_cover_retry_exhausted_returns_none(monkeypatch):
    """重试耗尽仍失败 → 返回 None（占位块降级）。"""
    import asyncio

    real_sleep = asyncio.sleep
    state = {"calls": 0}
    monkeypatch.setattr(
        "astrbot_plugin_song_identifier.output.asyncio.sleep",
        lambda s: real_sleep(0),
    )

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self, url, **kwargs):
            state["calls"] += 1
            raise ConnectionError("Cannot connect")

    import astrbot_plugin_song_identifier.output as output_module

    monkeypatch.setattr(
        output_module.aiohttp, "ClientSession", lambda **kw: _FakeSession()
    )

    fmt = ResultFormatter({"advanced": {}})
    cover = await fmt._load_cover("https://example.com/cover.jpg")

    assert cover is None
    assert state["calls"] == 3


@pytest.mark.asyncio
async def test_load_cover_zero_retry_config_no_retry(monkeypatch):
    """retry_times=0 时封面下载失败不重试。"""
    state = {"calls": 0}

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self, url, **kwargs):
            state["calls"] += 1
            raise ConnectionError("Cannot connect")

    import astrbot_plugin_song_identifier.output as output_module

    monkeypatch.setattr(output_module.aiohttp, "ClientSession", lambda **kw: _FakeSession())

    fmt = ResultFormatter({"advanced": {"retry_times": 0, "retry_interval": 0}})
    cover = await fmt._load_cover("https://example.com/cover.jpg")

    assert cover is None
    assert state["calls"] == 1
