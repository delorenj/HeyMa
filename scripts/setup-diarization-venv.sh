#!/usr/bin/env bash
# setup-diarization-venv.sh — (re)create the diarization venv.
#
# Speaker diarization uses Sortformer (NVIDIA NeMo), which needs deps that would
# bloat the main transcription venv, so it lives in its own .venv-diarization.
# bin/transcribe auto-selects this venv AND auto-appends --diarization whenever
# .venv-diarization/bin/python exists; when it's absent, transcription still runs
# (no diarization) via the main uv env. So this script is the one step to turn
# speaker labels on.
#
# Diarization runs on CPU by design (scripts/transcribe.py forces it) — it adds
# ~0.25x realtime, independent of the GPU (large-v3) transcription path.
#
# Model weights (nvidia/diar_streaming_sortformer_4spk-v2) download to the HF
# cache on first use; set HF_TOKEN if your cache is empty and the repo is gated.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${DIARIZATION_VENV:-$REPO/.venv-diarization}"

echo "[diarization] creating venv at $VENV (python 3.12)"
uv venv --python 3.12 "$VENV"

echo "[diarization] installing HeyMa (editable) + nemo_toolkit[asr]"
uv pip install --python "$VENV/bin/python" -e "$REPO" "nemo_toolkit[asr]"

echo "[diarization] done. bin/transcribe will now auto-diarize."
echo "[diarization] verify: transcribe <a-multi-speaker-clip>.mp3  # look for **Speaker N** in the .md"
