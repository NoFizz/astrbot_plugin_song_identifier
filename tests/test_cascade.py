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


def make_engines_config(primary="", secondary="", fallback=""):
    return {
        "engines": {
            "select": {
                "primary": primary,
                "secondary": secondary,
                "fallback": fallback,
            },
            "xfyun": {"app_id": "A", "api_key": "K", "api_secret": "S"},
            "acrcloud": {"host": "h", "access_key": "K", "access_secret": "S"},
            "xfyun_humming": {"app_id": "", "api_key": ""},
        },
        "advanced": {"identify_timeout": 60},
    }


def test_build_engines_order():
    identifier = build_engines(
        make_engines_config(
            primary="ACRCloud",
            secondary="讯飞开放平台 ACRCloud",
            fallback="Shazam",
        )
    )
    engines = identifier.engines
    assert isinstance(engines[0], AcrcloudEngine)
    assert isinstance(engines[1], XfyunAcrEngine)
    assert isinstance(engines[2], ShazamEngine)


def test_build_engines_skips_empty_slots():
    """空档位跳过：只配置首选，则引擎链只有首选。"""
    identifier = build_engines(make_engines_config(primary="ACRCloud"))
    names = [type(e).__name__ for e in identifier.engines]
    assert names == ["AcrcloudEngine"]


def test_build_engines_all_empty():
    """全部留空 → 空引擎链（识别必然失败，但不报错）。"""
    identifier = build_engines(make_engines_config())
    assert identifier.engines == []


def test_build_engines_unknown_label_skipped():
    """无法识别的标签当作空处理。"""
    identifier = build_engines(make_engines_config(primary="不存在的东西"))
    assert identifier.engines == []


def test_build_engines_humming_slot_uses_xfyun_humming_keys():
    """选中讯飞哼唱识别档位时，哼唱引擎复用讯飞 ACRCloud 凭据。"""
    config = make_engines_config(primary="讯飞开放平台")
    identifier = build_engines(config)
    engines = identifier.engines
    assert len(engines) == 1
    assert isinstance(engines[0], XfyunHummingEngine)
    assert engines[0].app_id == "A"
    assert engines[0].api_key == "K"


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

