"""End-to-end song decryptor with three performance optimizations:

1. Download/decrypt overlap — start decrypting fragments as they arrive
2. Persistent TCP connection — reuse aria session across tracks (batch mode)
3. Prefetch pipeline — fetch next track's m3u8/M4S while decrypting current

Equivalent of a prior reference implementation, but significantly faster.
"""
from __future__ import annotations

import os
import re
import sys
import time
import threading
import struct
import socket
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from typing import Optional, List, Callable

import httpx

from . import apple_api, m3u8_select, m4a_writer, m4s_parser, aria_rpc

_URL_ALBUM_RE = re.compile(
    r"https?://(?:beta\.)?music\.apple\.com/(?P<sf>[a-z]{2})/album/[^/]+/(?P<id>\d+)")
_URL_ALBUM_SONG_RE = re.compile(
    r"https?://(?:beta\.)?music\.apple\.com/(?P<sf>[a-z]{2})/album/[^/]+/(?P<aid>\d+)\?i=(?P<sid>\d+)")
_URL_SONG_RE = re.compile(
    r"https?://(?:beta\.)?music\.apple\.com/(?P<sf>[a-z]{2})/song/[^/]+/(?P<id>\d+)")


@dataclass
class DecryptResult:
    song_id: str
    out_path: str
    sample_rate: int
    bit_depth: int
    samples_count: int
    decrypted_bytes: int
    elapsed_seconds: float


@dataclass
class _PreparedTrack:
    """Pre-fetched data ready for decryption — produced by the prefetch stage."""
    song_id: str
    song: apple_api.SongMeta
    variant: m3u8_select.AlacVariant
    parsed: m4s_parser.ParsedSong
    prepare_time: float


def _parse_apple_url(url: str) -> tuple[str, str, Optional[str]]:
    m = _URL_ALBUM_SONG_RE.search(url)
    if m:
        return m["sf"], m["aid"], m["sid"]
    m = _URL_ALBUM_RE.search(url)
    if m:
        return m["sf"], m["id"], None
    m = _URL_SONG_RE.search(url)
    if m:
        return m["sf"], "", m["id"]
    raise ValueError(f"unrecognized Apple Music URL: {url!r}")


# ━━━━━━━━━━━━━━━━━━━━ Persistent aria connection (opt #2) ━━━━━━━━━━━━━━━━━━

class PersistentDecryptSession:
    """Keeps a single TCP connection to aria across multiple tracks.

    Instead of connect→setup key→decrypt→close per track, this class
    connects once and reissues key context headers for each new track.
    Saves ~0.8s per track (TCP handshake + FairPlay key derivation).
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 47010,
                 timeout: float = 120.0):
        self._host = host
        self._port = port
        self._timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._current_desc: Optional[int] = None
        self._lock = threading.Lock()

    def _ensure_connected(self):
        if self._sock is None:
            self._sock = aria_rpc._make_socket(self._host, self._port, self._timeout)
            self._current_desc = None

    def _close(self):
        if self._sock:
            try:
                self._sock.sendall(b"\x00\x00\x00\x00\x00")
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            self._current_desc = None

    def _reconnect(self):
        self._close()
        self._ensure_connected()

    def decrypt_track_pipelined(
        self,
        samples: List[aria_rpc.Sample],
        keys: list[str],
        track_id: str,
        progress: Optional[Callable] = None,
    ) -> List[bytes]:
        """Decrypt all samples for one track using the persistent connection.

        The connection is reused across calls — only key context headers
        are re-sent. Falls back to reconnect on error.
        """
        if not samples:
            return []

        with self._lock:
            try:
                return self._decrypt_pipelined_inner(samples, keys, track_id, progress)
            except (ConnectionError, OSError, BrokenPipeError):
                self._reconnect()
                return self._decrypt_pipelined_inner(samples, keys, track_id, progress)

    def _decrypt_pipelined_inner(
        self,
        samples: List[aria_rpc.Sample],
        keys: list[str],
        track_id: str,
        progress: Optional[Callable],
    ) -> List[bytes]:
        self._ensure_connected()
        sock = self._sock
        assert sock is not None

        results: List[Optional[bytes]] = [None] * len(samples)
        error_box: List[Optional[Exception]] = [None]
        gate = threading.Semaphore(64)

        def _writer():
            try:
                current_desc: Optional[int] = None
                for sample in samples:
                    if error_box[0] is not None:
                        return
                    gate.acquire()
                    if sample.desc_index != current_desc:
                        if current_desc is not None:
                            sock.sendall(b"\x00\x00\x00\x00")
                        key_uri = keys[sample.desc_index]
                        tid = "0" if key_uri == aria_rpc.PREFETCH_KEY else track_id
                        tid_b = tid.encode("ascii")
                        kuri_b = key_uri.encode("utf-8")
                        sock.sendall(
                            bytes([len(tid_b)]) + tid_b
                            + bytes([len(kuri_b)]) + kuri_b
                        )
                        current_desc = sample.desc_index
                    sock.sendall(struct.pack("<I", len(sample.data)) + sample.data)
            except Exception as e:
                error_box[0] = e

        def _reader():
            try:
                total = sum(len(s.data) for s in samples)
                done = 0
                for idx, sample in enumerate(samples):
                    if error_box[0] is not None:
                        return
                    n = len(sample.data)
                    buf = bytearray()
                    while len(buf) < n:
                        chunk = sock.recv(min(n - len(buf), 262144))
                        if not chunk:
                            raise ConnectionError(f"aria closed at sample {idx}")
                        buf.extend(chunk)
                    results[idx] = bytes(buf)
                    done += n
                    gate.release()
                    if progress:
                        progress(done, total)
            except Exception as e:
                error_box[0] = e

        wt = threading.Thread(target=_writer, daemon=True)
        rt = threading.Thread(target=_reader, daemon=True)
        wt.start()
        rt.start()
        wt.join()
        rt.join()

        if error_box[0] is not None:
            self._reconnect()
            raise error_box[0]

        self._current_desc = None
        return results  # type: ignore

    def close(self):
        with self._lock:
            self._close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ━━━━━━━━━━━━━━━━━━━━ Prefetch + overlap pipeline (opt #1, #3) ━━━━━━━━━━━━

def _prepare_track(
    song_id: str,
    storefront: str,
    aria_host: str,
    aria_m3u8_port: int,
    max_sample_rate_hz: int,
    auth_token: Optional[str],
    media_user_token: Optional[str],
    apple_proxy: Optional[str],
) -> _PreparedTrack:
    """Fetch metadata + m3u8 + download M4S + parse — everything before decrypt."""
    t0 = time.monotonic()
    with apple_api.AppleMusicClient(
        authorization_token=auth_token,
        media_user_token=media_user_token,
        proxy=apple_proxy,
    ) as ac:
        song = ac.get_song(song_id, storefront)

    master_url = aria_rpc.fetch_master_playlist(
        song_id, host=aria_host, port=aria_m3u8_port)

    with httpx.Client(timeout=30.0, follow_redirects=True) as hc:
        master_text, _ = m3u8_select.fetch_master(master_url, client=hc)
    variant = m3u8_select.pick_alac_variant(
        master_text, master_url, max_sample_rate_hz=max_sample_rate_hz)

    parsed = m4s_parser.fetch_and_parse(variant.stream_url)

    return _PreparedTrack(
        song_id=song_id, song=song, variant=variant, parsed=parsed,
        prepare_time=time.monotonic() - t0,
    )


def decrypt_one_track(
    *,
    song_id: str,
    out_dir: str,
    storefront: str = "us",
    authorization_token: Optional[str] = None,
    media_user_token: Optional[str] = None,
    aria_host: str = "127.0.0.1",
    aria_decrypt_port: int = 47010,
    aria_m3u8_port: int = 47020,
    max_sample_rate_hz: int = 192_000,
    apple_proxy: Optional[str] = None,
    faststart: bool = False,
    progress: Optional[Callable] = None,
    _session: Optional[PersistentDecryptSession] = None,
    _prepared: Optional[_PreparedTrack] = None,
) -> DecryptResult:
    """Decrypt a single track. Accepts optional pre-built session and
    pre-fetched data for pipeline integration."""
    start = time.monotonic()
    os.makedirs(out_dir, exist_ok=True)

    if _prepared:
        prep = _prepared
    else:
        if progress:
            progress("meta", song_id)
        prep = _prepare_track(
            song_id, storefront, aria_host, aria_m3u8_port,
            max_sample_rate_hz, authorization_token, media_user_token,
            apple_proxy,
        )

    if progress:
        progress("decrypt", prep.parsed.total_data_size)

    def _prog(done, total):
        if progress:
            progress("decrypt-progress", (done, total))

    if _session:
        decrypted = _session.decrypt_track_pipelined(
            prep.parsed.samples, prep.variant.keys,
            track_id=song_id, progress=_prog)
    else:
        decrypted = aria_rpc.decrypt_samples_pipelined(
            prep.parsed.samples, prep.variant.keys,
            track_id=song_id, host=aria_host, port=aria_decrypt_port,
            progress=_prog)

    safe_title = re.sub(r'[/\\<>:"|?*]', "_", prep.song.title)
    base_name = f"{song_id}_{safe_title}".rstrip(". ")
    out_path = os.path.join(out_dir, f"{base_name}.m4a")
    if progress:
        progress("write", out_path)
    if faststart:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as tf:
            tmp_path = tf.name
        try:
            m4a_writer.patch_to_alac_m4a(prep.parsed, decrypted, tmp_path)
            m4a_writer.to_faststart_m4a(tmp_path, out_path)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    else:
        m4a_writer.patch_to_alac_m4a(prep.parsed, decrypted, out_path)

    elapsed = time.monotonic() - start
    if progress:
        progress("done", out_path)
    return DecryptResult(
        song_id=song_id,
        out_path=out_path,
        sample_rate=prep.variant.sample_rate_hz,
        bit_depth=prep.variant.bit_depth,
        samples_count=len(prep.parsed.samples),
        decrypted_bytes=prep.parsed.total_data_size,
        elapsed_seconds=elapsed,
    )


def decrypt_batch(
    song_ids: List[str],
    *,
    out_dir: str,
    storefront: str = "us",
    authorization_token: Optional[str] = None,
    media_user_token: Optional[str] = None,
    aria_host: str = "127.0.0.1",
    aria_decrypt_port: int = 47010,
    aria_m3u8_port: int = 47020,
    max_sample_rate_hz: int = 192_000,
    apple_proxy: Optional[str] = None,
    faststart: bool = False,
    prefetch_depth: int = 2,
    progress: Optional[Callable] = None,
) -> List[DecryptResult]:
    """Decrypt multiple tracks with all three optimizations:

    1. Persistent TCP session — one connection reused across all tracks
    2. Prefetch pipeline — download next track while decrypting current
    3. Pipelined decrypt — writer/reader threads within each track

    `prefetch_depth` controls how many tracks ahead to pre-download.
    """
    if not song_ids:
        return []

    results: List[DecryptResult] = []
    executor = ThreadPoolExecutor(max_workers=prefetch_depth)

    def _prefetch(sid: str) -> _PreparedTrack:
        return _prepare_track(
            sid, storefront, aria_host, aria_m3u8_port,
            max_sample_rate_hz, authorization_token, media_user_token,
            apple_proxy,
        )

    with PersistentDecryptSession(aria_host, aria_decrypt_port) as session:
        pending: List[tuple[str, Future]] = []
        submitted = 0

        # seed the prefetch pipeline
        for sid in song_ids[:prefetch_depth]:
            pending.append((sid, executor.submit(_prefetch, sid)))
            submitted += 1

        for idx, sid in enumerate(song_ids):
            # get the pre-fetched result for current track
            _, future = pending.pop(0)
            try:
                prep = future.result(timeout=120)
            except Exception as e:
                if progress:
                    progress("error", f"{sid}: {e}")
                continue

            # submit next prefetch
            if submitted < len(song_ids):
                next_sid = song_ids[submitted]
                pending.append((next_sid, executor.submit(_prefetch, next_sid)))
                submitted += 1

            # decrypt current track (while next is downloading in background)
            if progress:
                progress("track-start", f"[{idx+1}/{len(song_ids)}] {sid}")
            try:
                res = decrypt_one_track(
                    song_id=sid,
                    out_dir=out_dir,
                    storefront=storefront,
                    authorization_token=authorization_token,
                    media_user_token=media_user_token,
                    aria_host=aria_host,
                    aria_decrypt_port=aria_decrypt_port,
                    aria_m3u8_port=aria_m3u8_port,
                    max_sample_rate_hz=max_sample_rate_hz,
                    apple_proxy=apple_proxy,
                    faststart=faststart,
                    progress=progress,
                    _session=session,
                    _prepared=prep,
                )
                results.append(res)
            except Exception as e:
                if progress:
                    progress("error", f"{sid}: {e}")

    executor.shutdown(wait=False)
    return results
