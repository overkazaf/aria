# Setup — what you need to run aria

This repository ships **the orchestration / pipeline / API code only**. To
exercise the real download paths you need to supply a few external
pieces yourself. The list below tells you what is required for each
feature, where the code looks for it, and what can be skipped.

## 1. Python deps

```bash
pip install -r requirements.txt
# requirements.txt: flask, httpx, m3u8, pywidevine
```

For demos / plots / asciinema rendering (optional):

```bash
pip install numpy scipy matplotlib pyte
```

External binaries on `PATH`:

| Binary | Used for | Install |
|--------|----------|---------|
| `ffmpeg` / `ffprobe` | container muxing, metadata inspection | `apt install ffmpeg` |
| `mp4decrypt` (Bento4) | AAC Widevine decryption step | https://www.bento4.com |
| `asciinema` | re-recording the demo cast (optional) | `pip install asciinema` |

## 2. Apple Music tokens — required for `/search`, `/song`, `/album`, AAC download

```bash
mkdir -p ~/.config/aria
cp config.example.json ~/.config/aria/config.json
$EDITOR ~/.config/aria/config.json
```

Fields:

| Field | Required for | Where to get it |
|-------|--------------|-----------------|
| `accessToken` | All catalog endpoints + AAC download | DevTools → Network → any `amp-api.music.apple.com` request → copy the `Authorization: Bearer eyJh…` JWT. If omitted, the service tries to auto-bootstrap an anonymous web-player token; that token works for `search` / `song` / `album` but **not** for `download`. |
| `mediaUserToken` | AAC download (your account context) | DevTools → Application → Cookies → `media-user-token` on `music.apple.com`. |
| `devicePath` | AAC download | Optional. Absolute path to a `.wvd` device file. If omitted, the first `*.wvd` in `~/.config/aria/wvd/` is used. |

Override the config location with `ARIA_CONFIG=/some/other/path.json`.

## 3. Widevine device file (`.wvd`) — required for AAC download

Apple Music's AAC tier is protected by Widevine. You need a Widevine
device blob to issue licenses. This is **not** supplied here:

```bash
mkdir -p ~/.config/aria/wvd
cp /path/to/your_device.wvd ~/.config/aria/wvd/
```

How to obtain a `.wvd`: there are public tools (e.g. `pywidevine create-device`)
that build one from extracted client_id + private_key material. Obtaining
those is your responsibility and out of scope for this repository.

Override with `ARIA_WVD_DIR=/some/other/dir`.

## 4. ALAC path — requires an external `aria` DRM helper

The ALAC (Hi-Res Lossless) path delegates the FairPlay decryption to a
separate, locally-running TCP service that this repository **does NOT
ship**. The Python code only speaks the wire protocol to it.

You have two options:

### Option 4a — real helper (decrypts actual content)

```bash
# Start your own helper (out of scope for this repo)
./your-aria-helper -D 47010 -M 47020 -H 127.0.0.1
```

The wire protocol the helper must speak is fully specified in
[`docs/PROTOCOL.md`](docs/PROTOCOL.md) — feel free to implement your
own.

### Option 4b — stub helper (NOP-decrypt, smoke-tests only)

For protocol / integration testing without any real DRM material, this
repo ships a stub that speaks the protocol correctly but returns
ciphertext unchanged ("NOP decrypt"):

```bash
python3 tools/stub_aria_daemon.py
# or: python3 tools/stub_aria_daemon.py --master-url "http://your-test-host/master.m3u8"
```

This lets you exercise `aria_rpc.py`, `streaming_bmff.py`,
`m4a_writer.py`, the CLI, and `server.py` end-to-end. Output audio will
be garbage (encrypted samples treated as plaintext), but all the
non-DRM plumbing gets validated.

### Port / host overrides (both options)

```bash
export ARIA_HOST=127.0.0.1
export ARIA_DECRYPT_PORT=47010
export ARIA_M3U8_PORT=47020
```

If no helper is available (real or stub), only the AAC path works.
`server.py` will still start; ALAC requests fail when the TCP connect
times out.

## 5. Native C client (optional, fastest)

```bash
cd native_decrypt
make            # builds aria_client + decrypt_samples
```

`am_alac/native_bridge.py` will spawn `./aria_client` if present;
otherwise the pure-Python `aria_rpc.py` path is used.

## 6. Verify

```bash
# health check, no tokens needed
python server.py --port 8899 &
curl http://localhost:8899/health

# self-test pipeline (no Apple servers contacted)
cd demo && ./run_demo.sh
```

## What works without external pieces?

| Feature | Needs | Status |
|---------|-------|--------|
| `/health` | nothing | ✅ |
| Self-test (`demo/`) | ffmpeg | ✅ |
| `/search` / `/song` / `/album` / `/artist` | accessToken (or auto-bootstrap) | ✅ |
| AAC `/download` | tokens + `.wvd` + `mp4decrypt` | ⚠️ user-supplied |
| ALAC `/download?fmt=alac` (real) | aria DRM helper running on 47010/47020 | ⚠️ NOT shipped — bring your own (see `docs/PROTOCOL.md`) |
| ALAC `/download?fmt=alac` (smoke-test only) | `tools/stub_aria_daemon.py` | ✅ NOP-decrypt; protocol validated but output audio is garbage |

If a piece is missing the rest still runs; nothing else is required.
