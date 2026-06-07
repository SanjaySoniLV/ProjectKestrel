"""ONNX execution-provider coordinator: GPU/CPU state machine + auto-promote/demote.

Owns provider state for one analysis run. The ``SpeciesNetSAMHQWrapper`` consults
this object to decide which providers a fresh ``InferenceSession`` should request,
and to react to inference failures by recreating sessions on a different provider.

Failure modes handled:
- DML "GPU device instance has been suspended" (Windows OOM / driver reset).
- CoreML cold-start "model_path must not be empty" (macOS first-run path loss).
- DXGI device removed / hung / reset.

Policy (tuned for typical 1000-image folders):
- Start in ``GPU`` if user enabled GPU, else ``CPU`` (and never promote).
- A "session-dead" exception in ``GPU`` demotes to ``CPU`` and asks the wrapper to
  recreate every loaded session.
- After ``initial_promote_threshold`` consecutive successful CPU images, attempt
  one promotion back to ``GPU``. If that promotion (or the next inference) fails,
  demote again and double the threshold (capped). After
  ``max_demotions_before_lockout`` demote events, transition to ``GPU_LOCKED_OUT``
  and stay on CPU for the rest of the run.

This module is single-thread by design — the analysis pipeline processes images
sequentially. If parallel-image processing is ever introduced, add a lock around
``on_run_success`` / ``on_run_failure`` / ``attempt_promotion``.
"""

from __future__ import annotations

import gc
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from . import gpu_providers, gpu_providers_for

if TYPE_CHECKING:
    from .resilient_session import ResilientOnnxSession


class ProviderState(Enum):
    GPU = "gpu"
    CPU = "cpu"
    GPU_LOCKED_OUT = "gpu_locked_out"


class FailureAction(Enum):
    RECREATE_AND_RETRY = "recreate_and_retry"
    PROPAGATE = "propagate"


@dataclass(frozen=True)
class ResilienceConfig:
    initial_promote_threshold: int = 25
    max_promote_threshold: int = 400
    promote_backoff_factor: float = 2.0
    max_demotions_before_lockout: int = 3
    max_attempts_per_image: int = 2
    aggressive_recreate: bool = False

    @classmethod
    def from_settings(cls, settings: dict | None) -> "ResilienceConfig":
        s = settings or {}
        if not bool(s.get("gpu_resilience_enabled", True)):
            return cls(
                initial_promote_threshold=cls.initial_promote_threshold,
                max_promote_threshold=cls.max_promote_threshold,
                promote_backoff_factor=cls.promote_backoff_factor,
                max_demotions_before_lockout=0,
                max_attempts_per_image=1,
                aggressive_recreate=False,
            )
        return cls(aggressive_recreate=bool(s.get("gpu_aggressive_recreate", False)))


# Substring matches against str(exc) for known "session is now corpse" errors.
_SESSION_DEAD_SIGNATURES: tuple[str, ...] = (
    "GPU device instance has been suspended",
    "887A0005",
    "DXGI_ERROR_DEVICE_REMOVED",
    "DXGI_ERROR_DEVICE_HUNG",
    "DXGI_ERROR_DEVICE_RESET",
    "GetDeviceRemovedReason",
    "!model_path.empty()",
    "model_path must not be empty",
    # TensorRT EP failures (engine cache corruption, TRT/CUDA version
    # mismatch, OOM during build, etc.). Treating these as session-dead
    # triggers the rebuild path, which on retry uses CUDA EP (next in the
    # provider list) instead of TRT.
    "Could not deserialize engine",
    "TensorRT EP failed",
    "trt_engine_cache",
    "engine deserialize",
)


def is_session_dead(exc: BaseException, *, aggressive: bool = False) -> bool:
    """Return True if ``exc`` indicates the ONNX session needs to be recreated.

    Conservative by default: only known broken-session signatures match. With
    ``aggressive=True``, any ``onnxruntime`` Fail/RuntimeException matches —
    useful as a triage flag when a new failure mode appears in the wild.
    """
    msg = str(exc) if exc is not None else ""
    type_name = type(exc).__name__ if exc is not None else ""

    if any(sig in msg for sig in _SESSION_DEAD_SIGNATURES):
        return True
    if "CoreML" in msg and "fail" in msg.lower():
        return True

    if aggressive:
        # Match onnxruntime.capi.onnxruntime_pybind11_state.{Fail,RuntimeException,...}
        mod = type(exc).__module__ if exc is not None else ""
        if "onnxruntime" in mod or type_name in {"Fail", "RuntimeException"}:
            return True
    return False


_RecreateFn = Callable[[bool], None]
_StatusFn = Callable[[str], None]


class ProviderCoordinator:
    """State machine + recreation orchestrator for a single analysis run.

    Wiring expectations:
    - ``register_recreate_callback`` is called once by the wrapper, supplying a
      function ``recreate(target_use_gpu: bool) -> None`` that tears down all
      live sessions and rebuilds the previously-loaded ones on the new provider.
    - The wrapper calls ``providers_for(kind)`` when constructing each session.
    - The wrapper calls ``on_run_success()`` after each successful image and
      ``on_run_failure(exc)`` from the per-image retry loop.
    """

    def __init__(
        self,
        *,
        user_gpu_enabled: bool,
        cfg: Optional[ResilienceConfig] = None,
        status_cb: Optional[_StatusFn] = None,
    ):
        self._user_gpu_enabled = bool(user_gpu_enabled)
        self.cfg = cfg or ResilienceConfig()
        self._status_cb = status_cb
        self._state = ProviderState.GPU if self._user_gpu_enabled else ProviderState.CPU
        self._cpu_success_streak = 0
        self._current_promote_threshold = self.cfg.initial_promote_threshold
        self._demotions = 0
        self._recreate_cb: Optional[_RecreateFn] = None
        # All ResilientOnnxSession instances built against this coord. The
        # coordinator walks this list to recreate sessions on demote/promote,
        # so that ALL pipeline sessions (wrapper detector/classifier/SAM AND
        # the separately-owned BirdSpeciesClassifier / QualityClassifier) move
        # together. If only some sessions migrate, the dead-provider sessions
        # error every subsequent image and the run looks "stuck on error".
        self._sessions: list["ResilientOnnxSession"] = []

    # ---- public read-only ----

    @property
    def effective_use_gpu(self) -> bool:
        return self._state is ProviderState.GPU

    @property
    def state(self) -> ProviderState:
        return self._state

    @property
    def cpu_success_streak(self) -> int:
        return self._cpu_success_streak

    @property
    def current_promote_threshold(self) -> int:
        return self._current_promote_threshold

    # ---- wiring ----

    def register_recreate_callback(self, fn: _RecreateFn) -> None:
        self._recreate_cb = fn

    def update_status_cb(self, status_cb: Optional[_StatusFn]) -> None:
        """Rebind the status callback. Called when a wrapper is reused across
        analysis runs so notifications target the active folder, not the one
        whose ``status_cb`` closure was captured at first construction.
        """
        self._status_cb = status_cb

    def _register_session(self, sess: "ResilientOnnxSession") -> None:
        self._sessions.append(sess)

    def recreate_all(self) -> None:
        """Rebuild every registered session against the coordinator's current
        provider list. Called after a state transition (demote or promote).
        Safe to call when no sessions are registered (no-op).
        """
        for sess in list(self._sessions):
            sess._rebuild()
        gc.collect()

    def providers_for(self, kind: str, model_path: object = None) -> list:
        """Return the ONNX providers list for a fresh session of the given kind.

        ``kind`` is a free-form string ("detector"/"classifier"/"sam_enc"/
        "sam_dec"/"bird_species"/"quality"). On Linux+CUDA, per-model
        TRT routing happens via ``gpu_providers_for(kind, model_path)`` — which
        keys off the ONNX file's basename to pick the right TRT engine cache
        subdir. The 4 TRT-eligible models (see ml/__init__.py) get a TRT EP
        prepended; everything else gets the platform default GPU EP.
        """
        if self._state is ProviderState.GPU:
            return gpu_providers_for(kind, model_path)
        return ["CPUExecutionProvider"]

    # ---- event handlers ----

    def on_run_success(self) -> None:
        if self._state is ProviderState.GPU:
            return
        # CPU or LOCKED_OUT — count successes; only GPU promotion uses the streak.
        self._cpu_success_streak += 1

    def on_run_failure(self, exc: BaseException) -> FailureAction:
        """Classify ``exc`` and decide whether the wrapper should recreate sessions.

        Side effect: when returning RECREATE_AND_RETRY, this method has already
        transitioned to CPU and emitted a status message. The wrapper is expected
        to call its recreate function (which re-asks ``providers_for`` and
        therefore picks up the new state) before retrying.
        """
        if not is_session_dead(exc, aggressive=self.cfg.aggressive_recreate):
            return FailureAction.PROPAGATE
        if self._state is not ProviderState.GPU:
            # Already on CPU; recreating won't help. Let the per-image catcher
            # in pipeline.py mark this image errored.
            return FailureAction.PROPAGATE
        self._demote(reason=str(exc))
        return FailureAction.RECREATE_AND_RETRY

    def should_try_promote(self) -> bool:
        if self._state is not ProviderState.CPU:
            return False
        if not self._user_gpu_enabled:
            return False
        return self._cpu_success_streak >= self._current_promote_threshold

    def attempt_promotion(self) -> bool:
        """Try to promote CPU→GPU once. Returns True on success, False on failure.

        Called by the wrapper between images when ``should_try_promote()`` is
        true. On failure, the coordinator stays on CPU, doubles the threshold
        (capped), and may transition to ``GPU_LOCKED_OUT`` if too many demotions
        have accumulated.
        """
        if self._state is not ProviderState.CPU:
            return False
        if self._recreate_cb is None:
            return False
        self._notify("Re-attempting GPU acceleration...")
        # Optimistically transition before recreate, so providers_for() returns
        # GPU during the rebuild. On failure we transition back inside _demote.
        self._state = ProviderState.GPU
        self._cpu_success_streak = 0
        try:
            self._recreate_cb(True)
        except Exception as e:
            self._on_promotion_failure(reason=str(e))
            # GPU rebuild failed — coordinator is now on CPU, but some sessions
            # may have _session = None (set to None before the failed _build).
            # Force a CPU rebuild so they're usable for the next image instead
            # of causing an AttributeError cascade.
            try:
                self._recreate_cb(False)
            except Exception:
                pass  # Best-effort: per-image errors will follow on next run
            return False
        return True

    # ---- internal ----

    def _demote(self, *, reason: str) -> None:
        self._state = ProviderState.CPU
        self._cpu_success_streak = 0
        self._demotions += 1
        if self._demotions >= self.cfg.max_demotions_before_lockout > 0:
            self._state = ProviderState.GPU_LOCKED_OUT
            self._notify("GPU disabled for the rest of this run — repeated GPU failures.")
            return
        next_at = self._current_promote_threshold
        self._notify(
            f"Switched to CPU after GPU error — will retry GPU after {next_at} successful images."
        )

    def _on_promotion_failure(self, *, reason: str) -> None:
        # Transition back to CPU and back off the threshold.
        self._state = ProviderState.CPU
        self._cpu_success_streak = 0
        self._demotions += 1
        new_threshold = int(self._current_promote_threshold * self.cfg.promote_backoff_factor)
        self._current_promote_threshold = min(new_threshold, self.cfg.max_promote_threshold)
        if self._demotions >= self.cfg.max_demotions_before_lockout > 0:
            self._state = ProviderState.GPU_LOCKED_OUT
            self._notify("GPU disabled for the rest of this run — repeated GPU failures.")
            return
        self._notify(
            f"GPU re-enable failed — staying on CPU "
            f"(next attempt after {self._current_promote_threshold} more images)."
        )

    def _notify(self, msg: str) -> None:
        if self._status_cb is None:
            return
        try:
            self._status_cb(msg)
        except Exception:
            pass


__all__ = [
    "FailureAction",
    "ProviderCoordinator",
    "ProviderState",
    "ResilienceConfig",
    "is_session_dead",
]
