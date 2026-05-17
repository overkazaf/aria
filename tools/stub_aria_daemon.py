#!/usr/bin/env python3
"""Stub `aria` daemon — speaks the wire protocol on ports 47010 / 47020 but
does NO actual decryption. Returns ciphertext verbatim ("NOP decrypt").

Useful for:
  • Exercising aria_rpc.py end-to-end without a real DRM helper
  • Smoke-testing client integrations (CLI, server.py, native_bridge.py)
  • Validating protocol implementations or schema changes

NOT useful for:
  • Actually decrypting Apple Music content (returns ciphertext unchanged)

Usage:
    python3 tools/stub_aria_daemon.py
    python3 tools/stub_aria_daemon.py --host 127.0.0.1 -D 47010 -M 47020
    python3 tools/stub_aria_daemon.py --master-url "http://127.0.0.1:8000/test.m3u8"

Then run the client as usual — `python cli.py download <id> --fmt alac`
will speak to this stub instead of a real helper. Output will be garbage
audio (encrypted samples treated as plaintext), but the protocol path and
all upstream code (m4s parsing, BMFF streaming, m4a writing) get exercised.
"""
from __future__ import annotations

import argparse
import logging
import socket
import socketserver
import struct
import sys
import threading
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("aria-stub")

SOCK_BUFSIZE = 256 * 1024
DEFAULT_MASTER_URL = "http://127.0.0.1:8000/stub/master.m3u8"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_DECRYPT_PORT = 47010
DEFAULT_M3U8_PORT = 47020

# Will be set from CLI args
_master_url: str = DEFAULT_MASTER_URL


# ───────────────────────── helpers ─────────────────────────

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), SOCK_BUFSIZE))
        if not chunk:
            raise ConnectionError(f"client closed (got {len(buf)}/{n} bytes)")
        buf.extend(chunk)
    return bytes(buf)


def _recv_len_prefixed_string(sock: socket.socket) -> str:
    """Read `1B len + N bytes`, decode as UTF-8."""
    ln = _recv_exact(sock, 1)[0]
    if ln == 0:
        return ""
    return _recv_exact(sock, ln).decode("utf-8", errors="replace")


# ───────────────────────── port 47020 (m3u8) ─────────────────────────

class M3u8Handler(socketserver.BaseRequestHandler):
    """Receive `1B len + adamId`, return master URL terminated by '\\n'."""

    def handle(self) -> None:
        sock: socket.socket = self.request
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            adam_id = _recv_len_prefixed_string(sock)
        except Exception as e:
            log.warning("m3u8: recv failed: %s", e)
            return
        log.info("m3u8 ← adamId=%s  →  %s", adam_id, _master_url)
        try:
            sock.sendall(_master_url.encode("utf-8") + b"\n")
        except OSError as e:
            log.warning("m3u8 send failed: %s", e)


# ───────────────────────── port 47010 (decrypt) ─────────────────────────

class DecryptHandler(socketserver.BaseRequestHandler):
    """NOP-decrypt: read key context + per-sample length-prefixed cipher;
    echo back ciphertext verbatim as the 'plaintext'.
    """

    def handle(self) -> None:
        sock: socket.socket = self.request
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        peer = self.client_address
        log.info("decrypt ← connection from %s:%d", peer[0], peer[1])

        keys_seen = 0
        samples_seen = 0
        bytes_seen = 0
        first_key_context = True

        try:
            while True:
                # Read either: 4B key-switch-sentinel(0), or 4B sample-length, or
                # 1B track_id-length (initial key context, not preceded by sentinel).
                if first_key_context:
                    self._read_key_context(sock)
                    keys_seen += 1
                    first_key_context = False

                # Peek 4 bytes to disambiguate
                # If all zero (0x00 0x00 0x00 0x00) → key switch (or end-of-session if 5th is 0)
                # Otherwise → LE uint32 sample length
                head = sock.recv(4, socket.MSG_WAITALL)
                if not head:
                    log.info("decrypt → client closed cleanly")
                    break
                if len(head) < 4:
                    log.warning("decrypt: truncated header (%d bytes)", len(head))
                    break

                if head == b"\x00\x00\x00\x00":
                    # End-of-prev-key sentinel OR start of end-of-session.
                    # Peek 5th byte: if also 0, end of session; else, new key context.
                    nxt = sock.recv(1, socket.MSG_PEEK)
                    if nxt == b"\x00":
                        # Consume the 5th zero byte
                        _ = _recv_exact(sock, 1)
                        log.info("decrypt ← end-of-session sentinel (5×0x00)")
                        break
                    else:
                        # New key context
                        log.info("decrypt ← key-switch sentinel (4×0x00) →"
                                 " reading new key context")
                        self._read_key_context(sock)
                        keys_seen += 1
                        continue

                # Treat as sample length
                (sample_len,) = struct.unpack("<I", head)
                if sample_len == 0:
                    log.warning("decrypt: zero-length sample (suspicious)")
                    continue
                if sample_len > 16 * 1024 * 1024:
                    log.warning("decrypt: implausibly large sample (%d bytes); aborting", sample_len)
                    break

                ciphertext = _recv_exact(sock, sample_len)
                # NOP "decrypt" — echo back
                sock.sendall(ciphertext)

                samples_seen += 1
                bytes_seen += sample_len
                if samples_seen % 500 == 0:
                    log.info("decrypt: %d samples (%s) processed",
                             samples_seen, _human(bytes_seen))

        except ConnectionError as e:
            log.info("decrypt → client disconnected: %s", e)
        except Exception:
            log.exception("decrypt: unexpected error")
        finally:
            log.info("decrypt session done: keys=%d samples=%d bytes=%s",
                     keys_seen, samples_seen, _human(bytes_seen))

    def _read_key_context(self, sock: socket.socket) -> None:
        """1B len + track_id + 1B len + keyUri."""
        track_id = _recv_len_prefixed_string(sock)
        key_uri = _recv_len_prefixed_string(sock)
        log.info("decrypt ← key context: track_id=%r keyUri=%r",
                 track_id, key_uri[:64] + ("…" if len(key_uri) > 64 else ""))


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ───────────────────────── server lifecycle ─────────────────────────

class _ReusableThreadedServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    global _master_url

    p = argparse.ArgumentParser(
        description="aria stub daemon — NOP-decrypt protocol responder")
    p.add_argument("--host", "-H", default=DEFAULT_HOST,
                   help=f"bind address (default: {DEFAULT_HOST})")
    p.add_argument("--decrypt-port", "-D", type=int, default=DEFAULT_DECRYPT_PORT,
                   help=f"decrypt port (default: {DEFAULT_DECRYPT_PORT})")
    p.add_argument("--m3u8-port", "-M", type=int, default=DEFAULT_M3U8_PORT,
                   help=f"m3u8 port (default: {DEFAULT_M3U8_PORT})")
    p.add_argument("--master-url", default=DEFAULT_MASTER_URL,
                   help="HLS master URL to return on m3u8 port "
                        f"(default: {DEFAULT_MASTER_URL})")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="enable DEBUG logging")
    args = p.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    _master_url = args.master_url

    decrypt_srv = _ReusableThreadedServer((args.host, args.decrypt_port),
                                          DecryptHandler)
    m3u8_srv = _ReusableThreadedServer((args.host, args.m3u8_port),
                                       M3u8Handler)

    t_dec = threading.Thread(target=decrypt_srv.serve_forever,
                             name="decrypt-srv", daemon=True)
    t_m3u = threading.Thread(target=m3u8_srv.serve_forever,
                             name="m3u8-srv", daemon=True)

    log.info("STUB aria daemon — NOT a real DRM helper, returns ciphertext as-is")
    log.info("  decrypt port:   tcp://%s:%d", args.host, args.decrypt_port)
    log.info("  m3u8 port:      tcp://%s:%d", args.host, args.m3u8_port)
    log.info("  master URL:     %s", args.master_url)
    log.info("Ctrl-C to stop.")

    t_dec.start()
    t_m3u.start()

    try:
        while True:
            threading.Event().wait(60)
    except KeyboardInterrupt:
        log.info("shutting down…")
        decrypt_srv.shutdown()
        m3u8_srv.shutdown()


if __name__ == "__main__":
    main()
