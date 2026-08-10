"""main.py 编排层测试：触发 → 识别 → 增强 → 输出。"""

import pytest

from astrbot.api.message_components import At, Plain, Record, Reply
from astrbot_plugin_song_identifier.enrichment import EnrichedSong
from astrbot_plugin_song_identifier.main import SongIdentifierPlugin
from astrbot_plugin_song_identifier.models import SongInfo


class _FakeIdentifier:
    def __init__(self, song=None, timed_out=False):
        self.song = song
        self.timed_out = timed_out
        self.calls = 0

    async def identify(self, artifact, session):
        self.calls += 1
        from astrbot_plugin_song_identifier.recognition import RecognitionOutcome

        return RecognitionOutcome(song=self.song, errors=(), timed_out=self.timed_out)


class _FakeMaterializer:
    def __init__(self, ok=True):
        self.ok = ok

    async def materialize(self, component):
        from pathlib import Path

        from astrbot_plugin_song_identifier.media import MediaArtifact

        if not self.ok:
            return None
        path = Path("fake.wav")
        return MediaArtifact(path=path, created_paths=(path,))


class _FakeEnricher:
    def __init__(self):
        self.calls = 0

    async def enrich(self, song):
        self.calls += 1
        return EnrichedSong(song=song, netease_id="123")


class _FakeFormatter:
    def __init__(self):
        self.sent = []

    def format_text(self, enriched):
        return f"{enriched.song.title} - {enriched.song.artist}"

    def format_link(self, enriched):
        return "🔗 https://music.163.com/song/123"

    async def build_image(self, enriched):
        return b"\xff\xd8FAKEJPEG"


class _Event:
    def __init__(self, messages=None, message_str="", private=False):
        self._messages = messages or []
        self.message_str = message_str
        self._private = private
        self.sent = []
        self.stopped = False

    def get_messages(self):
        return self._messages

    def get_self_id(self):
        return "bot-1"

    def get_sender_id(self):
        return "user-1"

    def is_private_chat(self):
        return self._private

    def plain_result(self, text):
        return {"type": "plain", "text": text}

    def chain_result(self, chain):
        return {"type": "chain", "chain": chain}

    async def send(self, result):
        self.sent.append(result)

    def stop_event(self):
        self.stopped = True


def _make_plugin(identifier=None, formatter=None):
    from astrbot_plugin_song_identifier.media import TriggerDetector

    plugin = SongIdentifierPlugin.__new__(SongIdentifierPlugin)
    plugin.config = {"output": {"link": True}}
    plugin.detector = TriggerDetector("识曲")
    plugin.materializer = _FakeMaterializer()
    plugin.identifier = identifier or _FakeIdentifier(
        song=SongInfo(title="晴天", artist="周杰伦", provider="acrcloud", mode="music")
    )
    plugin.enricher = _FakeEnricher()
    plugin.formatter = formatter or _FakeFormatter()
    return plugin


def _record_event():
    record = Record(file="x.amr")
    reply = Reply(id="1", chain=[record])
    return _Event(messages=[At(qq="bot-1"), Plain(text="识曲"), reply], message_str="识曲")


@pytest.mark.asyncio
async def test_on_message_full_success_text():
    plugin = _make_plugin()
    ev = _record_event()

    await plugin.on_message(ev)

    assert len(ev.sent) == 2  # 歌曲文本 + 分条试听链接
    assert "晴天 - 周杰伦" in ev.sent[0]["text"]
    assert "🔗" in ev.sent[1]["text"]
    assert ev.stopped is True


@pytest.mark.asyncio
async def test_on_message_no_media_hint():
    plugin = _make_plugin()
    # 有 Reply 引用段，但链内无媒体
    ev = _Event(
        messages=[At(qq="bot-1"), Plain(text="识曲"), Reply(id="1", chain=[Plain(text="旧消息")])],
        message_str="识曲",
    )

    await plugin.on_message(ev)

    assert len(ev.sent) == 1
    assert "语音或视频" in ev.sent[0]["text"]
    assert ev.stopped is True


@pytest.mark.asyncio
async def test_on_message_materialize_failed_hint():
    plugin = _make_plugin()
    plugin.materializer = _FakeMaterializer(ok=False)
    ev = _record_event()

    await plugin.on_message(ev)

    assert len(ev.sent) == 1
    assert "获取失败" in ev.sent[0]["text"]


@pytest.mark.asyncio
async def test_on_message_not_triggered_ignored():
    plugin = _make_plugin()
    ev = _Event(messages=[At(qq="bot-1"), Plain(text="hello")], message_str="hello")

    await plugin.on_message(ev)

    assert ev.sent == []
    assert ev.stopped is False


@pytest.mark.asyncio
async def test_on_message_identify_no_result_hint():
    plugin = _make_plugin(identifier=_FakeIdentifier(song=None))
    ev = _record_event()

    await plugin.on_message(ev)

    assert len(ev.sent) == 1
    assert "未能识别" in ev.sent[0]["text"]


@pytest.mark.asyncio
async def test_on_message_timeout_hint():
    plugin = _make_plugin(identifier=_FakeIdentifier(song=None, timed_out=True))
    ev = _record_event()

    await plugin.on_message(ev)

    assert len(ev.sent) == 1
    assert "超时" in ev.sent[0]["text"]
