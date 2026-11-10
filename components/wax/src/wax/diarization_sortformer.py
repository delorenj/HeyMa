"""Owned, side-effect-free adapter for NeMo's streaming Sortformer model.

Wax needs two things from Sortformer: load it, and get per-frame speaker
activity for a whole recording. Keeping that contract here makes the runtime
reproducible from tracked Wax source and keeps an import check from allocating a
hidden model on the GPU.

Inference is NeMo's own ``forward`` (``forward_streaming`` in streaming mode),
which handles chunk offsets, left/right context and the speaker cache. Wax used
to hand-roll a one-second loop inherited from WhisperLiveKit; it re-fed most of
the previous second as context but told the model only one frame of it was
context, overrode ``subsampling_factor`` to 10 while the encoder subsamples by 8,
and so smeared timing and split one voice into extra speakers (transcript 1878,
2026-11-09). Wax diarizes finished files, so it uses NVIDIA's high-latency
"batch" setting from the model card instead of the one-second-lag one.
"""

import json

import torch
from nemo.collections.asr.models import SortformerEncLabelModel


MODEL_NAME = "nvidia/diar_streaming_sortformer_4spk-v2"

# Model card, "30.4 s latency" row (best DER). Units are 80 ms output frames.
STREAMING_CONFIG = {
    "chunk_len": 340,
    "chunk_right_context": 40,
    "chunk_left_context": 1,
    "fifo_len": 40,
    "spkcache_update_period": 300,
    "spkcache_len": 188,
}


def load_model(device):
    """Load exactly one Sortformer on ``device``, configured for whole-file inference."""
    target = torch.device(device)
    if target.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError(
            "CUDA requested for Sortformer but torch.cuda.is_available() is false"
        )

    model = SortformerEncLabelModel.from_pretrained(MODEL_NAME)
    model.eval()
    model = model.to(target)
    modules = model.sortformer_modules
    for name, value in STREAMING_CONFIG.items():
        setattr(modules, name, value)
    modules.log = False
    modules._check_streaming_parameters()
    return model


def frame_seconds(model) -> float:
    """Seconds per output frame: encoder subsampling x mel hop (8 x 10 ms)."""
    return model.sortformer_modules.subsampling_factor * model.preprocessor._cfg.window_stride


def frame_probabilities(model, signal):
    """Speaker activity for a 16 kHz mono signal: ([[p_spk0..p_spk3] per frame], frame_s)."""
    with torch.inference_mode():
        audio = torch.as_tensor(signal, dtype=torch.float32, device=model.device).unsqueeze(0)
        length = torch.tensor([audio.shape[1]], device=model.device)
        preds = model.forward(audio_signal=audio, audio_signal_length=length)
    return preds[0].float().cpu().tolist(), frame_seconds(model)


def cuda_smoke() -> dict:
    """Load Sortformer and run real inference over one second of audio on CUDA."""
    if not torch.cuda.is_available():
        raise RuntimeError("torch CUDA unavailable")

    model = load_model("cuda")
    with torch.inference_mode():
        audio = torch.zeros((1, 16000), device=model.device)
        predictions = model.forward(
            audio_signal=audio, audio_signal_length=torch.tensor([16000], device=model.device)
        )
    torch.cuda.synchronize()
    if predictions.device.type != "cuda":
        raise RuntimeError(f"Sortformer prediction landed on {predictions.device}")
    return {
        "device": str(predictions.device),
        "gpu": torch.cuda.get_device_name(predictions.device),
        "model": MODEL_NAME,
        "prediction_shape": list(predictions.shape),
        "frame_s": frame_seconds(model),
        "torch": torch.__version__,
        "cuda": torch.version.cuda,
    }


if __name__ == "__main__":
    print(json.dumps(cuda_smoke(), sort_keys=True))
