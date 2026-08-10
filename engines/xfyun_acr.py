import asyncio
import base64
import gzip
import hashlib
import hmac
import json
import os
import tempfile
import time
from collections.abc import Mapping
from email.utils import formatdate
from pathlib import Path
from urllib.parse import quote

import aiohttp

from ..models import ErrorKind, RecognitionError, SongInfo
from .acrcloud import parse_acrcloud_response


def build_xfyun_request_body(
    app_id: str, mode: str, audio: bytes, max_audio_base64_bytes: int = 1_048_576
) -> dict:
    """Build one-shot Xfyun ACRCloud music or humming JSON."""

    if mode not in {"music", "humming"}:
        raise RecognitionError(
            ErrorKind.INPUT_INVALID, "xfyun_acr", mode, "unsupported recognition mode"
        )
    encoded = base64.b64encode(audio).decode()
    if len(encoded.encode()) > max_audio_base64_bytes:
        raise RecognitionError(
            ErrorKind.INPUT_INVALID,
            "xfyun_acr",
            mode,
            "base64 audio exceeds the conservative provider limit",
        )
    service = "acr_music" if mode == "music" else "acr_humming"
    return {
        "header": {"app_id": app_id, "status": 3},
        "parameter": {
            service: {
                "mode": mode,
                "output_text": {
                    "encoding": "utf8",
                    "compress": "raw",
                    "format": "json",
                },
            }
        },
        "payload": {
            "data": {
                "encoding": "lame",
                "sample_rate": 16000,
                "channels": 1,
                "bit_depth": 16,
                "status": 3,
                "audio": encoded,
                "frame_size": 0,
            }
        },
    }


def build_xfyun_authorization(
    api_key: str, api_secret: str, host: str, path: str, date: str
) -> str:
    """Build the documented Xfyun HMAC-SHA256 authorization value."""

    origin = f"host: {host}\ndate: {date}\nPOST {path} HTTP/1.1"
    digest = hmac.new(api_secret.encode(), origin.encode(), hashlib.sha256).digest()
    signature = base64.b64encode(digest).decode()
    authorization = (
        f'api_key="{api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{signature}"'
    )
    return base64.b64encode(authorization.encode()).decode()


def decode_xfyun_response(payload: Mapping, mode: str) -> SongInfo | None:
    """Decode Xfyun's outer response and embedded ACRCloud payload."""

    provider = "xfyun_acr"
    if not isinstance(payload, Mapping):
        raise RecognitionError(
            ErrorKind.PROTOCOL_ERROR, provider, mode, "response is not an object"
        )
    header = payload.get("header")
    if not isinstance(header, Mapping):
        raise RecognitionError(
            ErrorKind.PROTOCOL_ERROR, provider, mode, "response header is missing"
        )
    code = header.get("code")
    if code != 0:
        message = str(header.get("message") or "Xfyun request failed")
        lowered = message.lower()
        kind = (
            ErrorKind.AUTH_FAILED
            if any(word in lowered for word in ("app_id", "auth", "signature", "key"))
            else ErrorKind.PROTOCOL_ERROR
        )
        raise RecognitionError(kind, provider, mode, message, code)
    outer_payload = payload.get("payload")
    output = (
        outer_payload.get("output_text") if isinstance(outer_payload, Mapping) else None
    )
    if not isinstance(output, Mapping) or not output.get("text"):
        raise RecognitionError(
            ErrorKind.PROTOCOL_ERROR, provider, mode, "output_text is missing"
        )
    try:
        data = base64.b64decode(str(output["text"]), validate=True)
        compress = str(output.get("compress") or "raw").lower()
        if compress == "gzip":
            data = gzip.decompress(data)
        elif compress != "raw":
            raise ValueError(f"unsupported compression: {compress}")
        encoding = str(output.get("encoding") or "utf8").lower()
        codec = "gb2312" if encoding == "gb2312" else "utf-8"
        text = data.decode(codec)
        output_format = str(output.get("format") or "json").lower()
        if output_format != "json":
            raise ValueError(f"unsupported format: {output_format}")
        inner = json.loads(text)
    except (ValueError, OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RecognitionError(
            ErrorKind.PROTOCOL_ERROR,
            provider,
            mode,
            "embedded response cannot be decoded",
        ) from error
    song = parse_acrcloud_response(
        inner,
        music_key="humming" if mode == "humming" else "music",
        provider=provider,
    )
    if song is not None:
        song.mode = mode
        song.provider_sid = str(header.get("sid")) if header.get("sid") else None
    return song


class XfyunAcrEngine:
    """Configuration holder for one Xfyun ACRCloud recognition mode."""

    HOST = "cn-east-1.api.xf-yun.com"
    PATHS = {
        "music": "/v1/private/s29ebee0d",
        "humming": "/v1/private/s9884ba49",
    }

    def __init__(self, app_id: str, api_key: str, api_secret: str, mode: str):
        if mode not in self.PATHS:
            raise ValueError(f"unsupported Xfyun mode: {mode}")
        self.app_id = app_id.strip()
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.provider = "xfyun_acr"
        self.mode = mode
        self.host = self.HOST
        self.path = self.PATHS[mode]
        self.base_url = f"https://{self.host}"

    def is_configured(self) -> bool:
        """Return whether all required credentials are present."""

        return bool(self.app_id and self.api_key and self.api_secret)

    async def identify(self, artifact, session: aiohttp.ClientSession, deadline: float):
        """Send one-shot MP3 data to the selected Xfyun ACR route."""

        from .. import log

        if not self.is_configured():
            raise RecognitionError(
                ErrorKind.NOT_CONFIGURED,
                self.provider,
                self.mode,
                "provider credentials are incomplete",
            )
        if deadline <= time.monotonic():
            raise RecognitionError(
                ErrorKind.TIMEOUT, self.provider, self.mode, "deadline expired"
            )
        log.debug(f"讯飞({self.mode}) 转换 MP3: {artifact.path.name}")
        mp3_path = await self._to_mp3(artifact.path)
        if mp3_path is None:
            raise RecognitionError(
                ErrorKind.INPUT_INVALID,
                self.provider,
                self.mode,
                "MP3 conversion failed",
            )
        try:
            audio = mp3_path.read_bytes()
            body = build_xfyun_request_body(self.app_id, self.mode, audio)
            log.debug(
                f"讯飞({self.mode}) 请求体: audio_base64={len(body['payload']['data']['audio'])} chars, "
                f"encoding=lame"
            )
            date = formatdate(usegmt=True)
            authorization = build_xfyun_authorization(
                self.api_key, self.api_secret, self.host, self.path, date
            )
            url = (
                f"{self.base_url}{self.path}"
                f"?authorization={quote(authorization)}"
                f"&host={quote(self.host)}&date={quote(date)}"
            )
            timeout = max(0.1, deadline - time.monotonic())
            try:
                async with session.post(
                    url,
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as response:
                    text = await response.text()
                    log.debug(
                        f"讯飞({self.mode}) 响应: HTTP {response.status}, {len(text)} bytes"
                    )
                    if response.status in {401, 403}:
                        raise RecognitionError(
                            ErrorKind.AUTH_FAILED,
                            self.provider,
                            self.mode,
                            "HTTP authentication failed",
                            response.status,
                        )
                    if response.status == 429:
                        raise RecognitionError(
                            ErrorKind.RATE_LIMITED,
                            self.provider,
                            self.mode,
                            "HTTP rate limit exceeded",
                            response.status,
                            True,
                        )
                    if response.status >= 500:
                        raise RecognitionError(
                            ErrorKind.TEMPORARY_NETWORK,
                            self.provider,
                            self.mode,
                            "upstream service error",
                            response.status,
                            True,
                        )
                    if response.status < 200 or response.status >= 300:
                        raise RecognitionError(
                            ErrorKind.PROTOCOL_ERROR,
                            self.provider,
                            self.mode,
                            "unexpected HTTP response",
                            response.status,
                        )
            except RecognitionError:
                raise
            except (TimeoutError, aiohttp.ServerTimeoutError) as error:
                raise RecognitionError(
                    ErrorKind.TIMEOUT, self.provider, self.mode, "request timed out"
                ) from error
            except aiohttp.ClientError as error:
                raise RecognitionError(
                    ErrorKind.TEMPORARY_NETWORK,
                    self.provider,
                    self.mode,
                    type(error).__name__,
                    retryable=True,
                ) from error
            try:
                payload = json.loads(text)
            except ValueError as error:
                raise RecognitionError(
                    ErrorKind.PROTOCOL_ERROR,
                    self.provider,
                    self.mode,
                    "response is not valid JSON",
                ) from error
            song = decode_xfyun_response(payload, self.mode)
            if song is not None:
                log.debug(
                    f"讯飞({self.mode}) 识别成功: {song.title} - {song.artist or '未知'}"
                )
            else:
                log.debug(f"讯飞({self.mode}) 无识别结果")
            return song
        finally:
            mp3_path.unlink(missing_ok=True)

    async def _to_mp3(self, source):
        """Create a real 16 kHz mono MP3 for the Xfyun lame payload."""

        output = (
            Path(tempfile.gettempdir())
            / f"xfyun_{os.getpid()}_{os.urandom(8).hex()}.mp3"
        )
        try:
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-i",
                str(source),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-codec:a",
                "libmp3lame",
                "-b:a",
                "64k",
                str(output),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await process.wait()
            if process.returncode != 0 or not output.exists():
                output.unlink(missing_ok=True)
                return None
            return output
        except asyncio.CancelledError:
            output.unlink(missing_ok=True)
            raise
        except OSError:
            output.unlink(missing_ok=True)
            return None
