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
    ) -> None:
        # Normalize to an absolute path before handing it to ONNX Runtime. This
        # is the macOS Bug A mitigation — CoreML's graph-optimization path can
        # lose the model directory when it receives a relative path, leading to
        # ``Initializer ... model_path must not be empty``.
        self._kind = str(kind)
        self._path = Path(model_path).resolve()
        self._coord = coord
        self._session: Any = None
        self._build()
        # Register with the coordinator so a single recreate_all() call walks
        # every ONNX session in the run — wrapper sessions AND classifier
        # sessions owned by pipeline.py.
        coord._register_session(self)

    def _build(self) -> None:
        import onnxruntime as ort  # imported lazily so test environments without ORT can import this module

        providers = self._coord.providers_for(self._kind)
        self._session = ort.InferenceSession(str(self._path), providers=providers)

    def _rebuild(self) -> None:
        """Drop the current session and build a new one using the coordinator's
        current provider list. Called by the wrapper's ``recreate_sessions``
        path. Releasing the old session first matters for DML/CoreML, which
        hold device memory only as long as the C++ session object lives.
        """
        self._session = None
        self._build()

    # ---- pass-through API mirroring ort.InferenceSession ----

    def run(self, output_names, input_feed, run_options=None):
        try:
            return self._session.run(output_names, input_feed, run_options)
        except Exception:
            # Re-raise unchanged. The coordinator is consulted at the wrapper /
            # per-image-retry level, NOT here, so we don't double-handle.
            raise

    def get_providers(self) -> list[str]:
        return list(self._session.get_providers())

    def get_inputs(self):
        return self._session.get_inputs()

    def get_outputs(self):
        return self._session.get_outputs()


__all__ = ["ResilientOnnxSession"]
