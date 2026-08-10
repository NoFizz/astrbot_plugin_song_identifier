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

        from . import log

        source = await self._resolve_source(component)
        if source is None:
            log.warning("媒体来源解析失败（下载/转码不可用）")
            return None
        log.debug(
            f"开始转换格式: {source.name} → 16kHz 单声道 wav (截取 {self.max_seconds}s)"
        )
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        output = self.temp_dir / f"songid_{os.getpid()}_{os.urandom(8).hex()}.wav"
        artifact = await self._convert(source, output)
        if artifact is None:
            log.warning("ffmpeg 转换失败")
            return None
        metadata = await self.probe(output)
        if metadata:
            log.debug(
                f"转换完成: {artifact.path.name} {metadata.duration:.1f}s, "
                f"{metadata.sample_rate}Hz, {metadata.channels}ch, "
                f"{metadata.size_bytes} bytes"
            )
        else:
            log.debug(f"转换完成: {artifact.path.name}（时长未知）")
        return artifact

    async def probe(self, path: Path) -> MediaMetadata | None:
        """Read normalized audio metadata using ffprobe.

        使用带 key 的输出（key=value 行），按 key 精确取值，
        不依赖 ffprobe 的字段输出顺序（实测 mp4 输出顺序不固定）。
        """
        import asyncio

        from . import log

        try:
            process = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=sample_rate,channels,sample_fmt",
                "-of",
                "default=noprint_wrappers=1",
                str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await process.communicate()
            if process.returncode != 0:
                log.warning(f"ffprobe 探测失败: returncode={process.returncode}")
                return None
            values: dict[str, str] = {}
            for line in stdout.decode().splitlines():
                if "=" in line:
                    key, _, value = line.partition("=")
                    # stream 段可能输出多条，取最后一个有效值
                    if value:
                        values[key.strip()] = value.strip()
            if "duration" not in values:
                log.warning("ffprobe 输出缺少 duration")
                return None
            return MediaMetadata(
                duration=float(values["duration"]),
                sample_rate=int(values.get("sample_rate", 0) or 0),
                channels=int(values.get("channels", 0) or 0),
                sample_format=values.get("sample_fmt", ""),
                size_bytes=path.stat().st_size,
            )
        except (OSError, ValueError) as error:
            log.warning(f"ffprobe 探测异常: {error}")
            return None

    async def _resolve_source(self, component) -> Path | None:
        from . import log

        if isinstance(component, Record | Video):
            kind = "语音" if isinstance(component, Record) else "视频"
            log.debug(f"解析{kind}来源: convert_to_file_path")
            try:
                path = await component.convert_to_file_path()
            except Exception as error:
                log.warning(f"{kind}下载/转码异常: {error}")
                return None
        elif isinstance(component, File):
            log.debug("解析文件来源: get_file")
            try:
                path = await component.get_file(allow_return_url=False)
            except Exception as error:
                log.warning(f"文件下载异常: {error}")
                return None
        else:
            log.warning(f"不支持的媒体段类型: {type(component).__name__}")
            return None
        if not path or not Path(path).exists():
            log.warning(f"媒体来源不存在: {path!r}")
            return None
        log.debug(f"媒体来源就绪: {path}")
        return Path(path)

    async def _convert(self, source: Path, output: Path) -> MediaArtifact | None:
        import asyncio

        from . import log

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
        except OSError as error:
            log.warning(f"无法启动 ffmpeg: {error}")
            return None

        try:
            await process.wait()
        except asyncio.CancelledError:
            log.warning("ffmpeg 转换被取消，清理输出")
            process.kill()
            await process.wait()
            output.unlink(missing_ok=True)
            raise
        if process.returncode != 0 or not output.exists():
            log.warning(f"ffmpeg 转换失败: returncode={process.returncode}")
            output.unlink(missing_ok=True)
            return None
        return MediaArtifact(path=output, created_paths=(output,))
