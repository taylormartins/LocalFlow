"""Whisper transcription via faster-whisper / CTranslate2."""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

from . import _cuda  # noqa: F401  - registers CUDA DLL dirs before ctranslate2 loads
from .config import VocabularyConfig, WhisperConfig

log = logging.getLogger(__name__)

# faster-whisper resolves bare names against its own alias table; anything with a
# slash is treated as a HuggingFace repo id.
MODEL_ALIASES = {
    "turbo": "large-v3-turbo",
}


def _open(WhisperModel, name: str, device: str, kwargs: dict):  # noqa: N803
    """Load from the local HuggingFace cache without touching the network.

    faster-whisper otherwise asks the Hub whether the cached model is current on
    every load, so a flaky network at boot or wake breaks the load entirely.
    Only a model that has never been downloaded goes online.
    """
    try:
        return WhisperModel(name, device=device, local_files_only=True, **kwargs)
    except Exception as exc:
        if "local" not in type(exc).__name__.lower() and "not found" not in str(exc).lower():
            raise
        log.info("%s not in the local cache; downloading", name)
        return WhisperModel(name, device=device, **kwargs)


def _is_cuda_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(k in text for k in ("cuda", "cublas", "cudnn", "gpu", "device"))


class Transcriber:
    """Lazily-loaded Whisper model. Falls back to CPU if CUDA init fails."""

    def __init__(self, cfg: WhisperConfig, vocab: VocabularyConfig | None = None):
        self.cfg = cfg
        self.vocab = vocab or VocabularyConfig()
        self._model = None
        self._lock = threading.Lock()
        self.device_in_use = cfg.device

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _initial_prompt(self) -> str | None:
        """Bias decoding toward the user's jargon.

        Whisper conditions on this text as if it preceded the audio, so a bare
        comma-separated list of terms is enough to fix most proper-noun spellings.
        """
        if not self.vocab.terms:
            return None
        return "Glossary: " + ", ".join(self.vocab.terms) + "."

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return

            from faster_whisper import WhisperModel

            name = MODEL_ALIASES.get(self.cfg.model, self.cfg.model)
            kwargs = {"compute_type": self.cfg.compute_type}
            if self.cfg.download_root:
                kwargs["download_root"] = self.cfg.download_root

            started = time.perf_counter()
            try:
                self._model = _open(WhisperModel, name, self.cfg.device, kwargs)
                self.device_in_use = self.cfg.device
            except Exception as exc:
                # Only a real CUDA failure justifies CPU (25x slower, and the live
                # preview then hogs the CPU). On 2026-10-12 a HuggingFace network
                # check failing right after wake ("Server disconnected without
                # sending a response.") was taken for a CUDA failure and left
                # Whisper on the CPU all morning. Anything else: raise, and the
                # next dictation retries the load.
                if self.cfg.device == "cpu" or not _is_cuda_error(exc):
                    raise
                log.warning("CUDA init failed (%s); falling back to CPU", exc)
                self._model = _open(WhisperModel, name, "cpu", {"compute_type": "int8"})
                self.device_in_use = "cpu"

            log.info(
                "loaded %s on %s in %.1fs", name, self.device_in_use, time.perf_counter() - started
            )

    def warmup(self) -> None:
        """Load weights and run one tiny inference so the first real dictation is fast."""
        self.load()
        self.transcribe(np.zeros(16000, dtype=np.float32))

    def transcribe(self, audio: np.ndarray) -> str:
        if audio.size == 0:
            return ""
        self.load()
        assert self._model is not None

        segments, _info = self._model.transcribe(
            audio,
            language=self.cfg.language or None,
            beam_size=self.cfg.beam_size,
            vad_filter=self.cfg.vad_filter,
            initial_prompt=self._initial_prompt(),
            condition_on_previous_text=False,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()

    def unload(self) -> None:
        with self._lock:
            self._model = None
