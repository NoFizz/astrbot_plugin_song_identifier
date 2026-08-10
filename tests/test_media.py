from pathlib import Path

import pytest
from astrbot_plugin_song_identifier.media import (
    MediaArtifact,
    MediaExtractor,
    MediaMaterializer,
    run_ffmpeg,
)

from astrbot.api.message_components import Plain, Record, Reply


def test_extract_media_from_reply_chain():
    record = Record(file="song.mp3")
    event = type(
        "Event",
        (),
        {
            "get_messages": lambda self: [
                Reply(id="1", chain=[Plain(text="old"), record])
            ]
        },
    )()

    extracted = MediaExtractor.extract_media(event)

    assert isinstance(extracted, Record)
    assert extracted.file == record.file


@pytest.mark.asyncio
async def test_artifact_cleanup_removes_only_created_files(tmp_path):
    source = tmp_path / "source.mp3"
    created = tmp_path / "normalized.wav"
    source.write_bytes(b"source")
    created.write_bytes(b"wav")

    artifact = MediaArtifact(path=created, created_paths=(created,))
    await artifact.cleanup()

    assert not created.exists()
    assert source.exists()


@pytest.mark.asyncio
async def test_artifact_cleanup_is_idempotent(tmp_path):
    created = Path(tmp_path) / "normalized.wav"
    created.write_bytes(b"wav")
    artifact = MediaArtifact(path=created, created_paths=(created,))

    await artifact.cleanup()
    await artifact.cleanup()

    assert not created.exists()


def test_materializer_defaults_to_acrcloud_safe_duration():
    assert MediaMaterializer().max_seconds == 12


def test_materializer_clamps_max_seconds_to_12():
    """媒体时长硬上限 12 秒（ACRCloud 官方只处理前 12 秒）。"""
    assert MediaMaterializer(max_seconds=99).max_seconds == 12
    assert MediaMaterializer(max_seconds=0).max_seconds == 1
    assert MediaMaterializer(max_seconds=-5).max_seconds == 1


@pytest.mark.asyncio
async def test_cleanup_removes_temp_source_but_not_user_file(tmp_path):
    """识别结束清理 AstrBot temp 源文件，但绝不删除用户本地文件。"""
    import asyncio

    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    user_file = tmp_path / "user_recording.amr"
    user_file.write_bytes(b"user data")

    temp_source = temp_dir / "media_audio_20260810_abc.wav"
    temp_source.write_bytes(b"temp source")
    normalized = temp_dir / "songid_test.wav"
    normalized.write_bytes(b"wav")

    materializer = MediaMaterializer(temp_dir=temp_dir)
    artifact = MediaArtifact(
        path=normalized,
        created_paths=(normalized,),
        source_temp_paths=(temp_source,),
    )

    # 手动验证 _is_temp_path 判定：temp 内临时文件 → True；用户文件 → False
    assert materializer._is_temp_path(temp_source) is True
    assert materializer._is_temp_path(user_file) is False

    await artifact.cleanup()

    assert not normalized.exists()
    assert not temp_source.exists()
    assert user_file.exists()  # 用户文件必须保留


@pytest.mark.asyncio
async def test_run_ffmpeg_timeout_terminates_process(monkeypatch):
    """run_ffmpeg 超时必须 terminate 并回收子进程，不遗留。"""
    import asyncio

    class FakeProcess:
        def __init__(self):
            self.terminated = False
            self.killed = False

        async def wait(self):
            if self.terminated or self.killed:
                return 0  # 被终止后回收立即完成
            await asyncio.sleep(10)  # 永不结束，触发超时
            return 0

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

    fake = FakeProcess()

    async def fake_create(*args, **kwargs):
        return fake

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create)

    with pytest.raises(asyncio.TimeoutError):
        await run_ffmpeg(["ffmpeg", "-y"], timeout=0.05)

    assert fake.terminated is True


@pytest.mark.asyncio
async def test_run_ffmpeg_cancellation_terminates_process(monkeypatch):
    """外部取消 run_ffmpeg 必须 terminate 并回收子进程。"""
    import asyncio

    class FakeProcess:
        def __init__(self):
            self.terminated = False
            self.killed = False

        async def wait(self):
            if self.terminated or self.killed:
                return 0
            await asyncio.sleep(10)
            return 0

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

    fake = FakeProcess()

    async def fake_create(*args, **kwargs):
        return fake

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create)

    task = asyncio.create_task(run_ffmpeg(["ffmpeg", "-y"], timeout=30))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert fake.terminated is True


@pytest.mark.asyncio
async def test_materializer_passes_duration_and_audio_shape_to_ffmpeg(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"source")
    calls = []

    class FakeProcess:
        returncode = 0

        async def wait(self):
            # 模拟真实 asyncio.subprocess.Process.wait：返回 returncode
            output = Path(calls[-1][-1])
            output.write_bytes(b"wav")
            return self.returncode

        async def communicate(self):
            # ffprobe 输出: duration / sample_rate / channels / sample_fmt
            return b"12.0\n16000\n1\ns16\n", b""

    async def fake_create_process(*args, **kwargs):
        calls.append(args)
        return FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_process)
    record = Record(file=str(source))

    async def fake_resolve_source(self):
        return str(source)

    monkeypatch.setattr(Record, "convert_to_file_path", fake_resolve_source)
    artifact = await MediaMaterializer(temp_dir=tmp_path).materialize(record)

    assert artifact is not None
    assert "-t" in calls[0]
    assert calls[0][calls[0].index("-t") + 1] == "12"
    assert calls[0][calls[0].index("-ar") + 1] == "16000"
    assert calls[0][calls[0].index("-ac") + 1] == "1"


@pytest.mark.asyncio
async def test_probe_returns_audio_metadata(tmp_path, monkeypatch):
    audio = tmp_path / "normalized.wav"
    audio.write_bytes(b"wav")

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            # 真实 ffprobe 输出（带 key，顺序不固定）
            return b"sample_fmt=s16\nsample_rate=16000\nchannels=1\nduration=12.000000\n", b""

    async def fake_create_process(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_process)

    metadata = await MediaMaterializer(temp_dir=tmp_path).probe(audio)

    assert metadata is not None
    assert metadata.duration == 12.0
    assert metadata.sample_rate == 16000
    assert metadata.channels == 1
    assert metadata.sample_format == "s16"


@pytest.mark.asyncio
async def test_probe_tolerates_reordered_and_extra_lines(tmp_path, monkeypatch):
    """ffprobe 输出顺序不固定（实测 mp4 流字段在前）：按 key 解析应稳定。"""
    audio = tmp_path / "normalized.wav"
    audio.write_bytes(b"wav")

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            # 乱序 + 重复字段（多流时 sample_rate 可能出现多次）
            return (
                b"fltp\n"  # 无 key 行（应忽略）
                b"sample_rate=44100\n"
                b"channels=2\n"
                b"duration=80.363605\n"
                b"sample_rate=16000\n"
                b"sample_fmt=s16\n",
                b"",
            )

    async def fake_create_process(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_process)

    metadata = await MediaMaterializer(temp_dir=tmp_path).probe(audio)

    assert metadata is not None
    assert metadata.duration == 80.363605
    assert metadata.sample_rate == 16000  # 取最后一个有效值
    assert metadata.channels == 2
    assert metadata.sample_format == "s16"
