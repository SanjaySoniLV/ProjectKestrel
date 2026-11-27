"""Cull mode passes: the per-folder session file and Revert / Reset.

Cull mode records its progress in ``.kestrel/cull_session.json``: the pass in
progress (its starting picks, per-scene cuts and confirmed scenes) and, for
every finished pass, the decision each photo ended with and what that pass
moved into ``_KESTREL_Rejects``. The photo decisions themselves stay where the
rest of the app reads them, in the ``culled`` / ``culled_origin`` columns of
``kestrel_database.csv``; this file only adds what those columns cannot hold.

File moves are undone through the culling-pass history the bridge already
keeps (``.kestrel/cull_history.json``), which restores the database snapshot
taken just before each move. Those snapshots are older than the user's latest
edits, so after any undo the user columns are written back on top:

- Undo move keeps every decision and favorite the user has now.
- Revert to pass N sets decisions to how pass N ended.
- Reset clears every decision.

Favorites are never cleared by any of the three.

Nothing here imports UI or bridge code; the bridge passes in the two history
callables it owns.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from typing import Callable, Iterable

from kestrel_analyzer.database import write_json_atomic, write_text_atomic
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
