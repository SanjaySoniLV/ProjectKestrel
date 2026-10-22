"""Per-folder sidecar of crop embeddings (BioCLIP species head, CLIP smart search).

Several models live side by side, one *slot* each. The primary slot (``None``)
is the original store directly under ``<folder>/.kestrel/embeddings/`` (BioCLIP
2); every other slot is a subfolder ``embeddings/<slot>/`` with the identical
layout (smart search uses ``slot="clip-b16"``). Slots are independent: each
one resets only when ITS model or dim changes, so adding a model never touches
the BioCLIP rows, and a folder written before slots existed reads unchanged.

Layout of one slot directory::

    index.jsonl      line 1: header {"format", "version", "model", "dim",
                     "dtype", "data"}; then one line per image write:
                     {"f": <image filename>, "rows": {<crop_index>: <row>}}
                     The LAST line for a filename wins; ``"rows": {}`` is a
                     tombstone (file removed or re-analysed without birds).
    <data>.f16       raw little-endian float16 rows of ``dim`` values, one
                     per crop, appended in write order. ``<data>`` is a random
                     generation id named by the header.

Writes are append-only (data row first, then the index line that points at
it), so a crash can only leave unreferenced rows or a torn last index line,
both ignored on load. ``compact()`` rewrites live rows into a NEW data file,
atomically replaces the index (which names the new file), then deletes the old
data file, so the index never points into the wrong file.

Every public method swallows its own errors (logging them): this store must
never fail an analysis run.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Dict, Iterable, Optional, Tuple

import numpy as np

from .logging_utils import log_warning

EMBEDDINGS_DIRNAME = "embeddings"
INDEX_FILENAME = "index.jsonl"
FORMAT = "kestrel-embeddings"
VERSION = 1
DEFAULT_DIM = 768


_SLOT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _valid_slot(slot: Optional[str]) -> Optional[str]:
    if slot is None:
        return None
    slot = str(slot)
    if not _SLOT_RE.match(slot) or slot.endswith(".f16") or slot.endswith(".tmp"):
        raise ValueError(f"invalid embedding slot name: {slot!r}")
    return slot


def _dir(kestrel_dir: str, slot: Optional[str] = None) -> str:
    base = os.path.join(kestrel_dir, EMBEDDINGS_DIRNAME)
    slot = _valid_slot(slot)
    return base if slot is None else os.path.join(base, slot)


def _read_index(path: str) -> Tuple[Optional[dict], Dict[str, Dict[int, int]]]:
    header = None
    live: Dict[str, Dict[int, int]] = {}
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # torn final line after a crash
            if i == 0 and isinstance(rec, dict) and rec.get("format") == FORMAT:
                header = rec
                continue
            if not isinstance(rec, dict) or "f" not in rec:
                continue
            rows = rec.get("rows") or {}
            try:
                parsed = {int(k): int(v) for k, v in rows.items()}
            except (TypeError, ValueError, AttributeError):
                continue
            if parsed:
                live[str(rec["f"])] = parsed
            else:
                live.pop(str(rec["f"]), None)
    return header, live


def load_embeddings(kestrel_dir: str, slot: Optional[str] = None, model: Optional[str] = None,
                    dim: int = DEFAULT_DIM):
    """Return ``(keys, matrix, header)`` for one slot (default: the primary one).

    ``keys`` is a list of ``(filename, crop_index)``; ``matrix`` is float16
    ``[len(keys), dim]`` (rows are L2-normalised). Empty when absent/corrupt,
    or when ``model`` is given and the slot holds a different model.
    """
    empty = ([], np.zeros((0, int(dim)), np.float16), None)
    try:
        d = _dir(kestrel_dir, slot)
        header, live = _read_index(os.path.join(d, INDEX_FILENAME))
        if header is None:
            raise ValueError("missing header")
        if model is not None and header.get("model") != model:
            return empty
        dim = int(header.get("dim", DEFAULT_DIM))
        data = np.fromfile(os.path.join(d, str(header["data"])), dtype="<f2")
        n_rows = data.size // dim
        data = data[: n_rows * dim].reshape(n_rows, dim)
    except Exception:
        return empty
    keys, idx = [], []
    for fn in sorted(live):
        for crop, row in sorted(live[fn].items()):
            if 0 <= row < n_rows:
                keys.append((fn, crop))
                idx.append(row)
    return keys, data[np.array(idx, dtype=np.int64)] if idx else np.zeros((0, dim), np.float16), header


def live_filenames(kestrel_dir: str, slot: Optional[str] = None, model: Optional[str] = None) -> set:
    """Filenames with at least one live row in a slot (cheap: reads only the index)."""
    try:
        header, live = _read_index(os.path.join(_dir(kestrel_dir, slot), INDEX_FILENAME))
    except Exception:
        return set()
    if header is None or (model is not None and header.get("model") != model):
        return set()
    return set(live)


class EmbeddingStore:
    def __init__(self, kestrel_dir: str, model: str, dim: int = DEFAULT_DIM, log_path: Optional[str] = None,
                 slot: Optional[str] = None):
        self.model = str(model)
        self.dim = int(dim)
        self.slot = slot
        self.dir = ""
        self._log_path = log_path
        self._ok = False
        self._live: Dict[str, Dict[int, int]] = {}
        self._rows = 0
        self._data_name = ""
        try:
            self.dir = _dir(kestrel_dir, slot)
            self._open()
            self._ok = True
        except Exception as e:
            self._warn(f"embedding store disabled: {type(e).__name__}: {e}")

    # ------------------------------------------------------------------ internals

    def _warn(self, msg: str) -> None:
        try:
            tag = "embeddings" if self.slot is None else f"embeddings:{self.slot}"
            log_warning(self._log_path, f"[{tag}] {msg}", stage="embeddings")
        except Exception:
            pass

    @property
    def _index_path(self) -> str:
        return os.path.join(self.dir, INDEX_FILENAME)

    def _data_path(self, name: Optional[str] = None) -> str:
        return os.path.join(self.dir, name or self._data_name)

    def _header(self, data_name: str) -> dict:
        return {"format": FORMAT, "version": VERSION, "model": self.model,
                "dim": self.dim, "dtype": "float16", "data": data_name}

    def _open(self) -> None:
        os.makedirs(self.dir, exist_ok=True)
        header, live = (None, {})
        if os.path.exists(self._index_path):
            try:
                header, live = _read_index(self._index_path)
            except Exception:
                header = None
        if (
            header is None
            or header.get("model") != self.model
            or int(header.get("dim", -1)) != self.dim
            or not str(header.get("data", "")).endswith(".f16")
        ):
            # Absent, corrupt or written by a different model: start over.
            self._reset()
            return
        self._data_name = os.path.basename(str(header["data"]))
        # A torn final index line (crash mid-append) must not swallow the
        # next appended line: terminate it.
        needs_nl = False
        with open(self._index_path, "rb") as f:
            if f.seek(0, os.SEEK_END) > 0:
                f.seek(-1, os.SEEK_END)
                needs_nl = f.read(1) != b"\n"
        if needs_nl:
            with open(self._index_path, "ab") as f:
                f.write(b"\n")
        row_bytes = 2 * self.dim
        size = os.path.getsize(self._data_path()) if os.path.exists(self._data_path()) else 0
        if size % row_bytes:
            # Torn trailing row: truncate so appends stay row-aligned.
            with open(self._data_path(), "r+b") as f:
                f.truncate(size - size % row_bytes)
            size -= size % row_bytes
        self._rows = size // row_bytes
        self._live = {fn: {c: r for c, r in rows.items() if r < self._rows} for fn, rows in live.items()}
        self._live = {fn: rows for fn, rows in self._live.items() if rows}

    def _new_data_name(self) -> str:
        return f"{uuid.uuid4().hex[:16]}.f16"

    def _reset(self) -> None:
        # Only plain files of THIS slot: other slots are subdirectories.
        for name in os.listdir(self.dir):
            if (name.endswith(".f16") or name.endswith(".tmp")) and os.path.isfile(os.path.join(self.dir, name)):
                try:
                    os.remove(os.path.join(self.dir, name))
                except OSError:
                    pass
        self._data_name = self._new_data_name()
        open(self._data_path(), "wb").close()
        self._write_index_atomic(self._header(self._data_name), {})
        self._live, self._rows = {}, 0

    def _write_index_atomic(self, header: dict, live: Dict[str, Dict[int, int]]) -> None:
        tmp = self._index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(header) + "\n")
            for fn in sorted(live):
                f.write(json.dumps({"f": fn, "rows": {str(k): v for k, v in sorted(live[fn].items())}}) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._index_path)

    def _append_line(self, filename: str, rows: Dict[int, int]) -> None:
        with open(self._index_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"f": filename, "rows": {str(k): v for k, v in sorted(rows.items())}}) + "\n")

    # ------------------------------------------------------------------ public

    def put(self, filename: str, vectors: Dict[int, "np.ndarray"]) -> None:
        """Record (replacing any previous) the crop embeddings for one image."""
        if not self._ok:
            return
        try:
            filename = str(filename)
            clean = {}
            for crop, v in (vectors or {}).items():
                if v is None:
                    continue
                arr = np.asarray(v, dtype=np.float32).reshape(-1)
                if arr.size != self.dim or not np.all(np.isfinite(arr)):
                    continue
                clean[int(crop)] = arr
            if not clean:
                if filename in self._live:
                    self._append_line(filename, {})
                    self._live.pop(filename, None)
                return
            order = sorted(clean)
            block = np.stack([clean[c] for c in order]).astype("<f2")
            with open(self._data_path(), "ab") as f:
                f.write(block.tobytes())
            rows = {c: self._rows + i for i, c in enumerate(order)}
            self._rows += len(order)
            self._append_line(filename, rows)
            self._live[filename] = rows
        except Exception as e:
            self._warn(f"put({filename}) failed: {type(e).__name__}: {e}")

    def has(self, filename: str) -> bool:
        return str(filename) in self._live

    @property
    def filenames(self) -> set:
        return set(self._live)

    def remove(self, filenames: Iterable[str]) -> None:
        if not self._ok:
            return
        try:
            for fn in filenames:
                fn = str(fn)
                if fn in self._live:
                    self._append_line(fn, {})
                    self._live.pop(fn, None)
        except Exception as e:
            self._warn(f"remove failed: {type(e).__name__}: {e}")

    def compact(self, keep_filenames: Optional[Iterable[str]] = None) -> None:
        """Rewrite only live rows (optionally restricted to ``keep_filenames``)."""
        if not self._ok:
            return
        try:
            keep = set(map(str, keep_filenames)) if keep_filenames is not None else None
            live = {fn: rows for fn, rows in self._live.items() if keep is None or fn in keep}
            n_live = sum(len(r) for r in live.values())
            if n_live == self._rows and len(live) == len(self._live) and not self._has_dead_lines():
                return
            old = np.fromfile(self._data_path(), dtype="<f2")
            old = old[: (old.size // self.dim) * self.dim].reshape(-1, self.dim)
            new_name = self._new_data_name()
            new_live: Dict[str, Dict[int, int]] = {}
            chunks = []
            nxt = 0
            for fn in sorted(live):
                rows = {}
                for crop, r in sorted(live[fn].items()):
                    if r < old.shape[0]:
                        chunks.append(old[r])
                        rows[crop] = nxt
                        nxt += 1
                if rows:
                    new_live[fn] = rows
            tmp = self._data_path(new_name) + ".tmp"
            with open(tmp, "wb") as f:
                if chunks:
                    f.write(np.stack(chunks).astype("<f2").tobytes())
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._data_path(new_name))
            self._write_index_atomic(self._header(new_name), new_live)
            old_name = self._data_name
            self._data_name, self._live, self._rows = new_name, new_live, nxt
            try:
                os.remove(self._data_path(old_name))
            except OSError:
                pass
        except Exception as e:
            self._warn(f"compact failed: {type(e).__name__}: {e}")

    def _has_dead_lines(self) -> bool:
        try:
            with open(self._index_path, "r", encoding="utf-8") as f:
                n_lines = sum(1 for line in f if line.strip()) - 1
            return n_lines != len(self._live)
        except Exception:
            return True
