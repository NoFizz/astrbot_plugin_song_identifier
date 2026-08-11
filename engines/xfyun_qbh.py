import base64
import hashlib
import json
import time
from collections.abc import Mapping

import aiohttp

from ..models import ErrorKind, RecognitionError, SongInfo


def build_qbh_headers(
    app_id: str, api_key: str, timestamp: str | None = None
) -> dict[str, str]:
    """Build Xfyun qbh headers for the humming-only AFS engine."""

    current = timestamp or str(int(time.time()))
    # Base64 变体说明：官方文档文字写 MIME Base64，官方 Python demo 用
    # urlsafe_b64encode。当前固定参数集的 Base64 恰好不含 +/- 字符，两种编码
    # 等价（golden 测试锁定）。若将来修改 params，必须先验证 standard/urlsafe
    # 编码是否一致，避免鉴权静默失败。
    params = {"engine_type": "afs", "aue": "raw", "sample_rate": "16000"}
    encoded = base64.b64encode(
        json.dumps(params, separators=(",", ":")).encode()
    ).decode()
    checksum = hashlib.md5((api_key + current + encoded).encode()).hexdigest()
    return {
        "X-Appid": app_id,
        "X-CurTime": current,
        "X-Param": encoded,
        "X-CheckSum": checksum,
    }


def parse_qbh_response(payload: Mapping) -> SongInfo | None:
    """Parse a qbh humming response into a provider-neutral result."""

    provider = "xfyun_qbh"
    mode = "humming"
    if not isinstance(payload, Mapping):
        raise RecognitionError(
            ErrorKind.PROTOCOL_ERROR, provider, mode, "response is not an object"
        )
    code = str(payload.get("code"))
    if code != "0":
        kind = ErrorKind.INPUT_INVALID
        if code in {"10105", "102", "10407"}:
            kind = ErrorKind.AUTH_FAILED
        elif code in {"10114", "11200"}:
            kind = ErrorKind.RATE_LIMITED
        raise RecognitionError(
            kind,
            provider,
            mode,
            str(payload.get("desc") or "qbh request failed"),
            code,
        )
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    if not isinstance(first, Mapping) or not first.get("song"):
        return None
    return SongInfo(
        title=str(first["song"]),
        artist=str(first["singer"]) if first.get("singer") else None,
        provider=provider,
        mode=mode,
        acrid=str(first["song_id"]) if first.get("song_id") else None,
    )


class XfyunQbhEngine:
    """Xfyun legacy humming-only qbh adapter configuration."""

    provider = "xfyun_qbh"
    mode = "humming"
    url = "https://webqbh.xfyun.cn/v1/service/v1/qbh"

    def __init__(self, app_id: str, api_key: str):
        self.app_id = app_id.strip()
        self.api_key = api_key.strip()

    def is_configured(self) -> bool:
        """Return whether qbh credentials are present."""

        return bool(self.app_id and self.api_key)

    async def identify(self, artifact, session: aiohttp.ClientSession, deadline: float):
        """Send raw normalized WAV bytes to the qbh humming endpoint."""

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
        audio = artifact.path.read_bytes()
        log.debug(f"qbh 构建请求: 音频 {len(audio)} bytes")
        if len(audio) > 2 * 1024 * 1024:
            raise RecognitionError(
                ErrorKind.INPUT_INVALID,
                self.provider,
                self.mode,
                "audio exceeds the 2 MiB provider limit",
            )
        timeout = max(0.1, deadline - time.monotonic())
        try:
            async with session.post(
                self.url,
                data=audio,
                headers=build_qbh_headers(self.app_id, self.api_key),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                text = await response.text()
                log.debug(f"qbh 响应: HTTP {response.status}, {len(text)} bytes")
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
        song = parse_qbh_response(payload)
        if song is not None:
            log.debug(f"qbh 识别成功: {song.title} - {song.artist or '未知'}")
        else:
            log.debug("qbh 无识别结果（空 data 或业务错误）")
        return song
