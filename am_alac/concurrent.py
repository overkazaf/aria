"""Bounded-concurrency orchestrator: decrypt many tracks in parallel.

Design
======
A track's lifecycle is

      meta  →  m3u8  →  download  →  decrypt  →  write
       ↑           HTTP             aria        mostly
                                                CPU

Stages 1-3 are network-bound (latency dominates); stage 4 is bound by
aria's RTT × n_samples; stage 5 is mostly CPU but small.  Aria
happily accepts multiple concurrent decrypt sessions, so the right
strategy is to run the *whole* pipeline per-track on an asyncio task
and cap the fan-out with a semaphore.

We stand up

  - a single shared `httpx.AsyncClient` so HTTP/2 multiplexing kicks in
    against amp-api.music.apple.com (Apple terminates HTTP/2);
  - a `decrypt_semaphore` capping live decrypt sessions (one per track
    holding it);
  - a `pipeline_semaphore` capping live whole pipelines (defaults to
    `max(2, decrypt_concurrency)` — there's no benefit to opening 32
    HTTP fetches when only 4 can decrypt).

Telemetry
=========
For each track we emit per-stage timing as a JSON line on a caller-
supplied stream.  Schema:

    {"event":"track_stage","song_id":"...","stage":"download",
     "elapsed_ms":3120,"started_at_ms":172939…}

Plus higher-level events:

    {"event":"track_started","song_id":"..."}
    {"event":"track_done",   "song_id":"...","out_path":"...",
     "total_ms":17284,"bytes":48721344}
    {"event":"track_failed", "song_id":"...","error":"...",
     "stage":"decrypt"}

The orchestrator returns a list of `DecryptResult | Exception` matching
the input order, so the caller can decide its own retry policy without
losing partial successes.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional, Sequence, TextIO, Union

import httpx

from . import apple_api, async_rpc, m3u8_select, m4a_writer, m4s_parser
from .decryptor import DecryptResult


# ──────────────────────────── telemetry ────────────────────────────────────

class _StageTimer:
    """Records per-stage durations for a single track and emits events."""

    def __init__(self, song_id: str, sink: Optional[TextIO]):
        self.song_id = song_id
        self.sink = sink
        self.t0 = time.monotonic()
        self.stage_starts: dict[str, float] = {}
        self.stage_durations: dict[str, float] = {}

    def _emit(self, **kw) -> None:
        if self.sink is None:
            return
        rec = {"t": int(time.monotonic() * 1000), **kw}
        self.sink.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.sink.flush()

    def begin(self, stage: str) -> None:
        self.stage_starts[stage] = time.monotonic()
        self._emit(event="track_stage_begin", song_id=self.song_id, stage=stage)

    def end(self, stage: str) -> None:
        start = self.stage_starts.get(stage, self.t0)
        ms = int((time.monotonic() - start) * 1000)
        self.stage_durations[stage] = ms / 1000.0
        self._emit(event="track_stage_end", song_id=self.song_id,
                   stage=stage, elapsed_ms=ms)

    def total_ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)


# ──────────────────────────── orchestrator ─────────────────────────────────

@dataclass
class OrchestratorConfig:
    out_dir: str
    storefront: str = "us"
    auth_token: Optional[str] = None
    media_user_token: Optional[str] = None
    apple_proxy: Optional[str] = None

    aria_host: str = "127.0.0.1"
    aria_decrypt_port: int = 47010
    aria_m3u8_port: int = 47020

    max_sample_rate_hz: int = 192_000
    faststart: bool = False

    decrypt_concurrency: int = 4
    pipeline_concurrency: Optional[int] = None  # default = decrypt_concurrency

    telemetry_sink: Optional[TextIO] = None     # JSONL, one line per event


_FILENAME_BAD = re.compile(r'[/\\<>:"|?*]')


def _safe_filename(song_id: str, title: str) -> str:
    return f"{song_id}_{_FILENAME_BAD.sub('_', title)}".rstrip(". ")


async def _decrypt_one_track(
    song_id: str,
    cfg: OrchestratorConfig,
    auth_token_lock: asyncio.Lock,
    auth_token_holder: dict,
    decrypt_sem: asyncio.Semaphore,
    pipeline_sem: asyncio.Semaphore,
    http_client: httpx.AsyncClient,
) -> DecryptResult:
    """Run one full pipeline.  Holds `pipeline_sem` for the entire run and
    `decrypt_sem` only during the aria-decrypt phase, so that the
    metadata/HTTP stages of one track can overlap with the decrypt phase
    of another."""
    timer = _StageTimer(song_id, cfg.telemetry_sink)
    timer._emit(event="track_started", song_id=song_id)
    os.makedirs(cfg.out_dir, exist_ok=True)

    async with pipeline_sem:
        # 1. metadata via Apple Music REST.  We share a single auto-refreshed
        # web token across tasks; first task to need one fetches it under
        # the lock, the rest pick it up.
        timer.begin("meta")
        async with auth_token_lock:
            if not auth_token_holder.get("token") and not cfg.auth_token:
                auth_token_holder["token"] = await _bootstrap_token(http_client)
        token = cfg.auth_token or auth_token_holder["token"]
        song_meta = await _apple_get_song(
            http_client, song_id, cfg.storefront, token,
            cfg.media_user_token)
        timer.end("meta")

        # 2. aria m3u8 RPC for the enhanced HLS master URL
        timer.begin("m3u8")
        master_url = await async_rpc.fetch_master_playlist(
            song_id,
            host=cfg.aria_host, port=cfg.aria_m3u8_port)

        master_resp = await http_client.get(master_url, follow_redirects=True)
        master_resp.raise_for_status()
        master_text = master_resp.text
        variant = m3u8_select.pick_alac_variant(
            master_text, master_url,
            max_sample_rate_hz=cfg.max_sample_rate_hz)
        timer.end("m3u8")

        # 3. download encrypted m4s
        timer.begin("download")
        m4s_resp = await http_client.get(variant.stream_url, follow_redirects=True)
        m4s_resp.raise_for_status()
        m4s_bytes = m4s_resp.content
        parsed = m4s_parser.parse_m4s(m4s_bytes)
        timer.end("download")

        # 4. decrypt — gated by decrypt_sem so we don't oversubscribe aria
        timer.begin("decrypt")
        async with decrypt_sem:
            timer._emit(event="decrypt_session_acquired", song_id=song_id)
            decrypted = await async_rpc.decrypt_samples_async(
                parsed.samples, variant.keys, song_id,
                host=cfg.aria_host, port=cfg.aria_decrypt_port)
        timer.end("decrypt")

        # 5. write
        timer.begin("write")
        out_path = os.path.join(
            cfg.out_dir,
            f"{_safe_filename(song_id, song_meta.title)}.m4a")
        if cfg.faststart:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as tf:
                tmp_path = tf.name
            try:
                await asyncio.to_thread(
                    m4a_writer.patch_to_alac_m4a, parsed, decrypted, tmp_path)
                await asyncio.to_thread(
                    m4a_writer.to_faststart_m4a, tmp_path, out_path)
            finally:
                try: os.unlink(tmp_path)
                except OSError: pass
        else:
            await asyncio.to_thread(
                m4a_writer.patch_to_alac_m4a, parsed, decrypted, out_path)
        timer.end("write")

    total_ms = timer.total_ms()
    timer._emit(event="track_done",
                song_id=song_id, out_path=out_path,
                total_ms=total_ms,
                bytes=parsed.total_data_size)
    return DecryptResult(
        song_id=song_id,
        out_path=out_path,
        sample_rate=variant.sample_rate_hz,
        bit_depth=variant.bit_depth,
        samples_count=len(parsed.samples),
        decrypted_bytes=parsed.total_data_size,
        elapsed_seconds=total_ms / 1000.0,
    )


# Apple API helpers — async wrappers around the sync httpx client used by
# `apple_api.AppleMusicClient`. We keep that sync class as the source of
# truth for endpoint URLs and response parsing; the helpers below just
# replay those requests over httpx.AsyncClient.

async def _bootstrap_token(http_client: httpx.AsyncClient) -> str:
    """Async equivalent of `apple_api.get_web_token`."""
    r = await http_client.get(
        "https://beta.music.apple.com/", follow_redirects=True)
    r.raise_for_status()
    m = apple_api._INDEX_JS_RE.search(r.content)
    if not m:
        raise RuntimeError("no /assets/index*-*.js link found")
    js_path = m.group(0).decode("ascii")
    r = await http_client.get(
        "https://beta.music.apple.com" + js_path, follow_redirects=True)
    r.raise_for_status()
    m = apple_api._TOKEN_RE.search(r.content)
    if not m:
        raise RuntimeError("no eyJh... token found in index JS")
    return m.group(0).decode("ascii")


async def _apple_get_song(
    http_client: httpx.AsyncClient,
    song_id: str,
    storefront: str,
    auth_token: str,
    media_user_token: Optional[str],
) -> apple_api.SongMeta:
    url = f"{apple_api.API_HOST}/v1/catalog/{storefront}/songs/{song_id}"
    headers = {
        "Authorization": f"Bearer {auth_token}",
        "User-Agent": apple_api.UA_BROWSER,
        "Origin": "https://beta.music.apple.com",
    }
    if media_user_token:
        headers["Media-User-Token"] = media_user_token
    r = await http_client.get(url, headers=headers, params={
        "include": "albums,artists",
        "extend": "extendedAssetUrls",
    })
    r.raise_for_status()
    return apple_api.SongMeta._from_response(r.json(), song_id, storefront)


async def decrypt_tracks_concurrent(
    song_ids: Sequence[str],
    cfg: OrchestratorConfig,
) -> List[Union[DecryptResult, Exception]]:
    """Run all `song_ids` concurrently. Returns one result per id, in
    input order; exceptions are returned in-place rather than raised so a
    single bad id doesn't kill the batch."""
    pipeline_n = cfg.pipeline_concurrency or max(2, cfg.decrypt_concurrency)
    decrypt_sem  = asyncio.Semaphore(cfg.decrypt_concurrency)
    pipeline_sem = asyncio.Semaphore(pipeline_n)
    auth_token_lock = asyncio.Lock()
    auth_token_holder: dict = {"token": None}

    limits = httpx.Limits(
        max_connections=pipeline_n * 4,
        max_keepalive_connections=pipeline_n * 2,
    )
    transport = (httpx.AsyncHTTPTransport(proxy=cfg.apple_proxy)
                 if cfg.apple_proxy else None)

    async with httpx.AsyncClient(
        timeout=60.0, http2=True, limits=limits, transport=transport
    ) as http_client:
        tasks = [
            asyncio.create_task(_decrypt_one_track(
                sid, cfg,
                auth_token_lock, auth_token_holder,
                decrypt_sem, pipeline_sem, http_client,
            ), name=f"track-{sid}")
            for sid in song_ids
        ]
        results: List[Union[DecryptResult, Exception]] = []
        for sid, task in zip(song_ids, tasks):
            try:
                results.append(await task)
            except Exception as exc:
                if cfg.telemetry_sink is not None:
                    rec = {
                        "t": int(time.monotonic() * 1000),
                        "event": "track_failed",
                        "song_id": sid,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    cfg.telemetry_sink.write(json.dumps(rec) + "\n")
                    cfg.telemetry_sink.flush()
                results.append(exc)
    return results
