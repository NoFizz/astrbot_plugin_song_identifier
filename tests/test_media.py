from pathlib import Path

import pytest
from astrbot_plugin_song_identifier.media import (
    MediaArtifact,
    MediaExtractor,
    MediaMaterializer,
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
            output = Path(calls[-1][-1])
            output.write_bytes(b"wav")

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
            return b"12.0\n16000\n1\ns16\n", b""

    async def fake_create_process(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_process)

    metadata = await MediaMaterializer(temp_dir=tmp_path).probe(audio)

    assert metadata is not None
    assert metadata.duration == 12.0
    assert metadata.sample_rate == 16000
    assert metadata.channels == 1
    assert metadata.sample_format == "s16"
