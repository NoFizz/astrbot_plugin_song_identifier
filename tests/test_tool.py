import os
import tempfile

import pytest

from astrbot.api.message_components import At, Plain, Record, Reply

from astrbot_plugin_song_identifier.main import (
    SongIdentifierPlugin,
    SongInfo,
)


class FakeEnricher:
    async def enrich(self, song):
        return song


class FakeIdentifier:
    def __init__(self, result):
        self.result = result

    async def identify(self, audio_path, session):
        return self.result


class FakeFormatter:
    def format_text(self, song):
        return f"{song.title} - {song.artist}"

    def format_link(self, song):
        if song.song_id:
            return f"🔗 https://music.163.com/song/{song.song_id}"
        return None


class MockEvent:
    def __init__(self, messages, message_str=""):
        self._messages = messages
        self.message_str = message_str
        self.sent = []

    def get_messages(self):
        return self._messages

    def plain_result(self, text):
        return {"type": "plain", "text": text}


def make_tool_plugin(identifier_result):
    plugin = SongIdentifierPlugin.__new__(SongIdentifierPlugin)
    plugin.materializer = FakeMaterializer()
    plugin.identifier = FakeIdentifier(identifier_result)
    plugin.enricher = FakeEnricher()
    plugin.formatter = FakeFormatter()
    plugin.config = {}
    return plugin


class FakeMaterializer:
    async def materialize(self, comp):
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        return path

    async def _probe_duration(self, path):
        return 30.0


@pytest.mark.asyncio
async def test_tool_returns_song_text():
    """LLM 工具：引用媒体 + 识别成功 → 返回歌名/歌手/链接文本。"""
    plugin = make_tool_plugin(
        identifier_result=SongInfo(
            title="晴天", artist="周杰伦", song_id="001", source="netease"
        )
    )
    record = Record(file="x.amr")
    reply = Reply(id="1", chain=[record])
    ev = MockEvent(messages=[reply])
    results = [r async for r in plugin.identify_song(ev)]
    assert len(results) == 1
    text = results[0]["text"]
    assert "晴天 - 周杰伦" in text
    assert "🔗 https://music.163.com/song/001" in text


@pytest.mark.asyncio
async def test_tool_no_media_hint():
    """LLM 工具：无引用媒体 → 返回引导提示。"""
    plugin = make_tool_plugin(
        identifier_result=SongInfo(title="晴天", source="acrcloud")
    )
    ev = MockEvent(messages=[Plain(text="这是什么歌")])
    results = [r async for r in plugin.identify_song(ev)]
    assert len(results) == 1
    assert "没有可识别的媒体" in results[0]["text"]


@pytest.mark.asyncio
async def test_tool_identify_failed_hint():
    """LLM 工具：所有引擎未识别出 → 返回失败提示。"""
    plugin = make_tool_plugin(identifier_result=None)
    record = Record(file="x.amr")
    reply = Reply(id="1", chain=[record])
    ev = MockEvent(messages=[reply])
    results = [r async for r in plugin.identify_song(ev)]
    assert len(results) == 1
    assert "未能识别出歌曲" in results[0]["text"]
