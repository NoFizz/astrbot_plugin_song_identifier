import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from pathlib import Path

import aiohttp

from ..media import MediaArtifact
from ..models import ErrorKind, RecognitionError, SongInfo


def build_acrcloud_signature(
    access_key: str,
    access_secret: str,
    timestamp: str,
    data_type: str = "audio",
    signature_version: str = "1",
) -> str:
    """Build the documented ACRCloud identification signature."""

    string_to_sign = (
        f"POST\n/v1/identify\n{access_key}\n"
        f"{data_type}\n{signature_version}\n{timestamp}"
    )
    digest = hmac.new(
        access_secret.encode(), string_to_sign.encode(), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode()


def parse_acrcloud_response(
    payload: Mapping, music_key: str = "music", provider: str = "acrcloud"
) -> SongInfo | None:
    """Parse an ACRCloud response or raise a classified provider error."""

    if not isinstance(payload, Mapping):
        raise RecognitionError(
            ErrorKind.PROTOCOL_ERROR,
            provider,
            music_key,
            "response is not an object",
        )
    status = payload.get("status")
    if not isinstance(status, Mapping):
        raise RecognitionError(
            ErrorKind.PROTOCOL_ERROR,
            provider,
            music_key,
            "response status is missing",
        )
    code = status.get("code")
    if code == 1001:
        return None
    if code != 0:
        kind = ErrorKind.PROTOCOL_ERROR
        retryable = False
        if code in {3001, 3014}:
            kind = ErrorKind.AUTH_FAILED
        elif code in {3003, 3015}:
            kind = ErrorKind.RATE_LIMITED
            retryable = code == 3015
        elif code in {3000, 3010}:
            kind = ErrorKind.TEMPORARY_NETWORK
            retryable = True
        elif code in {2000, 2004, 3002, 3006}:
            kind = ErrorKind.INPUT_INVALID
        raise RecognitionError(
            kind,
            provider,
            music_key,
            str(status.get("msg") or "ACRCloud request failed"),
            code,
            retryable,
        )
    metadata = payload.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    results = metadata.get(music_key)
    if not isinstance(results, list) or not results:
        return None
    first = results[0]
    if not isinstance(first, Mapping) or not first.get("title"):
        return None
    artists = first.get("artists")
    artist = None
    if isinstance(artists, list):
        names = [
            str(item["name"])
            for item in artists
            if isinstance(item, Mapping) and item.get("name")
        ]
        artist = ", ".join(names) or None
    album_data = first.get("album")
    album = album_data.get("name") if isinstance(album_data, Mapping) else None
    score = first.get("score")
    return SongInfo(
        title=str(first["title"]),
        artist=artist,
        album=str(album) if album else None,
        provider=provider,
        mode="humming" if music_key == "humming" else "music",
        score=float(score) if isinstance(score, int | float) else None,
        acrid=str(first["acrid"]) if first.get("acrid") else None,
    )


class AcrcloudEngine:
    """ACRCloud native music recognition adapter."""

    provider = "acrcloud"
    mode = "music"

    def __init__(self, host: str, access_key: str, access_secret: str):
        self.host = host.strip()
        self.access_key = access_key.strip()
        self.access_secret = access_secret.strip()

    def is_configured(self) -> bool:
        """Return whether all required credentials are present."""

        return bool(self.host and self.access_key and self.access_secret)

    async def identify(
        self, artifact: MediaArtifact, session: aiohttp.ClientSession, deadline: float
    ) -> SongInfo | None:
        """Identify a normalized audio artifact."""

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
        sample = artifact.path.read_bytes()
        timestamp = str(int(time.time()))
        form = aiohttp.FormData()
        form.add_field("access_key", self.access_key)
        form.add_field(
            "sample",
            sample,
            filename=Path(artifact.path).name,
            content_type="application/octet-stream",
        )
        form.add_field("sample_bytes", str(len(sample)))
        form.add_field("timestamp", timestamp)
        form.add_field(
            "signature",
            build_acrcloud_signature(self.access_key, self.access_secret, timestamp),
        )
        form.add_field("signature_version", "1")
        form.add_field("data_type", "audio")
        url = self.host if self.host.startswith("http") else f"https://{self.host}"
        timeout = max(0.1, deadline - time.monotonic())
        try:
            async with session.post(
                url.rstrip("/") + "/v1/identify",
                data=form,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                text = await response.text()
                if response.status == 401 or response.status == 403:
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
                if response.status >= 400:
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
                ErrorKind.TIMEOUT,
                self.provider,
                self.mode,
                "request timed out",
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
        return parse_acrcloud_response(payload)
