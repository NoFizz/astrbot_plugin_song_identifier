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
_QQ_SEARCH_URL = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"


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
                qq_songmid = await self._fetch_qq_songmid(client, song)
                return EnrichedSong(
                    song=song,
                    netease_id=song_id or None,
                    qq_songmid=qq_songmid,
                    cover_url=cover or None,
                )
        except Exception as error:
            # 增强失败不阻塞识别核心：返回空增强
            log.warning(f"网易云增强失败: {error}")
            return EnrichedSong(song=song)

    async def _fetch_qq_songmid(self, client, song: SongInfo) -> str | None:
        """按歌名+歌手搜索 QQ 音乐，返回 songmid（带标题/歌手校验）。

        QQ 搜索为非官方接口，失败或匹配不符时返回 None（卡片回退，不阻塞）。
        """
        from . import log

        try:
            resp = await client.get(
                _QQ_SEARCH_URL,
                params={
                    "w": f"{song.title or ''} {song.artist or ''}".strip(),
                    "format": "json",
                    "n": 1,
                    "p": 1,
                },
                headers={
                    "User-Agent": _NETEASE_HEADERS["User-Agent"],
                    "Referer": "https://y.qq.com/",
                },
            )
            if resp.status_code != 200:
                log.debug(f"QQ 搜索: HTTP {resp.status_code}")
                return None
            payload = resp.json()
            songs = ((payload.get("data") or {}).get("song") or {}).get("list") or []
            if not songs:
                log.debug("QQ 搜索: 无结果")
                return None
            first = songs[0]
            songmid = first.get("songmid")
            if not songmid:
                return None
            # 标题/歌手基础校验：避免同名误匹配
            hit_title = str(first.get("songname") or "").strip()
            if not self._names_match(song.title, hit_title):
                log.debug(f"QQ 搜索: 标题不匹配（{hit_title}），跳过")
                return None
            # 歌手校验：同歌名不同歌手的歌曲不得误配 songmid（响应无 singer 时跳过）
            if song.artist:
                singers = [
                    str(s.get("name") or "")
                    for s in first.get("singer") or []
                    if isinstance(s, dict)
                ]
                if singers and not any(
                    self._names_match(song.artist, s) for s in singers
                ):
                    log.debug(f"QQ 搜索: 歌手不匹配（{singers}），跳过")
                    return None
            log.debug(f"QQ 搜索: 命中 songmid={songmid}")
            return str(songmid)
        except Exception as error:
            log.warning(f"QQ 搜索失败: {error}")
            return None

    @staticmethod
    def _names_match(original: str | None, hit: str) -> bool:
        """名称基础匹配：去除空白与常见括号后缀后比较包含关系。

        同时用于标题与歌手校验（QQ/网易云增强防同名误配）。
        """
        import re

        if not original or not hit:
            return False

        def norm(s: str) -> str:
            return re.sub(r"[\s()（）【】\[\]-]", "", s).lower()

        o, h = norm(original), norm(hit)
        return bool(o and h and (o in h or h in o))

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
