import asyncio
import time
from collections.abc import Mapping

from ..models import ErrorKind, RecognitionError, SongInfo


def parse_shazam_response(payload: Mapping) -> SongInfo | None:
    """Parse a ShazamIO result and use matches as the hit indicator."""

    if not isinstance(payload, Mapping):
        raise RecognitionError(
            ErrorKind.PROTOCOL_ERROR,
            "shazam",
            "music",
            "response is not an object",
        )
    matches = payload.get("matches")
    if not isinstance(matches, list):
        raise RecognitionError(
            ErrorKind.PROTOCOL_ERROR,
            "shazam",
            "music",
            "matches is missing",
        )
    if not matches:
        return None
    track = payload.get("track")
    if not isinstance(track, Mapping) or not track.get("title"):
        raise RecognitionError(
            ErrorKind.PROTOCOL_ERROR,
            "shazam",
            "music",
            "matched response has no track",
        )
    return SongInfo(
        title=str(track["title"]),
        artist=str(track["subtitle"]) if track.get("subtitle") else None,
        provider="shazam",
        mode="music",
        acrid=str(track["key"]) if track.get("key") else None,
    )


class ShazamEngine:
    """ShazamIO recognition adapter."""

    provider = "shazam"
    mode = "music"

    def is_configured(self) -> bool:
        """ShazamIO does not require user credentials."""

        return True

    async def identify(self, artifact, session=None, deadline=None) -> SongInfo | None:
        """Recognize an artifact using ShazamIO's current recognize API."""

        try:
            from shazamio import Shazam

            request = Shazam().recognize(str(artifact.path))
            if deadline is None:
                payload = await request
            else:
                payload = await asyncio.wait_for(
                    request, timeout=max(0.001, deadline - time.monotonic())
                )
        except TimeoutError as error:
            raise RecognitionError(
                ErrorKind.TIMEOUT,
                self.provider,
                self.mode,
                "request timed out",
            ) from error
        except RecognitionError:
            raise
        except (ValueError, TypeError) as error:
            raise RecognitionError(
                ErrorKind.INPUT_INVALID, self.provider, self.mode, type(error).__name__
            ) from error
        except ImportError as error:
            raise RecognitionError(
                ErrorKind.NOT_CONFIGURED,
                self.provider,
                self.mode,
                "shazamio is unavailable",
            ) from error
        except Exception as error:
            raise RecognitionError(
                ErrorKind.TEMPORARY_NETWORK,
                self.provider,
                self.mode,
                type(error).__name__,
                retryable=True,
            ) from error
        return parse_shazam_response(payload)
