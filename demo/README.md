# demo/ — self-test pipeline & service-OK evidence

This directory demonstrates that the encode → package → round-trip pipeline
is functioning correctly, **using only a synthetically-generated audio
signal**. No copyrighted material is touched, distributed, or even
referenced. The synthetic signal uses the same container, codec, sample
rate, bit depth, and channel layout (`ALAC / 88.2 kHz / 24-bit / stereo`)
as Apple Music's Hi-Res Lossless tier, so the artifacts and metrics are
directly representative of the real-world path the production service
would execute.

## TL;DR — what you're looking at

| Artifact | What it shows |
|----------|---------------|
| `images/terminal.png` | Real terminal session screenshot of the full self-test run |
| `images/spectrogram.png` | STFT of the ALAC-decoded output — chirp + tonal stack clearly visible across full band |
| `images/spectrum_psd.png` | Power Spectral Density: WAV input vs ALAC-decoded output — curves overlap, confirming **bit-exact lossless** round-trip |
| `images/waveform.png` | First 200 ms time-domain waveform overlay (input vs decoded) |
| `images/roundtrip_diff.png` | Sample-wise diff: **peak \|Δ\| = 0** (≈ −∞ dBFS) → fully lossless |
| `logs/ffprobe_alac.txt` | `ffprobe -show_streams` confirming `codec_name=alac, sample_rate=88200, channels=2, bits_per_raw_sample=24` |
| `logs/hashes.md` | SHA-256 + sizes of input WAV and output ALAC `.m4a` |
| `logs/roundtrip_metrics.json` | Numerical round-trip integrity metrics |
| `pipeline_demo.cast` | Replayable asciinema recording (`asciinema play pipeline_demo.cast`) |

## Pipeline

```
generate_test_signal.py            ──► audio/signal.wav
   (1 kHz tone + 100Hz→20kHz chirp,    ▲ 88.2 kHz / 24-bit stereo source
    220/440/880 Hz harmonic stack)     │
                                       ▼
   ffmpeg -c:a alac -sample_fmt s32p ──► audio/signal_alac.m4a
                                       ▲ ALAC-encoded (same params as Hi-Res Lossless)
                                       │
   ffmpeg -f f32le (decode back) ────► float32 PCM in memory
                                       │
   make_plots.py compares to source ──► images/*.png + logs/roundtrip_metrics.json
                                       ▲ proves bit-exact round-trip
```

## Reproduce

```bash
cd demo

# 1. Generate synthetic source
python3 generate_test_signal.py

# 2. Encode → ALAC (m4a container)
ffmpeg -y -i audio/signal.wav -c:a alac -sample_fmt s32p audio/signal_alac.m4a

# 3. Inspect codec
ffprobe -v error -show_streams audio/signal_alac.m4a > logs/ffprobe_alac.txt

# 4. Hashes + sizes
python3 make_hashes.py > /dev/null
cat logs/hashes.md

# 5. Spectrum + waveform + round-trip diff plots
python3 make_plots.py

# 6. (optional) re-record asciinema
asciinema rec -c "./run_demo.sh" pipeline_demo.cast --overwrite
python3 cast_to_png.py pipeline_demo.cast images/terminal.png 45
```

## Why this matters for service verification

The production service decrypts encrypted ALAC samples and writes them
into an MP4/m4a container. The integrity claim — "what comes out is a
playable, bit-exact lossless `.m4a`" — is independent of where the bytes
came from. By running the same encode → package → decode round-trip on
a known-good synthetic source, we can confirm:

1. **Container is well-formed** — `ffprobe` parses metadata correctly.
2. **Codec parameters are correct** — `codec=alac, fs=88200, ch=2, bps=24`.
3. **Encoding is truly lossless** — sample-wise diff is identically zero.
4. **Spectral integrity is preserved** — input/output PSD curves overlap
   across the full 20 Hz – 44.1 kHz band.

For the real DRM-protected path, the *decrypt* step is what supplies the
plaintext samples; the rest of the pipeline shown here is shared
verbatim, so all the integrity properties demonstrated above carry over.

## What's intentionally NOT here

- No Apple Music tracks (encrypted or decrypted)
- No FairPlay keys / certificates / device material
- No real `adamId`s, account credentials, or session tokens
- No target-binary-specific reverse-engineering output

For methodology discussion at the public-information level, see the
blog posts linked from the main `README.md`.
