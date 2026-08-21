"""Owned, side-effect-free adapter for NeMo's streaming Sortformer model.

Wax only needs three operations from the old vendored WhisperLiveKit backend:
load a model, configure its streaming parameters, and allocate streaming state.
Keeping that tiny contract here makes the runtime reproducible from tracked Wax
source and prevents an import check from allocating a hidden model on the GPU.
"""

import json

import torch
from nemo.collections.asr.models import SortformerEncLabelModel
from nemo.collections.asr.modules import AudioToMelSpectrogramPreprocessor


MODEL_NAME = "nvidia/diar_streaming_sortformer_4spk-v2"


def load_model(device):
    """Load exactly one Sortformer and its preprocessor on ``device``."""
    target = torch.device(device)
    if target.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError(
            "CUDA requested for Sortformer but torch.cuda.is_available() is false"
        )

    model = SortformerEncLabelModel.from_pretrained(MODEL_NAME)
    model.eval()
    model = model.to(target)

    # One-second-lag streaming configuration inherited from the proven
    # WhisperLiveKit implementation. Wax feeds one second of audio per step.
    modules = model.sortformer_modules
    modules.chunk_len = 10
    modules.subsampling_factor = 10
    modules.chunk_right_context = 0
    modules.chunk_left_context = 10
    modules.spkcache_len = 188
    modules.fifo_len = 188
    modules.spkcache_update_period = 144
    modules.log = False
    modules._check_streaming_parameters()

    audio2mel = AudioToMelSpectrogramPreprocessor(
        window_size=0.025,
        normalize="NA",
        n_fft=512,
        features=128,
        pad_to=0,
    ).to(target)
    return model, audio2mel


class StreamingSortformerState:
    """Mutable state expected by ``forward_streaming_step``."""

    def __init__(self):
        self.spkcache = None
        self.spkcache_lengths = None
        self.spkcache_preds = None
        self.fifo = None
        self.fifo_lengths = None
        self.fifo_preds = None
        self.spk_perm = None
        self.mean_sil_emb = None
        self.n_sil_frames = None


def init_streaming_state(modules, *, batch_size=1, async_streaming=False, device=None):
    """Allocate the state tensors on the same device as Sortformer."""
    state = StreamingSortformerState()
    if async_streaming:
        state.spkcache = torch.zeros(
            (batch_size, modules.spkcache_len, modules.fc_d_model), device=device
        )
        state.spkcache_preds = torch.zeros(
            (batch_size, modules.spkcache_len, modules.n_spk), device=device
        )
        state.spkcache_lengths = torch.zeros(
            (batch_size,), dtype=torch.long, device=device
        )
        state.fifo = torch.zeros(
            (batch_size, modules.fifo_len, modules.fc_d_model), device=device
        )
        state.fifo_lengths = torch.zeros(
            (batch_size,), dtype=torch.long, device=device
        )
    else:
        state.spkcache = torch.zeros(
            (batch_size, 0, modules.fc_d_model), device=device
        )
        state.fifo = torch.zeros(
            (batch_size, 0, modules.fc_d_model), device=device
        )
    state.mean_sil_emb = torch.zeros(
        (batch_size, modules.fc_d_model), device=device
    )
    state.n_sil_frames = torch.zeros(
        (batch_size,), dtype=torch.long, device=device
    )
    return state


def cuda_smoke() -> dict:
    """Load Sortformer and execute one real streaming step on CUDA."""
    if not torch.cuda.is_available():
        raise RuntimeError("torch CUDA unavailable")

    model, audio2mel = load_model("cuda")
    signal = torch.zeros((1, 16000), device=model.device)
    signal_length = torch.tensor([16000], device=model.device)
    features, _ = audio2mel.get_features(signal, signal_length)
    state = init_streaming_state(
        model.sortformer_modules,
        batch_size=1,
        async_streaming=True,
        device=model.device,
    )
    empty = torch.zeros(
        (1, 0, model.sortformer_modules.n_spk), device=model.device
    )
    with torch.inference_mode():
        _, predictions = model.forward_streaming_step(
            processed_signal=torch.transpose(features, 1, 2),
            processed_signal_length=torch.tensor(
                [features.shape[2]], device=model.device
            ),
            streaming_state=state,
            total_preds=empty,
            left_offset=0,
            right_offset=8,
        )
    torch.cuda.synchronize()
    if predictions.device.type != "cuda":
        raise RuntimeError(f"Sortformer prediction landed on {predictions.device}")
    return {
        "device": str(predictions.device),
        "gpu": torch.cuda.get_device_name(predictions.device),
        "model": MODEL_NAME,
        "prediction_shape": list(predictions.shape),
        "torch": torch.__version__,
        "cuda": torch.version.cuda,
    }


if __name__ == "__main__":
    print(json.dumps(cuda_smoke(), sort_keys=True))
