"""Shazam 引擎测试：解析、无结果与异常分类。"""

from pathlib import Path

import pytest

from astrbot_plugin_song_identifier.engines.shazam import ShazamEngine
from astrbot_plugin_song_identifier.media import MediaArtifact
from astrbot_plugin_song_identifier.models import ErrorKind, RecognitionError


def _artifact(tmp_path):
    return MediaArtifact(path=Path(tmp_path) / "fake.wav", created_paths=())


@pytest.mark.asyncio
async def test_shazam_parse(monkeypatch, tmp_path):
    from shazamio import Shazam

    async def fake_recognize(self, path, **kwargs):
        return {
            "matches": [{"id": "1"}],
            "track": {"key": "k1", "title": "告白气球", "subtitle": "周杰伦"},
        }

    monkeypatch.setattr(Shazam, "recognize", fake_recognize)
    engine = ShazamEngine()
    info = await engine.identify(_artifact(tmp_path))
    assert info.title == "告白气球"
    assert info.artist == "周杰伦"
    assert info.provider == "shazam"
    assert info.acrid == "k1"


@pytest.mark.asyncio
async def test_shazam_no_result(monkeypatch, tmp_path):
    from shazamio import Shazam

    async def fake_recognize(self, path, **kwargs):
        return {"matches": []}

    monkeypatch.setattr(Shazam, "recognize", fake_recognize)
    engine = ShazamEngine()
    assert await engine.identify(_artifact(tmp_path)) is None


@pytest.mark.asyncio
async def test_shazam_missing_dependency_classified(monkeypatch, tmp_path):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "shazamio":
            raise ImportError("shazamio not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    engine = ShazamEngine()
    with pytest.raises(RecognitionError) as raised:
        await engine.identify(_artifact(tmp_path))
    assert raised.value.kind is ErrorKind.NOT_CONFIGURED
