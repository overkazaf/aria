#!/usr/bin/env bash
# Self-test pipeline demo — runs locally, NO Apple Music access required.
# Used as the script for the asciinema recording.
set -e
cd "$(dirname "$0")"

GREEN='\033[1;32m'; CYAN='\033[1;36m'; YEL='\033[1;33m'; DIM='\033[2m'; NC='\033[0m'
pause() { sleep 0.6; }
say()   { echo -e "${CYAN}\$ $*${NC}"; pause; eval "$*"; pause; }

echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  aria — Self-Test Pipeline                                        ${NC}"
echo -e "${GREEN}  (synthetic audio · NO copyrighted material involved)             ${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo
pause

echo -e "${YEL}# Step 1 — Generate synthetic 5 s @ 88.2 kHz / 24-bit stereo source${NC}"
say "python3 generate_test_signal.py"

echo -e "${YEL}# Step 2 — Encode to ALAC (Apple Lossless), same params as Hi-Res${NC}"
say "ffmpeg -y -hide_banner -loglevel error -i audio/signal.wav -c:a alac -sample_fmt s32p audio/signal_alac.m4a && ls -lh audio/"

echo -e "${YEL}# Step 3 — Inspect the encoded ALAC stream (codec / sample rate / bit depth)${NC}"
say "ffprobe -v error -show_streams -select_streams a audio/signal_alac.m4a | head -15"

echo -e "${YEL}# Step 4 — SHA-256 + sizes for verification${NC}"
say "python3 make_hashes.py | column -t"

echo -e "${YEL}# Step 5 — Round-trip integrity: decode ALAC back, compare to source${NC}"
say "python3 make_plots.py 2>&1 | grep -E 'peak|RMS|OK'"

echo -e "${YEL}# Step 6 — Read back the metrics JSON${NC}"
say "cat logs/roundtrip_metrics.json"

echo
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  ALAC round-trip is bit-exact (peak |Δ| = 0).                     ${NC}"
echo -e "${GREEN}  Pipeline OK — see images/ for spectrum + waveform + diff plots.  ${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
