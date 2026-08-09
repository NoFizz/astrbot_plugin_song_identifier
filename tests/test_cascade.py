import pytest
from astrbot_plugin_song_identifier.main import (
    AcrcloudEngine,
    ShazamEngine,
    SongIdentifier,
    SongInfo,
    XfyunAcrEngine,
    XfyunHummingEngine,
    build_engines,
)


class FakeEngine:
    def __init__(self, name, result, configured=True):
        self.name = name
        self.result = result
        self.configured = configured
        self.calls = []

    def is_configured(self):
        return self.configured

    async def identify(self, audio_path, session):
        self.calls.append(self.name)
        return self.result


@pytest.mark.asyncio
async def test_cascade_respects_order():
    e1 = FakeEngine("first", SongInfo(title="A", source="first"))
    e2 = FakeEngine("second", SongInfo(title="B", source="second"))
    identifier = SongIdentifier(engines=[e1, e2], timeout=10)
    info = await identifier.identify("/tmp/fake.wav", object())
    assert info.source == "first"
    assert e1.calls == ["first"]
    assert e2.calls == []


@pytest.mark.asyncio
async def test_cascade_skips_unconfigured_and_falls_back():
    e1 = FakeEngine("unconfigured", None, configured=False)
    e2 = FakeEngine("second", None)
    e3 = FakeEngine("third", SongInfo(title="C", source="third"))
    identifier = SongIdentifier(engines=[e1, e2, e3], timeout=10)
    info = await identifier.identify("/tmp/fake.wav", object())
    assert info.source == "third"
    assert e1.calls == []
    assert e2.calls == ["second"]
    assert e3.calls == ["third"]


@pytest.mark.asyncio
async def test_cascade_all_fail():
    e1 = FakeEngine("a", None)
    e2 = FakeEngine("b", None)
    identifier = SongIdentifier(engines=[e1, e2], timeout=10)
    assert await identifier.identify("/tmp/fake.wav", object()) is None


def make_engines_config(order, shazam_enabled=True):
    return {
        "engines": {
            "order": order,
            "shazam": {"enabled": shazam_enabled},
            "xfyun": {"app_id": "A", "api_key": "K", "api_secret": "S"},
            "acrcloud": {"host": "h", "access_key": "K", "access_secret": "S"},
            "xfyun_humming": {"app_id": "", "api_key": ""},
        },
        "advanced": {"identify_timeout": 30},
    }


def test_build_engines_order(monkeypatch):
    identifier, humming = build_engines(
        make_engines_config("xfyun,acrcloud,shazam")
    )
    engines = identifier.engines
    assert isinstance(engines[0], XfyunAcrEngine)
    assert isinstance(engines[1], AcrcloudEngine)
    assert isinstance(engines[2], ShazamEngine)
    assert isinstance(humming, XfyunHummingEngine)


def test_build_engines_skips_unknown_and_empty(monkeypatch):
    identifier, _ = build_engines(make_engines_config("shazam,,unknown"))
    names = [type(e).__name__ for e in identifier.engines]
    assert names == ["ShazamEngine"]


def test_build_engines_shazam_disabled():
    identifier, _ = build_engines(make_engines_config("xfyun,shazam", shazam_enabled=False))
    names = [type(e).__name__ for e in identifier.engines]
    assert names == ["XfyunAcrEngine"]


def test_build_engines_humming_falls_back_to_xfyun_keys():
    """哼唱引擎未单独配置时复用讯飞 ACRCloud 凭据。"""
    config = {
        "engines": {
            "order": "xfyun",
            "shazam": {"enabled": False},
            "xfyun": {"app_id": "A", "api_key": "K", "api_secret": "S"},
            "acrcloud": {"host": "h", "access_key": "K", "access_secret": "S"},
            "xfyun_humming": {"app_id": "", "api_key": ""},
        },
        "advanced": {"identify_timeout": 30},
    }
    _, humming = build_engines(config)
    assert humming.app_id == "A"
    assert humming.api_key == "K"


@pytest.mark.asyncio
async def test_cascade_with_real_shazam_engine(monkeypatch):
    from shazamio import Shazam

    async def fake_recognize(self, path, **kwargs):
        return {"track": {"title": "T", "subtitle": "A"}}

    monkeypatch.setattr(Shazam, "recognize", fake_recognize)
    identifier = SongIdentifier(engines=[ShazamEngine()], timeout=10)
    info = await identifier.identify("/tmp/fake.wav", None)
    assert info.title == "T"
    assert info.artist == "A"
    assert info.source == "shazam"
