"""Apple Music REST API client.

All endpoints are public-catalog (Bearer token). The `media-user-token` is
only required for lyrics; the `authorization-token` (web-player JWT) is
fetched automatically from beta.music.apple.com if not supplied.

Reverse-engineered from a prior reference implementation:
  - getInfoFromAdam()    → lines 2696-2733  (single song)
  - getMeta()            → lines 1306-1382  (album/playlist + tracks)
  - getSongLyrics()      → lines 1383-1407  (TTML)
  - getToken()           → lines 2734-2774  (auto-refresh web JWT)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

API_HOST = "https://amp-api.music.apple.com"

UA_ITUNES = (
    "iTunes/12.11.3 (Windows; Microsoft Windows 10 x64 Professional Edition "
    "(Build 19041); x64) AppleWebKit/7611.1022.4001.1 (dt:2)"
)
UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
)


# ──────────────────────────── token ────────────────────────────────────────

# Apple changed their bundle naming around 2025-Q1: was "index-legacy-<hex>.js"
# (hyphen separator), now "index-legacy~<hex>.js" (tilde separator). Accept both.
# JWT chars: [A-Za-z0-9_.\-] — `.` is the section separator; we need it.
_TOKEN_RE = re.compile(rb'eyJh[A-Za-z0-9_.\-]{100,800}')
_INDEX_JS_RE = re.compile(rb"/assets/index(?:-legacy)?[~\-][A-Za-z0-9_]+\.js")


def get_web_token(client: Optional[httpx.Client] = None) -> str:
    """Scrape the public Apple Music web-player JWT.

    1. GET https://beta.music.apple.com/  → find /assets/index-legacy-*.js
    2. GET that JS   → regex out an `eyJh...` token
    """
    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=15.0, follow_redirects=True)
    try:
        r = client.get("https://beta.music.apple.com/")
        r.raise_for_status()
        m = _INDEX_JS_RE.search(r.content)
        if not m:
            raise RuntimeError("no /assets/index-legacy-*.js link found")
        js_path = m.group(0).decode("ascii")
        r = client.get("https://beta.music.apple.com" + js_path)
        r.raise_for_status()
        m = _TOKEN_RE.search(r.content)
        if not m:
            raise RuntimeError("no eyJh... token found in index JS")
        return m.group(0).decode("ascii")
    finally:
        if own_client:
            client.close()


# ──────────────────────────── songs / albums ───────────────────────────────

@dataclass
class SongMeta:
    """Parsed subset of /v1/catalog/{sf}/songs/{id} response."""
    id: str
    title: str
    artist: str
    album: str
    storefront: str
    isrc: str
    track_number: int
    disc_number: int
    enhanced_hls: str               # extendedAssetUrls.enhancedHls (master m3u8)
    is_apple_digital_master: bool
    audio_traits: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class AlbumMeta:
    """Parsed subset of /v1/catalog/{sf}/albums/{id}."""
    id: str
    name: str
    artist: str
    artwork_url: str
    upc: str
    release_date: str
    record_label: str
    copyright: str
    is_apple_digital_master: bool
    is_complete: bool
    track_count: int
    tracks: list[SongMeta]
    raw: dict = field(default_factory=dict)


class AppleMusicClient:
    """Thin Apple Music REST client.

    Use as a context manager to share the underlying httpx.Client.
    """

    def __init__(
        self,
        authorization_token: Optional[str] = None,
        media_user_token: Optional[str] = None,
        language: str = "",
        timeout: float = 30.0,
        proxy: Optional[str] = None,
    ):
        self._auth = authorization_token
        self._mut = media_user_token
        self._lang = language
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"Origin": "https://music.apple.com"},
            proxy=proxy,
        )

    def __enter__(self) -> "AppleMusicClient":
        return self

    def __exit__(self, *args) -> None:
        self._client.close()

    @property
    def authorization_token(self) -> str:
        if self._auth is None:
            self._auth = get_web_token(self._client)
        return self._auth

    def _common_headers(self, ua: str) -> dict:
        h = {
            "User-Agent": ua,
            "Authorization": f"Bearer {self.authorization_token}",
        }
        if self._mut:
            h["Media-User-Token"] = self._mut
        return h

    # --- song ---

    def get_song(self, adam_id: str, storefront: str = "us") -> SongMeta:
        """GET /v1/catalog/{sf}/songs/{id}?extend=extendedAssetUrls&include=albums"""
        params = {
            "extend": "extendedAssetUrls",
            "include": "albums",
        }
        if self._lang:
            params["l"] = self._lang
        r = self._client.get(
            f"{API_HOST}/v1/catalog/{storefront}/songs/{adam_id}",
            params=params,
            headers=self._common_headers(UA_ITUNES),
        )
        r.raise_for_status()
        data = r.json()
        for d in data.get("data", []):
            if d.get("id") == adam_id:
                return self._song_from_data(d, storefront)
        raise LookupError(f"song {adam_id} not found in storefront {storefront}")

    @staticmethod
    def _song_from_data(d: dict, storefront: str) -> SongMeta:
        a = d.get("attributes") or {}
        eassets = a.get("extendedAssetUrls") or {}
        return SongMeta(
            id=d.get("id", ""),
            title=a.get("name", ""),
            artist=a.get("artistName", ""),
            album=a.get("albumName", ""),
            storefront=storefront,
            isrc=a.get("isrc", ""),
            track_number=int(a.get("trackNumber", 0) or 0),
            disc_number=int(a.get("discNumber", 1) or 1),
            enhanced_hls=eassets.get("enhancedHls", "") or "",
            is_apple_digital_master=bool(a.get("isAppleDigitalMaster")
                                         or a.get("isMasteredForItunes")),
            audio_traits=list(a.get("audioTraits") or []),
            raw=d,
        )

    # --- album / playlist ---

    def get_album(self, album_id: str, storefront: str = "us") -> AlbumMeta:
        """GET /v1/catalog/{sf}/albums/{id} or /playlists/{id}.

        Auto-pages through /tracks for long playlists.
        """
        is_playlist = "pl." in album_id
        kind = "playlists" if is_playlist else "albums"
        params = {
            "omit[resource]": "autos",
            "include": "tracks,artists,record-labels",
            "include[songs]": "artists",
            "fields[artists]": "name,artwork",
            "fields[albums:albums]": "artistName,artwork,name,releaseDate,url",
            "fields[record-labels]": "name",
            "extend": "editorialVideo",
        }
        if self._lang:
            params["l"] = self._lang
        r = self._client.get(
            f"{API_HOST}/v1/catalog/{storefront}/{kind}/{album_id}",
            params=params,
            headers=self._common_headers(UA_BROWSER),
        )
        r.raise_for_status()
        obj = r.json()
        if not obj.get("data"):
            raise LookupError(f"{kind[:-1]} {album_id} not found")
        d = obj["data"][0]
        a = d.get("attributes") or {}

        # Pagination for playlists
        tracks_rel = ((d.get("relationships") or {}).get("tracks") or {})
        track_data = list(tracks_rel.get("data") or [])
        if is_playlist and tracks_rel.get("next"):
            offset = 0
            while True:
                offset += 100
                r2 = self._client.get(
                    f"{API_HOST}/v1/catalog/{storefront}/{kind}/{album_id}/tracks",
                    params={"offset": str(offset),
                            **({"l": self._lang} if self._lang else {})},
                    headers=self._common_headers(UA_BROWSER),
                )
                r2.raise_for_status()
                obj2 = r2.json()
                track_data.extend(obj2.get("data") or [])
                if not obj2.get("next"):
                    break

        tracks: list[SongMeta] = []
        for t in track_data:
            tracks.append(self._song_from_data(t, storefront))

        artwork_url = (a.get("artwork") or {}).get("url", "")
        return AlbumMeta(
            id=album_id,
            name=a.get("name", ""),
            artist=a.get("artistName", ""),
            artwork_url=artwork_url,
            upc=a.get("upc", ""),
            release_date=a.get("releaseDate", ""),
            record_label=a.get("recordLabel", ""),
            copyright=a.get("copyright", ""),
            is_apple_digital_master=bool(a.get("isAppleDigitalMaster")
                                         or a.get("isMasteredForItunes")),
            is_complete=bool(a.get("isComplete")),
            track_count=int(a.get("trackCount", len(tracks)) or len(tracks)),
            tracks=tracks,
            raw=d,
        )

    # --- lyrics ---

    def get_lyrics_ttml(
        self,
        song_id: str,
        storefront: str = "us",
        kind: str = "lyrics",
    ) -> Optional[str]:
        """GET /v1/catalog/{sf}/songs/{id}/{kind}.

        kind ∈ {"lyrics", "syllable-lyrics"}. Requires media-user-token.
        Returns TTML XML string or None on 404.
        """
        if not self._mut:
            return None
        r = self._client.get(
            f"{API_HOST}/v1/catalog/{storefront}/songs/{song_id}/{kind}",
            headers=self._common_headers(UA_BROWSER),
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        try:
            return data["data"][0]["attributes"]["ttml"]
        except (KeyError, IndexError):
            return None
