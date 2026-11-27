"""Culling passes: the move history, Cull mode's session file, and Revert / Reset.

Cull mode records its progress in ``.kestrel/cull_session.json``: the pass in
progress (its starting picks, per-scene cuts and confirmed scenes) and, for
every finished pass, the decision each photo ended with and what that pass
moved into ``_KESTREL_Rejects``. The photo decisions themselves stay where the
rest of the app reads them, in the ``culled`` / ``culled_origin`` columns of
``kestrel_database.csv``; this file only adds what those columns cannot hold.

File moves are undone through the culling-pass history at the end of this
module (``.kestrel/cull_history.json``), which restores the database snapshot
taken just before each move. Those snapshots are older than the user's latest
edits, so after any undo the user columns are written back on top:

- Undo move keeps every decision and favorite the user has now.
- Revert to pass N sets decisions to how pass N ended.
- Reset clears every decision.

Favorites are never cleared by any of the three.

Nothing here imports UI or bridge code. The bridge validates paths, lists the
rejects folder and passes in the callables that move files back.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from datetime import datetime, timezone
from typing import Callable, Iterable

from kestrel_analyzer.database import copy_file_atomic, write_json_atomic, write_text_atomic
from settings_utils import info, warn

SESSION_NAME = 'cull_session.json'
DB_NAME = 'kestrel_database.csv'
REJECT_DIR_NAME = '_KESTREL_Rejects'
VERSION = 1

MAX_PASSES = 10
MAX_FILES = 200_000
MAX_SCENE_KEYS = 50_000
_DECISIONS = {'accept', 'reject', 'accept:manual', 'reject:manual'}
_NO_BIRD = {'accept', 'reject'}
_SCENE_KEY = re.compile(r'^[A-Za-z0-9_.:-]{1,64}$')
_HISTORY_ID = re.compile(r'^[A-Za-z0-9_]{1,40}$')
_MOVED_ROW_FIELDS = ('filename', 'scene_count', 'quality', 'export_path', 'crop_path',
                     'capture_time', 'species', 'rating')
USER_COLUMNS = ('culled', 'culled_origin', 'favorite')


# ── Sanitizing ────────────────────────────────────────────────────────────────

def _plain_name(value) -> str:
    """A bare file name (no directories), or '' if it isn't one."""
    name = str(value or '').strip()
    if not name or name in {'.', '..'} or len(name) > 255:
        return ''
    if '/' in name or '\\' in name or ':' in name or '\x00' in name:
        return ''
    return name


def _num(value, lo: float, hi: float, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, v))


def _rules(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    no_bird = str(raw.get('no_bird') or 'reject')
    return {
        'threshold': round(_num(raw.get('threshold'), 0.0, 1.0, 0.5), 4),
        'min_per_scene': int(_num(raw.get('min_per_scene'), 0, 20, 1)),
        'max_per_scene': int(_num(raw.get('max_per_scene'), 0, 100, 0)),
        'no_bird': no_bird if no_bird in _NO_BIRD else 'reject',
    }


def _cuts(raw) -> dict:
    out = {}
    if not isinstance(raw, dict):
        return out
    for k, v in list(raw.items())[:MAX_SCENE_KEYS]:
        key = str(k)
        if _SCENE_KEY.match(key):
            out[key] = int(_num(v, 0, 100_000, 0))
    return out


def _scene_list(raw) -> list:
    if not isinstance(raw, (list, tuple)):
        return []
    seen, out = set(), []
    for k in raw[:MAX_SCENE_KEYS]:
        key = str(k)
        if _SCENE_KEY.match(key) and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _decisions(raw) -> dict:
    out = {}
    if not isinstance(raw, dict):
        return out
    for fn, v in list(raw.items())[:MAX_FILES]:
        name = _plain_name(fn)
        if name and v in _DECISIONS:
            out[name] = v
    return out


def _moved_rows(raw) -> list:
    out = []
    if not isinstance(raw, list):
        return out
    for r in raw[:MAX_FILES]:
        if not isinstance(r, dict) or not _plain_name(r.get('filename')):
            continue
        out.append({k: str(r.get(k) if r.get(k) is not None else '')[:1024] for k in _MOVED_ROW_FIELDS})
    return out


def _iso(value) -> str:
    s = str(value or '')[:40]
    return s if re.match(r'^[0-9T:.+\-Z ]*$', s) else ''


def _finished_pass(raw, number: int) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    hid = str(raw.get('history_id') or '')
    return {
        'number': number,
        'finished_at': _iso(raw.get('finished_at')),
        'rules': _rules(raw.get('rules')),
        'decisions': _decisions(raw.get('decisions')),
        'accepted': int(_num(raw.get('accepted'), 0, MAX_FILES, 0)),
        'rejected': int(_num(raw.get('rejected'), 0, MAX_FILES, 0)),
        'moved': int(_num(raw.get('moved'), 0, MAX_FILES, 0)),
        'history_id': hid if _HISTORY_ID.match(hid) else '',
        'move_undone': bool(raw.get('move_undone')),
        'metadata_written': bool(raw.get('metadata_written')),
        'moved_rows': _moved_rows(raw.get('moved_rows')),
    }


def sanitize(data) -> dict:
    """Return a clean session dict; anything malformed falls back to defaults."""
    data = data if isinstance(data, dict) else {}
    passes_raw = data.get('passes') if isinstance(data.get('passes'), list) else []
    passes = [_finished_pass(p, i + 1) for i, p in enumerate(passes_raw[-MAX_PASSES:])]
    # The pass in progress is always the one after the last finished pass,
    # unless that last pass has just ended and no new pass has started.
    ended = bool(data.get('ended')) and bool(passes)
    current = len(passes) if ended else len(passes) + 1
    return {
        'version': VERSION,
        'current': current,
        'ended': ended,
        'started': bool(data.get('started')) and not ended,
        'rules': _rules(data.get('rules')),
        'cuts': _cuts(data.get('cuts')),
        'confirmed': _scene_list(data.get('confirmed')),
        'passes': passes,
    }


# ── File I/O ──────────────────────────────────────────────────────────────────

def session_path(kestrel_dir: str) -> str:
    return os.path.join(kestrel_dir, SESSION_NAME)


def load(kestrel_dir: str) -> dict | None:
    """The folder's session, or None when Cull mode has not been used here."""
    path = session_path(kestrel_dir)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return sanitize(json.load(f))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        warn(f'[cull-session] unreadable {SESSION_NAME}, starting fresh: {e}')
        return None


def save(kestrel_dir: str, data) -> dict:
    clean = sanitize(data)
    write_json_atomic(session_path(kestrel_dir), clean, indent=1)
    return clean


def clear(kestrel_dir: str) -> None:
    try:
        os.remove(session_path(kestrel_dir))
    except FileNotFoundError:
        pass


# ── Which finished passes can be reverted to ─────────────────────────────────

def _names_on_disk(root_real: str) -> set:
    names = set()
    for d in (root_real, os.path.join(root_real, REJECT_DIR_NAME)):
        try:
            for n in os.listdir(d):
                names.add(n.casefold())
        except FileNotFoundError:
            continue
    return names


def describe(root_real: str, session: dict) -> dict:
    """Add ``on_disk`` to each finished pass: every photo it decided is still
    in the folder or in _KESTREL_Rejects, so Revert to this pass can restore it."""
    on_disk = _names_on_disk(root_real)
    out = dict(session)
    out['passes'] = []
    for p in session.get('passes', []):
        view = {k: v for k, v in p.items() if k != 'decisions'}
        files = p.get('decisions', {}).keys()
        missing = sum(1 for f in files if f.casefold() not in on_disk)
        view['file_count'] = len(p.get('decisions', {}))
        view['missing'] = missing
        view['on_disk'] = missing == 0
        view['decisions'] = p.get('decisions', {})
        out['passes'].append(view)
    return out


# ── User columns in kestrel_database.csv ─────────────────────────────────────

def _read_csv(path: str):
    with open(path, 'r', encoding='utf-8-sig', newline='') as f:
        text = f.read()
    reader = csv.DictReader(io.StringIO(text))
    fields = list(reader.fieldnames or [])
    return fields, list(reader)


def _write_csv(path: str, fields: list, rows: list) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction='ignore', lineterminator='\r\n')
    w.writeheader()
    for r in rows:
        w.writerow({k: ('' if r.get(k) is None else r.get(k)) for k in fields})
    write_text_atomic(path, buf.getvalue())


def read_user_columns(db_path: str) -> dict:
    """filename -> {culled, culled_origin, favorite} for every row in the CSV."""
    if not os.path.isfile(db_path):
        return {}
    _fields, rows = _read_csv(db_path)
    return {r.get('filename', ''): {c: r.get(c) or '' for c in USER_COLUMNS} for r in rows if r.get('filename')}


def write_user_columns(db_path: str, values: dict, default: dict | None = None) -> int:
    """Set the user columns row by row.

    ``values`` maps filename -> {column: value}. Rows not in ``values`` get
    ``default`` when given and are left alone otherwise. Returns rows changed.
    """
    if not os.path.isfile(db_path):
        return 0
    fields, rows = _read_csv(db_path)
    for c in USER_COLUMNS:
        if c not in fields:
            fields.append(c)
    changed = 0
    for r in rows:
        upd = values.get(r.get('filename', ''), default)
        if not upd:
            continue
        before = tuple(r.get(c) or '' for c in USER_COLUMNS)
        for c, v in upd.items():
            if c in USER_COLUMNS:
                r[c] = v
        if tuple(r.get(c) or '' for c in USER_COLUMNS) != before:
            changed += 1
    _write_csv(db_path, fields, rows)
    return changed


def decision_columns(code: str) -> dict:
    """``accept`` / ``reject:manual`` etc. -> culled / culled_origin values."""
    state, _, origin = code.partition(':')
    return {'culled': state, 'culled_origin': origin or 'verified'}


# ── Undo move / Revert / Reset ────────────────────────────────────────────────

UndoFn = Callable[[], dict]
HistoryIdsFn = Callable[[], Iterable[str]]


def _undo_moves(ids_to_undo: set, history_ids: HistoryIdsFn, undo_last: UndoFn) -> dict:
    """Undo history passes from the newest down while the newest is in ``ids_to_undo``."""
    restored, errors = 0, []
    for _ in range(MAX_PASSES + 2):
        ids = list(history_ids())
        if not ids or ids[-1] not in ids_to_undo:
            break
        res = undo_last() or {}
        if not res.get('success'):
            errors.append(res.get('error') or 'undo failed')
            break
        restored += int(res.get('restored') or 0)
        errors.extend(res.get('errors') or [])
    return {'restored': restored, 'errors': errors}


def _favorites_now(db_path: str, session: dict) -> dict:
    favs = {fn: v['favorite'] for fn, v in read_user_columns(db_path).items() if v.get('favorite')}
    for p in session.get('passes', []):
        for r in p.get('moved_rows', []):
            fn = r.get('filename')
            if fn and fn not in favs and r.get('favorite'):
                favs[fn] = r['favorite']
    return favs


def undo_move(kestrel_dir: str, history_ids: HistoryIdsFn, undo_last: UndoFn) -> dict:
    """Move the newest moved pass's rejects back, keeping every current decision."""
    session = load(kestrel_dir)
    if not session:
        return {'success': False, 'error': 'no Cull mode passes in this folder'}
    target = next((p for p in reversed(session['passes'])
                   if p['history_id'] and not p['move_undone']), None)
    if not target:
        return {'success': False, 'error': 'nothing to undo'}
    ids = list(history_ids())
    if not ids or ids[-1] != target['history_id']:
        return {'success': False, 'error': 'a later move must be undone first'}
    db_path = os.path.join(kestrel_dir, DB_NAME)
    keep = read_user_columns(db_path)
    res = _undo_moves({target['history_id']}, history_ids, undo_last)
    if res['errors'] and not res['restored']:
        return {'success': False, 'error': '; '.join(map(str, res['errors'][:3]))}
    write_user_columns(db_path, keep)
    target['move_undone'] = True
    save(kestrel_dir, session)
    info(f"[cull-session] undid pass {target['number']} move: {res['restored']} file(s)")
    return {'success': True, 'pass': target['number'], **res}


def revert(kestrel_dir: str, to_pass: int, history_ids: HistoryIdsFn, undo_last: UndoFn) -> dict:
    """Revert to the end of pass ``to_pass``; 0 resets the folder.

    Moves made by later passes (all moves, for a reset) are undone. Decisions
    become those pass ``to_pass`` ended with (none, for a reset); favorites
    stay as they are now.
    """
    session = load(kestrel_dir) or sanitize({})
    passes = session['passes']
    if to_pass < 0 or to_pass > len(passes):
        return {'success': False, 'error': f'no pass {to_pass}'}
    db_path = os.path.join(kestrel_dir, DB_NAME)
    favorites = _favorites_now(db_path, session)

    if to_pass == 0:
        later_ids = set(history_ids())
    else:
        later_ids = {p['history_id'] for p in passes[to_pass:] if p['history_id'] and not p['move_undone']}
    res = _undo_moves(later_ids, history_ids, undo_last)
    left = later_ids & set(history_ids())
    if left:
        return {'success': False, 'error': '; '.join(map(str, res['errors'][:3])) or 'some moves could not be undone',
                **res}

    if to_pass == 0:
        _clear_decisions(db_path, favorites)
        clear(kestrel_dir)
        info(f"[cull-session] folder reset: {res['restored']} file(s) moved back")
        return {'success': True, 'pass': 0, **res}

    target = passes[to_pass - 1]
    values = {}
    for fn, code in target['decisions'].items():
        values[fn] = {**decision_columns(code), 'favorite': favorites.get(fn, '')}
    for fn, fav in favorites.items():
        values.setdefault(fn, {'favorite': fav})
    write_user_columns(db_path, values)
    session['passes'] = passes[:to_pass]
    session['ended'] = True
    session['started'] = False
    session['confirmed'] = []
    session['cuts'] = {}
    session['rules'] = target['rules']
    save(kestrel_dir, session)
    info(f"[cull-session] reverted to pass {to_pass}: {res['restored']} file(s) moved back")
    return {'success': True, 'pass': to_pass, **res}


def _clear_decisions(db_path: str, favorites: dict) -> None:
    if not os.path.isfile(db_path):
        return
    fields, rows = _read_csv(db_path)
    for c in USER_COLUMNS:
        if c not in fields:
            fields.append(c)
    for r in rows:
        r['culled'] = ''
        r['culled_origin'] = ''
        r['favorite'] = favorites.get(r.get('filename', ''), '')
    _write_csv(db_path, fields, rows)


# ── Culling-pass history (file moves and their database snapshots) ────────────
#
# Every reject move is recorded as a pass in ``.kestrel/cull_history.json``,
# with a per-pass snapshot of the database and scenedata taken just before the
# move, so passes can be undone newest first. The canonical
# ``kestrel_database_old.csv`` slot is kept in step with the newest pass, so
# builds that only know the single-slot undo still restore the right state.
#
# Folders culled by older builds have a ``_KESTREL_Rejects`` folder but no
# history. Those files become a "legacy" pass at the bottom of the stack,
# backed by the ``_old`` slot (or, once a new pass has rotated that slot aside,
# by the newest ``OLD_precull_*`` archive).
#
# The bridge validates paths and lists the rejects folder; the file moves back
# and the ``_old`` slot handling stay bridge methods passed in as callables.

HISTORY_NAME = 'cull_history.json'
_PASS_DB_STEM = 'cullpass_{pid}_kestrel_database.csv'
_PASS_SD_STEM = 'cullpass_{pid}_kestrel_scenedata.json'
_OLD_DB_SLOT = 'kestrel_database_old.csv'
_OLD_SD_SLOT = 'kestrel_scenedata_old.json'
#: Upper bound on undoable passes kept per folder. Each pass keeps a full
#: database copy, so an endlessly re-culled folder must not grow forever.
#: Dropping the oldest pass only loses its *undo*; its files stay in
#: ``_KESTREL_Rejects``.
HISTORY_MAX_PASSES = 10


def read_history(kestrel_dir: str) -> list:
    """Return the recorded passes, oldest first. Unreadable → empty."""
    path = os.path.join(kestrel_dir, HISTORY_NAME)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        warn(f'[cull-history] unreadable history, ignoring: {e}')
        return []
    passes = data.get('passes') if isinstance(data, dict) else None
    if not isinstance(passes, list):
        return []
    clean = []
    for p in passes:
        if not isinstance(p, dict):
            continue
        pid = str(p.get('id') or '')
        if not _HISTORY_ID.fullmatch(pid):
            continue
        files = [str(f) for f in (p.get('files') or []) if isinstance(f, str)]
        clean.append({
            'id': pid,
            'created_at': str(p.get('created_at') or ''),
            'files': files,
            'legacy': bool(p.get('legacy')),
        })
    return clean


def write_history(kestrel_dir: str, passes: list) -> None:
    path = os.path.join(kestrel_dir, HISTORY_NAME)
    if not passes:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    write_json_atomic(path, {'version': 1, 'passes': passes}, indent=2)


def history_ids(kestrel_dir: str, has_rejects: Callable[[], bool]) -> list:
    """Pass ids oldest first; ``['legacy']`` for an older build's rejects."""
    ids = [p['id'] for p in read_history(kestrel_dir)]
    if not ids and has_rejects():
        ids = ['legacy']
    return ids


def _snapshot_paths(kestrel_dir: str, pid: str):
    return (
        os.path.join(kestrel_dir, _PASS_DB_STEM.format(pid=pid)),
        os.path.join(kestrel_dir, _PASS_SD_STEM.format(pid=pid)),
    )


def _remove_snapshots(kestrel_dir: str, pid: str) -> None:
    for p in _snapshot_paths(kestrel_dir, pid):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
        except OSError as e:
            warn(f'[cull-history] could not remove {os.path.basename(p)}: {e}')


def _pass_view(kestrel_dir: str, reject_names_cf: set, p: dict, number: int) -> dict:
    db_snap, _sd = _snapshot_paths(kestrel_dir, p['id'])
    restorable = [f for f in p['files'] if f.casefold() in reject_names_cf]
    return {
        'id': p['id'],
        'number': number,
        'created_at': p['created_at'],
        'legacy': p['legacy'],
        'file_count': len(p['files']),
        'restorable_count': len(restorable),
        'has_db_backup': os.path.isfile(db_snap),
    }


def _legacy_pass(kestrel_dir: str, legacy_files: list) -> dict:
    """The pass for rejects an older build moved, backed by its newest archive."""
    archives = sorted(
        n for n in os.listdir(kestrel_dir)
        if n.startswith('OLD_precull_kestrel_database_') and n.endswith('.csv')
    )
    ldb, lsd = _snapshot_paths(kestrel_dir, 'legacy')
    if archives:
        newest = archives[-1]
        copy_file_atomic(os.path.join(kestrel_dir, newest), ldb)
        sd_name = newest.replace('OLD_precull_kestrel_database_', 'OLD_precull_kestrel_scenedata_')[:-4] + '.json'
        if os.path.isfile(os.path.join(kestrel_dir, sd_name)):
            copy_file_atomic(os.path.join(kestrel_dir, sd_name), lsd)
    return {'id': 'legacy', 'created_at': '', 'files': legacy_files, 'legacy': True}


def record_pass(kestrel_dir: str, files: list, reject_names: Callable[[], list]) -> dict:
    """Record a reject move of ``files`` (already sanitized) as a new pass.

    The pass's undo point is the ``_old`` database/scenedata pair the bridge's
    ``backup_kestrel_db`` wrote just before the move; it is copied to a
    per-pass snapshot so a later pass cannot overwrite it. ``reject_names()``
    lists the primary images now in ``_KESTREL_Rejects``; it is only called
    for a folder's first recorded pass.
    """
    if not files:
        return {'success': True, 'recorded': False, 'reason': 'nothing moved'}
    passes = read_history(kestrel_dir)
    now = datetime.now(timezone.utc)

    # First pass recorded on a folder an older build already culled: keep
    # those earlier rejects undoable as a legacy pass underneath.
    if not passes:
        this_pass_cf = {f.casefold() for f in files}
        legacy_files = [n for n in reject_names() if n.casefold() not in this_pass_cf]
        if legacy_files:
            passes.append(_legacy_pass(kestrel_dir, legacy_files))

    pid = now.strftime('%Y%m%d_%H%M%S_%f')
    db_snap, sd_snap = _snapshot_paths(kestrel_dir, pid)
    csv_slot = os.path.join(kestrel_dir, _OLD_DB_SLOT)
    sd_slot = os.path.join(kestrel_dir, _OLD_SD_SLOT)
    has_backup = os.path.isfile(csv_slot)
    if has_backup:
        copy_file_atomic(csv_slot, db_snap)
        if os.path.isfile(sd_slot):
            copy_file_atomic(sd_slot, sd_snap)
    passes.append({'id': pid, 'created_at': now.isoformat(), 'files': files, 'legacy': False})

    dropped = 0
    while len(passes) > HISTORY_MAX_PASSES:
        old = passes.pop(0)
        _remove_snapshots(kestrel_dir, old['id'])
        dropped += 1
    write_history(kestrel_dir, passes)
    info(f'[cull-history] recorded pass {pid}: {len(files)} file(s), {len(passes)} pass(es) undoable')
    return {'success': True, 'recorded': True, 'pass_id': pid, 'pass_count': len(passes),
            'has_db_backup': has_backup, 'dropped_oldest': dropped}


def describe_history(kestrel_dir: str, reject_names: list) -> dict:
    """The undoable passes, newest last, with how many files each can restore."""
    reject_cf = {n.casefold() for n in reject_names}
    passes = read_history(kestrel_dir)
    views = [_pass_view(kestrel_dir, reject_cf, p, i + 1) for i, p in enumerate(passes)]
    if not passes and reject_names:
        views = [{
            'id': 'legacy', 'number': 1, 'created_at': '', 'legacy': True,
            'file_count': len(reject_names), 'restorable_count': len(reject_names),
            'has_db_backup': os.path.isfile(os.path.join(kestrel_dir, _OLD_DB_SLOT)),
        }]
    # Passes whose files are all gone can't be undone; skip past them so the
    # newest pass with something left is what Undo acts on.
    undoable = [v for v in views if v['restorable_count'] > 0]
    return {
        'success': True,
        'passes': views,
        'undoable_count': len(undoable),
        'next_undo': undoable[-1] if undoable else None,
        'reject_count': len(reject_names),
    }


MoveBackFn = Callable[[list], dict]
RotateSlotsFn = Callable[[list], object]


def undo_last_pass(kestrel_dir: str, reject_names: list, move_back: MoveBackFn,
                   restore_old_slot: UndoFn, rotate_slots: RotateSlotsFn) -> dict:
    """Undo the newest pass that still has files in ``_KESTREL_Rejects``.

    ``move_back(names)`` moves files (and sidecars) back out of the rejects
    folder; ``restore_old_slot()`` restores the single ``_old`` backup for a
    folder an older build culled; ``rotate_slots(pairs)`` archives the
    ``_old`` slot. Passes above the target whose files are all gone are
    dropped from the history first.
    """
    reject_cf = {n.casefold() for n in reject_names}
    passes = read_history(kestrel_dir)

    if not passes:
        # Older build's single move: same behaviour as that build.
        if not reject_names:
            return {'success': False, 'error': 'nothing to undo'}
        res = move_back(reject_names)
        if res.get('success'):
            rr = restore_old_slot()
            res['db_restored'] = bool(rr.get('success'))
            if not rr.get('success'):
                res['db_restore_error'] = rr.get('error', '')
        res['pass_id'] = 'legacy'
        res['remaining_passes'] = 0
        return res

    while passes and not any(f.casefold() in reject_cf for f in passes[-1]['files']):
        gone = passes.pop()
        _remove_snapshots(kestrel_dir, gone['id'])
    if not passes:
        write_history(kestrel_dir, passes)
        return {'success': False, 'error': 'nothing to undo (files already removed from _KESTREL_Rejects)'}

    target = passes[-1]
    restorable = [f for f in target['files'] if f.casefold() in reject_cf]
    res = move_back(restorable)
    if not res.get('success'):
        # Keep the pass so the user can retry after fixing conflicts.
        write_history(kestrel_dir, passes)
        res['pass_id'] = target['id']
        res['remaining_passes'] = len(passes)
        return res

    db_snap, sd_snap = _snapshot_paths(kestrel_dir, target['id'])
    db_restored = False
    if os.path.isfile(db_snap):
        copy_file_atomic(db_snap, os.path.join(kestrel_dir, DB_NAME))
        if os.path.isfile(sd_snap):
            copy_file_atomic(sd_snap, os.path.join(kestrel_dir, 'kestrel_scenedata.json'))
        db_restored = True

    passes.pop()
    _remove_snapshots(kestrel_dir, target['id'])
    write_history(kestrel_dir, passes)
    _point_old_slot_at(kestrel_dir, passes[-1]['id'] if passes else None, rotate_slots)

    res['pass_id'] = target['id']
    res['db_restored'] = db_restored
    res['remaining_passes'] = len(passes)
    info(f"[cull-history] undid pass {target['id']}: {res.get('restored', 0)} file(s), {len(passes)} pass(es) left")
    return res


def _point_old_slot_at(kestrel_dir: str, pid: str | None, rotate_slots: RotateSlotsFn) -> None:
    """Keep the single ``_old`` slot on the newest remaining pass's snapshot,
    so an older build's Undo restores the right state."""
    csv_slot = os.path.join(kestrel_dir, _OLD_DB_SLOT)
    sd_slot = os.path.join(kestrel_dir, _OLD_SD_SLOT)
    prev_db, prev_sd = _snapshot_paths(kestrel_dir, pid) if pid else (None, None)
    if prev_db and os.path.isfile(prev_db):
        copy_file_atomic(prev_db, csv_slot)
        if os.path.isfile(prev_sd):
            copy_file_atomic(prev_sd, sd_slot)
        elif os.path.isfile(sd_slot):
            os.remove(sd_slot)
    else:
        # No snapshot below (history exhausted, or a legacy pass whose backup
        # was never captured): move the slot aside rather than leave the
        # just-undone pass's backup where Undo would find it.
        rotate_slots([
            (csv_slot, 'precull_kestrel_database', '.csv'),
            (sd_slot, 'precull_kestrel_scenedata', '.json'),
        ])
