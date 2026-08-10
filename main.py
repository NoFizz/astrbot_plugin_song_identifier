import asyncio
import base64
import hashlib
import hmac
import io
import json
import os
import re
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

# 详细日志开关：由插件配置 advanced.debug_log 控制（Star 初始化时设置）
_DEBUG_LOG = False


def _log_debug(msg: str) -> None:
    """输出详细分步日志；仅当插件配置开启 debug_log 时生效。

    用于区分两类日志：基础日志（logger.info/warning 直接调用，始终输出）
    与详细日志（本函数，调试用）。关闭时只保留开始识曲/成功/失败等基础日志。

    Args:
        msg: 日志内容。
    """
    if _DEBUG_LOG:
        logger.info(msg)


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
    """将媒体段统一落地为截取前 N 秒的 16k 单声道 wav（语音/视频/文件通用）。"""

    def __init__(self, max_seconds: int = 30):
        """初始化媒体落地器。

        Args:
            max_seconds: 媒体统一截取的最大时长（秒）。ACRCloud 官方建议
                识别内容小于 12 秒且文件小于 1MB（30s×16k×16bit≈960KB），
                超长音频会生成超大 wav 导致上传受限或识别失败；语音/视频/
                文件三类媒体统一截取前 N 秒并重采样为 16k 单声道。
        """
        self.max_seconds = max_seconds

    async def materialize(self, component) -> str | None:
        """把媒体段统一落地为截取前 max_seconds 秒的 16k 单声道 wav。

        Args:
            component: Record/Video/File 消息段。

        Returns:
            本地 wav 文件路径；无法落地时返回 None。
        """
        if isinstance(component, Record):
            src = (
                f"file={component.file!r}, url={component.url!r}, path={component.path!r}"
            )
            _log_debug(f"[识曲] 语音段属性: {src}")
            path = await component.convert_to_file_path()
            if not path:
                logger.warning("[识曲] 语音下载/转码失败")
                return None
            _log_debug(
                f"[识曲] 语音转码完成: {path}, 截取前 {self.max_seconds}s"
            )
            return await self._normalize_to_wav(path, "语音")
        if isinstance(component, Video):
            video_path = await component.convert_to_file_path()
            if not video_path:
                logger.warning("[识曲] 视频下载失败（convert_to_file_path 返回空）")
                return None
            video_size = os.path.getsize(video_path) if os.path.exists(video_path) else 0
            _log_debug(
                f"[识曲] 视频已就绪: {video_path} ({video_size} bytes), "
                f"截取前 {self.max_seconds}s 抽音轨"
            )
            return await self._normalize_to_wav(video_path, "视频")
        if isinstance(component, File):
            _log_debug(
                f"[识曲] 文件段: name={component.name!r}, "
                f"url={component.url!r}, local={component.file_!r}"
            )
            path = await component.get_file(allow_return_url=False)
            if not path or not os.path.exists(path):
                logger.warning("[识曲] 文件下载失败")
                return None
            _log_debug(
                f"[识曲] 文件下载完成: {path} "
                f"({os.path.getsize(path)} bytes), 截取前 {self.max_seconds}s"
            )
            return await self._normalize_to_wav(path, "文件")
        logger.warning(f"[识曲] 不支持的媒体段类型: {type(component).__name__}")
        return None

    async def _normalize_to_wav(self, src_path: str, kind: str) -> str | None:
        """用 ffmpeg 将媒体统一转为截取前 max_seconds 秒的 16k 单声道 wav。

        Args:
            src_path: 源音频/视频文件路径。
            kind: 日志用媒体类型名（语音/视频/文件）。

        Returns:
            成功时返回输出 wav 路径；ffmpeg 失败或输出不存在时返回 None。
        """
        out_path = str(
            Path(tempfile.gettempdir())
            / f"songid_{os.getpid()}_{uuid.uuid4().hex}.wav"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-i",
                src_path,
                "-t",
                str(self.max_seconds),
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
        _log_debug(
            f"[识曲] {kind}转换完成: {out_path} "
            f"({os.path.getsize(out_path)} bytes, 截取 {self.max_seconds}s)"
        )
        return out_path

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


def parse_acrcloud_response(payload: dict, music_key: str = "music") -> SongInfo | None:
    """解析 ACRCloud 格式识别响应，返回歌曲信息；无结果返回 None。

    Args:
        payload: ACRCloud 响应 dict。
        music_key: 结果数组键名，"music"（原声识别）或 "humming"（哼唱识别）；
            两者字段结构相同（title/artists/album）。

    Returns:
        歌曲信息；无结果时返回 None。
    """
    if payload.get("status", {}).get("code") != 0:
        return None
    results = (payload.get("metadata") or {}).get(music_key) or []
    if not results:
        return None
    first = results[0]
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
        _log_debug(
            f"[识曲] ACRCloud 请求: {url}, 上传音频 {len(sample)} bytes"
        )
        async with session.post(url, data=form) as resp:
            text = await resp.text()
        _log_debug(
            f"[识曲] ACRCloud 响应: HTTP {resp.status}, {len(text)} bytes"
        )
        _log_debug(f"[识曲] ACRCloud 响应内容: {text[:200]}")
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


class XfyunAcrEngine:
    """讯飞开放平台/ACRCloud 引擎（国内直连，要求 mp3 音频）。

    按官方文档（xfyun.cn/doc/voiceservice/music_recognition）实现两级识别：
    先调用音乐识别端点（s29ebee0d，原声识别）；识别不出时自动调用哼唱
    识别端点（s9884ba49，旋律识别）；仍无结果则返回 None 交给下一引擎。
    """

    MUSIC_ENDPOINT = "/v1/private/s29ebee0d"
    HUMMING_ENDPOINT = "/v1/private/s9884ba49"
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
            # 一级：原声识别（听歌识曲）
            info = await self._request_identify(mp3_path, session, "music")
            if info is not None:
                return info
            # 二级：哼唱识别（旋律匹配）
            _log_debug("[识曲] 讯飞原声识别无结果，尝试讯飞哼唱识别")
            return await self._request_identify(mp3_path, session, "humming")
        finally:
            try:
                os.remove(mp3_path)
            except OSError:
                pass

    async def _request_identify(
        self, mp3_path: str, session, mode: str
    ) -> SongInfo | None:
        """调用讯飞 ACRCloud 识别端点（music 原声 / humming 哼唱）。

        Args:
            mp3_path: 16k 单声道 mp3 文件路径。
            session: aiohttp.ClientSession。
            mode: "music" 或 "humming"。

        Returns:
            歌曲信息；无结果或请求失败时返回 None。
        """
        endpoint = (
            self.MUSIC_ENDPOINT if mode == "music" else self.HUMMING_ENDPOINT
        )
        service_key = "acr_music" if mode == "music" else "acr_humming"
        date = formatdate(usegmt=True)
        authorization = build_xfyun_authorization(
            self.api_key, self.api_secret, self.host, endpoint, date
        )
        url = (
            f"https://{self.host}{endpoint}"
            f"?authorization={quote(authorization)}"
            f"&host={self.host}&date={quote(date)}"
        )
        if self.host.startswith("http"):
            url = f"{self.host}{endpoint}?authorization={quote(authorization)}&host={self.host}&date={quote(date)}"
        with open(mp3_path, "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode()
        body = {
            "header": {"app_id": self.app_id, "status": 3},
            "parameter": {
                service_key: {
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
                    "audio": audio_b64,
                    "frame_size": 0,
                }
            },
        }
        async with session.post(url, json=body) as resp:
            text = await resp.text()
        _log_debug(
            f"[识曲] 讯飞{mode}识别请求完成: HTTP {resp.status}, "
            f"{len(text)} bytes"
        )
        _log_debug(f"[识曲] 讯飞{mode}识别响应: {text[:200]}")
        try:
            payload = json.loads(text)
        except ValueError:
            logger.warning(f"讯飞{mode}识别响应非 JSON: {text[:200]}")
            return None
        if payload.get("header", {}).get("code") != 0:
            return None
        text_b64 = (payload.get("payload") or {}).get("output_text", {}).get("text", "")
        if not text_b64:
            return None
        try:
            inner = json.loads(base64.b64decode(text_b64))
        except Exception:
            return None
        # 原声识别结果在 metadata.music，哼唱识别结果在 metadata.humming
        info = parse_acrcloud_response(inner, music_key="music" if mode == "music" else "humming")
        if info is not None:
            info.source = "xfyun"
        return info

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
            _log_debug(
                f"[识曲] 讯飞哼唱识别请求完成: HTTP {resp.status}, "
                f"上传 {len(audio_bytes)} bytes, 响应 {len(text)} bytes"
            )
            _log_debug(f"[识曲] 讯飞哼唱识别响应: {text[:200]}")
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
                        _log_debug(f"[识曲] 引擎 {name} 未配置，跳过")
                        continue
                    _log_debug(f"[识曲] 尝试引擎 {name} ...")
                    info = await engine.identify(audio_path, session)
                    if info is not None:
                        _log_debug(
                            f"[识曲] 引擎 {name} 识别成功: "
                            f"{info.title} - {info.artist or '未知歌手'}"
                        )
                        return info
                    _log_debug(f"[识曲] 引擎 {name} 无结果，尝试下一引擎")
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
    # 配置下拉选项（中文标签）→ 引擎标识；"留空"或未知标签视为跳过
    label_to_key = {
        "ACRCloud": "acrcloud",
        "Shazam": "shazam",
        "讯飞开放平台/ACRCloud": "xfyun",
        "讯飞开放平台/自研": "xfyun_humming",
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


NETEASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com",
    "Cookie": "appver=2.0.2",
}


async def _netease_search(client, query: str) -> dict | None:
    """网易云搜索首个歌曲条目。

    Args:
        client: httpx.AsyncClient 实例。
        query: 搜索关键词（歌名+歌手）。

    Returns:
        首个歌曲 dict；无结果或请求失败时返回 None。
    """
    resp = await client.get(
        "https://music.163.com/api/search/get/web",
        params={"s": query, "type": 1, "limit": 1},
        headers=NETEASE_HEADERS,
    )
    resp.raise_for_status()
    payload = resp.json()
    songs = ((payload.get("result") or {}).get("songs")) or []
    return songs[0] if songs else None


def _netease_outer_url(song: SongInfo) -> str | None:
    """网易云官方外链试听地址（custom 卡片 audio 兜底）。

    Args:
        song: 歌曲信息（需含网易云 song_id）。

    Returns:
        外链试听 URL；无 song_id 时返回 None。
    """
    if song.song_id:
        return f"https://music.163.com/song/media/outer/url?id={song.song_id}.mp3"
    return None


class SongEnricher:
    """用歌名+歌手在网易云音乐搜索，补全歌曲 ID/封面/试听链接。

    网易云搜索质量与稳定性优于聚合站（txqq.pro 已改版失效）；song_id 为
    网易云歌曲 ID，可直接生成 music.163.com 试听链接并支持 QQ 音乐
    163 卡片（CQ:music type=163）。
    """

    SEARCH_URL = "https://music.163.com/api/search/get/web"
    DETAIL_URL = "https://music.163.com/api/song/detail/"

    HEADERS = NETEASE_HEADERS

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
        _log_debug(f"[识曲] 增强查询(网易云): '{query}'")
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    self.SEARCH_URL,
                    params={"s": query, "type": 1, "limit": 1},
                    headers=self.HEADERS,
                )
                _log_debug(
                    f"[识曲] 网易云搜索: HTTP {resp.status_code}, "
                    f"{len(resp.content)} bytes"
                )
                _log_debug(f"[识曲] 网易云搜索响应: {resp.text[:200]}")
                resp.raise_for_status()
                payload = resp.json()
                songs = ((payload.get("result") or {}).get("songs")) or []
                if not songs:
                    _log_debug("[识曲] 增强未命中，使用识别引擎原始信息")
                    return song
                first = songs[0]
                artists = ", ".join(
                    a.get("name", "")
                    for a in (first.get("artists") or [])
                    if a.get("name")
                )
                song_id = str(first.get("id") or "")
                _log_debug(
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


class NeteaseCardProvider:
    """网易云音乐卡片：163 卡片（仅需歌曲 ID，客户端原生渲染播放）。"""

    async def build_music_segment(self, song: SongInfo) -> dict | None:
        """构造 CQ:music 163 段；无歌曲 ID 且搜索失败时返回 None。

        Args:
            song: 歌曲信息。

        Returns:
            CQ:music 段 dict；失败时返回 None。
        """
        song_id = song.song_id
        if not song_id:
            query = f"{song.title or ''} {song.artist or ''}".strip()
            if not query:
                return None
            try:
                async with httpx.AsyncClient() as client:
                    first = await _netease_search(client, query)
                song_id = str((first or {}).get("id") or "")
            except Exception as e:
                logger.warning(f"[识曲] 网易云卡片搜索失败: {e}")
                return None
        if not song_id:
            return None
        return {"type": "music", "data": {"type": "163", "id": song_id}}


class QQMusicCardProvider:
    """QQ 音乐卡片：原生 qq 卡片（songmid，客户端渲染）。"""

    SEARCH_URL = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"

    async def build_music_segment(self, song: SongInfo) -> dict | None:
        """按歌名+歌手搜索 QQ 音乐，构造原生 qq 卡片段。

        Args:
            song: 歌曲信息。

        Returns:
            CQ:music 段 dict；失败时返回 None。
        """
        query = f"{song.title or ''} {song.artist or ''}".strip()
        if not query:
            return None
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    self.SEARCH_URL,
                    params={"w": query, "format": "json", "n": 1, "p": 1},
                    headers={
                        "User-Agent": NETEASE_HEADERS["User-Agent"],
                        "Referer": "https://y.qq.com/",
                    },
                )
                resp.raise_for_status()
                payload = resp.json()
                songs = (
                    ((payload.get("data") or {}).get("song") or {}).get("list") or []
                )
                if not songs:
                    return None
                songmid = songs[0].get("songmid")
                if not songmid:
                    return None
                return {"type": "music", "data": {"type": "qq", "id": songmid}}
        except Exception as e:
            logger.warning(f"[识曲] QQ音乐卡片搜索失败: {e}")
            return None


# 配置下拉选项（中文标签）→ 音乐卡片平台 provider
PLATFORM_PROVIDERS = {
    "网易云音乐": NeteaseCardProvider(),
    "QQ音乐": QQMusicCardProvider(),
}


class ResultFormatter:
    """将识别结果格式化为文本/图片/音乐卡片。"""

    CARD_WIDTH = 500
    CARD_HEIGHT = 240
    THUMB_SIZE = 240

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def format_text(self, song: SongInfo) -> str:
        """按用户模板格式化歌曲文本（不含链接——链接由独立消息发送）。

        模板支持 {title}/{artist}/{album} 占位符；album 仅 ACRCloud 系
        引擎（acrcloud/xfyun）有值，其他引擎或对应开关关闭时占位符替换为空。

        Args:
            song: 歌曲信息。

        Returns:
            格式化后的文本；结果为空时返回兜底提示。
        """
        template = (
            _cfg(self.cfg, "output", "text_template", default="{title} - {artist}")
            or "{title} - {artist}"
        )
        title_enabled = _cfg(self.cfg, "output", "title", default=True)
        artist_enabled = _cfg(self.cfg, "output", "artist", default=True)
        values = {
            "title": song.title if (title_enabled and song.title) else "",
            "artist": song.artist if (artist_enabled and song.artist) else "",
            "album": song.album or "",
        }
        # 解析模板中手写的字面转义序列（\r\n → 真实 CRLF，\n → LF，\t → 制表符）；
        # 编辑器直接回车的真实换行不受影响（不匹配字面反斜杠序列）
        text = (
            template.replace("\\r\\n", "\r\n")
            .replace("\\n", "\n")
            .replace("\\t", "\t")
        )
        for key, value in values.items():
            text = text.replace("{" + key + "}", value)
        # 占位符为空后残留分隔符的清理：仅处理空格/制表符，绝不触碰换行（\r\n 原样保留）
        text = re.sub(r"[ \t]{2,}", " ", text)      # 合并连续空格
        text = re.sub(r"(?: ?- ){2,}", " - ", text)  # 合并连续 " - " 段（如 {album} 为空）
        return text.strip(" -") or "未能获取歌曲信息"

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
        global _DEBUG_LOG
        _DEBUG_LOG = bool(_cfg(config, "advanced", "debug_log", default=False))
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
        logger.info("[识曲] 开始识曲")
        _log_debug(
            f"[识曲] 触发检测命中: 群聊={not event.is_private_chat()}, "
            f"发送者={event.get_sender_id()}"
        )

        messages = event.get_messages() or []
        for comp in messages:
            if isinstance(comp, Reply):
                _log_debug(
                    f"[识曲] 引用消息: id={comp.id}, sender="
                    f"{comp.sender_nickname}({comp.sender_id}), "
                    f"文本='{(comp.message_str or '')[:50]}'"
                )
                break

        media = MediaExtractor.extract_media(event)
        if media is None:
            _log_debug("[识曲] 引用消息中无媒体段，提示用户")
            await event.send(event.plain_result("请引用包含语音或视频的消息后再试。"))
            event.stop_event()
            return
        _log_debug(f"[识曲] 媒体段类型: {type(media).__name__}")

        try:
            song, materialize_ok = await self._run_identification(media)
            if not materialize_ok:
                hint = "媒体文件获取失败，请重试。"
            elif song is None:
                hint = "未能识别出歌曲，请确认音频清晰且时长足够（建议 15 秒以上）。"
            else:
                await self._send_result(event, song)
                return
            logger.warning(f"[识曲] 识别失败: {hint}")
            await event.send(event.plain_result(hint))
            event.stop_event()
        except asyncio.TimeoutError:
            logger.warning("[识曲] 识别超时")
            await event.send(event.plain_result("识别超时，请稍后重试。"))
        except Exception as e:
            logger.exception(f"[识曲] 主流程异常: {e}")
            await event.send(event.plain_result(f"识曲出错：{e}"))
        finally:
            event.stop_event()

    async def _run_identification(self, media) -> tuple[SongInfo | None, bool]:
        """落地媒体并执行引擎识别与增强（监听器直连与 LLM 工具共用）。

        Args:
            media: 消息段（Record/Video/File）。

        Returns:
            (song, materialize_ok) 元组：song 为增强后的歌曲信息（识别失败
            时为 None）；materialize_ok 为 False 表示媒体落地失败。
        """
        audio_path = await self.materializer.materialize(media)
        if not audio_path or not os.path.exists(audio_path):
            logger.warning("[识曲] 媒体落地失败（无本地音频文件）")
            return None, False
        size = os.path.getsize(audio_path)
        duration = await self.materializer._probe_duration(audio_path)
        fmt = Path(audio_path).suffix.lstrip(".") or "?"
        _log_debug(
            f"[识曲] 音频就绪: 路径={audio_path}, 格式={fmt}, "
            f"大小={size} bytes ({size / 1024:.1f} KB)"
            + (f", 时长={duration:.1f}s" if duration is not None else ", 时长=未知")
        )
        timeout = aiohttp.ClientTimeout(
            total=float(_cfg(self.config, "advanced", "identify_timeout", default=60))
        )
        async with aiohttp.ClientSession(timeout=timeout) as session:
            song = await self.identifier.identify(audio_path, session)
        if song is None:
            return None, True
        song = await self.enricher.enrich(song)
        logger.info(
            f"[识曲] 识别成功: {song.title} - {song.artist or '未知歌手'} "
            f"(source={song.source})"
        )
        return song, True

    @filter.llm_tool(name="identify_song")
    async def identify_song(self, event: AstrMessageEvent):
        """识别语音/视频/音频文件中的歌曲。

        当用户引用了（回复了）一条包含语音、视频或音频文件的消息，并询问
        这是什么歌、歌名是什么、BGM 是什么时，调用此工具进行歌曲识别。
        媒体文件自动从用户引用的消息中获取，无需额外参数。

        Args:
            无需参数。
        """
        media = MediaExtractor.extract_media(event)
        if media is None:
            yield event.plain_result(
                "用户消息中没有可识别的媒体：需要引用（回复）一条包含"
                "语音/视频/音频文件的消息。"
            )
            return
        song, materialize_ok = await self._run_identification(media)
        if not materialize_ok:
            yield event.plain_result("媒体文件处理失败，请重试。")
            return
        if song is None:
            yield event.plain_result(
                "未能识别出歌曲，请确认音频清晰且时长足够（建议 15 秒以上）。"
            )
            return
        text = self.formatter.format_text(song)
        link = self.formatter.format_link(song)
        if link:
            text = f"{text}\n{link}"
        yield event.plain_result(text)

    async def _send_result(self, event: AstrMessageEvent, song: SongInfo):
        """按 output_format 配置发送识别结果：card/image/text。

        试听链接（output.link）为独立开关：无论何种输出形式，先发歌曲
        内容，再分开发送试听链接（text/image/card 统一两条消息）。

        Args:
            event: 消息事件。
            song: 识别到的歌曲信息。
        """
        fmt_label = _cfg(self.config, "output", "format", default="文本") or "文本"
        fmt = {
            "文本": "text",
            "图片": "image",
            "卡片": "card",
        }.get(str(fmt_label).strip(), "text")
        _log_debug(f"[识曲] 输出格式: {fmt}")
        if fmt == "card":
            ok = await self._try_send_card(event, song)
            if ok:
                _log_debug("[识曲] QQ 音乐卡片发送成功")
                await self._send_link_if_enabled(event, song)
                return
            logger.warning("[识曲] 卡片发送失败/不支持，降级为文本")
        if fmt == "image":
            image_bytes = await self.formatter.build_image(song)
            if image_bytes:
                await event.send(
                    event.chain_result([Comp.Image.fromBytes(image_bytes)])
                )
                _log_debug(f"[识曲] 图片卡片发送完成 ({len(image_bytes)} bytes)")
                await self._send_link_if_enabled(event, song)
                return
            logger.warning("[识曲] 图片生成失败，降级为文本")
        text = self.formatter.format_text(song)
        await event.send(event.plain_result(text))
        _log_debug(f"[识曲] 歌曲内容已发送: {text[:80]}")
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
        _log_debug(f"[识曲] 试听链接已发送: {link_text}")

    async def _try_send_card(self, event: AstrMessageEvent, song: SongInfo) -> bool:
        """按配置的音乐卡片平台三档顺序尝试发送卡片。

        平台顺序来自配置 output.card_platforms（首选→次选→备选）；
        「留空」或无法构建/发送的平台自动跳过，全部失败返回 False 降级。

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
        target_key = "user_id" if event.is_private_chat() else "group_id"
        for slot in ("primary", "secondary"):
            label = (
                _cfg(self.config, "output", "card_platforms", slot, default="") or ""
            )
            if label == "留空" or label not in PLATFORM_PROVIDERS:
                _log_debug(f"[识曲] 卡片平台 {slot}（{label or '留空'}）跳过")
                continue
            provider = PLATFORM_PROVIDERS[label]
            try:
                segment = await provider.build_music_segment(song)
            except Exception as e:
                logger.warning(f"[识曲] {label} 卡片构建异常: {e}")
                segment = None
            if not segment:
                logger.warning(f"[识曲] {label} 无法构建卡片，尝试下一档")
                continue
            try:
                await bot.api.call_action(
                    action, **{target_key: target, "message": [segment]}
                )
                _log_debug(f"[识曲] {label} 卡片发送成功")
                return True
            except Exception as e:
                logger.warning(f"[识曲] {label} 卡片发送失败: {e}，尝试下一档")
        return False


