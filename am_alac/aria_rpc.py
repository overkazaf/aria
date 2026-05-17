"""TCP client for the out-of-tree DRM helper referred to here as **aria**.

The helper itself is built and run separately from this repository (its
internals are intentionally not published here).  This module only speaks
to it over two raw, locally-unauthenticated TCP ports.

Default ports: 47010 (decrypt) / 47020 (m3u8).  The helper accepts
whatever values are passed to its `-D` / `-M` flags; the wire protocol
described below is independent of the chosen port numbers.

Port 47020 (m3u8 / get-stream):
    client → server : 1B len + N bytes adamId (ASCII digits)
    server → client : enhanced-HLS master URL, terminated by '\n'

Port 47010 (decrypt, sample-by-sample):
    Per (descIndex change):
        [if not first] 4B 0x00000000  (end-of-prev-key sentinel)
        1B len + N bytes track_id  (ASCII digits)
        1B len + N bytes keyUri    (e.g. "skd://itunes.apple.com/.../...")
    Per sample:
        4B LE uint32 length
        N bytes encrypted sample
        ← N bytes decrypted (same length)
    End of session:
        5B 0x00 00 00 00 00

Wire protocol observed by black-box probing of the helper; specific source
references are intentionally omitted from this public release.
"""
from __future__ import annotations

import socket
import struct
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from queue import Queue, Empty
from typing import Iterable, Iterator, List, Optional

PREFETCH_KEY = "skd://itunes.apple.com/P000000000/s1/e1"
PREFETCH_TRACK_ID = "0"

_SOCK_BUFSIZE = 256 * 1024  # 256 KB send/recv buffer


def _make_socket(host: str, port: int, timeout: float) -> socket.socket:
    s = socket.create_connection((host, port), timeout=timeout)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, _SOCK_BUFSIZE)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _SOCK_BUFSIZE)
    s.settimeout(timeout)
    return s


# ──────────────────────────── m3u8 port (47020) ────────────────────────────

def fetch_master_playlist(
    adam_id: str,
    *,
    host: str = "127.0.0.1",
    port: int = 47020,
    timeout: float = 30.0,
) -> str:
    if not adam_id.isdigit():
        raise ValueError(f"adamId must be ASCII digits, got {adam_id!r}")
    if len(adam_id) > 255:
        raise ValueError(f"adamId too long for 1-byte length prefix: {len(adam_id)}")

    with _make_socket(host, port, timeout) as s:
        s.sendall(bytes([len(adam_id)]) + adam_id.encode("ascii"))
        buf = bytearray()
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf.extend(chunk)
            if b"\n" in chunk:
                break
        line = bytes(buf).split(b"\n", 1)[0].strip()
    if not line:
        raise RuntimeError(f"aria returned empty m3u8 URL for adamId={adam_id}")
    return line.decode("utf-8", errors="replace")


# ──────────────────────────── decrypt port (47010) ─────────────────────────

@dataclass
class Sample:
    data: bytes
    desc_index: int
    duration: int = 0


class DecryptSession:
    """A single connected session to aria's decrypt port."""

    def __init__(
        self,
        track_id: str,
        keys: list[str],
        *,
        host: str = "127.0.0.1",
        port: int = 47010,
        timeout: float = 120.0,
    ):
        if not track_id:
            raise ValueError("track_id must be non-empty")
        for k in keys:
            if len(k) > 255:
                raise ValueError(f"keyUri too long for 1-byte length prefix: {len(k)}")
        self.track_id = track_id
        self.keys = list(keys)
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._current_desc: Optional[int] = None

    def __enter__(self) -> "DecryptSession":
        self._sock = _make_socket(self.host, self.port, self.timeout)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._sock is not None and exc_type is None:
                try:
                    self._sock.sendall(b"\x00\x00\x00\x00\x00")
                except OSError:
                    pass
        finally:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(min(n - len(buf), _SOCK_BUFSIZE))
            if not chunk:
                raise ConnectionError(
                    f"aria closed mid-decrypt (got {len(buf)}/{n} bytes)")
            buf.extend(chunk)
        return bytes(buf)

    def _switch_key_context(self, desc_index: int) -> None:
        assert self._sock is not None
        if not (0 <= desc_index < len(self.keys)):
            raise IndexError(
                f"desc_index {desc_index} out of range for {len(self.keys)} keys")
        key_uri = self.keys[desc_index]
        track_id = PREFETCH_TRACK_ID if key_uri == PREFETCH_KEY else self.track_id

        if self._current_desc is not None:
            self._sock.sendall(b"\x00\x00\x00\x00")

        track_id_b = track_id.encode("ascii")
        key_uri_b = key_uri.encode("utf-8")
        if len(track_id_b) > 255 or len(key_uri_b) > 255:
            raise ValueError("track_id or keyUri exceeds 1-byte length prefix")
        header = (
            bytes([len(track_id_b)]) + track_id_b
            + bytes([len(key_uri_b)]) + key_uri_b
        )
        self._sock.sendall(header)
        self._current_desc = desc_index

    def decrypt(self, sample: Sample) -> bytes:
        assert self._sock is not None, "use within `with DecryptSession(...)`"
        if sample.desc_index != self._current_desc:
            self._switch_key_context(sample.desc_index)
        self._sock.sendall(struct.pack("<I", len(sample.data)) + sample.data)
        return self._recv_exact(len(sample.data))


# ─────────────────── pipelined bulk decrypt (writer+reader threads) ───────

def decrypt_samples_pipelined(
    samples: List[Sample],
    keys: list[str],
    track_id: str,
    *,
    host: str = "127.0.0.1",
    port: int = 47010,
    timeout: float = 120.0,
    pipeline_depth: int = 64,
    progress: Optional[callable] = None,
) -> List[bytes]:
    """Decrypt all samples using TCP pipelining for maximum throughput.

    Instead of send-wait-recv per sample, we split into a writer thread
    (sends all ciphertexts back-to-back) and a reader (collects all
    plaintexts). The TCP stream is full-duplex so this eliminates the
    round-trip latency bottleneck. A bounded semaphore caps how far the
    writer can get ahead of the reader (pipeline_depth), preventing the
    server's recv buffer from overflowing.

    Returns list of plaintext bytes in the same order as input samples.
    """
    if not samples:
        return []

    results: List[Optional[bytes]] = [None] * len(samples)
    error_box: List[Optional[Exception]] = [None]
    gate = threading.Semaphore(pipeline_depth)

    sock = _make_socket(host, port, timeout)

    def _writer():
        try:
            current_desc: Optional[int] = None
            for idx, sample in enumerate(samples):
                if error_box[0] is not None:
                    return
                gate.acquire()

                if sample.desc_index != current_desc:
                    if current_desc is not None:
                        sock.sendall(b"\x00\x00\x00\x00")
                    key_uri = keys[sample.desc_index]
                    tid = PREFETCH_TRACK_ID if key_uri == PREFETCH_KEY else track_id
                    tid_b = tid.encode("ascii")
                    kuri_b = key_uri.encode("utf-8")
                    header = (
                        bytes([len(tid_b)]) + tid_b
                        + bytes([len(kuri_b)]) + kuri_b
                    )
                    sock.sendall(header)
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
                    chunk = sock.recv(min(n - len(buf), _SOCK_BUFSIZE))
                    if not chunk:
                        raise ConnectionError(
                            f"aria closed mid-decrypt at sample {idx} "
                            f"(got {len(buf)}/{n} bytes)")
                    buf.extend(chunk)
                results[idx] = bytes(buf)
                done += n
                gate.release()
                if progress is not None:
                    progress(done, total)
        except Exception as e:
            error_box[0] = e

    writer_t = threading.Thread(target=_writer, daemon=True)
    reader_t = threading.Thread(target=_reader, daemon=True)

    writer_t.start()
    reader_t.start()
    writer_t.join()
    reader_t.join()

    try:
        if error_box[0] is None:
            sock.sendall(b"\x00\x00\x00\x00\x00")
    except OSError:
        pass
    sock.close()

    if error_box[0] is not None:
        raise error_box[0]

    return results  # type: ignore[return-value]


# ─────────────── streaming pipelined (constant memory) ────────────────────

def decrypt_samples_streaming(
    samples: List[Sample],
    keys: list[str],
    track_id: str,
    on_sample: callable,
    *,
    host: str = "127.0.0.1",
    port: int = 47010,
    timeout: float = 120.0,
    pipeline_depth: int = 64,
    progress: Optional[callable] = None,
) -> int:
    """Like decrypt_samples_pipelined but calls on_sample(idx, plaintext)
    instead of accumulating results in RAM. Peak memory = 1 sample.

    Returns total bytes decrypted.
    """
    if not samples:
        return 0

    error_box: List[Optional[Exception]] = [None]
    gate = threading.Semaphore(pipeline_depth)
    sock = _make_socket(host, port, timeout)

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
                    tid = PREFETCH_TRACK_ID if key_uri == PREFETCH_KEY else track_id
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
                    chunk = sock.recv(min(n - len(buf), _SOCK_BUFSIZE))
                    if not chunk:
                        raise ConnectionError(f"aria closed at sample {idx}")
                    buf.extend(chunk)
                on_sample(idx, bytes(buf))
                done += n
                gate.release()
                if progress is not None:
                    progress(done, total)
        except Exception as e:
            error_box[0] = e

    wt = threading.Thread(target=_writer, daemon=True)
    rt = threading.Thread(target=_reader, daemon=True)
    wt.start()
    rt.start()
    wt.join()
    rt.join()

    try:
        if error_box[0] is None:
            sock.sendall(b"\x00\x00\x00\x00\x00")
    except OSError:
        pass
    sock.close()

    if error_box[0] is not None:
        raise error_box[0]

    return sum(len(s.data) for s in samples)


# ─────────────────── legacy sequential API (kept for compatibility) ───────

def decrypt_samples(
    samples: Iterable[Sample],
    keys: list[str],
    track_id: str,
    *,
    host: str = "127.0.0.1",
    port: int = 47010,
    progress: Optional[callable] = None,
) -> Iterator[bytes]:
    """Stream-decrypt all samples sequentially. Use decrypt_samples_pipelined
    for better throughput."""
    samples = list(samples)
    total = sum(len(s.data) for s in samples)
    done = 0

    with DecryptSession(track_id, keys, host=host, port=port) as sess:
        for s in samples:
            pt = sess.decrypt(s)
            done += len(pt)
            if progress is not None:
                progress(done, total)
            yield pt
