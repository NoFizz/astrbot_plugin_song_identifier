import asyncio
import base64
import hashlib
import hmac
import io
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from email.utils import formatdate
from pathlib import Path
from urllib.parse import quote

import aiohttp
import httpx
from PIL import Image, ImageDraw, ImageFont

from astrbot.api import logger
from astrbot.api import message_components as Comp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, File, Record, Reply, Video
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.event_message_type import EventMessageType


def _load_cjk_font(size: int = 20):
    """加载系统中文字体，失败时回退 Pillow 默认字体。

    Args:
        size: 字体大小。

    Returns:
        ImageFont.FreeTypeFont 或默认字体。
    """
    candidates = [
        "C:/Windows/Fonts/msyh.ttc",  # 微软雅黑
        "C:/Windows/Fonts/simhei.ttf",  # 黑体
        "C:/Windows/Fonts/simsun.ttc",  # 宋体
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",  # Linux
        "/System/Library/Fonts/PingFang.ttc",  # macOS
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _cfg(config: dict, *keys, default=None):
    """从嵌套配置中安全取值。

    Args:
        config: 插件配置 dict（可能为嵌套结构）。
        keys: 逐层键路径，如 _cfg(config, "engines", "xfyun", "app_id")。
        default: 任一环节缺失时的默认值。

    Returns:
        命中路径上的值；缺失时返回 default。
    """
    node = config
    for key in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(key)
        if node is None:
            return default
    return node


@dataclass
class SongInfo:
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    cover_url: str | None = None
    audio_url: str | None = None
    song_id: str | None = None
    source: str = ""

    def is_valid(self) -> bool:
        return bool(self.title and self.title.strip())


class TriggerDetector:
    """判断消息是否触发识曲：群聊需 @bot + 关键词 + 引用；私聊只需关键词 + 引用。

    Returns:
        True 表示触发识曲，False 表示不触发。
    """

    def __init__(self, keyword: str):
        self.keyword = keyword

    def check(self, event) -> bool:
        messages = event.get_messages()
        if not messages:
            return False
        text = event.message_str or ""
        if self.keyword not in text:
            return False
        has_reply = any(isinstance(comp, Reply) for comp in messages)
        if not has_reply:
            return False
        if event.is_private_chat():
            return True
        for comp in messages:
            if isinstance(comp, At) and str(comp.qq) == str(event.get_self_id()):
                return True
        return False


class MediaExtractor:
    """从引用消息中提取第一个可识别的媒体段。"""

    MEDIA_TYPES = (Record, Video, File)

    @staticmethod
    def extract_media(event):
        """在事件的第一个 Reply 段的 chain 中查找第一个媒体段。

        Args:
            event: AstrBot 消息事件（需提供 get_messages()）。

        Returns:
            找到的第一个 Record/Video/File 段；若无引用消息或链中无媒体段，返回 None。
        """
        messages = event.get_messages() or []
        for comp in messages:
            if not isinstance(comp, Reply):
                continue
            for seg in comp.chain or []:
                if isinstance(seg, MediaExtractor.MEDIA_TYPES):
                    return seg
        return None


class MediaMaterializer:
    """将媒体段落地为本地音频文件（语音→wav，视频→抽音轨wav，文件→原格式）。"""

    def __init__(self, max_seconds: int = 30):
        """初始化媒体落地器。

        Args:
            max_seconds: 视频抽音轨时的最大时长（秒）。ACRCloud 官方建议
                识别内容小于 12 秒且文件小于 1MB（30s×16k×16bit≈960KB），
                超长音频会生成超大 wav 导致上传受限或识别失败，因此按需
                截取前 N 秒。
        """
        self.max_seconds = max_seconds

    async def materialize(self, component) -> str | None:
        """把媒体段落地为本地音频文件路径。

        Args:
            component: Record/Video/File 消息段。

        Returns:
            本地音频文件路径；无法落地时返回 None。
        """
        if isinstance(component, Record):
            src = (
                f"file={component.file!r}, url={component.url!r}, path={component.path!r}"
            )
            logger.info(f"[识曲] 语音段属性: {src}")
            path = await component.convert_to_file_path()
            if path:
                logger.info(f"[识曲] 语音转码完成: {path}")
            return path
        if isinstance(component, Video):
            video_path = await component.convert_to_file_path()
            if not video_path:
                logger.warning("[识曲] 视频下载失败（convert_to_file_path 返回空）")
                return None
            video_size = os.path.getsize(video_path) if os.path.exists(video_path) else 0
            logger.info(
                f"[识曲] 视频已就绪: {video_path} ({video_size} bytes), "
                f"截取前 {self.max_seconds}s 抽音轨"
            )
            out_path = str(
                Path(tempfile.gettempdir())
                / f"songid_{os.getpid()}_{uuid.uuid4().hex}.wav"
            )
            result = await self._extract_audio_from_video(video_path, out_path)
            if result:
                out_size = (
                    os.path.getsize(result) if os.path.exists(result) else 0
                )
                logger.info(f"[识曲] 音轨提取完成: {result} ({out_size} bytes)")
            return result
        if isinstance(component, File):
            logger.info(
                f"[识曲] 文件段: name={component.name!r}, "
                f"url={component.url!r}, local={component.file_!r}"
            )
            path = await component.get_file(allow_return_url=False)
            if path:
                logger.info(f"[识曲] 文件下载完成: {path}")
            return path
        logger.warning(f"[识曲] 不支持的媒体段类型: {type(component).__name__}")
        return None

    async def _probe_duration(self, path: str) -> float | None:
        """用 ffprobe 探测音频时长（秒）。

        Args:
            path: 本地音频文件路径。

        Returns:
            时长（秒）；探测失败时返回 None。
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            if proc.returncode != 0:
                return None
            return float(out.decode().strip())
        except (OSError, ValueError):
            return None

    async def _extract_audio_from_video(
        self, video_path: str, out_path: str
    ) -> str | None:
        """用 ffmpeg 从视频中抽取音轨：截取前 max_seconds 秒、16k 单声道 wav。

        Args:
            video_path: 本地视频文件路径。
            out_path: 输出 wav 文件路径。

        Returns:
            成功时返回 out_path；ffmpeg 失败或输出不存在时返回 None。
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-i",
                video_path,
                "-t",
                str(self.max_seconds),
                "-vn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                out_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return None
        try:
            await asyncio.wait_for(proc.wait(), timeout=60)
            failed = proc.returncode != 0 or not os.path.exists(out_path)
        except asyncio.TimeoutError:
            proc.kill()
            failed = True
        except asyncio.CancelledError:
            proc.kill()
            try:
                os.unlink(out_path)
            except OSError:
                pass
            raise
        if failed:
            try:
                os.unlink(out_path)
            except OSError:
                pass
            return None
        return out_path


def build_acrcloud_signature(
    access_key: str,
    access_secret: str,
    timestamp: str,
    data_type: str = "audio",
    signature_version: str = "1",
) -> str:
    """构造 ACRCloud V1 识别接口签名（官方算法，docs.acrcloud.cn/api/identification-api.html）。

    签名串格式（换行分隔）：
    ``POST\\n/v1/identify\\n{access_key}\\n{data_type}\\n{signature_version}\\n{timestamp}``
    其中 data_type 为 "audio"，signature_version 为 "1"。

    Args:
        access_key: 控制台中的 access_key。
        access_secret: 控制台中的 access_secret。
        timestamp: 请求时间戳（字符串形式，与表单字段一致）。
        data_type: 识别数据类型，固定 "audio"。
        signature_version: 签名版本，固定 "1"。

    Returns:
        base64 编码的 HMAC-SHA1 签名。
    """
    string_to_sign = (
        f"POST\n/v1/identify\n{access_key}\n"
        f"{data_type}\n{signature_version}\n{timestamp}"
    )
    digest = hmac.new(
        access_secret.encode(), string_to_sign.encode(), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode()


def parse_acrcloud_response(payload: dict) -> SongInfo | None:
    """解析 ACRCloud 格式识别响应，返回歌曲信息；无结果返回 None。"""
    if payload.get("status", {}).get("code") != 0:
        return None
    music = (payload.get("metadata") or {}).get("music") or []
    if not music:
        return None
    first = music[0]
    if not first.get("title"):
        return None
    artists = ", ".join(
        a.get("name", "") for a in (first.get("artists") or []) if a.get("name")
    )
    album = (first.get("album") or {}).get("name") or None
    return SongInfo(
        title=first.get("title"),
        artist=artists or None,
        album=album,
        source="acrcloud",
    )


class AcrcloudEngine:
    """ACRCloud 官方识曲引擎（aiohttp 直接实现 HTTP API + HMAC 签名）。"""

    def __init__(self, host: str, access_key: str, access_secret: str):
        self.host = host
        self.access_key = access_key
        self.access_secret = access_secret

    def is_configured(self) -> bool:
        return bool(self.host and self.access_key and self.access_secret)

    async def identify(self, audio_path: str, session) -> SongInfo | None:
        if not self.is_configured() or not os.path.exists(audio_path):
            return None
        timestamp = str(int(time.time()))
        signature = build_acrcloud_signature(
            self.access_key, self.access_secret, timestamp
        )
        with open(audio_path, "rb") as f:
            sample = f.read()
        form = aiohttp.FormData()
        form.add_field("access_key", self.access_key)
        form.add_field(
            "sample",
            sample,
            filename=Path(audio_path).name,
            content_type="application/octet-stream",
        )
        form.add_field("sample_bytes", str(os.path.getsize(audio_path)))
        form.add_field("timestamp", timestamp)
        form.add_field("signature", signature)
        form.add_field("signature_version", "1")
        form.add_field("data_type", "audio")
        form.add_field("channels", "1")
        url = self.host if self.host.startswith("http") else f"https://{self.host}"
        url = url.rstrip("/") + "/v1/identify"
        logger.info(
            f"[识曲] ACRCloud 请求: {url}, 上传音频 {len(sample)} bytes"
        )
        async with session.post(url, data=form) as resp:
            text = await resp.text()
        logger.info(
            f"[识曲] ACRCloud 响应: HTTP {resp.status}, {len(text)} bytes"
        )
        logger.info(f"[识曲] ACRCloud 响应内容: {text[:200]}")
        try:
            payload = json.loads(text)
        except ValueError:
            # ACRCloud 对超长/无效音频会以 text/plain 返回错误文本而非 JSON
            logger.warning(f"ACRCloud 响应非 JSON: {text[:200]}")
            return None
        return parse_acrcloud_response(payload)


class ShazamEngine:
    """Shazam 备用识曲引擎（shazamio 非官方接口）。"""

    def is_configured(self) -> bool:
        return True

    async def identify(self, audio_path: str, session) -> SongInfo | None:
        try:
            from shazamio import Shazam

            out = await Shazam().recognize(audio_path)
        except Exception:
            return None
        if not out or not out.get("track"):
            return None
        track = out["track"]
        if not track.get("title"):
            return None
        return SongInfo(
            title=track.get("title"),
            artist=track.get("subtitle") or None,
            source="shazam",
        )


def build_xfyun_authorization(
    api_key: str, api_secret: str, host: str, path: str, date: str
) -> str:
    """构造讯飞 HMAC-SHA256 鉴权 authorization 参数（官方算法）。"""
    signature_origin = f"host: {host}\ndate: {date}\nPOST {path} HTTP/1.1"
    signature_sha = hmac.new(
        api_secret.encode(), signature_origin.encode(), hashlib.sha256
    ).digest()
    signature = base64.b64encode(signature_sha).decode()
    authorization_origin = (
        f'api_key="{api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{signature}"'
    )
    return base64.b64encode(authorization_origin.encode()).decode()


def parse_xfyun_acr_response(payload: dict) -> SongInfo | None:
    """解析讯飞 ACRCloud 音乐识别响应（内层为 ACRCloud 格式）。"""
    if payload.get("header", {}).get("code") != 0:
        return None
    text_b64 = (((payload.get("payload") or {}).get("output_text")) or {}).get(
        "text", ""
    )
    if not text_b64:
        return None
    try:
        inner = json.loads(base64.b64decode(text_b64))
    except Exception:
        return None
    info = parse_acrcloud_response(inner)
    if info is not None:
        info.source = "xfyun"
    return info


class XfyunAcrEngine:
    """讯飞 ACRCloud 音乐识别引擎（国内直连，要求 mp3 音频）。"""

    ENDPOINT = "/v1/private/s29ebee0d"
    DEFAULT_HOST = "cn-east-1.api.xf-yun.com"

    def __init__(self, app_id: str, api_key: str, api_secret: str):
        self.app_id = app_id
        self.api_key = api_key
        self.api_secret = api_secret
        self.host = self.DEFAULT_HOST

    def is_configured(self) -> bool:
        return bool(self.app_id and self.api_key and self.api_secret)

    async def identify(self, audio_path: str, session) -> SongInfo | None:
        if not self.is_configured() or not os.path.exists(audio_path):
            return None
        mp3_path = await self._to_mp3(audio_path)
        if not mp3_path:
            return None
        try:
            date = formatdate(usegmt=True)
            authorization = build_xfyun_authorization(
                self.api_key, self.api_secret, self.host, self.ENDPOINT, date
            )
            url = (
                f"https://{self.host}{self.ENDPOINT}"
                f"?authorization={quote(authorization)}"
                f"&host={self.host}&date={quote(date)}"
            )
            if self.host.startswith("http"):
                url = f"{self.host}{self.ENDPOINT}?authorization={quote(authorization)}&host={self.host}&date={quote(date)}"
            with open(mp3_path, "rb") as f:
                audio_b64 = base64.b64encode(f.read()).decode()
            body = {
                "header": {"app_id": self.app_id, "status": 3},
                "parameter": {
                    "acr_music": {
                        "mode": "music",
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
                        "audio": audio_b64,
                        "frame_size": 0,
                    }
                },
            }
            async with session.post(url, json=body) as resp:
                text = await resp.text()
            logger.info(
                f"[识曲] 讯飞音乐识别请求完成: HTTP {resp.status}, "
                f"{len(text)} bytes"
            )
            logger.info(f"[识曲] 讯飞音乐识别响应: {text[:200]}")
            try:
                payload = json.loads(text)
            except ValueError:
                logger.warning(f"讯飞音乐识别响应非 JSON: {text[:200]}")
                return None
            return parse_xfyun_acr_response(payload)
        finally:
            try:
                os.remove(mp3_path)
            except OSError:
                pass

    async def _to_mp3(self, wav_path: str) -> str | None:
        """wav → 16k 单声道 mp3（lame），供讯飞接口使用。"""
        mp3_path = str(
            Path(tempfile.gettempdir()) / f"xfyun_{os.getpid()}_{uuid.uuid4().hex}.mp3"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-i",
                wav_path,
                "-codec:a",
                "libmp3lame",
                "-ar",
                "16000",
                "-ac",
                "1",
                "-b:a",
                "64k",
                mp3_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return None
        try:
            await asyncio.wait_for(proc.wait(), timeout=60)
            failed = proc.returncode != 0 or not os.path.exists(mp3_path)
        except asyncio.TimeoutError:
            proc.kill()
            failed = True
        except asyncio.CancelledError:
            proc.kill()
            try:
                os.unlink(mp3_path)
            except OSError:
                pass
            raise
        if failed:
            try:
                os.unlink(mp3_path)
            except OSError:
                pass
            return None
        return mp3_path


def build_qbh_headers(app_id: str, api_key: str) -> dict:
    """构造讯飞 qbh 哼唱识别请求头（官方算法）。

    Args:
        app_id: 讯飞开放平台应用 ID。
        api_key: 讯飞开放平台 API Key。

    Returns:
        X-Appid/X-CurTime/X-Param/X-CheckSum 四个请求头组成的字典。
    """
    curtime = str(int(time.time()))
    param = {"aue": "raw", "sample_rate": "16000"}
    x_param = base64.b64encode(json.dumps(param).encode()).decode()
    checksum = hashlib.md5((api_key + curtime + x_param).encode()).hexdigest()
    return {
        "X-Appid": app_id,
        "X-CurTime": curtime,
        "X-Param": x_param,
        "X-CheckSum": checksum,
    }


def parse_qbh_response(payload: dict) -> SongInfo | None:
    """解析讯飞 qbh 哼唱识别响应。

    Args:
        payload: 讯飞 qbh 接口返回的 JSON 响应。

    Returns:
        命中时返回第一条歌曲的 SongInfo；错误码或空 data 时返回 None。
    """
    if str(payload.get("code")) != "0":
        return None
    data = payload.get("data") or []
    if not data:
        return None
    first = data[0]
    if not first.get("song"):
        return None
    return SongInfo(
        title=first.get("song"),
        artist=first.get("singer") or None,
        song_id=str(first.get("song_id") or "") or None,
        source="xfyun_humming",
    )


class XfyunHummingEngine:
    """讯飞 qbh 哼唱识别引擎（哼唱旋律识别）。"""

    DEFAULT_URL = "https://webqbh.xfyun.cn/v1/service/v1/qbh"

    def __init__(self, app_id: str, api_key: str):
        self.app_id = app_id
        self.api_key = api_key
        self.url = self.DEFAULT_URL

    def is_configured(self) -> bool:
        return bool(self.app_id and self.api_key)

    async def identify(self, audio_path: str, session) -> SongInfo | None:
        """哼唱音频识别：先重采样为 16k 单声道 wav，再上传讯飞 qbh 接口。

        Args:
            audio_path: 本地音频文件路径。
            session: aiohttp ClientSession。

        Returns:
            命中时返回 SongInfo；未配置、文件不存在或转换失败时返回 None。
        """
        if not self.is_configured() or not os.path.exists(audio_path):
            return None
        wav_path = await self._to_16k_wav(audio_path)
        if not wav_path:
            return None
        try:
            headers = build_qbh_headers(self.app_id, self.api_key)
            with open(wav_path, "rb") as f:
                audio_bytes = f.read()
            async with session.post(
                self.url, data=audio_bytes, headers=headers
            ) as resp:
                text = await resp.text()
            logger.info(
                f"[识曲] 讯飞哼唱识别请求完成: HTTP {resp.status}, "
                f"上传 {len(audio_bytes)} bytes, 响应 {len(text)} bytes"
            )
            logger.info(f"[识曲] 讯飞哼唱识别响应: {text[:200]}")
            try:
                payload = json.loads(text)
            except ValueError:
                logger.warning(f"讯飞哼唱识别响应非 JSON: {text[:200]}")
                return None
            return parse_qbh_response(payload)
        finally:
            try:
                os.remove(wav_path)
            except OSError:
                pass

    async def _to_16k_wav(self, wav_path: str) -> str | None:
        """重采样为 16k 单声道 16bit wav，供讯飞哼唱接口使用。

        Args:
            wav_path: 输入音频文件路径。

        Returns:
            成功时返回临时 wav 路径（调用方负责清理）；失败时返回 None。
        """
        out_path = str(
            Path(tempfile.gettempdir()) / f"qbh_{os.getpid()}_{uuid.uuid4().hex}.wav"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-i",
                wav_path,
                "-ar",
                "16000",
                "-ac",
                "1",
                "-sample_fmt",
                "s16",
                out_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return None
        try:
            await asyncio.wait_for(proc.wait(), timeout=60)
            failed = proc.returncode != 0 or not os.path.exists(out_path)
        except asyncio.TimeoutError:
            proc.kill()
            failed = True
        except asyncio.CancelledError:
            proc.kill()
            try:
                os.unlink(out_path)
            except OSError:
                pass
            raise
        if failed:
            try:
                os.unlink(out_path)
            except OSError:
                pass
            return None
        return out_path


class SongIdentifier:
    """多引擎级联识别：按配置顺序依次尝试，返回第一个成功结果。"""

    def __init__(self, engines: list, timeout: float):
        self.engines = engines
        self.timeout = timeout

    async def identify(self, audio_path: str, session) -> SongInfo | None:
        async def _run() -> SongInfo | None:
            for engine in self.engines:
                name = type(engine).__name__
                try:
                    if not engine.is_configured():
                        logger.info(f"[识曲] 引擎 {name} 未配置，跳过")
                        continue
                    logger.info(f"[识曲] 尝试引擎 {name} ...")
                    info = await engine.identify(audio_path, session)
                    if info is not None:
                        logger.info(
                            f"[识曲] 引擎 {name} 识别成功: "
                            f"{info.title} - {info.artist or '未知歌手'}"
                        )
                        return info
                    logger.info(f"[识曲] 引擎 {name} 无结果，尝试下一引擎")
                except Exception as e:
                    logger.warning(f"[识曲] 引擎 {name} 失败: {e}")
                    continue
            logger.warning("[识曲] 所有引擎均未识别出歌曲")
            return None

        return await asyncio.wait_for(_run(), timeout=self.timeout)


def build_engines(config: dict) -> SongIdentifier:
    """按配置（首选/次选/备选三档）构造引擎链。

    Args:
        config: 插件配置 dict（嵌套结构：engines.select.primary 等）。

    Returns:
        SongIdentifier：按 首选→次选→备选 顺序排列的级联识别器。
    """
    engines_cfg = _cfg(config, "engines", default={}) or {}
    instances = {
        "acrcloud": AcrcloudEngine(
            host=_cfg(engines_cfg, "acrcloud", "host", default="") or "",
            access_key=_cfg(engines_cfg, "acrcloud", "access_key", default="")
            or "",
            access_secret=_cfg(engines_cfg, "acrcloud", "access_secret", default="")
            or "",
        ),
        "xfyun": XfyunAcrEngine(
            app_id=_cfg(engines_cfg, "xfyun", "app_id", default="") or "",
            api_key=_cfg(engines_cfg, "xfyun", "api_key", default="") or "",
            api_secret=_cfg(engines_cfg, "xfyun", "api_secret", default="") or "",
        ),
        "xfyun_humming": XfyunHummingEngine(
            app_id=(
                _cfg(engines_cfg, "xfyun_humming", "app_id", default="")
                or _cfg(engines_cfg, "xfyun", "app_id", default="")
                or ""
            ),
            api_key=(
                _cfg(engines_cfg, "xfyun_humming", "api_key", default="")
                or _cfg(engines_cfg, "xfyun", "api_key", default="")
                or ""
            ),
        ),
        "shazam": ShazamEngine(),
    }
    # 配置下拉选项（中文标签）→ 引擎标识；"无"或未知标签视为留空跳过
    label_to_key = {
        "ACRCloud": "acrcloud",
        "讯飞开放平台 ACRCloud": "xfyun",
        "讯飞开放平台": "xfyun_humming",
        "Shazam": "shazam",
    }

    engines = []
    added = set()
    for slot in ("primary", "secondary", "fallback"):
        label = _cfg(engines_cfg, "select", slot, default="") or ""
        key = label_to_key.get(str(label).strip())
        if key and key in instances and key not in added:
            engines.append(instances[key])
            added.add(key)
    return SongIdentifier(
        engines=engines,
        timeout=float(_cfg(config, "advanced", "identify_timeout", default=60)),
    )


class SongEnricher:
    """用歌名+歌手在网易云音乐搜索，补全歌曲 ID/封面/试听链接。

    网易云搜索质量与稳定性优于聚合站（txqq.pro 已改版失效）；song_id 为
    网易云歌曲 ID，可直接生成 music.163.com 试听链接并支持 QQ 音乐
    163 卡片（CQ:music type=163）。
    """

    SEARCH_URL = "https://music.163.com/api/search/get/web"
    DETAIL_URL = "https://music.163.com/api/song/detail/"

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        ),
        "Referer": "https://music.163.com",
        "Cookie": "appver=2.0.2",
    }

    async def enrich(self, song: SongInfo) -> SongInfo:
        """用网易云搜索补全歌曲信息；失败或未命中返回原对象（不修改入参）。

        Args:
            song: 识别引擎返回的歌曲信息。

        Returns:
            增强后的 SongInfo（source 置为 "netease"）；失败时返回原对象。
        """
        query = f"{song.title or ''} {song.artist or ''}".strip()
        if not query:
            return song
        logger.info(f"[识曲] 增强查询(网易云): '{query}'")
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    self.SEARCH_URL,
                    params={"s": query, "type": 1, "limit": 1},
                    headers=self.HEADERS,
                )
                logger.info(
                    f"[识曲] 网易云搜索: HTTP {resp.status_code}, "
                    f"{len(resp.content)} bytes"
                )
                logger.info(f"[识曲] 网易云搜索响应: {resp.text[:200]}")
                resp.raise_for_status()
                payload = resp.json()
                songs = ((payload.get("result") or {}).get("songs")) or []
                if not songs:
                    logger.info("[识曲] 增强未命中，使用识别引擎原始信息")
                    return song
                first = songs[0]
                artists = ", ".join(
                    a.get("name", "")
                    for a in (first.get("artists") or [])
                    if a.get("name")
                )
                song_id = str(first.get("id") or "")
                logger.info(
                    f"[识曲] 增强命中: {first.get('name')} - {artists} "
                    f"(id={song_id})"
                )
                cover_url = (
                    await self._fetch_cover(client, song_id) if song_id else None
                )
                return SongInfo(
                    title=song.title,
                    artist=song.artist,
                    album=song.album,
                    cover_url=cover_url or song.cover_url,
                    audio_url=song.audio_url,
                    song_id=song_id or song.song_id,
                    source="netease",
                )
        except Exception as e:
            logger.warning(f"[识曲] 增强失败: {e}")
            return song

    async def _fetch_cover(self, client, song_id: str) -> str | None:
        """按歌曲 ID 查询网易云详情，获取专辑封面 URL。

        Args:
            client: httpx.AsyncClient 实例。
            song_id: 网易云歌曲 ID。

        Returns:
            封面 URL；查询失败时返回 None。
        """
        try:
            resp = await client.get(
                self.DETAIL_URL,
                params={"id": song_id, "ids": f"[{song_id}]"},
                headers=self.HEADERS,
            )
            if resp.status_code != 200:
                return None
            payload = resp.json()
            detail_songs = payload.get("songs") or []
            if not detail_songs:
                return None
            album = detail_songs[0].get("album") or {}
            return album.get("picUrl")
        except Exception as e:
            logger.warning(f"[识曲] 封面获取失败: {e}")
            return None


class ResultFormatter:
    """将识别结果格式化为文本/图片/音乐卡片。"""

    CARD_WIDTH = 500
    CARD_HEIGHT = 240
    THUMB_SIZE = 240

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def format_text(self, song: SongInfo) -> str:
        """格式化歌曲文本（歌名/歌手，不含链接——链接由独立消息发送）。"""
        parts = []
        if _cfg(self.cfg, "output", "title", default=True) and song.title:
            parts.append(song.title)
        if _cfg(self.cfg, "output", "artist", default=True) and song.artist:
            parts.append(song.artist)
        text = " - ".join(parts)
        return text.strip() or "未能获取歌曲信息"

    def _build_link(self, song: SongInfo) -> str | None:
        if song.audio_url:
            return song.audio_url
        if song.song_id:
            if song.source == "netease":
                return f"https://music.163.com/song/{song.song_id}"
            return f"https://y.qq.com/n/ryqq/songDetail/{song.song_id}"
        return None

    async def build_image(self, song: SongInfo) -> bytes | None:
        """绘制音乐卡片图（封面 + 渐变遮罩 + 歌名 + 歌手）。"""
        try:
            canvas = Image.new("RGB", (self.CARD_WIDTH, self.CARD_HEIGHT), "#1a1a2e")
            cover = None
            if song.cover_url:
                cover = await self._load_cover(song.cover_url)
            if cover is not None:
                cover = cover.resize((self.THUMB_SIZE, self.THUMB_SIZE))
                canvas.paste(cover, (0, 0))
                overlay = Image.new(
                    "RGB",
                    (self.CARD_WIDTH - self.THUMB_SIZE, self.THUMB_SIZE),
                    "#2d2d44",
                )
                canvas.paste(overlay, (self.THUMB_SIZE, 0))
            draw = ImageDraw.Draw(canvas)
            font = _load_cjk_font()
            title = song.title or "未知歌曲"
            draw.text((self.THUMB_SIZE + 20, 40), title, fill="#ffffff", font=font)
            if song.artist:
                draw.text(
                    (self.THUMB_SIZE + 20, 100), song.artist, fill="#bbbbbb", font=font
                )
            buffer = io.BytesIO()
            canvas.save(buffer, format="JPEG", quality=85)
            return buffer.getvalue()
        except Exception:
            return None

    async def _load_cover(self, url: str):
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
        return Image.open(io.BytesIO(data)).convert("RGB")

    def format_link(self, song: SongInfo) -> str | None:
        """生成带图标的试听链接文本；无可用链接时返回 None。

        Args:
            song: 歌曲信息。

        Returns:
            "🔗 <url>" 形式的文本；无链接时返回 None。
        """
        link = self._build_link(song)
        return f"🔗 {link}" if link else None

    def build_card_payload(
        self, song: SongInfo, target_id: str, is_private: bool = False
    ) -> dict:
        """构造 CQ:music 卡片发送 payload。

        优先使用网易云 163 卡片（仅需歌曲 ID，无需音频直链）；无歌曲 ID
        时退回 custom 卡片（需 audio_url）。

        Args:
            song: 识别到的歌曲信息。
            target_id: 群号（群聊）或用户号（私聊）。
            is_private: 是否为私聊（使用 user_id 键）。

        Returns:
            可直接传给 call_action 的 payload dict。
        """
        key = "user_id" if is_private else "group_id"
        if song.song_id:
            return {
                key: target_id,
                "message": [
                    {"type": "music", "data": {"type": "163", "id": song.song_id}}
                ],
            }
        return {
            key: target_id,
            "message": [
                {
                    "type": "music",
                    "data": {
                        "type": "custom",
                        "url": song.audio_url or "",
                        "audio": song.audio_url or "",
                        "title": song.title or "",
                        "image": song.cover_url or "",
                        "singer": song.artist or "",
                    },
                }
            ],
        }

    def build_custom_card_payload(
        self, song: SongInfo, target_id: str, is_private: bool = False
    ) -> dict:
        """构造 CQ:music custom 卡片 payload（163 卡片失败时的兜底）。

        有网易云歌曲 ID 时使用官方外链试听
        （music.163.com/song/media/outer/url?id={id}.mp3）作为音频地址。

        Args:
            song: 识别到的歌曲信息。
            target_id: 群号（群聊）或用户号（私聊）。
            is_private: 是否为私聊（使用 user_id 键）。

        Returns:
            可直接传给 call_action 的 payload dict。
        """
        key = "user_id" if is_private else "group_id"
        audio = song.audio_url or (
            f"https://music.163.com/song/media/outer/url?id={song.song_id}.mp3"
            if song.song_id
            else ""
        )
        return {
            key: target_id,
            "message": [
                {
                    "type": "music",
                    "data": {
                        "type": "custom",
                        "url": audio,
                        "audio": audio,
                        "title": song.title or "",
                        "image": song.cover_url or "",
                        "singer": song.artist or "",
                    },
                }
            ],
        }


@register(
    "astrbot_plugin_song_identifier",
    "NoFizz",
    "引用语音/视频消息识曲插件（讯飞/ACRCloud/Shazam）",
    "1.0.0",
)
class SongIdentifierPlugin(Star):
    """识别 QQ 群/私聊中引用语音/视频/音频文件消息的歌曲。"""

    def __init__(self, context: Context, config: dict):
        """构造插件，装配触发检测、媒体落地、识别引擎链、增强与格式化器。

        Args:
            context: AstrBot Star 上下文。
            config: 插件配置 dict。
        """
        super().__init__(context)
        self.config = config
        self.detector = TriggerDetector(
            _cfg(config, "trigger", "keyword", default="识曲") or "识曲"
        )
        self.materializer = MediaMaterializer(
            max_seconds=int(
                _cfg(config, "advanced", "audio_max_seconds", default=30)
            )
        )
        self.identifier = build_engines(config)
        self.enricher = SongEnricher()
        self.formatter = ResultFormatter(config)

    @filter.event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """处理所有消息事件：识别触发词与引用媒体，编排识别流程并发送结果。

        Args:
            event: 消息事件。
        """
        if not self.detector.check(event):
            return
        logger.info(
            f"[识曲] 触发检测命中: 群聊={not event.is_private_chat()}, "
            f"发送者={event.get_sender_id()}"
        )

        messages = event.get_messages() or []
        for comp in messages:
            if isinstance(comp, Reply):
                logger.info(
                    f"[识曲] 引用消息: id={comp.id}, sender="
                    f"{comp.sender_nickname}({comp.sender_id}), "
                    f"文本='{(comp.message_str or '')[:50]}'"
                )
                break

        media = MediaExtractor.extract_media(event)
        if media is None:
            logger.info("[识曲] 引用消息中无媒体段，提示用户")
            await event.send(event.plain_result("请引用包含语音或视频的消息后再试。"))
            event.stop_event()
            return
        logger.info(f"[识曲] 媒体段类型: {type(media).__name__}")

        try:
            audio_path = await self.materializer.materialize(media)
            if not audio_path or not os.path.exists(audio_path):
                logger.warning("[识曲] 媒体落地失败（无本地音频文件）")
                await event.send(event.plain_result("媒体文件获取失败，请重试。"))
                event.stop_event()
                return
            size = os.path.getsize(audio_path)
            duration = await self.materializer._probe_duration(audio_path)
            fmt = Path(audio_path).suffix.lstrip(".") or "?"
            logger.info(
                f"[识曲] 音频就绪: 路径={audio_path}, 格式={fmt}, "
                f"大小={size} bytes ({size / 1024:.1f} KB)"
                + (f", 时长={duration:.1f}s" if duration is not None else ", 时长=未知")
            )

            timeout = aiohttp.ClientTimeout(
                total=float(
                    _cfg(self.config, "advanced", "identify_timeout", default=60)
                )
            )
            async with aiohttp.ClientSession(timeout=timeout) as session:
                song = await self.identifier.identify(audio_path, session)

            if song is None:
                hint = "未能识别出歌曲，请确认音频清晰且时长足够（建议 15 秒以上）。"
                logger.warning(f"[识曲] 识别失败: {hint}")
                await event.send(event.plain_result(hint))
                event.stop_event()
                return

            logger.info(
                f"[识曲] 识别成功: {song.title} - {song.artist or '未知歌手'} "
                f"(source={song.source})"
            )
            song = await self.enricher.enrich(song)
            await self._send_result(event, song)
        except asyncio.TimeoutError:
            logger.warning("[识曲] 识别超时")
            await event.send(event.plain_result("识别超时，请稍后重试。"))
        except Exception as e:
            logger.exception(f"[识曲] 主流程异常: {e}")
            await event.send(event.plain_result(f"识曲出错：{e}"))
        finally:
            event.stop_event()

    async def _send_result(self, event: AstrMessageEvent, song: SongInfo):
        """按 output_format 配置发送识别结果：card/image/text。

        试听链接（output.link）为独立开关：无论何种输出形式，先发歌曲
        内容，再分开发送试听链接（text/image/card 统一两条消息）。

        Args:
            event: 消息事件。
            song: 识别到的歌曲信息。
        """
        fmt = _cfg(self.config, "output", "format", default="text")
        logger.info(f"[识曲] 输出格式: {fmt}")
        if fmt == "card":
            ok = await self._try_send_card(event, song)
            if ok:
                logger.info("[识曲] QQ 音乐卡片发送成功")
                await self._send_link_if_enabled(event, song)
                return
            logger.warning("[识曲] 卡片发送失败/不支持，降级为文本")
        if fmt == "image":
            image_bytes = await self.formatter.build_image(song)
            if image_bytes:
                await event.send(
                    event.chain_result([Comp.Image.fromBytes(image_bytes)])
                )
                logger.info(f"[识曲] 图片卡片发送完成 ({len(image_bytes)} bytes)")
                await self._send_link_if_enabled(event, song)
                return
            logger.warning("[识曲] 图片生成失败，降级为文本")
        text = self.formatter.format_text(song)
        await event.send(event.plain_result(text))
        logger.info(f"[识曲] 歌曲内容已发送: {text[:80]}")
        await self._send_link_if_enabled(event, song)

    async def _send_link_if_enabled(self, event: AstrMessageEvent, song: SongInfo):
        """按 output.link 开关分条发送试听链接（所有输出形式生效）。

        Args:
            event: 消息事件。
            song: 歌曲信息。
        """
        if not _cfg(self.config, "output", "link", default=True):
            return
        link_text = self.formatter.format_link(song)
        if not link_text:
            return
        await event.send(event.plain_result(link_text))
        logger.info(f"[识曲] 试听链接已发送: {link_text}")

    async def _try_send_card(self, event: AstrMessageEvent, song: SongInfo) -> bool:
        """尝试发送 QQ 音乐卡片；不支持时返回 False 降级。

        两级策略：先发网易云 163 卡片（仅需歌曲 ID）；失败或缺失 ID 时
        改用 custom 卡片（网易云外链试听兜底）。

        Args:
            event: 消息事件。
            song: 识别到的歌曲信息。

        Returns:
            True 表示卡片发送成功；不支持或发送失败时返回 False。
        """
        bot = getattr(event, "bot", None)
        if bot is None:
            return False
        action = (
            "send_private_msg" if event.is_private_chat() else "send_group_msg"
        )
        target = (
            event.get_sender_id() if event.is_private_chat() else event.get_group_id()
        )
        try:
            # 一级：网易云 163 卡片
            if song.song_id:
                try:
                    await bot.api.call_action(
                        action,
                        **self.formatter.build_card_payload(
                            song, target, is_private=event.is_private_chat()
                        ),
                    )
                    return True
                except Exception as e:
                    logger.warning(f"[识曲] 163 卡片发送失败，尝试 custom: {e}")
            # 二级：custom 卡片（网易云外链试听兜底）
            payload = self.formatter.build_custom_card_payload(
                song, target, is_private=event.is_private_chat()
            )
            audio = payload["message"][0]["data"]["audio"]
            if not audio:
                logger.warning("[识曲] custom 卡片无音频地址，放弃卡片")
                return False
            await bot.api.call_action(action, **payload)
            return True
        except Exception as e:
            logger.warning(f"card send failed: {e}")
            return False
