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
async def test_materialize_record_normalizes(monkeypatch):
    """语音落地：convert 后统一截取/重采样为 wav。"""
    from astrbot_plugin_song_identifier.main import MediaMaterializer

    mm = MediaMaterializer()
    captured = {}

    async def fake_convert(self):
        return "/tmp/out.wav"

    async def fake_normalize(src_path, kind):
        captured["src"] = src_path
        captured["kind"] = kind
        return "/tmp/normalized.wav"

    monkeypatch.setattr(Record, "convert_to_file_path", fake_convert)
    monkeypatch.setattr(mm, "_normalize_to_wav", fake_normalize)

    record = Record(file="r.amr")
    assert await mm.materialize(record) == "/tmp/normalized.wav"
    assert captured["src"] == "/tmp/out.wav"
    assert captured["kind"] == "语音"


@pytest.mark.asyncio
async def test_materialize_video_normalizes(monkeypatch):
    from astrbot_plugin_song_identifier.main import MediaMaterializer

    mm = MediaMaterializer()

    async def fake_convert(self):
        return "/tmp/video.mp4"

    async def fake_normalize(src_path, kind):
        assert src_path == "/tmp/video.mp4"
        assert kind == "视频"
        return "/tmp/normalized.wav"

    monkeypatch.setattr(Video, "convert_to_file_path", fake_convert)
    monkeypatch.setattr(mm, "_normalize_to_wav", fake_normalize)

    video = Video(file="v.mp4")
    result = await mm.materialize(video)
    assert result == "/tmp/normalized.wav"


@pytest.mark.asyncio
async def test_materialize_file_normalizes(monkeypatch):
    """文件落地：下载后统一截取/重采样为 wav。"""
    import tempfile
    from pathlib import Path

    from astrbot_plugin_song_identifier.main import MediaMaterializer

    mm = MediaMaterializer()
    captured = {}

    # 文件必须真实存在（materialize 检查 os.path.exists）
    fd, real_path = tempfile.mkstemp(suffix=".mp3")
    import os

    os.close(fd)

    async def fake_get_file(self, allow_return_url):
        return real_path

    async def fake_normalize(src_path, kind):
        captured["src"] = src_path
        captured["kind"] = kind
        return "/tmp/normalized.wav"

    monkeypatch.setattr(File, "get_file", fake_get_file)
    monkeypatch.setattr(mm, "_normalize_to_wav", fake_normalize)

    f = File(name="a.mp3", url="http://x/a.mp3")
    assert await mm.materialize(f) == "/tmp/normalized.wav"
    assert captured["kind"] == "文件"
    os.unlink(real_path)


@pytest.mark.asyncio
async def test_normalize_limits_duration_and_sample_rate(monkeypatch, tmp_path):
    """统一转换必须限制时长（-t）并降采样到 16k：超长音频（如数分钟视频/文件）

    会生成数十 MB 的 wav，超出识曲引擎的最佳识别窗口与上传限制。
    """
    import asyncio
    from pathlib import Path

    from astrbot_plugin_song_identifier.main import MediaMaterializer

    captured = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        Path(args[-1]).write_bytes(b"")  # 模拟 ffmpeg 生成输出文件（保留存在）

        class FakeProc:
            returncode = 0

            async def wait(self):
                return 0

        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    mm = MediaMaterializer(max_seconds=60)
    result = await mm._normalize_to_wav("video.mp4", "视频")
    args = captured["args"]
    assert result is not None and result.endswith(".wav")
    assert "-t" in args
    assert "60" in args
    assert "-ar" in args
    assert "16000" in args


@pytest.mark.asyncio
async def test_normalize_respects_max_seconds_config(monkeypatch, tmp_path):
    """max_seconds 配置应传给 ffmpeg 的 -t 参数。"""
    import asyncio
    from pathlib import Path

    from astrbot_plugin_song_identifier.main import MediaMaterializer

    captured = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args

        class FakeProc:
            returncode = 0

            async def wait(self):
                return 0

        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    out = str(tmp_path / "out2.wav")
    Path(out).write_bytes(b"")

    mm = MediaMaterializer(max_seconds=90)
    await mm._normalize_to_wav("video.mp4", "视频")
    assert "90" in captured["args"]

