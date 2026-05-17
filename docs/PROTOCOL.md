# aria wire protocol

The `aria` DRM helper exposes two raw TCP ports. Both are
**locally-unauthenticated** and use simple length-prefixed binary
framing — no TLS, no JSON, no auth header.

This document specifies the byte-level wire format so that:

- third-party clients can talk to a real helper without reading the
  Python source;
- a stub helper (`tools/stub_aria_daemon.py`) can be implemented for
  CI / smoke-testing without any real DRM material;
- the protocol can be re-implemented in other languages.

The reference Python client lives in `am_alac/aria_rpc.py`.

> **Default ports** (overridable via helper `-D` / `-M` flags or via the
> `ARIA_DECRYPT_PORT` / `ARIA_M3U8_PORT` env vars on the client):
>   - `47010` — decrypt
>   - `47020` — m3u8 (master playlist resolution)

---

## Port `47020` — m3u8 / get-stream

Resolves an Apple Music `adamId` to an enhanced-HLS master playlist URL.
One round-trip per connection.

```
client                            server (aria helper)
  │                                       │
  │ ── 1B  uint8  len(adamId) ──────────► │
  │ ── N   bytes adamId (ASCII digits) ─► │
  │                                       │
  │                                       │ ── adamId → Apple ──┐
  │                                       │ ←─ HLS master URL ──┘
  │                                       │
  │ ◄── UTF-8 master URL ────────────────│
  │ ◄── 0x0a ('\n' terminator) ──────────│
  │                                       │
  ▼ connection closed                     ▼
```

### Send (client → server)

| Offset | Size | Type   | Field    | Notes                              |
|-------:|-----:|--------|----------|------------------------------------|
|      0 |    1 | uint8  | `adamIdLen` | 1–255                           |
|      1 |  N=`adamIdLen` | bytes  | `adamId` | ASCII digits only        |

`adamId` is the Apple Music catalog ID (e.g. `1440841263`). The helper
validates it; non-digit or out-of-range IDs may yield an empty response.

### Receive (server → client)

Stream of UTF-8 bytes terminated by `0x0a` (`'\n'`). The line **before**
the newline is the HLS master URL. Anything after the newline (including
absent / EOF) is ignored.

The connection is closed by the server after writing the URL.

### Examples

```
client → server:  0a 31 34 34 30 38 34 31 32 36 33
                  └─┘ └────── '1440841263' ──────┘
                  10

server → client:  "https://aod-ssl.itunes.apple.com/itunes-assets/.../master.m3u8\n"
```

---

## Port `47010` — decrypt (sample-by-sample)

Full-duplex pipelined stream-cipher channel. One connection can carry:

- one or more **key contexts** (each scoped to a `track_id` + `keyUri`);
- per-context: zero or more **samples**, each `(LE-uint32 length, N
  bytes ciphertext) → N bytes plaintext`;
- terminated by an end-of-session sentinel.

```
client                            server (aria helper)
  │                                       │
  │ ── key context #0 (initial)  ───────► │
  │     1B + track_id                     │
  │     1B + keyUri                       │
  │                                       │
  │ ── sample[0]: 4B len + N cipher ────► │
  │ ◄── N plaintext ────────────────────│
  │ ── sample[1]: 4B len + N cipher ────► │
  │ ◄── N plaintext ────────────────────│
  │     …                                 │
  │                                       │
  │ ── 4×0x00  (key-switch sentinel) ───► │
  │ ── key context #1                     │
  │     1B + track_id                     │
  │     1B + keyUri                       │
  │                                       │
  │ ── sample[k]: 4B len + N cipher ────► │
  │ ◄── N plaintext ────────────────────│
  │     …                                 │
  │                                       │
  │ ── 5×0x00  (end-of-session sentinel)► │
  ▼ close                                  ▼
```

### Key context (header)

Sent at the start of a session and again after every key-switch sentinel.
Length-prefixed pair:

| Size | Field           | Notes                                                   |
|-----:|-----------------|---------------------------------------------------------|
|    1 | `trackIdLen`    | 1–255                                                   |
|    N | `trackId`       | ASCII digits (e.g. `"1440841263"`)                      |
|    1 | `keyUriLen`     | 1–255                                                   |
|    N | `keyUri`        | UTF-8 (typically `skd://...`); a known prefetch sentinel `skd://itunes.apple.com/P000000000/s1/e1` is used to "warm" the helper without affecting playback |

The helper uses `trackId` + `keyUri` to derive / look up the per-track
content key. No plaintext key ever crosses the wire.

### Per-sample request / response

| Direction | Size | Field        | Notes                                  |
|-----------|-----:|--------------|----------------------------------------|
| client →  |    4 | `len`        | LE `uint32`, length of ciphertext bytes that follow (and of plaintext that will come back) |
| client →  |    N | `ciphertext` | encrypted sample                       |
| server →  |    N | `plaintext`  | decrypted sample, **same length** as `ciphertext` |

Apple's SAMPLE-AES uses AES-128-CBC per sample, so plaintext and
ciphertext have the same length (block size matches input on
already-block-aligned samples; padding handled inside the helper).

### Key-switch sentinel

When changing to a new `(trackId, keyUri)` mid-session (typical when an
ALAC stream has multiple `EXT-X-KEY` entries across variants):

```
client → server:  00 00 00 00          // 4 × 0x00 sentinel
client → server:  <new key context>    // 1B+trackId + 1B+keyUri
```

The 4-byte sentinel signals "previous key context done, expect a new
header next." It is **not** sent before the first key context.

### End-of-session sentinel

```
client → server:  00 00 00 00 00       // 5 × 0x00
```

Signals the session is complete. The server closes the connection. This
is distinct from the key-switch sentinel by virtue of the **5th** zero
byte; clients that look ahead to disambiguate should peek for the 5th
byte after seeing a 4-byte zero header.

### Throughput / pipelining

The wire protocol is full-duplex. Clients SHOULD NOT
send-then-wait-then-send; instead, split into a writer thread (sends all
`(len, ciphertext)` back-to-back) and a reader thread (collects all
plaintexts in arrival order), bounded by a semaphore to cap how far the
writer can outrun the reader.

The reference client (`decrypt_samples_pipelined` in `aria_rpc.py`) uses
a pipeline depth of 64. Going higher offers diminishing returns and may
overflow the server's recv buffer.

`TCP_NODELAY` is **mandatory** on both client and server sides: the
header is small (5 B) and the per-sample length prefix is small (4 B);
without `TCP_NODELAY`, Nagle's algorithm batches them with 40 ms delays
and the throughput drops by 50×.

### Examples

Single track, two samples, then end-of-session (hex, client side):

```
0A 31 34 34 30 38 34 31 32 36 33          // 1B + "1440841263"
27 73 6B 64 3A 2F 2F 69 74 75 6E 65 73 ...   // 1B + "skd://itunes..."
40 00 00 00  AA AA AA … (0x40 bytes)      // sample[0]: LE 64, 64 bytes
80 00 00 00  BB BB BB … (0x80 bytes)      // sample[1]: LE 128, 128 bytes
00 00 00 00 00                            // end of session
```

---

## Implementing a helper

A minimum-viable helper implementing the protocol (but doing no
real DRM work, returning ciphertext verbatim as "plaintext") lives at
`tools/stub_aria_daemon.py`. Use it as a reference for protocol
correctness, framing edge cases, and the key-switch / end-of-session
disambiguation.

```bash
# Start the stub (NOP-decrypt, returns input bytes as "plaintext")
python3 tools/stub_aria_daemon.py
```

For a real helper, the encryption / key-derivation core is **out of
scope** for this repository — see the project README for context.

---

## Error handling

The protocol has **no error frames**. The helper signals failure by
closing the connection prematurely. Clients should:

- treat any short read (recv returning fewer bytes than requested before
  EOF) as a fatal error for the current session;
- not assume the helper validates `keyUri` syntactically — invalid keys
  may simply yield garbage plaintext or a stalled response;
- enforce a per-session timeout (the reference client uses 120 s).
