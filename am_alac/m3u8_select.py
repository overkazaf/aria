"""Pick the right ALAC variant from an Apple Music master playlist and
extract the SKD key URIs.

Reverse-engineered from a prior reference implementation
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
import m3u8


# --- key URI regex (a prior reference implementation) ---
# Matches "skd://..." in master playlist text (NOT just EXT-X-KEY lines —
# Apple's master shows the SKD URIs verbatim in some lines).
_SKD_RE = re.compile(r'"(skd?://[^"]*)"')


@dataclass
class AlacVariant:
    """A picked variant with everything needed for download + decrypt."""
    sample_rate_hz: int      # e.g. 44100, 48000, 96000, 192000
    bit_depth: int           # 16 or 24
    average_bandwidth: int
    audio: str               # e.g. "audio-stereo-256-44100-16"
    codecs: str              # always "alac" for our path
    stream_url: str          # full URL of the encrypted M4S (.../_m.mp4)
    keys: list[str]          # ordered SKD URIs (index = descIndex)


def _alac_codec_match(codecs: str) -> bool:
    """Codecs string in master is `alac` for our path."""
    return codecs == "alac"


def fetch_master(url: str, *, client: Optional[httpx.Client] = None) -> tuple[str, "m3u8.M3U8"]:
    own = client is None
    if own:
        client = httpx.Client(timeout=30.0, follow_redirects=True)
    try:
        r = client.get(url)
        r.raise_for_status()
        body = r.text
        playlist = m3u8.loads(body, uri=url)
        return body, playlist
    finally:
        if own:
            client.close()


def pick_alac_variant(
    master_text: str,
    master_url: str,
    *,
    max_sample_rate_hz: int = 192_000,
) -> AlacVariant:
    """Pick the highest-bandwidth ALAC variant whose sample-rate fits the cap.

    Mirrors a prior reference implementation.
    """
    master = m3u8.loads(master_text, uri=master_url)
    if not master.is_variant:
        raise ValueError("playlist is not a master / variant playlist")

    # Sort highest bandwidth first.
    variants = list(master.playlists)
    variants.sort(
        key=lambda v: int(getattr(v.stream_info, "average_bandwidth", 0) or 0),
        reverse=True,
    )

    chosen = None
    for v in variants:
        codecs = (v.stream_info.codecs or "").strip()
        if not _alac_codec_match(codecs):
            continue
        # Apple's audio group looks like:
        # GROUP-ID="audio-alac-stereo-128-48000-16"
        # AUDIO field in EXT-X-STREAM-INF mirrors that group id; we want
        # the second-to-last numeric part (sample-rate) ≤ cap.
        audio_id = v.stream_info.audio or ""
        parts = audio_id.split("-")
        if len(parts) < 2:
            continue
        try:
            sr = int(parts[-2])
            bd = int(parts[-1])
        except ValueError:
            continue
        if sr > max_sample_rate_hz:
            continue
        chosen = (v, sr, bd, audio_id, codecs)
        break

    if chosen is None:
        raise LookupError(
            f"no ALAC variant ≤ {max_sample_rate_hz} Hz in master playlist")

    v, sr, bd, audio_id, codecs = chosen

    # Build the encrypted M4S URL: replace .m3u8 → _m.mp4 (a prior reference implementation)
    variant_uri = urljoin(master_url, v.uri)
    if variant_uri.endswith(".m3u8"):
        stream_url = variant_uri[:-len(".m3u8")] + "_m.mp4"
    else:
        stream_url = variant_uri + "_m.mp4"

    # Extract SKD URIs (a prior reference implementation).
    # Index 0 is reserved for the prefetch sentinel.
    keys: list[str] = ["skd://itunes.apple.com/P000000000/s1/e1"]
    for m in _SKD_RE.finditer(master_text):
        uri = m.group(1)
        # a prior reference implementation filters by suffix; for ALAC tracks Apple uses c23 / c6.
        if uri.endswith("c23") or uri.endswith("c6"):
            keys.append(uri)

    return AlacVariant(
        sample_rate_hz=sr,
        bit_depth=bd,
        average_bandwidth=int(getattr(v.stream_info, "average_bandwidth", 0) or 0),
        audio=audio_id,
        codecs=codecs,
        stream_url=stream_url,
        keys=keys,
    )
