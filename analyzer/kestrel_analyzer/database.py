import json
import os
import tempfile
import time
from datetime import datetime

import pandas as pd

from .config import DATABASE_NAME, METADATA_FILENAME, SCENEDATA_FILENAME, VERSION
from .logging_utils import log_warning

# Leveled console logging — kestrel_analyzer is sometimes imported standalone
# (e.g. tests), so fall back to a no-op if settings_utils isn't reachable.
try:
    from ..settings_utils import info as _info, warn as _warn
except (ImportError, ValueError):
    # cli.py imports kestrel_analyzer as a top-level package; relative ``..``
    # walks above the root. Bare ``settings_utils`` works because analyzer/
    # is on sys.path in that case.
    try:
        from settings_utils import info as _info, warn as _warn  # type: ignore
    except ImportError:
        def _info(*_a, **_kw): pass
        def _warn(*_a, **_kw): pass

# Columns written by the analysis pipeline only (no user-editable data).
BASE_COLUMNS = [
    "filename",
    "species",
    "species_confidence",
    "family",
    "family_confidence",
    "quality",
    "export_path",
    "crop_path",
    "crops_json",
    "primary_crop_index",
    "scene_count",
    "feature_similarity",
    "feature_confidence",
    "color_similarity",
    "color_confidence",
    "similar",
    "secondary_species_list",
    "secondary_species_scores",
    "secondary_family_list",
    "secondary_family_scores",
    "exposure_correction",
    "exposure_pipeline",
    "exposure_subject_stops",
    "exposure_meter_scale",
    "detection_scores",
    "capture_time",
]

# Legacy user-editable columns previously stored in kestrel_database.csv.
# Migrated to kestrel_scenedata.json on first upgrade and stripped from the CSV.
LEGACY_USER_COLUMNS = ["rating", "normalized_rating", "scene_name", "rating_origin"]

# Schema version for kestrel_scenedata.json
SCENEDATA_VERSION = "2.0"

REQUIRED_COLUMNS = [
    "family",
    "family_confidence",
    "secondary_family_list",
    "secondary_family_scores",
]


def load_database(kestrel_dir: str, analyzer_name: str, log_path: str = None):
    db_path = os.path.join(kestrel_dir, DATABASE_NAME)
    metadata_path = os.path.join(kestrel_dir, METADATA_FILENAME)

    if os.path.exists(db_path):
        database = read_database_csv(db_path)
        # Upgrade legacy database: migrate user columns to scenedata.json
        if _needs_upgrade(database, kestrel_dir):
            database = _perform_db_upgrade(database, kestrel_dir, db_path, log_path)
    else:
        database = pd.DataFrame(columns=BASE_COLUMNS)
        try:
            if not os.path.exists(metadata_path):
                metadata = {
                    "version": VERSION,
                    "analyzer": analyzer_name,
                    "created_utc": datetime.utcnow().isoformat() + "Z",
                    "database_file": DATABASE_NAME,
                }
                with open(metadata_path, "w", encoding="utf-8") as mf:
                    json.dump(metadata, mf, indent=2)
        except Exception as e:
            if log_path:
                log_warning(
                    log_path,
                    f"Failed to write metadata file: {e}",
                    category=type(e),
                    stage="metadata_write",
                    context={"metadata_path": metadata_path},
                )
            else:
                _warn(f"[database] failed to write metadata file: {e}")

    database = ensure_columns(database)
    return database, db_path


def _needs_upgrade(database: pd.DataFrame, kestrel_dir: str) -> bool:
    """Return True if the database has legacy user columns and scenedata.json doesn't exist yet."""
    has_legacy = any(col in database.columns for col in LEGACY_USER_COLUMNS)
    scenedata_exists = os.path.exists(os.path.join(kestrel_dir, SCENEDATA_FILENAME))
    return has_legacy and not scenedata_exists


def _perform_db_upgrade(
    database: pd.DataFrame, kestrel_dir: str, db_path: str, log_path: str = None
) -> pd.DataFrame:
    """Migrate legacy database: extract user data to scenedata.json and strip legacy columns."""
    # Build and save scenedata from legacy database
    try:
        scenedata = _build_scenedata_from_legacy_db(database)
        save_scenedata(scenedata, kestrel_dir)
        _info(f"[database] Migrated legacy user data to {SCENEDATA_FILENAME}")
    except Exception as e:
        if log_path:
            log_warning(
                log_path,
                f"Failed to migrate legacy database to {SCENEDATA_FILENAME}: {e}",
                category=type(e),
                stage="db_upgrade",
                context={"kestrel_dir": kestrel_dir},
            )
        else:
            _warn(f"[database] failed to migrate legacy database: {e}")

    # Rename old CSV as backup, then save new one without legacy columns
    try:
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        old_path = os.path.join(kestrel_dir, f"OLD_kestrel_database_{timestamp}.csv")
        os.rename(db_path, old_path)
        cleaned = database.drop(
            columns=[c for c in LEGACY_USER_COLUMNS if c in database.columns],
            errors="ignore",
        )
        _to_csv_atomic(cleaned, db_path)
        _info(
            f"[database] Upgrade complete: backup at {os.path.basename(old_path)}, "
            f"new clean {DATABASE_NAME} saved."
        )
    except Exception as e:
        if log_path:
            log_warning(
                log_path,
                f"Failed to rename/save upgraded database: {e}",
                category=type(e),
                stage="db_upgrade",
                context={"kestrel_dir": kestrel_dir},
            )
        else:
            _warn(f"[database] failed to save upgraded database: {e}")

    return database.drop(
        columns=[c for c in LEGACY_USER_COLUMNS if c in database.columns],
        errors="ignore",
    )


def _build_scenedata_from_legacy_db(database: pd.DataFrame) -> dict:
    """Build a fresh scenedata dict from a legacy database DataFrame, preserving user edits."""
    scenedata: dict = {
        "version": SCENEDATA_VERSION,
        "image_ratings": {},
        "scenes": {},
    }

    # Extract per-image manual ratings
    if "rating" in database.columns:
        has_origin = "rating_origin" in database.columns
        for _, row in database.iterrows():
            filename = str(row.get("filename", ""))
            if not filename:
                continue
            origin = str(row.get("rating_origin", "")).lower() if has_origin else ""
            rating_val = row.get("rating", None)
            try:
                r = int(float(rating_val))
            except (TypeError, ValueError):
                continue
            # Save if explicitly manual, or if non-zero with no origin (implies user intent)
            if origin == "manual" or (not has_origin and 1 <= r <= 5):
                if 1 <= r <= 5:
                    scenedata["image_ratings"][filename] = r

    # Build scenes from scene_count grouping
    if "scene_count" in database.columns:
        groups: dict = {}
        for _, row in database.iterrows():
            sc = str(row.get("scene_count", "0"))
            if sc not in groups:
                groups[sc] = []
            fname = str(row.get("filename", ""))
            if fname:
                groups[sc].append(fname)

        for sc, filenames in groups.items():
            scene_name = ""
            if "scene_name" in database.columns:
                mask = database["scene_count"].astype(str) == sc
                for sn in database.loc[mask, "scene_name"]:
                    if str(sn).strip():
                        scene_name = str(sn).strip()
                        break
            scenedata["scenes"][sc] = {
                "scene_id": sc,
                "image_filenames": filenames,
                "name": scene_name,
                "status": "pending",
                "user_tags": {
                    "species": [],
                    "families": [],
                    "finalized": False,
                },
            }

    return scenedata


def build_scenedata_from_database(database: pd.DataFrame) -> dict:
    """Build a fresh scenedata dict from a clean (non-legacy) database.

    Used when creating a new kestrel_scenedata.json for a freshly-analyzed folder.
    """
    scenedata: dict = {
        "version": SCENEDATA_VERSION,
        "image_ratings": {},
        "scenes": {},
    }

    if "scene_count" not in database.columns or database.empty:
        return scenedata

    groups: dict = {}
    for _, row in database.iterrows():
        sc = str(row.get("scene_count", "0"))
        if sc not in groups:
            groups[sc] = []
        fname = str(row.get("filename", ""))
        if fname:
            groups[sc].append(fname)

    for sc, filenames in groups.items():
        scenedata["scenes"][sc] = {
            "scene_id": sc,
            "image_filenames": filenames,
            "name": "",
            "status": "pending",
            "user_tags": {
                "species": [],
                "families": [],
                "finalized": False,
            },
        }

    return scenedata


def update_scenedata_with_database(scenedata: dict, database: pd.DataFrame) -> dict:
    """Update existing scenedata by adding newly-analyzed images from database.

    New images are added to their correct scenes (by scene_count) without overwriting
    any user-edited data (ratings, names, tags, custom scene membership).
    Returns the mutated scenedata dict.
    """
    if (
        "filename" not in database.columns
        or "scene_count" not in database.columns
        or database.empty
    ):
        return scenedata

    # Build set of all filenames already tracked in scenedata
    known: set = set()
    for scene_entry in scenedata.get("scenes", {}).values():
        for fname in scene_entry.get("image_filenames", []):
            known.add(fname)

    scenes = scenedata.setdefault("scenes", {})
    for _, row in database.iterrows():
        fname = str(row.get("filename", ""))
        if not fname or fname in known:
            continue
        sc = str(row.get("scene_count", "0"))
        if sc not in scenes:
            scenes[sc] = {
                "scene_id": sc,
                "image_filenames": [],
                "name": "",
                "status": "pending",
                "user_tags": {"species": [], "families": [], "finalized": False},
            }
        scenes[sc]["image_filenames"].append(fname)
        known.add(fname)

    return scenedata


def load_scenedata(kestrel_dir: str) -> dict:
    """Load kestrel_scenedata.json. Returns an empty initialized dict if the file is missing."""
    scenedata_path = os.path.join(kestrel_dir, SCENEDATA_FILENAME)
    if os.path.exists(scenedata_path):
        try:
            with open(scenedata_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Ensure required keys (forward compatibility)
            data.setdefault("version", SCENEDATA_VERSION)
            data.setdefault("image_ratings", {})
            data.setdefault("scenes", {})
            return data
        except Exception as e:
            _warn(f"[database] failed to load {SCENEDATA_FILENAME}: {e}")
    return {"version": SCENEDATA_VERSION, "image_ratings": {}, "scenes": {}}


def save_scenedata(scenedata: dict, kestrel_dir: str) -> None:
    """Save scenedata dict to kestrel_scenedata.json.

    Written atomically: scenedata holds the user's ratings, tags and Accept/
    Reject decisions, so a partial write from a crash/power loss must never
    truncate the existing file.
    """
    scenedata_path = os.path.join(kestrel_dir, SCENEDATA_FILENAME)
    write_json_atomic(scenedata_path, scenedata, indent=2)


def ensure_columns(database: pd.DataFrame) -> pd.DataFrame:
    """Ensure required analysis columns exist with appropriate defaults."""
    for col in REQUIRED_COLUMNS:
        if col not in database.columns:
            if col.endswith("_list"):
                database[col] = [[] for _ in range(len(database))]
            elif col.endswith("_scores"):
                database[col] = [[] for _ in range(len(database))]
            else:
                database[col] = "Unknown" if "family" in col else 0.0
    if "exposure_correction" not in database.columns:
        database["exposure_correction"] = 0.0
    if "exposure_pipeline" not in database.columns:
        database["exposure_pipeline"] = "legacy_auto_bright_v1"
    if "exposure_subject_stops" not in database.columns:
        database["exposure_subject_stops"] = 0.0
    if "exposure_meter_scale" not in database.columns:
        database["exposure_meter_scale"] = 1.0
    if "detection_scores" not in database.columns:
        database["detection_scores"] = [[] for _ in range(len(database))]
    if "crops_json" not in database.columns:
        database["crops_json"] = "[]"
    if "primary_crop_index" not in database.columns:
        database["primary_crop_index"] = 0
    if "capture_time" not in database.columns:
        database["capture_time"] = ""
    return database


# Columns the UI writes to the CSV that the pipeline should preserve.
# These are NOT in BASE_COLUMNS but may be added by the UI's saveCsv().
_UI_PRESERVE_COLUMNS = ["culled", "culled_origin"]

# Prefix/suffix for the temp file used by ``_to_csv_atomic``. Distinctive so a
# temp left behind by a crashed/killed save is identifiable, and so it never
# collides with the ``OLD_kestrel_database_*.csv`` upgrade backups.
_TMP_FILE_PREFIX = ".kestrel_database_"
_TMP_FILE_SUFFIX = ".csv.tmp"


def retry_on_file_lock(op, attempts: int = 12, delay: float = 0.02):
    """Run ``op()``, retrying briefly on Windows' transient file-sharing errors.

    ``os.replace`` is atomic on both POSIX and Windows, but Windows adds a
    constraint POSIX does not have: CPython opens files without
    ``FILE_SHARE_DELETE``, so while *any* handle is open on the destination the
    underlying ``MoveFileEx`` fails with ``ERROR_ACCESS_DENIED`` (5) or
    ``ERROR_SHARING_VIOLATION`` (32). Symmetrically, a reader that calls
    ``open()`` during the rename can catch the destination in a delete-pending
    state and get ``ERROR_ACCESS_DENIED``. Both surface as ``PermissionError``.

    So on Windows the atomic write guarantees all-or-nothing *content* but not a
    collision-free ``open()``: the pipeline saving after every image and the UI's
    auto-refresh reader will occasionally step on each other in both directions.
    Both sides retry through here rather than failing — the writer to avoid
    silently dropping a save, the reader to avoid a spurious load error.

    Escalating backoff caps the total wait at roughly 1.3s; if the file is still
    locked after that the error propagates to the caller. POSIX never raises
    here and always succeeds on the first attempt.

    This is a mitigation, not a guarantee. A reader that reopens the file in a
    zero-gap loop can starve the writer past the retry window — genuinely fixing
    that would mean opening the destination with ``FILE_SHARE_DELETE``, which
    CPython's ``open()`` cannot do. The app's readers poll on a UI timer, so the
    window is wide open in practice.
    """
    for attempt in range(attempts):
        try:
            return op()
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay * (attempt + 1))


def read_database_csv(db_path: str, **read_csv_kwargs) -> pd.DataFrame:
    """``pd.read_csv(db_path)`` that tolerates a concurrent atomic save.

    Use this anywhere the analysis pipeline might be writing the same CSV. See
    ``retry_on_file_lock`` for why the bare call is not enough on Windows.
    """
    return retry_on_file_lock(lambda: pd.read_csv(db_path, **read_csv_kwargs))


def read_database_text(db_path: str) -> str:
    """Read the CSV as raw text, tolerating a concurrent atomic save.

    The bridge hands the file to the frontend verbatim rather than parsing it,
    so it needs the text form of ``read_database_csv``.
    """
    def _read() -> str:
        with open(db_path, 'r', encoding='utf-8') as f:
            return f.read()

    return retry_on_file_lock(_read)


def _to_csv_atomic(database: pd.DataFrame, db_path: str) -> None:
    """Write ``database`` to ``db_path`` so readers never observe a partial file.

    ``DataFrame.to_csv(path)`` truncates the destination and streams rows into
    it. The analysis pipeline saves after every processed image while the UI's
    auto-refresh timer reads the same path (``read_kestrel_csv`` /
    ``apply_normalization``), so a reader landing mid-write sees either an empty
    file (pandas ``EmptyDataError: No columns to parse from file``) or a row cut
    inside a quoted field such as ``crops_json`` (pandas ``ParserError: EOF
    inside string starting at row N``).

    Write to a unique temp file in the same directory and ``os.replace`` it into
    place instead. ``os.replace`` is atomic on POSIX and on Windows, so a
    concurrent reader observes either the complete previous file or the complete
    new one. This mirrors the atomic-save pattern in ``settings_utils.save_settings``.
    """
    directory = os.path.dirname(db_path) or "."
    os.makedirs(directory, exist_ok=True)

    # mkstemp creates with O_EXCL, so concurrent saves can never share a path.
    tmp_fd, tmp = tempfile.mkstemp(
        prefix=_TMP_FILE_PREFIX,
        suffix=_TMP_FILE_SUFFIX,
        dir=directory,
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="") as f:
            database.to_csv(f, index=False)
            # flush() errors (ENOSPC, EIO) must propagate: a partial temp
            # file must not be os.replace'd over a good destination.
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # fsync can legitimately fail on some network filesystems
                # (SMB/NFS shares are a common target); the replace below
                # still gives readers an all-or-nothing view.
                pass
        retry_on_file_lock(lambda: os.replace(tmp, db_path))
    except BaseException:
        # Do NOT fall back to a direct write — that is the partial-read path
        # this function exists to close. Leave the previous file intact.
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _write_file_atomic(path: str, write_fn, encoding: str = "utf-8") -> None:
    """Atomically write by calling ``write_fn(file)`` on a temp file, then replace.

    ``write_fn`` receives an open text file (encoding/newline already set) and
    must write the full payload into it. Shared by ``write_text_atomic`` and
    ``write_json_atomic``. Mirrors ``_to_csv_atomic`` / ``settings_utils.save_settings``.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    tmp_fd, tmp = tempfile.mkstemp(prefix=".kestrel_atomic_", suffix=".tmp", dir=directory)
    try:
        # If os.fdopen raises, it has NOT taken ownership of tmp_fd, so the
        # descriptor would leak (and on Windows keep the temp file locked). Close
        # it explicitly in that case; on success the with-block owns and closes it.
        try:
            f = os.fdopen(tmp_fd, "w", encoding=encoding, newline="")
        except BaseException:
            os.close(tmp_fd)
            raise
        with f:
            write_fn(f)
            # flush() errors (ENOSPC, EIO) must propagate: a partial temp
            # file must not be os.replace'd over a good destination.
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # fsync can legitimately fail on some network filesystems; the
                # replace below still gives readers an all-or-nothing view.
                pass
        retry_on_file_lock(lambda: os.replace(tmp, path))
    except BaseException:
        # Do NOT fall back to a direct write -- that is the partial-write path
        # this helper exists to close. Leave the previous file intact.
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def write_text_atomic(path: str, text: str, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` atomically (temp file + ``os.replace``).

    Plain ``open(path, "w")`` truncates the destination and streams into it, so
    a crash/power loss mid-write leaves a partial file behind. For the UI's
    raw-text CSV save that means a truncated database. Writing to a unique temp
    file in the same directory and ``os.replace``-ing it into place gives
    readers/crashes an all-or-nothing view.
    """
    _write_file_atomic(path, lambda f: f.write(text), encoding=encoding)


def write_json_atomic(path: str, obj, indent: int = 2) -> None:
    """Serialize ``obj`` to JSON at ``path`` atomically via streaming ``json.dump``.

    Unlike ``write_text_atomic(json.dumps(obj))``, this never materializes the
    full serialized string in memory -- important for large scenedata payloads
    (ratings, tags, cull decisions).
    """
    def _dump(f):
        json.dump(obj, f, indent=indent)

    _write_file_atomic(path, _dump)


def save_database(database: pd.DataFrame, db_path: str) -> None:
    """Save database to CSV, preserving UI-written columns from disk.

    The analysis pipeline only writes BASE_COLUMNS. The UI may have saved
    user-editable columns (culled, culled_origin) to the same CSV between
    pipeline saves. This function reads those columns from the existing CSV
    and merges them back into the pipeline's DataFrame before writing, so
    user decisions made during analysis are not lost.

    Legacy user columns (rating, scene_name, etc.) are stripped — those now
    live in kestrel_scenedata.json.
    """
    cols_to_drop = [c for c in LEGACY_USER_COLUMNS if c in database.columns]
    if cols_to_drop:
        database = database.drop(columns=cols_to_drop)

    # Preserve UI-written columns from the existing CSV on disk
    if os.path.exists(db_path):
        try:
            # Only read the columns we need to preserve (+ filename for joining)
            cols_to_read = ["filename"] + [
                c for c in _UI_PRESERVE_COLUMNS
            ]
            disk_df = read_database_csv(db_path, usecols=lambda c: c in cols_to_read)
            if not disk_df.empty and "filename" in disk_df.columns:
                for col in _UI_PRESERVE_COLUMNS:
                    if col in disk_df.columns and col not in database.columns:
                        # Build a filename→value map from disk
                        col_map = dict(
                            zip(disk_df["filename"].astype(str), disk_df[col])
                        )
                        database[col] = database["filename"].astype(str).map(col_map)
        except Exception:
            pass  # If we can't read the existing CSV, just write what we have

    _to_csv_atomic(database, db_path)
