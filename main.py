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

from astrbot.api.message_components import At, File, Record, Reply, Video
from astrbot.api.star import Context, Star, register


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


@register(
    "astrbot_plugin_song_identifier", "NoFizz", "引用语音/视频消息识曲插件", "1.0.0"
)
class SongIdentifierPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config


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
    """判断消息触发模式：群聊需 @bot + 关键词 + 引用；私聊只需关键词 + 引用。

    Returns:
        "music"（识曲）/"humming"（哼唱）/ None（不触发）
    """

    def __init__(self, keyword: str, humming_keyword: str = "哼唱"):
        self.keyword = keyword
        self.humming_keyword = humming_keyword

    def check(self, event) -> str | None:
        messages = event.get_messages()
        if not messages:
            return None
        text = event.message_str or ""
        mode = None
        if self.keyword in text:
            mode = "music"
        elif self.humming_keyword in text:
            mode = "humming"
        if mode is None:
            return None
        has_reply = any(isinstance(comp, Reply) for comp in messages)
        if not has_reply:
            return None
        if event.is_private_chat():
            return mode
        for comp in messages:
            if isinstance(comp, At) and str(comp.qq) == str(event.get_self_id()):
                return mode
        return None


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

    async def materialize(self, component) -> str | None:
        """把媒体段落地为本地音频文件路径。

        Args:
            component: Record/Video/File 消息段。

        Returns:
            本地音频文件路径；无法落地时返回 None。
        """
        if isinstance(component, Record):
            return await component.convert_to_file_path()
        if isinstance(component, Video):
            video_path = await component.convert_to_file_path()
            if not video_path:
                return None
            out_path = str(
                Path(tempfile.gettempdir())
                / f"songid_{os.getpid()}_{uuid.uuid4().hex}.wav"
            )
            return await self._extract_audio_from_video(video_path, out_path)
        if isinstance(component, File):
            return await component.get_file(allow_return_url=False)
        return None

    async def _extract_audio_from_video(
        self, video_path: str, out_path: str
    ) -> str | None:
        """用 ffmpeg 从视频中抽取音轨为单声道 44.1kHz wav。

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
                "-vn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "44100",
                "-ac",
                "1",
                out_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return None
        await proc.wait()
        if proc.returncode != 0 or not os.path.exists(out_path):
            return None
        return out_path


def build_acrcloud_signature(
    access_key: str, access_secret: str, timestamp: str
) -> str:
    """构造 ACRCloud 识别接口签名（官方算法）。"""
    string_to_sign = f"POST\n/v1/identify\n{access_key}\n{timestamp}"
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
        async with session.post(url, data=form) as resp:
            payload = await resp.json()
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
                payload = await resp.json()
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
        await proc.wait()
        if proc.returncode != 0 or not os.path.exists(mp3_path):
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
                payload = await resp.json()
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
        await proc.wait()
        if proc.returncode != 0 or not os.path.exists(out_path):
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
                try:
                    if not engine.is_configured():
                        continue
                    info = await engine.identify(audio_path, session)
                    if info is not None:
                        return info
                except Exception:
                    continue
            return None

        return await asyncio.wait_for(_run(), timeout=self.timeout)


def build_engines(config: dict) -> tuple[SongIdentifier, XfyunHummingEngine]:
    """按配置构造引擎链和哼唱引擎。

    Args:
        config: 插件配置 dict。

    Returns:
        (SongIdentifier, XfyunHummingEngine) 元组。
    """
    engine_map = {
        "xfyun": XfyunAcrEngine(
            app_id=config.get("xfyun_app_id", ""),
            api_key=config.get("xfyun_api_key", ""),
            api_secret=config.get("xfyun_api_secret", ""),
        ),
        "acrcloud": AcrcloudEngine(
            host=config.get("acrcloud_host", ""),
            access_key=config.get("acrcloud_access_key", ""),
            access_secret=config.get("acrcloud_access_secret", ""),
        ),
    }
    if config.get("enable_shazam_fallback", True):
        engine_map["shazam"] = ShazamEngine()

    engines = []
    added = set()
    order = str(config.get("engine_order", "xfyun,acrcloud,shazam"))
    for name in [n.strip() for n in order.split(",")]:
        if name in engine_map and name not in added:
            engines.append(engine_map[name])
            added.add(name)

    humming = XfyunHummingEngine(
        app_id=config.get("xfyun_humming_app_id", "") or config.get("xfyun_app_id", ""),
        api_key=config.get("xfyun_humming_api_key", "")
        or config.get("xfyun_api_key", ""),
    )
    return SongIdentifier(
        engines=engines, timeout=float(config.get("identify_timeout", 30))
    ), humming


class SongEnricher:
    """用歌名+歌手在 txqq.pro 聚合搜索，补全国内平台封面/试听链接/歌曲ID。"""

    SEARCH_URL = "https://music.txqq.pro/"

    def __init__(self, platform: str = "qq"):
        self.platform = platform

    async def enrich(self, song: SongInfo) -> SongInfo:
        query = f"{song.title or ''} {song.artist or ''}".strip()
        if not query:
            return song
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self.SEARCH_URL,
                    data={
                        "input": query,
                        "filter": "name",
                        "type": self.platform,
                        "page": 1,
                    },
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0 Safari/537.36"
                        ),
                        "Referer": self.SEARCH_URL,
                    },
                )
                resp.raise_for_status()
                payload = resp.json()
        except Exception:
            return song
        items = []
        if isinstance(payload, dict):
            items = payload.get("data") or []
        if not items:
            return song
        first = items[0]
        return SongInfo(
            title=song.title,
            artist=song.artist,
            album=song.album,
            cover_url=first.get("pic") or song.cover_url,
            audio_url=first.get("url") or first.get("link") or song.audio_url,
            song_id=str(first.get("songid") or "") or song.song_id,
            source=song.source,
        )


class ResultFormatter:
    """将识别结果格式化为文本/图片/音乐卡片。"""

    CARD_WIDTH = 500
    CARD_HEIGHT = 240
    THUMB_SIZE = 240

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def format_text(self, song: SongInfo) -> str:
        parts = []
        if self.cfg.get("output_title", True) and song.title:
            parts.append(song.title)
        if self.cfg.get("output_artist", True) and song.artist:
            parts.append(song.artist)
        text = " - ".join(parts)
        if self.cfg.get("output_link", True):
            link = self._build_link(song)
            if link:
                text += f"\n🔗 {link}"
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

    def build_card_payload(self, song: SongInfo, group_id: str) -> dict:
        """构造 CQ:music custom 卡片发送 payload。"""
        return {
            "group_id": group_id,
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
