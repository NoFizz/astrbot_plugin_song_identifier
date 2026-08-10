"""音乐平台增强层。

网易云/QQ 搜索属于非官方、易失集成：失败只返回空增强，
绝不修改识别核心的 provider/mode/标题/歌手/专辑等字段。
"""

from dataclasses import dataclass

import httpx

from .models import SongInfo

_NETEASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com",
    "Cookie": "appver=2.0.2",
}
_NETEASE_SEARCH_URL = "https://music.163.com/api/search/get/web"


@dataclass(slots=True)
class EnrichedSong:
    """识别结果 + 平台增强信息的组合。"""

    song: SongInfo
    netease_id: str | None = None
    qq_songmid: str | None = None
    cover_url: str | None = None

    @property
    def netease_url(self) -> str | None:
        return (
            f"https://music.163.com/song/{self.netease_id}" if self.netease_id else None
        )

    @property
    def qq_url(self) -> str | None:
        return (
            f"https://y.qq.com/n/ryqq/songDetail/{self.qq_songmid}"
            if self.qq_songmid
            else None
        )


class SongEnricher:
    """用歌名+歌手在网易云搜索，补全歌曲 ID 与封面。

    网易云搜索质量与稳定性优于聚合站；song_id 为网易云歌曲 ID，
    可生成 music.163.com 试听链接并支持 QQ 音乐 163 卡片。
    """

    async def enrich(self, song: SongInfo) -> EnrichedSong:
        from . import log

        query = f"{song.title or ''} {song.artist or ''}".strip()
        if not query:
            return EnrichedSong(song=song)
        log.debug(f"网易云增强: 查询 '{query}'")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    _NETEASE_SEARCH_URL,
                    params={"s": query, "type": 1, "limit": 1},
                    headers=_NETEASE_HEADERS,
                )
                resp.raise_for_status()
                payload = resp.json()
                songs = ((payload.get("result") or {}).get("songs")) or []
                if not songs:
                    log.debug("网易云增强: 无搜索结果")
                    return EnrichedSong(song=song)
                first = songs[0]
                song_id = str(first.get("id") or "")
                log.debug(f"网易云增强: 命中 id={song_id}")
                cover = None
                if song_id:
                    cover = await self._fetch_cover(client, song_id)
                return EnrichedSong(
                    song=song,
                    netease_id=song_id or None,
                    cover_url=cover or None,
                )
        except Exception as error:
            # 增强失败不阻塞识别核心：返回空增强
            log.warning(f"网易云增强失败: {error}")
            return EnrichedSong(song=song)

    async def _fetch_cover(self, client, song_id: str) -> str | None:
        from . import log

        try:
            resp = await client.get(
                "https://music.163.com/api/song/detail/",
                params={"id": song_id, "ids": f"[{song_id}]"},
                headers=_NETEASE_HEADERS,
            )
            if resp.status_code != 200:
                log.debug(f"网易云封面: HTTP {resp.status_code}")
                return None
            payload = resp.json()
            detail_songs = payload.get("songs") or []
            if not detail_songs:
                log.debug("网易云封面: 详情无数据")
                return None
            album = detail_songs[0].get("album") or {}
            pic = album.get("picUrl")
            log.debug(f"网易云封面: {'有' if pic else '无'}")
            return pic
        except Exception as error:
            log.warning(f"网易云封面获取失败: {error}")
            return None
