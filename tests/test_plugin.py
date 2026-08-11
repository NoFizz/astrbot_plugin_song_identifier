"""main.py 编排层测试：触发 → 识别 → 增强 → 输出。"""

import pytest
from astrbot_plugin_song_identifier.enrichment import EnrichedSong
from astrbot_plugin_song_identifier.main import SongIdentifierPlugin
from astrbot_plugin_song_identifier.models import SongInfo

from astrbot.api.message_components import At, Plain, Record, Reply


class _FakeIdentifier:
    def __init__(self, song=None, timed_out=False, errors=()):
        self.song = song
        self.timed_out = timed_out
        self.errors = errors
        self.calls = 0

    async def identify(self, artifact, session):
        self.calls += 1
        from astrbot_plugin_song_identifier.recognition import RecognitionOutcome

        return RecognitionOutcome(
            song=self.song, errors=self.errors, timed_out=self.timed_out
        )


class _FakeMaterializer:
    def __init__(self, ok=True):
        self.ok = ok
        self.max_seconds = 12

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
    import asyncio

    from astrbot_plugin_song_identifier.media import TriggerDetector

    plugin = SongIdentifierPlugin.__new__(SongIdentifierPlugin)
    plugin.config = {"output": {"link": True}}
    plugin._semaphore = asyncio.Semaphore(4)
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
async def test_identify_song_honors_image_output_format():
    """LLM 工具路径也应尊重 output.format：图片模式下发送图片而非纯文本。"""
    plugin = _make_plugin()
    plugin.config = {"output": {"format": "图片", "link": False}}

    record = Record(file="x.amr")
    reply = Reply(id="1", chain=[record])
    ev = _Event(messages=[reply])

    results = [r async for r in plugin.identify_song(ev, target="1")]

    # 成功路径：结果直接发送给用户，工具无 yield 文本
    assert results == []
    # 应发送图片消息（chain 含 Image）
    assert any(
        r.get("type") == "chain" and any(
            type(c).__name__ == "Image" for c in r.get("chain", [])
        )
        for r in ev.sent
    )


def test_plugin_instantiates_with_config_dict():
    """AstrBot 以 __init__(context, config) 实例化插件。

    AstrBot 用 TypeError 探测构造函数是否接受 config：若 __init__ 内部
    抛 TypeError 会被误判并二次调用失败（缺 config）。因此带 config 实例化
    必须成功且不抛 TypeError。
    """
    from astrbot_plugin_song_identifier.main import SongIdentifierPlugin

    class _FakeContext:
        pass

    config = {
        "trigger": {"keyword": "识曲"},
        "engines": {
            "select": {"primary": "ACRCloud", "secondary": "留空", "fallback": "留空"},
            "acrcloud": {"host": "", "access_key": "", "access_secret": ""},
            "xfyun": {"app_id": "", "api_key": "", "api_secret": ""},
        },
        "output": {"format": "文本", "text_template": "{title} - {artist}"},
        "advanced": {"identify_timeout": 60, "audio_max_seconds": 12},
    }
    # 模拟 star_manager.py 的调用路径：先带 config，成功即结束
    plugin = SongIdentifierPlugin(context=_FakeContext(), config=config)

    assert plugin.detector.keyword == "识曲"
    assert plugin.materializer.max_seconds == 12
    assert plugin.enricher is not None
    assert plugin.formatter is not None


@pytest.mark.asyncio
async def test_on_message_full_success_text():
    plugin = _make_plugin()
    ev = _record_event()

    await plugin.on_message(ev)

    assert len(ev.sent) == 2  # 歌曲文本 + 分条试听链接
    assert "晴天 - 周杰伦" in ev.sent[0]["text"]
    assert "🔗" in ev.sent[1]["text"]
    assert ev.stopped is True


def test_identify_song_tool_declares_required_target_param():
    """识别工具必须声明必需参数 target。

    AstrBot skills_like 两阶段工具模式下，阶段2 只下发 name + parameters（描述被剥掉）。
    无参数工具在阶段2 的 parameters 为空、描述为空，LLM 无法确认该不该调用 → 放弃。
    声明必需参数 target 后，阶段2 有参数 schema 可看，LLM 才能确认调用。
    """
    from inspect import getdoc

    from astrbot_plugin_song_identifier.main import SongIdentifierPlugin

    doc = getdoc(SongIdentifierPlugin.identify_song)
    assert "target" in doc, "工具必须声明 target 参数（见 Args 段）"
    assert "string" in doc, "target 参数必须带类型标注 string"


def test_plugin_has_no_shared_last_enriched_state():
    """插件实例不得持有共享的 _last_enriched 字段（并发串结果风险）。"""
    plugin = _make_plugin()
    assert not hasattr(plugin, "_last_enriched")


@pytest.mark.asyncio
async def test_identify_returns_request_local_enriched():
    """_identify 必须返回请求级 enriched，而非写入实例共享字段。"""
    from astrbot_plugin_song_identifier.media import MediaExtractor

    plugin = _make_plugin()
    record = Record(file="x.amr")
    reply = Reply(id="1", chain=[record])
    ev = _Event(messages=[reply])

    result = await plugin._identify(media=MediaExtractor.extract_media(ev))

    # 结果应包含请求级 enriched
    assert result.enriched is not None
    assert result.enriched.song.title == "晴天"
    # 插件实例不应存有任何结果状态
    assert not hasattr(plugin, "_last_enriched")


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
async def test_on_message_failure_logs_engine_reasons(monkeypatch):
    """识别失败时记录各引擎失败原因（provider/mode/kind/code）。"""
    import astrbot_plugin_song_identifier.log as log_module
    from astrbot_plugin_song_identifier.models import ErrorKind, RecognitionError

    warnings = []

    class _FakeLogger:
        def info(self, *a, **k):
            pass

        def warning(self, msg, *args, **kwargs):
            warnings.append(msg % args if args else msg)

        def exception(self, *a, **k):
            pass

    monkeypatch.setattr(log_module, "logger", _FakeLogger())
    plugin = _make_plugin(
        identifier=_FakeIdentifier(
            song=None,
            errors=(
                RecognitionError(
                    ErrorKind.AUTH_FAILED, "acrcloud", "music", "bad key", 3014
                ),
            ),
        )
    )
    ev = _record_event()

    await plugin.on_message(ev)

    joined = " ".join(warnings)
    assert "acrcloud" in joined
    assert "music" in joined
    assert "AUTH_FAILED" in joined or "auth_failed" in joined
    assert "3014" in joined


@pytest.mark.asyncio
async def test_on_message_timeout_hint():
    plugin = _make_plugin(identifier=_FakeIdentifier(song=None, timed_out=True))
    ev = _record_event()

    await plugin.on_message(ev)

    assert len(ev.sent) == 1
    assert "超时" in ev.sent[0]["text"]


@pytest.mark.asyncio
async def test_identify_song_tool_guards_unexpected_exception():
    """LLM 工具路径与 on_message 一致：意外异常转为友好提示，不向工具机制抛异常。"""
    plugin = _make_plugin()

    class _BoomIdentifier:
        async def identify(self, artifact, session):
            raise RuntimeError("boom")

    plugin.identifier = _BoomIdentifier()
    record = Record(file="x.amr")
    reply = Reply(id="1", chain=[record])
    ev = _Event(messages=[reply])

    results = [r async for r in plugin.identify_song(ev, target="1")]

    assert len(results) == 1
    assert "出错" in results[0]["text"]
