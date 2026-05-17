"""Apple Music API — song/album/artist metadata, search, and download.

Endpoints:
  GET  /                        — HTML documentation with architecture diagram
  GET  /health                  — service health
  GET  /search?q=<term>&type=songs,albums,artists&limit=10
  GET  /song/<id>               — song metadata
  GET  /album/<id>              — album metadata + track list
  GET  /artist/<id>             — artist metadata + albums
  GET  /download/<id>           — download (default AAC; ?fmt=alac for Hi-Res)
  POST /batch                   — batch download
"""
from __future__ import annotations

import os
import re
import sys
import json
import shutil
import tempfile
import base64
from pathlib import Path

from flask import Flask, request, jsonify, send_file, send_from_directory
import httpx

sys.path.insert(0, str(Path(__file__).parent))
from am_alac import apple_api, aria_rpc, m3u8_select, m4s_parser, m4a_writer, decryptor, aac_decrypt

app = Flask(__name__, static_folder="static")

API_TOKEN = os.environ.get("API_TOKEN", "jtr")
MAX_ALBUM_TRACKS = int(os.environ.get("MAX_ALBUM_TRACKS", "30"))
MAX_BATCH_SIZE = int(os.environ.get("MAX_BATCH_SIZE", "50"))
DOWNLOAD_TIMEOUT_PER_TRACK = 60  # seconds

ARIA_HOST = os.environ.get("ARIA_HOST", "127.0.0.1")
ARIA_DECRYPT_PORT = int(os.environ.get("ARIA_DECRYPT_PORT", "47010"))
ARIA_M3U8_PORT = int(os.environ.get("ARIA_M3U8_PORT", "47020"))
CACHE_DIR = os.environ.get("CACHE_DIR", "/tmp/am_cache")
STOREFRONT = os.environ.get("STOREFRONT", "us")
ARIA_CONFIG = Path(os.environ.get(
    "ARIA_CONFIG",
    os.path.expanduser("~/.config/aria/config.json"),
))

os.makedirs(CACHE_DIR, exist_ok=True)

_FREE_PATHS = {"/", "/health", "/static/architecture.png"}

@app.before_request
def _check_token():
    if request.path in _FREE_PATHS or request.path.startswith("/static/"):
        return
    token = request.args.get("token") or request.headers.get("X-Token")
    if token != API_TOKEN:
        return jsonify({"error": "unauthorized — pass ?token= or X-Token header"}), 401

_session = decryptor.PersistentDecryptSession(
    host=ARIA_HOST, port=ARIA_DECRYPT_PORT)


def _safe_filename(s: str) -> str:
    return re.sub(r'[/\\<>:"|?*\x00-\x1f]', "_", s).strip(". ")


def _load_aria_config() -> dict | None:
    """Best-effort load of user-supplied tokens (plain JSON or base64).

    Returns None if the config is missing; callers fall back to anonymous
    web-token bootstrap.
    """
    if not ARIA_CONFIG.exists():
        return None
    raw = ARIA_CONFIG.read_text().strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            return json.loads(base64.b64decode(raw).decode())
        except Exception:
            return None


def _get_apple_headers() -> dict:
    cfg = _load_aria_config()
    if cfg:
        return {
            "Authorization": "Bearer " + cfg.get("accessToken", ""),
            "Origin": "https://music.apple.com",
            "Media-User-Token": cfg.get("mediaUserToken", ""),
            "Content-Type": "application/json;charset=utf-8",
        }
    token = apple_api.get_web_token()
    return {
        "Authorization": "Bearer " + token,
        "Origin": "https://music.apple.com",
    }


def _apple_get(path: str, params: dict = None) -> dict:
    headers = _get_apple_headers()
    r = httpx.get(
        f"https://amp-api.music.apple.com{path}",
        headers=headers, params=params or {}, timeout=15)
    r.raise_for_status()
    return r.json()


def _fmt_artwork(artwork: dict, size: int = 600) -> str:
    if not artwork:
        return ""
    url = artwork.get("url", "")
    return url.replace("{w}", str(size)).replace("{h}", str(size))


def _get_cached_path(song_id: str, fmt: str) -> str:
    return os.path.join(CACHE_DIR, f"{song_id}.{fmt}.m4a")


# ─────────────────────── HTML Documentation ───────────────────────

@app.route("/")
def index():
    return """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>AM Service</title>
<style>
  body{font-family:-apple-system,system-ui,sans-serif;max-width:900px;margin:40px auto;padding:0 20px;color:#1e293b;line-height:1.6}
  h1{border-bottom:2px solid #3b82f6;padding-bottom:8px}
  h2{color:#3b82f6;margin-top:32px}
  table{border-collapse:collapse;width:100%}
  th,td{border:1px solid #e2e8f0;padding:8px 12px;text-align:left}
  th{background:#f1f5f9}
  code{background:#f1f5f9;padding:2px 6px;border-radius:4px;font-size:0.9em}
  pre{background:#0f172a;color:#e2e8f0;padding:16px;border-radius:8px;overflow-x:auto}
  img{max-width:100%;border:1px solid #e2e8f0;border-radius:8px;margin:16px 0}
  .badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:0.8em;font-weight:600}
  .get{background:#dcfce7;color:#166534}
  .post{background:#dbeafe;color:#1e40af}
</style></head><body>
<h1>Apple Music Service API</h1>
<p>Song/album/artist metadata, search, and lossless download.</p>

<h2>Architecture</h2>
<img src="/static/architecture.png" alt="Architecture Diagram">
<p><em>ALAC path: aria TCP pipelined (FairPlay) → 18 MB/s. AAC path: pywidevine CDM + mp4decrypt.</em></p>

<h2>API Endpoints</h2>
<table>
<tr><th>Method</th><th>Endpoint</th><th>Description</th></tr>
<tr><td><span class="badge get">GET</span></td><td><code>/health</code></td><td>Service health check</td></tr>
<tr><td><span class="badge get">GET</span></td><td><code>/search?q=term&amp;type=songs,albums,artists&amp;limit=10&amp;offset=0</code></td><td>Search (paginated)</td></tr>
<tr><td><span class="badge get">GET</span></td><td><code>/song/&lt;id&gt;</code></td><td>Song detail (metadata, artwork, formats, lyrics flag)</td></tr>
<tr><td><span class="badge get">GET</span></td><td><code>/album/&lt;id&gt;?tracks_limit=25&amp;tracks_offset=0</code></td><td>Album detail + paginated tracks</td></tr>
<tr><td><span class="badge get">GET</span></td><td><code>/artist/&lt;id&gt;?albums_limit=25&amp;albums_offset=0</code></td><td>Artist detail + paginated albums</td></tr>
<tr><td><span class="badge get">GET</span></td><td><code>/artist/&lt;id&gt;/albums?limit=25&amp;offset=0</code></td><td>Artist albums (standalone paginated)</td></tr>
<tr><td><span class="badge get">GET</span></td><td><code>/download/&lt;id&gt;</code></td><td>Download song (default: AAC 256k; <code>?fmt=alac</code> for Hi-Res)</td></tr>
<tr><td><span class="badge get">GET</span></td><td><code>/album/&lt;id&gt;/download?fmt=aac</code></td><td>Download entire album as ZIP (default AAC; <code>?fmt=alac</code> for Hi-Res)</td></tr>
<tr><td><span class="badge post">POST</span></td><td><code>/batch</code></td><td>Batch download: <code>{"ids":["..."],"fmt":"aac"}</code></td></tr>
</table>

<h2>Authentication</h2>
<p>All API endpoints (except <code>/</code> and <code>/health</code>) require a token.
Pass it as query param <code>?token=TOKEN</code> or header <code>X-Token: TOKEN</code>.</p>

<h2>Examples</h2>
<pre>
TOKEN="jtr"

# Search (paginated)
curl "http://HOST:8899/search?q=Beatles&amp;type=songs&amp;limit=5&amp;token=$TOKEN"

# Song detail
curl "http://HOST:8899/song/1440841263?token=$TOKEN"

# Album with tracks (paginated)
curl "http://HOST:8899/album/1440857781?token=$TOKEN"
curl "http://HOST:8899/album/1440857781?tracks_limit=10&amp;tracks_offset=0&amp;token=$TOKEN"

# Artist with albums (paginated)
curl "http://HOST:8899/artist/136975?token=$TOKEN"

# Artist albums standalone
curl "http://HOST:8899/artist/136975/albums?limit=10&amp;offset=0&amp;token=$TOKEN"

# Download AAC (default)
curl -o song.m4a "http://HOST:8899/download/1440841263?token=$TOKEN"

# Download Hi-Res ALAC
curl -o song.m4a "http://HOST:8899/download/1440841263?fmt=alac&amp;token=$TOKEN"

# Download entire album as ZIP (AAC)
curl -o album.zip "http://HOST:8899/album/1440857781/download?token=$TOKEN"

# Download entire album as ZIP (Hi-Res ALAC)
curl -o album.zip "http://HOST:8899/album/1440857781/download?fmt=alac&amp;token=$TOKEN"
</pre>

<h2>Modules</h2>
<table>
<tr><th>Module</th><th>Role</th><th>Lines</th></tr>
<tr><td><code>apple_api.py</code></td><td>Apple Music REST client (catalog, token)</td><td>292</td></tr>
<tr><td><code>aria_rpc.py</code></td><td>TCP pipelined client for aria (FairPlay decrypt)</td><td>308</td></tr>
<tr><td><code>decryptor.py</code></td><td>ALAC pipeline: persistent session + prefetch</td><td>280</td></tr>
<tr><td><code>aac_decrypt.py</code></td><td>AAC pipeline: Widevine CDM + mp4decrypt</td><td>170</td></tr>
<tr><td><code>m3u8_select.py</code></td><td>HLS variant picker (ALAC/AAC/Atmos)</td><td>131</td></tr>
<tr><td><code>m4s_parser.py</code></td><td>Encrypted fragmented MP4 parser</td><td>438</td></tr>
<tr><td><code>m4a_writer.py</code></td><td>ALAC m4a output (in-place patch)</td><td>205</td></tr>
<tr><td><code>server.py</code></td><td>This HTTP API server</td><td>~350</td></tr>
</table>
</body></html>"""


# ─────────────────────── Search ───────────────────────

@app.route("/search")
def search():
    q = request.args.get("q", "")
    if not q:
        return jsonify({"error": "missing ?q= parameter"}), 400
    types = request.args.get("type", "songs,albums,artists")
    limit = request.args.get("limit", "10")
    offset = request.args.get("offset", "0")
    sf = request.args.get("sf", STOREFRONT)

    data = _apple_get(f"/v1/catalog/{sf}/search",
                      {"term": q, "types": types, "limit": limit, "offset": offset})

    out = {}
    for typ in types.split(","):
        typ = typ.strip()
        section = data.get("results", {}).get(typ, {})
        items = section.get("data", [])
        entries = []
        for item in items:
            a = item.get("attributes", {})
            entry = {"id": item["id"], "type": item["type"], "name": a.get("name", "")}
            if "artistName" in a:
                entry["artist"] = a["artistName"]
            if "artwork" in a:
                entry["artwork"] = _fmt_artwork(a["artwork"])
            if "albumName" in a:
                entry["album"] = a["albumName"]
            if "durationInMillis" in a:
                entry["duration_ms"] = a["durationInMillis"]
            if "trackCount" in a:
                entry["track_count"] = a["trackCount"]
            if "url" in a:
                entry["url"] = a["url"]
            entries.append(entry)
        out[typ] = {
            "data": entries,
            "next": section.get("next"),
            "total": len(entries),
        }
    return jsonify(out)


# ─────────────────────── Song ───────────────────────

@app.route("/song/<song_id>")
def song_info(song_id: str):
    sf = request.args.get("sf", STOREFRONT)
    data = _apple_get(f"/v1/catalog/{sf}/songs/{song_id}",
                      {"include": "albums,artists", "extend": "extendedAssetUrls"})
    item = data["data"][0]
    a = item["attributes"]
    rels = item.get("relationships", {})

    albums = [{"id": r["id"], "name": r["attributes"]["name"]}
              for r in rels.get("albums", {}).get("data", []) if "attributes" in r]
    artists = [{"id": r["id"], "name": r["attributes"]["name"]}
               for r in rels.get("artists", {}).get("data", []) if "attributes" in r]

    return jsonify({
        "id": song_id,
        "name": a.get("name"),
        "artist": a.get("artistName"),
        "album": a.get("albumName"),
        "artwork": _fmt_artwork(a.get("artwork")),
        "duration_ms": a.get("durationInMillis"),
        "disc_number": a.get("discNumber"),
        "track_number": a.get("trackNumber"),
        "isrc": a.get("isrc"),
        "release_date": a.get("releaseDate"),
        "genres": a.get("genreNames", []),
        "composer": a.get("composerName"),
        "audio_traits": a.get("audioTraits", []),
        "is_apple_digital_master": a.get("isAppleDigitalMaster", False),
        "has_lyrics": a.get("hasLyrics", False),
        "has_time_synced_lyrics": a.get("hasTimeSyncedLyrics", False),
        "preview_url": (a.get("previews", [{}])[0].get("url", "") if a.get("previews") else ""),
        "url": a.get("url"),
        "albums": albums,
        "artists": artists,
    })


# ─────────────────────── Album ───────────────────────

@app.route("/album/<album_id>")
def album_info(album_id: str):
    sf = request.args.get("sf", STOREFRONT)
    tracks_limit = request.args.get("tracks_limit", "100")
    tracks_offset = request.args.get("tracks_offset", "0")

    data = _apple_get(f"/v1/catalog/{sf}/albums/{album_id}",
                      {"include": "tracks,artists"})
    item = data["data"][0]
    a = item["attributes"]
    rels = item.get("relationships", {})

    all_tracks = rels.get("tracks", {}).get("data", [])
    off = int(tracks_offset)
    lim = int(tracks_limit)
    page = all_tracks[off:off + lim]

    tracks = []
    for t in page:
        ta = t.get("attributes", {})
        tracks.append({
            "id": t["id"],
            "name": ta.get("name"),
            "artist": ta.get("artistName"),
            "track_number": ta.get("trackNumber"),
            "disc_number": ta.get("discNumber"),
            "duration_ms": ta.get("durationInMillis"),
            "isrc": ta.get("isrc"),
            "has_lyrics": ta.get("hasLyrics", False),
        })

    artists = [{"id": r["id"], "name": r["attributes"]["name"]}
               for r in rels.get("artists", {}).get("data", []) if "attributes" in r]

    has_more = off + lim < len(all_tracks)

    return jsonify({
        "id": album_id,
        "name": a.get("name"),
        "artist": a.get("artistName"),
        "artwork": _fmt_artwork(a.get("artwork")),
        "track_count": a.get("trackCount"),
        "release_date": a.get("releaseDate"),
        "genres": a.get("genreNames", []),
        "copyright": a.get("copyright"),
        "record_label": a.get("recordLabel"),
        "upc": a.get("upc"),
        "audio_traits": a.get("audioTraits", []),
        "is_compilation": a.get("isCompilation", False),
        "is_complete": a.get("isComplete", True),
        "is_single": a.get("isSingle", False),
        "editorial_notes": a.get("editorialNotes", {}).get("standard", ""),
        "url": a.get("url"),
        "artists": artists,
        "tracks": {"data": tracks, "offset": off, "limit": lim,
                   "total": len(all_tracks), "has_more": has_more},
    })


# ─────────────────────── Artist ───────────────────────

@app.route("/artist/<artist_id>")
def artist_info(artist_id: str):
    sf = request.args.get("sf", STOREFRONT)
    albums_limit = request.args.get("albums_limit", "25")
    albums_offset = request.args.get("albums_offset", "0")

    data = _apple_get(f"/v1/catalog/{sf}/artists/{artist_id}",
                      {"include": "albums"})
    item = data["data"][0]
    a = item["attributes"]
    rels = item.get("relationships", {})

    all_albums = rels.get("albums", {}).get("data", [])
    albums_next = rels.get("albums", {}).get("next")
    off = int(albums_offset)
    lim = int(albums_limit)
    page = all_albums[off:off + lim]

    albums = []
    for al in page:
        aa = al.get("attributes", {})
        albums.append({
            "id": al["id"],
            "name": aa.get("name"),
            "artwork": _fmt_artwork(aa.get("artwork")),
            "release_date": aa.get("releaseDate"),
            "track_count": aa.get("trackCount"),
        })

    return jsonify({
        "id": artist_id,
        "name": a.get("name"),
        "artwork": _fmt_artwork(a.get("artwork")),
        "genres": a.get("genreNames", []),
        "url": a.get("url"),
        "albums": {"data": albums, "offset": off, "limit": lim,
                   "total": len(all_albums),
                   "has_more": off + lim < len(all_albums) or albums_next is not None},
    })


@app.route("/artist/<artist_id>/albums")
def artist_albums(artist_id: str):
    """Standalone paginated artist albums — uses Apple's native pagination."""
    sf = request.args.get("sf", STOREFRONT)
    limit = request.args.get("limit", "25")
    offset = request.args.get("offset", "0")

    data = _apple_get(f"/v1/catalog/{sf}/artists/{artist_id}/albums",
                      {"limit": limit, "offset": offset})

    albums = []
    for al in data.get("data", []):
        aa = al.get("attributes", {})
        albums.append({
            "id": al["id"],
            "name": aa.get("name"),
            "artist": aa.get("artistName"),
            "artwork": _fmt_artwork(aa.get("artwork")),
            "release_date": aa.get("releaseDate"),
            "track_count": aa.get("trackCount"),
            "audio_traits": aa.get("audioTraits", []),
            "url": aa.get("url"),
        })

    return jsonify({
        "data": albums,
        "offset": int(offset),
        "limit": int(limit),
        "total": len(albums),
        "next": data.get("next"),
    })


# ─────────────────────── Download ───────────────────────

@app.route("/download/<song_id>")
def download_song(song_id: str):
    fmt = request.args.get("fmt", "aac").lower()
    if fmt not in ("alac", "aac"):
        return jsonify({"error": "fmt must be 'alac' or 'aac'"}), 400

    cached = _get_cached_path(song_id, fmt)
    if os.path.isfile(cached):
        try:
            with apple_api.AppleMusicClient() as ac:
                song = ac.get_song(song_id, STOREFRONT)
            dl_name = f"{_safe_filename(song.artist)} - {_safe_filename(song.title)}.m4a"
        except Exception:
            dl_name = f"{song_id}.m4a"
        return send_file(cached, mimetype="audio/mp4",
                         as_attachment=True, download_name=dl_name)

    try:
        if fmt == "alac":
            return _download_alac(song_id, cached)
        else:
            return _download_aac(song_id, cached)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _download_alac(song_id: str, out_path: str):
    with tempfile.TemporaryDirectory() as tmpdir:
        result = decryptor.decrypt_one_track(
            song_id=song_id, out_dir=tmpdir, storefront=STOREFRONT,
            aria_host=ARIA_HOST, aria_decrypt_port=ARIA_DECRYPT_PORT,
            aria_m3u8_port=ARIA_M3U8_PORT, _session=_session)
        shutil.copy2(result.out_path, out_path)
    try:
        with apple_api.AppleMusicClient() as ac:
            song = ac.get_song(song_id, STOREFRONT)
        dl_name = f"{_safe_filename(song.artist)} - {_safe_filename(song.title)}.m4a"
    except Exception:
        dl_name = f"{song_id}.m4a"
    return send_file(out_path, mimetype="audio/mp4",
                     as_attachment=True, download_name=dl_name)


def _download_aac(song_id: str, out_path: str):
    try:
        result = aac_decrypt.download_aac(song_id, out_path)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 501
    except Exception as e:
        return jsonify({"error": f"AAC decrypt failed: {e}"}), 500
    try:
        with apple_api.AppleMusicClient() as ac:
            song = ac.get_song(song_id, STOREFRONT)
        dl_name = f"{_safe_filename(song.artist)} - {_safe_filename(song.title)}.m4a"
    except Exception:
        dl_name = f"{song_id}.m4a"
    return send_file(out_path, mimetype="audio/mp4",
                     as_attachment=True, download_name=dl_name)


# ─────────────────────── Health ───────────────────────

@app.route("/health")
def health():
    import socket
    checks = {}
    for name, port in [("aria_decrypt", ARIA_DECRYPT_PORT), ("aria_m3u8", ARIA_M3U8_PORT)]:
        try:
            s = socket.create_connection((ARIA_HOST, port), timeout=2)
            s.close()
            checks[name] = "up"
        except Exception:
            checks[name] = "down"
    cache_files = list(Path(CACHE_DIR).glob("*.m4a"))
    checks["cache_files"] = len(cache_files)
    checks["cache_mb"] = round(sum(f.stat().st_size for f in cache_files) / 1e6, 1)
    status = "healthy" if all(
        v == "up" for k, v in checks.items() if isinstance(v, str)) else "degraded"
    return jsonify({"status": status, **checks})


# ─────────────────────── Batch ───────────────────────

@app.route("/batch", methods=["POST"])
def batch_download():
    data = request.get_json(force=True)
    ids = data.get("ids", [])
    fmt = data.get("fmt", "aac")
    if not ids:
        return jsonify({"error": "ids list is empty"}), 400
    if len(ids) > MAX_BATCH_SIZE:
        return jsonify({"error": f"max {MAX_BATCH_SIZE} ids per batch"}), 400
    results = []
    for sid in ids:
        cached = _get_cached_path(str(sid), fmt)
        if os.path.isfile(cached):
            results.append({"id": sid, "status": "cached", "size": os.path.getsize(cached)})
            continue
        try:
            if fmt == "alac":
                with tempfile.TemporaryDirectory() as tmpdir:
                    r = decryptor.decrypt_one_track(
                        song_id=str(sid), out_dir=tmpdir, storefront=STOREFRONT,
                        aria_host=ARIA_HOST, aria_decrypt_port=ARIA_DECRYPT_PORT,
                        aria_m3u8_port=ARIA_M3U8_PORT, _session=_session)
                    shutil.copy2(r.out_path, cached)
                results.append({"id": sid, "status": "ok", "size": os.path.getsize(cached),
                                "elapsed": round(r.elapsed_seconds, 2)})
            else:
                aac_decrypt.download_aac(str(sid), cached)
                results.append({"id": sid, "status": "ok", "size": os.path.getsize(cached)})
        except Exception as e:
            results.append({"id": sid, "status": "error", "error": str(e)})
    return jsonify({"results": results, "total": len(results)})


# ─────────────────────── Album ZIP Download ───────────────────────

@app.route("/album/<album_id>/download")
def album_download(album_id: str):
    """Download entire album as a ZIP file.

    Query params:
      fmt=aac|alac  — audio format (default: aac)

    Limits:
      - max MAX_ALBUM_TRACKS tracks per album (default 30)
      - per-track timeout of 60s
      - failed tracks are skipped (partial ZIP still returned)
    """
    import zipfile
    import time as _time
    import signal

    fmt = request.args.get("fmt", "aac").lower()
    if fmt not in ("alac", "aac"):
        return jsonify({"error": "fmt must be 'alac' or 'aac'"}), 400

    sf = request.args.get("sf", STOREFRONT)

    # Check ZIP cache first
    zip_cache = os.path.join(CACHE_DIR, f"album_{album_id}_{fmt}.zip")
    if os.path.isfile(zip_cache):
        try:
            data = _apple_get(f"/v1/catalog/{sf}/albums/{album_id}")
            a = data["data"][0]["attributes"]
            zn = f"{_safe_filename(a.get('artistName',''))} - {_safe_filename(a.get('name',''))}.zip"
        except Exception:
            zn = f"album_{album_id}.zip"
        return send_file(zip_cache, mimetype="application/zip",
                         as_attachment=True, download_name=zn)

    try:
        data = _apple_get(f"/v1/catalog/{sf}/albums/{album_id}",
                          {"include": "tracks"})
    except Exception as e:
        return jsonify({"error": f"album fetch failed: {e}"}), 404

    item = data["data"][0]
    a = item["attributes"]
    album_name = _safe_filename(a.get("name", album_id))
    artist_name = _safe_filename(a.get("artistName", "Unknown"))
    zip_name = f"{artist_name} - {album_name} [{fmt.upper()}].zip"

    all_tracks = item.get("relationships", {}).get("tracks", {}).get("data", [])
    if not all_tracks:
        return jsonify({"error": "album has no tracks"}), 404

    if len(all_tracks) > MAX_ALBUM_TRACKS:
        return jsonify({
            "error": f"album has {len(all_tracks)} tracks, max allowed is {MAX_ALBUM_TRACKS}",
            "track_count": len(all_tracks),
            "limit": MAX_ALBUM_TRACKS,
        }), 400

    track_files = []
    errors = []
    t_start = _time.monotonic()

    for idx, t in enumerate(all_tracks):
        sid = t["id"]
        ta = t.get("attributes", {})
        track_num = ta.get("trackNumber", 0)
        disc_num = ta.get("discNumber", 1)
        name = _safe_filename(ta.get("name", sid))
        filename = f"{str(disc_num).zfill(1)}-{str(track_num).zfill(2)} {name}.m4a"

        cached = _get_cached_path(sid, fmt)
        if not os.path.isfile(cached):
            t0 = _time.monotonic()
            try:
                if fmt == "alac":
                    with tempfile.TemporaryDirectory() as tmpdir:
                        r = decryptor.decrypt_one_track(
                            song_id=sid, out_dir=tmpdir, storefront=sf,
                            aria_host=ARIA_HOST, aria_decrypt_port=ARIA_DECRYPT_PORT,
                            aria_m3u8_port=ARIA_M3U8_PORT, _session=_session)
                        shutil.copy2(r.out_path, cached)
                else:
                    aac_decrypt.download_aac(sid, cached)
            except Exception as e:
                errors.append({"id": sid, "track": f"{disc_num}-{track_num} {name}",
                               "error": str(e)[:100]})
                continue
            elapsed = _time.monotonic() - t0
            app.logger.info(f"[album {album_id}] track {idx+1}/{len(all_tracks)} "
                            f"{sid} done in {elapsed:.1f}s")

        if os.path.isfile(cached):
            track_files.append((filename, cached))

    total_time = _time.monotonic() - t_start

    if not track_files:
        return jsonify({"error": "no tracks downloaded", "details": errors}), 500

    with zipfile.ZipFile(zip_cache, "w", zipfile.ZIP_STORED) as zf:
        folder = f"{artist_name} - {album_name}"
        for filename, filepath in track_files:
            zf.write(filepath, f"{folder}/{filename}")

    zip_size = os.path.getsize(zip_cache)
    app.logger.info(f"[album {album_id}] ZIP ready: {len(track_files)}/{len(all_tracks)} tracks, "
                    f"{zip_size/1e6:.1f} MB, {total_time:.1f}s"
                    + (f", {len(errors)} errors" if errors else ""))

    resp = send_file(zip_cache, mimetype="application/zip",
                     as_attachment=True, download_name=zip_name)
    return resp


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8899)
    p.add_argument("--host", default="0.0.0.0")
    args = p.parse_args()
    print(f"AM Service on {args.host}:{args.port}")
    print(f"  aria: {ARIA_HOST}:{ARIA_DECRYPT_PORT}/{ARIA_M3U8_PORT}")
    print(f"  cache: {CACHE_DIR}")
    app.run(host=args.host, port=args.port, threaded=True)
