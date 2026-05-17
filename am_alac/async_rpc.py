"""Async TCP client for the *aria* helper, mirroring `aria_rpc.py`'s shape.

Why a parallel module
=====================
The synchronous `aria_rpc` is the right primitive for one-shot CLI
runs.  When N tracks need to be decrypted concurrently, however,
non-blocking I/O lets the event loop interleave aria RTTs across
several sessions and overlap HTTP fetches (Apple API, segment
downloads) with another track's `decrypt` phase.

The aria helper accepts multiple simultaneous connections, so the only
thing we need on our side is one TCP session per concurrent track and a
shared semaphore to cap fan-out.

Wire format is identical to `aria_rpc`; see that file's module
docstring for the byte-level spec.
"""
from __future__ import annotations

import asyncio
import struct
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Iterable, List, Optional

from .aria_rpc import PREFETCH_KEY, PREFETCH_TRACK_ID, Sample


# ──────────────────────────── m3u8 port (47020) ────────────────────────────

async def fetch_master_playlist(
    adam_id: str,
    *,
    host: str = "127.0.0.1",
    port: int = 47020,
    timeout: float = 30.0,
) -> str:
    """Async equivalent of `aria_rpc.fetch_master_playlist`."""
    if not adam_id.isdigit():
        raise ValueError(f"adamId must be ASCII digits, got {adam_id!r}")
    if len(adam_id) > 255:
        raise ValueError(f"adamId too long for 1-byte length prefix: {len(adam_id)}")

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=timeout)
    try:
        writer.write(bytes([len(adam_id)]) + adam_id.encode("ascii"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=timeout)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass

    text = line.rstrip(b"\n").strip()
    if not text:
        raise RuntimeError(f"aria returned empty m3u8 URL for adamId={adam_id}")
    return text.decode("utf-8", errors="replace")


# ──────────────────────────── decrypt port (47010) ─────────────────────────

class AsyncDecryptSession:
    """Async counterpart to `aria_rpc.DecryptSession`.

    Acquired via `async with AsyncDecryptSession(...) as sess:` so the
    end-of-stream sentinel (5×0x00) is always sent on clean exit. On an
    exception we drop the connection without a sentinel — aria's
    other sessions are unaffected.
    """

    def __init__(
        self,
        track_id: str,
        keys: List[str],
        *,
        host: str = "127.0.0.1",
        port: int = 47010,
        timeout: float = 60.0,
    ):
        if not track_id:
            raise ValueError("track_id must be non-empty")
        for k in keys:
            if len(k) > 255:
                raise ValueError(
                    f"keyUri too long for 1-byte length prefix: {len(k)}")
        self.track_id = track_id
        self.keys = list(keys)
        self.host = host
        self.port = port
        self.timeout = timeout
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._current_desc: Optional[int] = None

    # ── context management ──

    async def __aenter__(self) -> "AsyncDecryptSession":
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=self.timeout,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            if self._writer is not None and exc_type is None:
                try:
                    self._writer.write(b"\x00\x00\x00\x00\x00")
                    await self._writer.drain()
                except (ConnectionError, OSError):
                    pass
        finally:
            if self._writer is not None:
                self._writer.close()
                try:
                    await self._writer.wait_closed()
                except (ConnectionError, OSError):
                    pass
                self._writer = None
                self._reader = None

    # ── internal helpers ──

    async def _switch_key_context(self, desc_index: int) -> None:
        assert self._writer is not None
        if not (0 <= desc_index < len(self.keys)):
            raise IndexError(
                f"desc_index {desc_index} out of range for {len(self.keys)} keys")
        key_uri = self.keys[desc_index]
        track_id = (
            PREFETCH_TRACK_ID if key_uri == PREFETCH_KEY else self.track_id)

        if self._current_desc is not None:
            self._writer.write(b"\x00\x00\x00\x00")

        track_id_b = track_id.encode("ascii")
        key_uri_b = key_uri.encode("utf-8")
        if len(track_id_b) > 255 or len(key_uri_b) > 255:
            raise ValueError("track_id or keyUri exceeds 1-byte length prefix")
        self._writer.write(
            bytes([len(track_id_b)]) + track_id_b
            + bytes([len(key_uri_b)]) + key_uri_b
        )
        await self._writer.drain()
        self._current_desc = desc_index

    # ── public ──

    async def decrypt(self, sample: Sample) -> bytes:
        """Decrypt one sample on this session. Switches key context if needed."""
        assert self._writer is not None and self._reader is not None, \
            "use within `async with AsyncDecryptSession(...)`"
        if sample.desc_index != self._current_desc:
            await self._switch_key_context(sample.desc_index)

        self._writer.write(struct.pack("<I", len(sample.data)) + sample.data)
        await self._writer.drain()
        return await asyncio.wait_for(
            self._reader.readexactly(len(sample.data)),
            timeout=self.timeout,
        )


async def decrypt_samples_async(
    samples: Iterable[Sample],
    keys: List[str],
    track_id: str,
    *,
    host: str = "127.0.0.1",
    port: int = 47010,
    progress: Optional[callable] = None,
) -> List[bytes]:
    """Decrypt all samples on a single async session, returning a list.

    Returns a list rather than yielding so the caller can `await` once
    and get the full result. For very large tracks consider using the
    session directly to stream samples one at a time.
    """
    samples = list(samples)
    total = sum(len(s.data) for s in samples)
    done = 0
    out: List[bytes] = []

    async with AsyncDecryptSession(
            track_id, keys, host=host, port=port) as sess:
        for s in samples:
            pt = await sess.decrypt(s)
            out.append(pt)
            done += len(pt)
            if progress is not None:
                progress(done, total)
    return out
