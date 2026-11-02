"""Drop-in wrapper around ``ort.InferenceSession`` that delegates failures to a coordinator.

The wrapper exposes the public surface of ``InferenceSession`` used in the
analyzer (``run``, ``get_providers``, ``get_inputs``, ``get_outputs``) so each
detector / classifier / SAM session-owner can hold one of these instead of a
raw session and not care about provider state.

Path normalization mitigation for the macOS Bug A failure:
``Path(model_path).resolve()`` is applied here once, before the session is
constructed, so all five session sites get the absolute-path mitigation for
free.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .provider_coordinator import ProviderCoordinator


class ResilientOnnxSession:
    """Thin pass-through to ``onnxruntime.InferenceSession`` with rebuild support.

    The coordinator is the only thing that calls ``_rebuild`` — the wrapper
    looks the session up via the coordinator after a recreate, so the detectors
    don't need to know anything about provider state.
    """

    def __init__(
        self,
        kind: str,
        model_path: Path | str,
        coord: "ProviderCoordinator",
        gpu_model_path: Path | str | None = None,
    ) -> None:
        # Normalize to an absolute path before handing it to ONNX Runtime. This
        # is the macOS Bug A mitigation — CoreML's graph-optimization path can
        # lose the model directory when it receives a relative path, leading to
        # ``Initializer ... model_path must not be empty``.
        self._kind = str(kind)
        self._path = Path(model_path).resolve()
        # Transformer towers: an alternative graph for GPU EPs (see
        # providers.tower_graph). ``model_path`` is then the CPU graph, which is
        # also what a failed GPU build falls back to.
        self._gpu_path = Path(gpu_model_path).resolve() if gpu_model_path else None
        self.model_path_used = str(self._path)
        self._coord = coord
        self._session: Any = None
        # Enhanced GPU mode: set when this session is on a vendor EP; ``_skip_vendor`` is set for
        # good once that EP failed here, so rebuilds stay on DirectML.
        self._vendor_active = False
        self._skip_vendor = False
        self._build()
        # Register with the coordinator so a single recreate_all() call walks
        # every ONNX session in the run — wrapper sessions AND classifier
        # sessions owned by pipeline.py.
        coord._register_session(self)

    def _build(self) -> None:
        from .providers import build_session  # lazy: ORT-free test environments import this module

        was_vendor = self._vendor_active
        self._vendor_active = False
        if self._build_vendor():
            return
        if was_vendor:  # rebuilt without the vendor EP (the run moved to CPU): not an error of the EP itself
            try:
                from .enhanced_gpu import note_vendor_left
                note_vendor_left(self._kind, "the run moved to the CPU", to="CPU")
            except Exception:
                pass
        providers = self._coord.providers_for(self._kind)
        # build_session records requested/actual providers + per-EP node
        # placement for the analysis log (see ml/providers.py).
        if self._gpu_path is None:
            self._session = build_session(str(self._path), providers, kind=self._kind)
            self.model_path_used = str(self._path)
            return
        from .providers import tower_graph

        path, provs, cfg = tower_graph(providers, self._path, self._gpu_path)
        try:
            self._session = build_session(path, provs, kind=self._kind, config=cfg)
            self.model_path_used = path
        except Exception as e:
            if path == str(self._path) and provs == ["CPUExecutionProvider"]:
                raise
            try:
                from ..logging_utils import debug
                debug(f"[providers] {self._kind}: {Path(path).name} on {provs} failed "
                      f"({type(e).__name__}: {str(e)[:200]}); using {self._path.name} on CPU")
            except Exception:
                pass
            self._session = build_session(str(self._path), ["CPUExecutionProvider"], kind=self._kind)
            self.model_path_used = str(self._path)

    def _build_vendor(self) -> bool:
        """Try the enhanced-GPU vendor EP for this model; False means use the normal providers."""
        if self._skip_vendor:
            return False
        try:
            from .providers import build_session
            # Towers use their fp16 GPU graph (with the constant-folding config) on the vendor EP too.
            if self._gpu_path is not None:
                from .providers import GPU_GRAPH_SESSION_CONFIG, _usable
                if not _usable(self._gpu_path):
                    return False
                path, cfg = self._gpu_path, dict(GPU_GRAPH_SESSION_CONFIG)
            else:
                path, cfg = self._path, None
            spec = self._coord.vendor_for(self._kind, path.name)
            if spec is None:
                return False
            self._session = build_session(str(path), [], kind=self._kind, config=cfg, vendor=spec)
            self.model_path_used = str(path)
            self._vendor_active = True
            try:
                from .enhanced_gpu import note_vendor_active
                note_vendor_active(self._kind, path.name)
            except Exception:
                pass
            return True
        except Exception as e:
            self._session = None
            self._skip_vendor = True
            try:
                from .enhanced_gpu import record_runtime_failure
                record_runtime_failure(self._kind, e)
            except Exception:
                pass
            return False

    def _rebuild(self) -> None:
        """Drop the current session and build a new one using the coordinator's
        current provider list. Called by the wrapper's ``recreate_sessions``
        path. Releasing the old session first matters for DML/CoreML, which
        hold device memory only as long as the C++ session object lives.

        The old session is restored if the build fails so that subsequent
        ``run()`` calls surface the original ONNX error rather than an
        uninformative ``AttributeError: 'NoneType' object has no attribute 'run'``.
        """
        old = self._session
        self._session = None  # Release so DML/CoreML can free device memory
        try:
            self._build()
        except Exception:
            self._session = old  # Restore: keeps run() errors meaningful
            raise

    # ---- pass-through API mirroring ort.InferenceSession ----

    def run(self, output_names, input_feed, run_options=None):
        try:
            return self._session.run(output_names, input_feed, run_options)
        except Exception as e:
            if self._vendor_active and self._vendor_fallback(e):
                return self._session.run(output_names, input_feed, run_options)
            # Re-raise unchanged. The coordinator is consulted at the wrapper /
            # per-image-retry level, NOT here, so we don't double-handle.
            raise

    def _vendor_fallback(self, exc: BaseException) -> bool:
        """A vendor-EP run failed: rebuild this session on DirectML and let the caller retry once.
        DirectX device-lost errors are left to the coordinator (a rebuild cannot fix them)."""
        from .provider_coordinator import is_device_lost
        if is_device_lost(exc):
            return False
        try:
            from .enhanced_gpu import record_runtime_failure
            record_runtime_failure(self._kind, exc)
        except Exception:
            pass
        self._skip_vendor = True
        try:
            self._rebuild()
        except Exception:
            return False
        return True

    def get_providers(self) -> list[str]:
        return list(self._session.get_providers())

    def get_inputs(self):
        return self._session.get_inputs()

    def get_outputs(self):
        return self._session.get_outputs()


__all__ = ["ResilientOnnxSession"]
