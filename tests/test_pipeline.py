import os
import tempfile

import pytest
from astrbot_plugin_song_identifier.main import (
    SongIdentifierPlugin,
    SongInfo,
)

from astrbot.api.message_components import At, Image, Plain, Record, Reply


class FakeApi:
    def __init__(self, should_raise=False):
        self.calls = []
        self.should_raise = should_raise

    async def call_action(self, action, **kwargs):
        if self.should_raise:
            raise RuntimeError("send failed")
        self.calls.append((action, kwargs))


class FakeBot:
    def __init__(self, should_raise=False):
        self.api = FakeApi(should_raise)


class FakeEnricher:
    async def enrich(self, song):
        return song


class FakeIdentifier:
    def __init__(self, result):
        self.result = result

    async def identify(self, audio_path, session):
        return self.result


class MockEvent:
    def __init__(self, messages, message_str="", bot=None):
        self._messages = messages
        self.message_str = message_str
        self.bot = bot
        self.sent = []
        self.stopped = False

    def get_messages(self):
        return self._messages

    def get_self_id(self):
        return "bot-1"

    def get_group_id(self):
        return "g1"

    def get_sender_id(self):
        return "user-1"

    def is_private_chat(self):
        return False

    def plain_result(self, text):
        return {"type": "plain", "text": text}

    def chain_result(self, chain):
        return {"type": "chain", "chain": chain}

    async def send(self, result):
        self.sent.append(result)

    def stop_event(self):
        self.stopped = True


def make_plugin(identifier_result=None):
    from astrbot_plugin_song_identifier.main import (
        MediaMaterializer,
        ResultFormatter,
        TriggerDetector,
    )

    plugin = SongIdentifierPlugin.__new__(SongIdentifierPlugin)
    plugin.config = {
        "trigger": {
            "keyword": "识曲",
        },
        "engines": {
            "select": {
                "primary": "ACRCloud",
                "secondary": "讯飞开放平台 ACRCloud",
                "fallback": "Shazam",
            },
            "xfyun": {"app_id": "A", "api_key": "K", "api_secret": "S"},
            "acrcloud": {"host": "h", "access_key": "K", "access_secret": "S"},
            "xfyun_humming": {"app_id": "A", "api_key": "K"},
        },
        "output": {
            "title": True,
            "artist": True,
            "link": True,
            "format": "文本",
            "card_platforms": {
                "primary": "网易云音乐",
                "secondary": "QQ音乐",
                "fallback": "留空",
            },
        },
        "advanced": {
            "identify_timeout": 60,
            "audio_max_seconds": 30,
        },
    }
    plugin.detector = TriggerDetector("识曲")
    plugin.materializer = MediaMaterializer()
    plugin.enricher = FakeEnricher()
    plugin.identifier = FakeIdentifier(identifier_result)
    plugin.formatter = ResultFormatter(plugin.config)
    return plugin


@pytest.mark.asyncio
async def test_full_pipeline_text_output(monkeypatch):
    plugin = make_plugin(
        identifier_result=SongInfo(title="晴天", artist="周杰伦", source="acrcloud")
    )

    record = Record(file="x.amr")
    reply = Reply(id="1", chain=[record])
    ev = MockEvent(
        messages=[At(qq="bot-1"), Plain(text="识曲"), reply], message_str="识曲"
    )

    async def fake_materialize(self, comp):
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        return path

    from astrbot_plugin_song_identifier.main import MediaMaterializer

    monkeypatch.setattr(MediaMaterializer, "materialize", fake_materialize)

    await plugin.on_message(ev)
    assert len(ev.sent) == 1
    assert "晴天" in ev.sent[0]["text"]
    assert ev.stopped is True


@pytest.mark.asyncio
async def test_pipeline_no_media_gives_hint():
    plugin = make_plugin()
    reply = Reply(id="1", chain=[Plain(text="旧消息")])
    ev = MockEvent(
        messages=[At(qq="bot-1"), Plain(text="识曲"), reply], message_str="识曲"
    )
    await plugin.on_message(ev)
    assert len(ev.sent) == 1
    assert "语音或视频" in ev.sent[0]["text"]


@pytest.mark.asyncio
async def test_pipeline_identify_failed_hint(monkeypatch):
    plugin = make_plugin(identifier_result=None)

    record = Record(file="x.amr")
    reply = Reply(id="1", chain=[record])
    ev = MockEvent(
        messages=[At(qq="bot-1"), Plain(text="识曲"), reply], message_str="识曲"
    )

    async def fake_materialize(self, comp):
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        return path

    from astrbot_plugin_song_identifier.main import MediaMaterializer

    monkeypatch.setattr(MediaMaterializer, "materialize", fake_materialize)

    await plugin.on_message(ev)
    assert len(ev.sent) == 1
    assert "识别" in ev.sent[0]["text"]


@pytest.mark.asyncio
async def test_pipeline_not_trigger_ignored():
    plugin = make_plugin(identifier_result=SongInfo(title="晴天", source="acrcloud"))
    ev = MockEvent(messages=[At(qq="bot-1"), Plain(text="hello")], message_str="hello")
    await plugin.on_message(ev)
    assert ev.sent == []
    assert ev.stopped is False


@pytest.mark.asyncio
async def test_pipeline_image_mode(monkeypatch):
    plugin = make_plugin(
        identifier_result=SongInfo(title="晴天", artist="周杰伦", source="acrcloud")
    )
    plugin.config["output"]["format"] = "图片"

    async def fake_build_image(song):
        return b"FAKEJPEG"

    monkeypatch.setattr(plugin.formatter, "build_image", fake_build_image)

    record = Record(file="x.amr")
    reply = Reply(id="1", chain=[record])
    ev = MockEvent(
        messages=[At(qq="bot-1"), Plain(text="识曲"), reply], message_str="识曲"
    )

    async def fake_materialize(self, comp):
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        return path

    from astrbot_plugin_song_identifier.main import MediaMaterializer

    monkeypatch.setattr(MediaMaterializer, "materialize", fake_materialize)

    await plugin.on_message(ev)
    assert len(ev.sent) == 1
    assert ev.sent[0]["type"] == "chain"
    assert any(isinstance(comp, Image) for comp in ev.sent[0]["chain"])
    assert ev.stopped is True


@pytest.mark.asyncio
async def test_pipeline_card_mode_group_success(monkeypatch):
    """card 模式：首选平台网易云（有 song_id → 163 卡片，无网络请求）发送成功。"""
    plugin = make_plugin(
        identifier_result=SongInfo(
            title="晴天", artist="周杰伦", song_id="487527980", source="netease"
        )
    )
    plugin.config["output"]["format"] = "卡片"

    bot = FakeBot()
    record = Record(file="x.amr")
    reply = Reply(id="1", chain=[record])
    ev = MockEvent(
        messages=[At(qq="bot-1"), Plain(text="识曲"), reply],
        message_str="识曲",
        bot=bot,
    )

    async def fake_materialize(self, comp):
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        return path

    from astrbot_plugin_song_identifier.main import MediaMaterializer

    monkeypatch.setattr(MediaMaterializer, "materialize", fake_materialize)

    await plugin.on_message(ev)
    assert len(bot.api.calls) == 1
    action, payload = bot.api.calls[0]
    assert action == "send_group_msg"
    assert payload["group_id"] == "g1"
    assert payload["message"][0]["data"] == {"type": "163", "id": "487527980"}
    # output_link 独立开关：卡片发送后附加试听链接
    assert len(ev.sent) == 1
    assert "🔗" in ev.sent[0]["text"]


@pytest.mark.asyncio
async def test_pipeline_card_failure_falls_back_to_text(monkeypatch):
    """card 模式：全部平台发送失败时降级为文本。

    仅配置首选平台（网易云 163 卡片，无网络请求），发送失败且无后续
    平台可试 → 降级文本。
    """
    plugin = make_plugin(
        identifier_result=SongInfo(
            title="晴天", artist="周杰伦", song_id="487527980", source="netease"
        )
    )
    plugin.config["output"]["format"] = "卡片"
    plugin.config["output"]["card_platforms"] = {
        "primary": "网易云音乐",
        "secondary": "留空",
        "fallback": "留空",
    }

    bot = FakeBot(should_raise=True)
    record = Record(file="x.amr")
    reply = Reply(id="1", chain=[record])
    ev = MockEvent(
        messages=[At(qq="bot-1"), Plain(text="识曲"), reply],
        message_str="识曲",
        bot=bot,
    )

    async def fake_materialize(self, comp):
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        return path

    from astrbot_plugin_song_identifier.main import MediaMaterializer

    monkeypatch.setattr(MediaMaterializer, "materialize", fake_materialize)

    await plugin.on_message(ev)
    # 卡片降级文本 + 分条试听链接（output.link 默认开启）
    assert len(ev.sent) == 2
    assert "晴天" in ev.sent[0]["text"]
    assert "🔗" in ev.sent[1]["text"]
    assert ev.stopped is True


@pytest.mark.asyncio
async def test_pipeline_materialize_failed_hint(monkeypatch):
    plugin = make_plugin(identifier_result=SongInfo(title="晴天", source="acrcloud"))

    async def fake_materialize(self, comp):
        return None

    from astrbot_plugin_song_identifier.main import MediaMaterializer

    monkeypatch.setattr(MediaMaterializer, "materialize", fake_materialize)

    record = Record(file="x.amr")
    reply = Reply(id="1", chain=[record])
    ev = MockEvent(
        messages=[At(qq="bot-1"), Plain(text="识曲"), reply], message_str="识曲"
    )

    await plugin.on_message(ev)
    # 媒体落地失败：只有提示一条，无歌曲内容与链接
    assert len(ev.sent) == 1
    assert "媒体文件获取失败" in ev.sent[0]["text"]
    assert ev.stopped is True


@pytest.mark.asyncio
async def test_pipeline_image_mode_appends_link_when_enabled(monkeypatch):
    """image 模式 + output_link 开启：图片卡片之后附加试听链接。"""
    plugin = make_plugin(
        identifier_result=SongInfo(
            title="晴天", artist="周杰伦", song_id="001", source="netease"
        )
    )
    plugin.config["output"]["format"] = "图片"

    async def fake_build_image(song):
        return b"FAKEJPEG"

    monkeypatch.setattr(plugin.formatter, "build_image", fake_build_image)

    record = Record(file="x.amr")
    reply = Reply(id="1", chain=[record])
    ev = MockEvent(
        messages=[At(qq="bot-1"), Plain(text="识曲"), reply], message_str="识曲"
    )

    async def fake_materialize(self, comp):
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        return path

    from astrbot_plugin_song_identifier.main import MediaMaterializer

    monkeypatch.setattr(MediaMaterializer, "materialize", fake_materialize)

    await plugin.on_message(ev)
    assert len(ev.sent) == 2
    assert ev.sent[0]["type"] == "chain"
    assert "🔗" in ev.sent[1]["text"]
    assert "https://music.163.com/song/001" in ev.sent[1]["text"]


@pytest.mark.asyncio
async def test_pipeline_image_mode_link_disabled(monkeypatch):
    """image 模式 + output_link 关闭：只发图片，不附加链接。"""
    plugin = make_plugin(
        identifier_result=SongInfo(
            title="晴天", artist="周杰伦", song_id="001", source="netease"
        )
    )
    plugin.config["output"]["format"] = "图片"
    plugin.config["output"]["link"] = False

    async def fake_build_image(song):
        return b"FAKEJPEG"

    monkeypatch.setattr(plugin.formatter, "build_image", fake_build_image)

    record = Record(file="x.amr")
    reply = Reply(id="1", chain=[record])
    ev = MockEvent(
        messages=[At(qq="bot-1"), Plain(text="识曲"), reply], message_str="识曲"
    )

    async def fake_materialize(self, comp):
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        return path

    from astrbot_plugin_song_identifier.main import MediaMaterializer

    monkeypatch.setattr(MediaMaterializer, "materialize", fake_materialize)

    await plugin.on_message(ev)
    assert len(ev.sent) == 1
    assert ev.sent[0]["type"] == "chain"


@pytest.mark.asyncio
async def test_pipeline_text_mode_splits_song_and_link(monkeypatch):
    """text 模式 + 有链接：先发歌曲内容，再分条发送试听链接。"""
    plugin = make_plugin(
        identifier_result=SongInfo(
            title="晴天", artist="周杰伦", song_id="001", source="netease"
        )
    )

    record = Record(file="x.amr")
    reply = Reply(id="1", chain=[record])
    ev = MockEvent(
        messages=[At(qq="bot-1"), Plain(text="识曲"), reply], message_str="识曲"
    )

    async def fake_materialize(self, comp):
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        return path

    from astrbot_plugin_song_identifier.main import MediaMaterializer

    monkeypatch.setattr(MediaMaterializer, "materialize", fake_materialize)

    await plugin.on_message(ev)
    assert len(ev.sent) == 2
    assert "晴天" in ev.sent[0]["text"]
    assert "🔗" in ev.sent[1]["text"]
    assert "https://music.163.com/song/001" in ev.sent[1]["text"]


@pytest.mark.asyncio
async def test_pipeline_text_mode_link_disabled(monkeypatch):
    """text 模式 + 链接关闭：只发歌曲内容一条。"""
    plugin = make_plugin(
        identifier_result=SongInfo(
            title="晴天", artist="周杰伦", song_id="001", source="netease"
        )
    )
    plugin.config["output"]["link"] = False

    record = Record(file="x.amr")
    reply = Reply(id="1", chain=[record])
    ev = MockEvent(
        messages=[At(qq="bot-1"), Plain(text="识曲"), reply], message_str="识曲"
    )

    async def fake_materialize(self, comp):
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        return path

    from astrbot_plugin_song_identifier.main import MediaMaterializer

    monkeypatch.setattr(MediaMaterializer, "materialize", fake_materialize)

    await plugin.on_message(ev)
    assert len(ev.sent) == 1
    assert "晴天" in ev.sent[0]["text"]
    assert "🔗" not in ev.sent[0]["text"]




