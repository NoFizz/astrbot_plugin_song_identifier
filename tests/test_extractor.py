import pytest

from astrbot.api.message_components import File, Plain, Record, Reply, Video


def test_extract_media_none_without_reply(mock_event):
    from astrbot_plugin_song_identifier.main import MediaExtractor

    ev = mock_event(messages=[Plain(text="识曲")])
    assert MediaExtractor.extract_media(ev) is None


def test_extract_media_from_reply_chain(mock_event):
    from astrbot_plugin_song_identifier.main import MediaExtractor

    record = Record(file="record.amr")
    reply = Reply(id="1", chain=[Plain(text="旧消息"), record])
    ev = mock_event(messages=[reply])
    assert MediaExtractor.extract_media(ev) is reply.chain[1]


def test_extract_media_prefers_first_media_segment(mock_event):
    from astrbot_plugin_song_identifier.main import MediaExtractor

    video = Video(file="v.mp4")
    record = Record(file="r.amr")
    reply = Reply(id="1", chain=[video, record])
    ev = mock_event(messages=[reply])
    assert MediaExtractor.extract_media(ev) is reply.chain[0]


@pytest.mark.asyncio
async def test_materialize_record_calls_convert(monkeypatch):
    from astrbot_plugin_song_identifier.main import MediaMaterializer

    mm = MediaMaterializer()

    async def fake_convert(self):
        return "/tmp/out.wav"

    monkeypatch.setattr(Record, "convert_to_file_path", fake_convert)
    record = Record(file="r.amr")
    assert await mm.materialize(record) == "/tmp/out.wav"


@pytest.mark.asyncio
async def test_materialize_video_extracts_audio(monkeypatch):
    from astrbot_plugin_song_identifier.main import MediaMaterializer

    mm = MediaMaterializer()

    async def fake_convert(self):
        return "/tmp/video.mp4"

    async def fake_extract(video_path, out_path):
        assert video_path == "/tmp/video.mp4"
        assert out_path.endswith(".wav")
        return out_path

    monkeypatch.setattr(Video, "convert_to_file_path", fake_convert)
    monkeypatch.setattr(mm, "_extract_audio_from_video", fake_extract)

    video = Video(file="v.mp4")
    result = await mm.materialize(video)
    assert result.endswith(".wav")


@pytest.mark.asyncio
async def test_materialize_file_uses_get_file(monkeypatch):
    from astrbot_plugin_song_identifier.main import MediaMaterializer

    mm = MediaMaterializer()

    async def fake_get_file(self, allow_return_url):
        return "/tmp/music.mp3"

    monkeypatch.setattr(File, "get_file", fake_get_file)
    f = File(name="a.mp3", url="http://x/a.mp3")
    assert await mm.materialize(f) == "/tmp/music.mp3"
