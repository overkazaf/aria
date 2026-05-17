#!/usr/bin/env python3
"""Generate a synthetic 24-bit / 88.2 kHz stereo test signal that mirrors the
shape of Apple Music's Hi-Res Lossless ALAC stream (sample rate, bit depth,
channel count).  Used to exercise the encode/decode/package pipeline end to
end **without touching any copyrighted material**.

Composition (5 s, stereo):
  • Left channel:   220 Hz + 440 Hz + 880 Hz tonal stack
                    + 100 Hz → 20 kHz log chirp
  • Right channel:  reversed phase of the tonal stack
                    + 20 kHz → 100 Hz reverse chirp
  • -6 dBFS peak, fade-in/out 25 ms
"""
import numpy as np
from scipy.io import wavfile

SR     = 88_200
DUR    = 5.0
PEAK   = 0.5             # -6 dBFS
FADE_S = 0.025
N      = int(SR * DUR)
t      = np.linspace(0, DUR, N, endpoint=False)

# ── Tonal stack (left) ──
left_tones = (
    np.sin(2 * np.pi * 220 * t)
  + np.sin(2 * np.pi * 440 * t)
  + np.sin(2 * np.pi * 880 * t)
) / 3.0

# ── Log chirp (left) 100 → 20k ──
f0, f1 = 100.0, 20_000.0
k = (f1 / f0) ** (1.0 / DUR)
chirp_up = np.sin(2 * np.pi * f0 * (k ** t - 1) / np.log(k))

left = 0.7 * left_tones + 0.3 * chirp_up

# ── Right channel: phase-reversed stack + reverse chirp ──
right_tones = -left_tones
chirp_dn = chirp_up[::-1]
right = 0.7 * right_tones + 0.3 * chirp_dn

# ── Stack to stereo ──
stereo = np.column_stack([left, right])

# Normalize to PEAK
stereo *= PEAK / np.max(np.abs(stereo))

# Fade in / out
fade_n = int(FADE_S * SR)
env = np.ones(N)
env[:fade_n]   = np.linspace(0, 1, fade_n)
env[-fade_n:]  = np.linspace(1, 0, fade_n)
stereo *= env[:, None]

# Convert to int24-stored-in-int32 (scipy writes 24-bit by truncating int32)
# Use int32 with the lower 8 bits zeroed for a true int24 representation
i24_max = 2 ** 23 - 1
samples = (stereo * i24_max).astype(np.int32) << 8

wavfile.write("audio/signal.wav", SR, samples)
print(f"OK  audio/signal.wav  ({N} samples × 2ch × 24-bit @ {SR} Hz, {DUR}s)")
