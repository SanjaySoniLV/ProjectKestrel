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

from . import gpu_providers
from .providers import CPU_PINNED_KINDS, TOWER_KINDS, tower_use_gpu

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
# DirectML errors carry the DXGI HRESULT in hex plus Windows' description text,
# never the DXGI_ERROR_* symbol, so each device-lost code needs its hex form:
# 887A0005 removed, 887A0006 hung, 887A0007 reset, 887A0020 driver internal
# error (seen after a device removal: every later session rebuild in the same
# process fails with it, and only a new process gets the GPU back).
_SESSION_DEAD_SIGNATURES: tuple[str, ...] = (
    "GPU device instance has been suspended",
    "887A0005",
    "887A0006",
    "887A0007",
    "887A0020",
    "DXGI_ERROR_DEVICE_REMOVED",
    "DXGI_ERROR_DEVICE_HUNG",
    "DXGI_ERROR_DEVICE_RESET",
    "DXGI_ERROR_DRIVER_INTERNAL_ERROR",
    "GetDeviceRemovedReason",
    "!model_path.empty()",
    "model_path must not be empty",
)


def error_text(exc_or_text: object) -> str:
    """``str(exc)``, except when ONNX Runtime's own message could not be decoded.

    ONNX Runtime passes its C++ error text to Python, which decodes it as UTF-8.
    On Windows that text can include the system's description of an HRESULT in the
    ANSI code page (cp1252 on a Spanish or French install). An accented letter
    there makes ``session.run`` raise ``UnicodeDecodeError: 'utf-8' codec can't
    decode byte ...`` instead of the real error, so ``str(exc)`` names neither
    the HRESULT nor the failing component. The undecodable message is still in
    the exception's ``object``; decode it leniently so the signatures below (all
    ASCII) can match and the logged reason is readable.
    """
    if isinstance(exc_or_text, str):
        return exc_or_text
    if exc_or_text is None:
        return ""
    if isinstance(exc_or_text, UnicodeDecodeError):
        raw = exc_or_text.object
        if isinstance(raw, (bytes, bytearray, memoryview)):
            raw = bytes(raw)
            try:
                import locale
                enc = locale.getpreferredencoding(False) or "utf-8"
                return raw.decode(enc, errors="replace")
            except (LookupError, ValueError):
                return raw.decode("utf-8", errors="replace")
    return str(exc_or_text)


def readable_error(exc: BaseException) -> BaseException:
    """Return ``exc``, or a ``RuntimeError`` carrying its real text if ONNX Runtime's
    message failed to decode (see ``error_text``). Callers raise the result
    ``from exc`` so the original stays in the chain."""
    if isinstance(exc, UnicodeDecodeError) and isinstance(exc.object, (bytes, bytearray, memoryview)):
        return RuntimeError(f"ONNX Runtime error (message not UTF-8): {error_text(exc)}")
    return exc


def is_session_dead(exc: BaseException, *, aggressive: bool = False) -> bool:
    """Return True if ``exc`` indicates the ONNX session needs to be recreated.

    Conservative by default: only known broken-session signatures match. With
    ``aggressive=True``, any ``onnxruntime`` Fail/RuntimeException matches —
    useful as a triage flag when a new failure mode appears in the wild.
    """
    msg = error_text(exc)
    type_name = type(exc).__name__ if exc is not None else ""

    if any(sig in msg for sig in _SESSION_DEAD_SIGNATURES) or is_device_lost(msg):
        return True
    if "CoreML" in msg and "fail" in msg.lower():
        return True

    if aggressive:
        # Match onnxruntime.capi.onnxruntime_pybind11_state.{Fail,RuntimeException,...}
        mod = type(exc).__module__ if exc is not None else ""
        if "onnxruntime" in mod or type_name in {"Fail", "RuntimeException"}:
            return True
    return False


# Signatures that mean the GPU *device* itself is gone (not just one session).
# A session rebuild in the same process cannot recover from these; a fresh
# process can. A subset of the session-dead signatures: the CoreML and
# empty-model-path entries describe a broken session, not a lost device.
_DEVICE_LOST_SIGNATURES: tuple[str, ...] = (
    "GPU device instance has been suspended",
    "887A0005",
    "887A0006",
    "887A0007",
    "887A0020",
    "DXGI_ERROR_DEVICE_REMOVED",
    "DXGI_ERROR_DEVICE_HUNG",
    "DXGI_ERROR_DEVICE_RESET",
    "DXGI_ERROR_DRIVER_INTERNAL_ERROR",
    "GetDeviceRemovedReason",
)


# DirectML also reports a poisoned device as a generic E_FAIL raised from its
# command recorder (observed: every frame failing with
# "DmlCommandRecorder.cpp(342) 80004005 Unspecified error" while another process
# shared the GPU) and as E_OUTOFMEMORY when GPU memory runs out. 80004005 alone
# is a generic code, so it only counts together with the DirectML component
# name; the out-of-memory HRESULT text is specific to the D3D/DXGI stack, unlike
# a Python MemoryError or an ONNX Runtime CPU allocation failure.
_DML_COMMAND_RECORDER = "DmlCommandRecorder"
_E_FAIL = "80004005"
_GPU_OUT_OF_MEMORY_SIGNATURES: tuple[str, ...] = (
    "8007000E",
    "Not enough memory resources",
)


def is_device_lost(exc_or_text: object) -> bool:
    """True if the error (or its text) says the GPU device was removed, reset
    or poisoned, so that only a new process will get a working GPU back."""
    msg = error_text(exc_or_text)
    if any(sig in msg for sig in _DEVICE_LOST_SIGNATURES):
        return True
    if _DML_COMMAND_RECORDER in msg and _E_FAIL in msg:
        return True
    return any(sig in msg for sig in _GPU_OUT_OF_MEMORY_SIGNATURES)


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
        # Set when the GPU was lost for this process (device removed/reset, or
        # the run locked itself out of the GPU). Sticky until
        # ``clear_gpu_lost()``: in-process rebuilds do not recover a lost
        # device, so callers use it to stop and continue in a fresh process.
        self._gpu_lost_reason: Optional[str] = None
        self._recreate_cb: Optional[_RecreateFn] = None
        # All ResilientOnnxSession instances built against this coord. The
        # coordinator walks this list to recreate sessions on demote/promote,
        # so that ALL pipeline sessions (wrapper detector/classifier/SAM AND
        # the separately-owned BioClipSpeciesClassifier / QualityClassifier) move
        # together. If only some sessions migrate, the dead-provider sessions
        # error every subsequent image and the run looks "stuck on error".
        self._sessions: list["ResilientOnnxSession"] = []
        # Per-component CPU fallback after a DirectML device-lost error (see handle_device_lost):
        # kind -> {"reason", "at"}. Sticky for the process: a lost device is not recovered
        # in-process, so these components stay on the CPU until Kestrel is restarted.
        self._cpu_components: dict[str, dict] = {}
        self._dml_lost = False
        self._fallback_listeners: list[Callable[[str, str], None]] = []

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

    @property
    def gpu_lost(self) -> bool:
        return self._gpu_lost_reason is not None

    @property
    def gpu_lost_reason(self) -> Optional[str]:
        return self._gpu_lost_reason

    @property
    def dml_lost(self) -> bool:
        """True once a DirectML device-lost error moved components to the CPU in this process."""
        return self._dml_lost

    @property
    def cpu_fallbacks(self) -> list[dict]:
        """Components that fell back to the CPU: ``[{"kind", "reason", "at"}]``, oldest first."""
        return [dict(v, kind=k) for k, v in self._cpu_components.items()]

    def add_fallback_listener(self, fn: Callable[[str, str], None]) -> None:
        """``fn(kind, reason)`` is called once per component that falls back to the CPU."""
        self._fallback_listeners.append(fn)

    def note_cpu_component(self, kind: str, reason: str) -> None:
        if kind in self._cpu_components:
            return
        import time
        self._cpu_components[kind] = {"reason": str(reason)[:300], "at": int(time.time())}
        for fn in list(self._fallback_listeners):
            try:
                fn(kind, str(reason)[:300])
            except Exception:
                pass

    def handle_device_lost(self, failing: "ResilientOnnxSession", exc: BaseException) -> None:
        """A run on ``failing`` raised a device-lost error. Move that session and every other session
        that runs on the shared DirectML device to the CPU. Sessions on a vendor EP keep running
        there. The caller then retries the same run on the CPU, so no frame is lost or saved empty."""
        reason = f"{type(exc).__name__}: {str(exc)[:200]}"
        first = not self._dml_lost
        self._dml_lost = True
        for sess in list(self._sessions):
            if sess is failing or sess.on_directml():
                had_gpu_graph = getattr(sess, "_gpu_path", None) is not None
                sess.fall_back_to_cpu()
                # The transformer towers have a separate GPU graph; the CPU graph can score slightly differently.
                self.note_cpu_component(
                    sess.kind, reason + (" [now on the CPU graph; scores may differ slightly]" if had_gpu_graph else ""))
        if first:
            self._notify("Part of the GPU stopped responding. Some analysis steps now run on the "
                         "CPU (slower). Restart Kestrel to restore full speed.")

    def clear_gpu_lost(self) -> None:
        self._gpu_lost_reason = None

    def _flag_gpu_lost(self, reason: str, *, locked_out: bool) -> None:
        if not self._user_gpu_enabled or self._gpu_lost_reason is not None:
            return
        if locked_out or is_device_lost(reason):
            self._gpu_lost_reason = (reason or "repeated GPU failures")[:500]

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

    def providers_for(self, kind: str) -> list[str]:
        """Return the ONNX providers list for a fresh session of the given kind.

        ``kind`` is a free-form string ("detector"/"sam_enc"/"sam_dec"/"quality"/
        "bird_species"/"siglip_image"). Every kind follows the coordinator's
        GPU/CPU state, except the transformer towers (``TOWER_KINDS``), which
        also honour the tower device policy, and ``CPU_PINNED_KINDS`` (the SigLIP
        quality tower), which always run on CPU.
        """
        if self._dml_lost and kind not in CPU_PINNED_KINDS:
            return ["CPUExecutionProvider"]  # the device is gone; a new session must not touch it
        if kind in CPU_PINNED_KINDS:
            # Consistency over speed (see providers.CPU_PINNED_KINDS): the SigLIP
            # quality score must not depend on the machine's GPU.
            return ["CPUExecutionProvider"]
        if self._state is ProviderState.GPU:
            # The int8 transformer towers (BioCLIP / SigLIP) follow their own
            # device policy: on CoreML they would split into many CPU/ANE
            # partitions, so ``auto`` keeps them on CPU there (ml/providers.py).
            if kind in TOWER_KINDS:
                from . import GPU_EP
                if not tower_use_gpu(GPU_EP):
                    return ["CPUExecutionProvider"]
            return gpu_providers()
        return ["CPUExecutionProvider"]

    def vendor_for(self, kind: str, model_name: str = ""):
        """Enhanced GPU mode: the vendor EP spec when this session should use it, else None.
        Independent of the DirectML state: a model on the vendor EP keeps running there when
        DirectML components fall back to the CPU. Off when the user turned the GPU off, after a
        lock-out or lost GPU, or when the model's device policy keeps it off the GPU."""
        if (not self._user_gpu_enabled or self._state is ProviderState.GPU_LOCKED_OUT
                or kind in CPU_PINNED_KINDS or self._gpu_lost_reason):
            return None
        if kind in TOWER_KINDS:
            from . import GPU_EP
            if not tower_use_gpu(GPU_EP):
                return None
        from . import enhanced_gpu
        return enhanced_gpu.vendor_spec_for(kind, model_name)

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
        self._demote(reason=error_text(exc))
        return FailureAction.RECREATE_AND_RETRY

    def should_try_promote(self) -> bool:
        if self._state is not ProviderState.CPU:
            return False
        if not self._user_gpu_enabled or self._dml_lost:
            return False  # a lost DirectML device is never retried in this process (rebuilds fail with 887A0020)
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
        locked = self._demotions >= self.cfg.max_demotions_before_lockout > 0
        self._flag_gpu_lost(reason, locked_out=locked)
        if locked:
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
        locked = self._demotions >= self.cfg.max_demotions_before_lockout > 0
        self._flag_gpu_lost(reason, locked_out=locked)
        if locked:
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
    "error_text",
    "is_device_lost",
    "is_session_dead",
    "readable_error",
]
