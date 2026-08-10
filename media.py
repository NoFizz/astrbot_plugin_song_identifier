import os
from dataclasses import dataclass
from pathlib import Path

from astrbot.api.message_components import At, File, Record, Reply, Video


class TriggerDetector:
    """判断消息是否触发识曲：关键词 + 引用；群聊需 @bot，私聊无需。

    Args:
        keyword: 触发关键词。
    """

    def __init__(self, keyword: str):
        self.keyword = keyword

    def check(self, event) -> bool:
        """返回消息是否触发识曲。"""
        if self.keyword not in (event.message_str or ""):
            return False
        has_reply = any(isinstance(comp, Reply) for comp in event.get_messages() or [])
        if not has_reply:
            return False
        if event.is_private_chat():
            return True
        for comp in event.get_messages() or []:
            if isinstance(comp, At) and str(comp.qq) == str(event.get_self_id()):
                return True
        return False


try:
    from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
except ImportError:  # pragma: no cover - exercised only with minimal test stubs.
    get_astrbot_temp_path = None


@dataclass(slots=True)
class MediaArtifact:
    """A normalized media file and the files created by the plugin."""

    path: Path
    created_paths: tuple[Path, ...]

    async def cleanup(self) -> None:
        """Remove plugin-created files without touching source media."""

        for path in self.created_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue


@dataclass(slots=True, frozen=True)
class MediaMetadata:
    """Audio metadata needed before sending a provider request."""

    duration: float
    sample_rate: int
    channels: int
    sample_format: str
    size_bytes: int


class MediaExtractor:
    """Extract the first supported media component from a quoted message."""

    MEDIA_TYPES = (Record, Video, File)

    @staticmethod
    def extract_media(event) -> Record | Video | File | None:
        """Return the first supported component in a Reply chain."""

        for component in event.get_messages() or []:
            if not isinstance(component, Reply):
                continue
            for segment in component.chain or []:
                if isinstance(segment, MediaExtractor.MEDIA_TYPES):
                    return segment
        return None


class MediaMaterializer:
    """Materialize and normalize input media for recognition providers."""

    def __init__(self, max_seconds: int = 12, temp_dir: Path | None = None):
        self.max_seconds = max(1, int(max_seconds))
        if temp_dir is not None:
            self.temp_dir = Path(temp_dir)
        elif get_astrbot_temp_path is not None:
            self.temp_dir = Path(get_astrbot_temp_path())
        else:
            self.temp_dir = Path.cwd() / "data" / "temp"

    async def materialize(self, component) -> MediaArtifact | None:
        """Convert a message component to a short mono WAV artifact."""

        source = await self._resolve_source(component)
        if source is None:
            return None
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        output = self.temp_dir / f"songid_{os.getpid()}_{os.urandom(8).hex()}.wav"
        return await self._convert(source, output)

    async def probe(self, path: Path) -> MediaMetadata | None:
        """Read normalized audio metadata using ffprobe."""

        import asyncio

        try:
            process = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=sample_rate,channels,sample_fmt",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await process.communicate()
            if process.returncode != 0:
                return None
            duration, sample_rate, channels, sample_format = (
                stdout.decode().splitlines()
            )
            return MediaMetadata(
                duration=float(duration),
                sample_rate=int(sample_rate),
                channels=int(channels),
                sample_format=sample_format,
                size_bytes=path.stat().st_size,
            )
        except (OSError, ValueError):
            return None

    async def _resolve_source(self, component) -> Path | None:
        if isinstance(component, Record | Video):
            path = await component.convert_to_file_path()
        elif isinstance(component, File):
            path = await component.get_file(allow_return_url=False)
        else:
            return None
        if not path or not Path(path).exists():
            return None
        return Path(path)

    async def _convert(self, source: Path, output: Path) -> MediaArtifact | None:
        import asyncio

        try:
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-i",
                str(source),
                "-t",
                str(self.max_seconds),
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                str(output),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return None

        try:
            await process.wait()
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            output.unlink(missing_ok=True)
            raise
        if process.returncode != 0 or not output.exists():
            output.unlink(missing_ok=True)
            return None
        return MediaArtifact(path=output, created_paths=(output,))
