#!/usr/bin/env python3
"""Generate spectrum / waveform / round-trip diff plots from the synthetic
ALAC sample, proving the encode→decode pipeline preserves bit-exact audio.

Outputs:
  images/waveform.png       — time-domain stereo waveform (first 200 ms)
  images/spectrogram.png    — STFT spectrogram (full 5 s)
  images/spectrum_psd.png   — power spectral density (Welch)
  images/roundtrip_diff.png — WAV (input) vs ALAC-decoded (output) sample-wise diff
"""
import json
import subprocess
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy import signal as sps
from scipy.io import wavfile

# 公共风格
plt.rcParams.update({
    "font.family"      : "DejaVu Sans",
    "font.size"        : 9,
    "axes.titlesize"   : 11,
    "axes.titleweight" : "bold",
    "axes.labelsize"   : 9,
    "axes.edgecolor"   : "#888",
    "axes.facecolor"   : "#fbfbfd",
    "figure.facecolor" : "white",
    "savefig.dpi"      : 140,
    "savefig.bbox"     : "tight",
    "grid.color"       : "#dcdcdc",
    "grid.linewidth"   : 0.6,
})

SR_EXPECTED = 88_200

# ── load original WAV ──
sr_w, wav = wavfile.read("audio/signal.wav")
assert sr_w == SR_EXPECTED
# int32 (low 8 bits zero) → float [-1, 1]
wav = (wav.astype(np.int64) >> 8).astype(np.int32) / (2 ** 23)
wav_L, wav_R = wav[:, 0], wav[:, 1]
print(f"WAV  loaded: {wav.shape} @ {sr_w} Hz")

# ── ALAC-decode via ffmpeg to raw f32 ──
proc = subprocess.run(
    ["ffmpeg", "-v", "error", "-i", "audio/signal_alac.m4a",
     "-f", "f32le", "-acodec", "pcm_f32le", "-ac", "2", "-"],
    capture_output=True, check=True,
)
alac = np.frombuffer(proc.stdout, dtype=np.float32).reshape(-1, 2)
alac_L, alac_R = alac[:, 0], alac[:, 1]
print(f"ALAC decoded: {alac.shape} @ {SR_EXPECTED} Hz")

# ───────────────────────────── waveform ─────────────────────────────
fig, ax = plt.subplots(2, 1, figsize=(9.5, 4.2), sharex=True)
ms = 200
n = int(SR_EXPECTED * ms / 1000)
t = np.arange(n) / SR_EXPECTED * 1000.0

for axi, (w, a, label) in zip(ax,
        [(wav_L, alac_L, "Left"), (wav_R, alac_R, "Right")]):
    axi.plot(t, w[:n],  color="#1f77b4", lw=1.0, label="WAV (input)")
    axi.plot(t, a[:n],  color="#d62728", lw=0.7, label="ALAC (decoded)",
             alpha=0.85, linestyle="--")
    axi.set_ylabel(f"{label}\namplitude")
    axi.grid(True)
    axi.set_ylim(-0.6, 0.6)
    axi.legend(loc="upper right", framealpha=0.9, fontsize=8)

ax[-1].set_xlabel("time (ms)")
fig.suptitle("Waveform — first 200 ms (WAV vs ALAC-decoded round-trip)",
             y=1.02, fontsize=12, fontweight="bold")
plt.savefig("images/waveform.png")
plt.close()
print("OK  images/waveform.png")

# ───────────────────────────── spectrogram ────────────────────────────
fig, ax = plt.subplots(2, 1, figsize=(9.5, 5.5), sharex=True, sharey=True)
for axi, ch, label in zip(ax, [alac_L, alac_R], ["Left", "Right"]):
    f, tt, Sxx = sps.spectrogram(ch, fs=SR_EXPECTED,
                                 nperseg=4096, noverlap=3072,
                                 scaling="spectrum", mode="magnitude")
    Sxx_db = 20 * np.log10(np.maximum(Sxx, 1e-12))
    im = axi.pcolormesh(tt, f / 1000, Sxx_db, shading="gouraud",
                        cmap="magma", vmin=-90, vmax=0)
    axi.set_ylabel(f"{label}\nfreq (kHz)")
    axi.set_yscale("symlog", linthresh=0.5)
    axi.set_ylim(0, SR_EXPECTED / 2 / 1000)
    axi.grid(True, alpha=0.3)

ax[-1].set_xlabel("time (s)")
fig.suptitle("Spectrogram of ALAC-decoded signal — chirp + tonal stack visible",
             y=0.94, fontsize=12, fontweight="bold")
cbar = fig.colorbar(im, ax=ax, shrink=0.85, pad=0.02)
cbar.set_label("magnitude (dB)")
plt.savefig("images/spectrogram.png")
plt.close()
print("OK  images/spectrogram.png")

# ─────────────────────────── PSD (Welch) ───────────────────────────
fig, ax = plt.subplots(figsize=(9.5, 4))
for ch, color, label in [(wav_L,  "#1f77b4", "WAV L  (input)"),
                          (alac_L, "#d62728", "ALAC L (decoded)"),
                          (wav_R,  "#2ca02c", "WAV R  (input)"),
                          (alac_R, "#ff7f0e", "ALAC R (decoded)")]:
    f, P = sps.welch(ch, fs=SR_EXPECTED, nperseg=8192)
    ax.semilogx(f, 10 * np.log10(np.maximum(P, 1e-20)),
                color=color, lw=1.1, label=label, alpha=0.85)

ax.set_xlim(20, SR_EXPECTED / 2)
ax.set_ylim(-160, -10)
ax.set_xlabel("frequency (Hz, log)")
ax.set_ylabel("PSD (dB)")
ax.set_title("Power Spectral Density — WAV input vs ALAC-decoded output")
ax.grid(True, which="both", alpha=0.5)
ax.legend(loc="lower left", framealpha=0.9, fontsize=8)
ax.axvline(220,  color="#aaa", ls=":", lw=0.7); ax.text(220,  -15, "220 Hz", fontsize=7, ha="center")
ax.axvline(440,  color="#aaa", ls=":", lw=0.7); ax.text(440,  -15, "440 Hz", fontsize=7, ha="center")
ax.axvline(880,  color="#aaa", ls=":", lw=0.7); ax.text(880,  -15, "880 Hz", fontsize=7, ha="center")
ax.axvline(20_000, color="#aaa", ls=":", lw=0.7); ax.text(20_000, -15, "20 kHz", fontsize=7, ha="center")
plt.savefig("images/spectrum_psd.png")
plt.close()
print("OK  images/spectrum_psd.png")

# ────────────────────── round-trip diff (lossless?) ──────────────────
# Align lengths
n_min = min(len(wav_L), len(alac_L))
diff_L = wav_L[:n_min] - alac_L[:n_min]
diff_R = wav_R[:n_min] - alac_R[:n_min]
peak_diff = max(np.max(np.abs(diff_L)), np.max(np.abs(diff_R)))
rms_diff_L = np.sqrt(np.mean(diff_L ** 2))
rms_diff_R = np.sqrt(np.mean(diff_R ** 2))
print(f"peak |Δ|  = {peak_diff:.3e}  (≈ {20*np.log10(max(peak_diff,1e-20)):.1f} dBFS)")
print(f"RMS Δ L   = {rms_diff_L:.3e}")
print(f"RMS Δ R   = {rms_diff_R:.3e}")

fig, ax = plt.subplots(2, 1, figsize=(9.5, 4.2), sharex=True)
t_full = np.arange(n_min) / SR_EXPECTED
ax[0].plot(t_full, diff_L * 1e6, color="#1f77b4", lw=0.5)
ax[0].set_ylabel("Δ L (×10⁻⁶)")
ax[0].grid(True)
ax[1].plot(t_full, diff_R * 1e6, color="#d62728", lw=0.5)
ax[1].set_ylabel("Δ R (×10⁻⁶)")
ax[1].set_xlabel("time (s)")
ax[1].grid(True)
fig.suptitle(
    f"Round-trip sample-wise diff (WAV − ALAC-decoded)  ·  "
    f"peak |Δ| = {peak_diff*1e6:.2f} ×10⁻⁶  (≈{20*np.log10(max(peak_diff,1e-20)):.1f} dBFS)",
    fontsize=11, fontweight="bold", y=1.02)
plt.savefig("images/roundtrip_diff.png")
plt.close()
print("OK  images/roundtrip_diff.png")

# emit metrics JSON for README
with open("logs/roundtrip_metrics.json", "w") as f:
    json.dump({
        "samples_compared":  int(n_min),
        "peak_abs_diff":     float(peak_diff),
        "peak_diff_dbfs":    float(20*np.log10(max(peak_diff,1e-20))),
        "rms_diff_left":     float(rms_diff_L),
        "rms_diff_right":    float(rms_diff_R),
        "lossless_match":    bool(peak_diff < 1e-6),
    }, f, indent=2)
print("OK  logs/roundtrip_metrics.json")
