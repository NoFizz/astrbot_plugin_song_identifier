import pytest

from astrbot_plugin_song_identifier.main import ShazamEngine


@pytest.mark.asyncio
async def test_shazam_parse(monkeypatch):
    from shazamio import Shazam

    async def fake_recognize(self, path, **kwargs):
        return {"track": {"title": "告白气球", "subtitle": "周杰伦"}}

    monkeypatch.setattr(Shazam, "recognize", fake_recognize)
    engine = ShazamEngine()
    info = await engine.identify("/tmp/fake.wav", None)
    assert info.title == "告白气球"
    assert info.artist == "周杰伦"
    assert info.source == "shazam"


@pytest.mark.asyncio
async def test_shazam_no_result(monkeypatch):
    from shazamio import Shazam

    async def fake_recognize(self, path, **kwargs):
        return None

    monkeypatch.setattr(Shazam, "recognize", fake_recognize)
    engine = ShazamEngine()
    assert await engine.identify("/tmp/fake.wav", None) is None
