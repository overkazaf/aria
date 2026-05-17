"""AAC decryption via Widevine CDM.

Handles: webplayback API → PSSH extraction → pywidevine license
→ mp4decrypt → decrypted AAC m4a.

Requires:
  - pywidevine + mp4decrypt in PATH
  - User-supplied Apple Music tokens & Widevine device file (see SETUP.md):
      $ARIA_CONFIG    → JSON with accessToken / mediaUserToken
                        (default: ~/.config/aria/config.json)
      $ARIA_WVD_DIR   → directory containing *.wvd device files
                        (default: ~/.config/aria/wvd)
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

CONFIG_FILE = Path(os.environ.get(
    "ARIA_CONFIG",
    os.path.expanduser("~/.config/aria/config.json"),
))
DEVICE_DIR = Path(os.environ.get(
    "ARIA_WVD_DIR",
    os.path.expanduser("~/.config/aria/wvd"),
))


@dataclass
class AacResult:
    song_id: str
    key: str
    stream_url: str
    out_path: str
    size: int


def _load_config() -> dict:
    """Load token config.

    Accepts either a plain JSON file (preferred) or a base64-encoded JSON
    blob (for back-compat).  See `config.example.json` for the schema.
    """
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(
            f"Aria config not found at {CONFIG_FILE}. "
            f"Set $ARIA_CONFIG or create the file (see config.example.json).")
    raw = CONFIG_FILE.read_text().strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(base64.b64decode(raw).decode())


def _get_wvd_path() -> Path:
    cfg = _load_config()
    p = Path(cfg.get("devicePath", ""))
    if p.exists():
        return p
    wvds = list(DEVICE_DIR.glob("*.wvd"))
    if wvds:
        return wvds[0]
    raise FileNotFoundError("No .wvd device file found")


def _save_token(at: str) -> None:
    """Persist a refreshed accessToken back to the user config."""
    try:
        cfg = _load_config()
    except FileNotFoundError:
        cfg = {}
    cfg["accessToken"] = at
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def _probe_token(at: str) -> bool:
    """Cheap accessToken validity probe — empty body means expired."""
    try:
        r = httpx.get(
            "https://amp-api.music.apple.com/v1/catalog/us/songs/1450330685",
            headers={"Authorization": f"Bearer {at}",
                     "Origin": "https://music.apple.com"},
            timeout=10,
        )
        return r.status_code == 200 and r.text.strip() != ""
    except Exception:
        return False


def _get_tokens() -> tuple[str, str]:
    """Return (accessToken, mediaUserToken).

    Auto-refresh the accessToken via apple_api.get_web_token() if missing or
    expired; persisted result is written back to the config file.
    """
    cfg = _load_config()
    at = cfg.get("accessToken", "")
    mut = cfg.get("mediaUserToken", "")

    if not at or not _probe_token(at):
        from .apple_api import get_web_token
        at = get_web_token()
        _save_token(at)
    return at, mut


def _get_webplayback(song_id: str, access_token: str, media_user_token: str) -> dict:
    if not media_user_token:
        raise RuntimeError(
            "mediaUserToken is missing — set `mediaUserToken` in "
            f"{CONFIG_FILE} (see SETUP.md for how to fetch one).")
    r = httpx.post(
        "https://play.itunes.apple.com/WebObjects/MZPlay.woa/wa/webPlayback",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Origin": "https://music.apple.com",
            "Media-User-Token": media_user_token,
        },
        json={"salableAdamId": song_id, "language": "en-US"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if "failureType" in data:
        msg = data.get("customerMessage", str(data))
        if "Sign in again" in msg or "session" in msg.lower():
            raise RuntimeError(
                "Apple webPlayback rejected the request: mediaUserToken "
                f"appears to be expired. Refresh it in {CONFIG_FILE} "
                "(DevTools → Application → Cookies → media-user-token on "
                "music.apple.com).  Server message: " + msg)
        raise RuntimeError(f"webplayback failed: {msg}")
    return data.get("songList", [{}])[0]


def _get_license(song_id: str, key_uri: str, challenge: str,
                 access_token: str, media_user_token: str,
                 license_url: str) -> str:
    r = httpx.post(
        license_url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Origin": "https://music.apple.com",
            "Media-User-Token": media_user_token,
        },
        json={
            "adamId": song_id,
            "challenge": challenge,
            "isLibrary": False,
            "key-system": "com.widevine.alpha",
            "uri": key_uri,
            "user-initiated": True,
        },
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if "license" not in data:
        raise RuntimeError(f"no license in response: {data}")
    return data["license"]


def get_decryption_key(song_id: str) -> tuple[str, str]:
    """Return (decryption_key_hex, encrypted_stream_url) for an AAC track.

    Uses Widevine CDM to derive the content key from Apple's license server.
    """
    from pywidevine import PSSH, Cdm, Device
    from pywidevine.license_protocol_pb2 import WidevinePsshData

    at, mut = _get_tokens()
    wp = _get_webplayback(song_id, at, mut)

    assets = wp.get("assets", [])
    # Prefer 256kbps AAC (flavor 28:ctrp256)
    audio_asset = None
    for pref in ["28:ctrp256", "30:cbcp256", "37:ibhp256"]:
        for a in assets:
            if a.get("flavor", "") == pref:
                audio_asset = a
                break
        if audio_asset:
            break
    if not audio_asset:
        for a in assets:
            if a.get("URL"):
                audio_asset = a
                break
    if not audio_asset:
        raise RuntimeError("no audio asset in webplayback response")

    stream_url = audio_asset["URL"]
    license_url = wp.get("hls-key-server-url", "")
    if not license_url:
        raise RuntimeError("missing license URL in webplayback")

    # Parse HLS manifest to extract PSSH and segment URLs
    import m3u8 as m3u8lib

    m3u = m3u8lib.load(stream_url)

    # Extract PSSH from EXT-X-KEY URI (format: data:text/plain;base64,<key_id>)
    key_uri = ""
    if m3u.keys:
        for k in m3u.keys:
            if k and k.uri and "base64," in k.uri:
                key_uri = k.uri
                break
    if not key_uri:
        raise RuntimeError("no Widevine PSSH found in HLS manifest keys")

    # download segment_map[0].uri — this is the complete encrypted m4a,
    # NOT individual HLS segments
    if not m3u.segment_map:
        raise RuntimeError("no segment map in HLS manifest")
    download_uri = m3u.segment_map[0].uri
    if not download_uri.startswith("http"):
        download_uri = m3u.base_uri + download_uri

    wvd_path = _get_wvd_path()
    cdm = Cdm.from_device(Device.load(wvd_path))

    pssh_data = WidevinePsshData()
    pssh_data.algorithm = 1
    pssh_data.key_ids.append(base64.b64decode(key_uri.split(",")[1]))
    pssh_obj = PSSH(pssh_data.SerializeToString())

    session = cdm.open()
    challenge = base64.b64encode(
        cdm.get_license_challenge(session, pssh_obj)
    ).decode()

    license_b64 = _get_license(song_id, key_uri, challenge, at, mut, license_url)
    cdm.parse_license(session, license_b64)

    dec_key = next(
        k for k in cdm.get_keys(session) if k.type == "CONTENT"
    ).key.hex()

    cdm.close(session)
    return dec_key, download_uri


def download_aac(song_id: str, out_path: str) -> AacResult:
    """Full pipeline: get key → download encrypted m4a → mp4decrypt → output."""
    dec_key, download_url = get_decryption_key(song_id)

    mp4decrypt = shutil.which("mp4decrypt")
    if not mp4decrypt:
        raise FileNotFoundError("mp4decrypt not in PATH")

    with tempfile.TemporaryDirectory() as tmpdir:
        enc = os.path.join(tmpdir, "enc.m4a")
        dec = os.path.join(tmpdir, "dec.m4a")

        with httpx.stream("GET", download_url, follow_redirects=True, timeout=60) as r:
            r.raise_for_status()
            with open(enc, "wb") as f:
                for chunk in r.iter_bytes(32768):
                    f.write(chunk)

        ret = subprocess.run(
            [mp4decrypt, "--key", f"1:{dec_key}", enc, dec],
            capture_output=True, timeout=60)
        if ret.returncode != 0 or not os.path.isfile(dec):
            raise RuntimeError(f"mp4decrypt failed: {ret.stderr.decode()[:300]}")

        shutil.copy2(dec, out_path)

    return AacResult(
        song_id=song_id,
        key=dec_key,
        stream_url=download_url,
        out_path=out_path,
        size=os.path.getsize(out_path),
    )
