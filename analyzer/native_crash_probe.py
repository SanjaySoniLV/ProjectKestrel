"""Find out how the previous session's process died, from the Windows event log.

``faulthandler`` (enabled in ``visualizer.main``) writes a Python traceback to
the runtime log when a native library raises an access violation or similar.
Several ways a Windows process dies never reach it, though:

* ``__fastfail`` (``0xC0000409``), which the release C runtime's ``abort()``,
  ``std::terminate`` and /GS stack-cookie checks all end in;
* heap corruption (``0xC0000374``), raised by the heap manager;
* a hung window that the user closes from the "Not responding" prompt.

Each of these leaves the runtime log ending mid-run with no traceback, which is
what the unexplained hard-death crash reports look like. Windows Error Reporting
still records them in the Application event log: event 1000 ("Application
Error") with the faulting module and exception code, or event 1002
("Application Hang"). When the next launch flags the prior session as unclean,
:func:`probe_prior_session` looks for such an event belonging to the prior
session's pid and returns lines for the runtime log, which the crash bundle
already includes.

Windows only, best-effort, never raises. Only basenames are reported — the
event's full application and module paths contain the user's home folder.
"""

from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

_EVENT_NS = '{http://schemas.microsoft.com/win/2004/08/events/event}'

# Positional layout of event 1000's EventData. Windows 10 emits unnamed
# <Data> elements in this order; newer builds name them, which is preferred.
_APP_ERROR_FIELDS = (
    'AppName', 'AppVersion', 'AppTimeStamp', 'ModuleName', 'ModuleVersion',
    'ModuleTimeStamp', 'ExceptionCode', 'FaultingOffset', 'ProcessId',
)

_EXCEPTION_NAMES = {
    'c0000005': 'access violation',
    'c0000409': 'fail-fast / stack buffer overrun (abort, std::terminate)',
    'c0000374': 'heap corruption',
    'c00000fd': 'stack overflow',
    'c0000417': 'invalid CRT parameter',
    'c000001d': 'illegal instruction',
    'e06d7363': 'unhandled C++ exception',
    '80000003': 'breakpoint',
}

# The prior session can be days old; the event log rolls over well before
# that matters, and a wide window costs nothing because the pid must match.
_QUERY_MAX_EVENTS = 50
_QUERY_TIMEOUT_S = 15
_CREATE_NO_WINDOW = 0x08000000

Runner = Callable[..., 'subprocess.CompletedProcess']


def build_query(since_utc: datetime) -> str:
    """XPath for Application Error / Application Hang events since ``since_utc``."""
    stamp = since_utc.strftime('%Y-%m-%dT%H:%M:%S.000Z')
    return (
        "*[System[Provider[@Name='Application Error' or @Name='Application Hang']"
        " and (EventID=1000 or EventID=1002)"
        f" and TimeCreated[@SystemTime>='{stamp}']]]"
    )


def _pid_matches(value: str, pid: int) -> bool:
    text = (value or '').strip().lower()
    if not text:
        return False
    if text.startswith('0x'):
        text = text[2:]
    try:
        # Both events record the pid in hex (1000 with a 0x prefix, 1002 without).
        return int(text, 16) == pid
    except ValueError:
        return False


def parse_events(xml_text: str, prev_pid: int, app_name: str) -> List[Dict[str, str]]:
    """Return the events in ``xml_text`` that belong to ``prev_pid`` of ``app_name``.

    ``xml_text`` is ``wevtutil qe ... /f:xml /e:Events`` output. Each result
    holds ``kind`` ('crash' or 'hang'), ``time`` and the event's data fields.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    want_app = (app_name or '').strip().lower()
    out: List[Dict[str, str]] = []
    for event in root.iter(f'{_EVENT_NS}Event'):
        system = event.find(f'{_EVENT_NS}System')
        data_els = event.findall(f'{_EVENT_NS}EventData/{_EVENT_NS}Data')
        if system is None or not data_els:
            continue
        event_id = (system.findtext(f'{_EVENT_NS}EventID') or '').strip()
        created = system.find(f'{_EVENT_NS}TimeCreated')
        values = [(d.text or '').strip() for d in data_els]
        named = {d.get('Name'): (d.text or '').strip() for d in data_els if d.get('Name')}

        if event_id == '1000':
            fields = named or dict(zip(_APP_ERROR_FIELDS, values))
            if not _pid_matches(fields.get('ProcessId', ''), prev_pid):
                continue
            kind = 'crash'
        elif event_id == '1002':
            fields = named or {'AppName': values[0] if values else ''}
            # Unnamed layout: AppName, AppVersion, ProcessID, StartTime, ...
            # Scanning a few slots tolerates layout drift; the neighbours are a
            # dotted version and a 64-bit FILETIME, neither of which parses to
            # a plausible pid.
            pid_values = [named[k] for k in ('ProcessID', 'ProcessId') if k in named] or values[1:4]
            if not any(_pid_matches(v, prev_pid) for v in pid_values):
                continue
            kind = 'hang'
        else:
            continue
        if want_app and fields.get('AppName', '').strip().lower() != want_app:
            continue
        fields = {k: v for k, v in fields.items() if k in _APP_ERROR_FIELDS or k == 'HangType'}
        fields['kind'] = kind
        fields['time'] = created.get('SystemTime', '') if created is not None else ''
        out.append(fields)
    return out


def describe(event: Dict[str, str]) -> str:
    """One log line for an event. Basenames only; no paths."""
    if event.get('kind') == 'hang':
        return (
            f"hang: Windows recorded the window as not responding and the process was "
            f"ended (Application Hang) time={event.get('time', '')}"
            + (f" hang_type={event['HangType']}" if event.get('HangType') else '')
        )
    code = (event.get('ExceptionCode') or '').lower()
    if code.startswith('0x'):
        code = code[2:]
    return (
        f"crash: exception=0x{code} ({_EXCEPTION_NAMES.get(code, 'unrecognised code')}) "
        f"module={os.path.basename(event.get('ModuleName', '') or '?')} "
        f"module_version={event.get('ModuleVersion', '') or '?'} "
        f"offset={event.get('FaultingOffset', '') or '?'} "
        f"app_version={event.get('AppVersion', '') or '?'} "
        f"time={event.get('time', '')}"
    )


def probe_prior_session(
    prev_pid: int,
    prev_started: Optional[datetime],
    app_name: Optional[str] = None,
    *,
    runner: Optional[Runner] = None,
    platform: Optional[str] = None,
) -> List[str]:
    """Return log lines describing how ``prev_pid`` died, or why that is unknown.

    ``prev_started`` is the prior session's naive-UTC start time. Returns an
    empty list off Windows or when there is nothing to look up.
    """
    if (platform or sys.platform) != 'win32' or not prev_pid or prev_started is None:
        return []
    app = app_name if app_name is not None else os.path.basename(sys.executable)
    run = runner or subprocess.run
    # A minute of slack absorbs the gap between process start and the
    # session-start stamp being written.
    since = prev_started - timedelta(minutes=1)
    try:
        proc = run(
            [
                'wevtutil', 'qe', 'Application',
                f'/q:{build_query(since)}',
                '/f:xml', '/e:Events', '/rd:true', f'/c:{_QUERY_MAX_EVENTS}',
            ],
            capture_output=True,
            text=True,
            timeout=_QUERY_TIMEOUT_S,
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception as exc:
        return [f'event log query failed: {type(exc).__name__}']
    if proc.returncode != 0:
        return [f'event log query failed: wevtutil exit {proc.returncode}']
    events = parse_events(proc.stdout or '', int(prev_pid), app)
    if not events:
        return [
            'none: no Application Error or Application Hang event for the prior '
            'process. It was not a native crash Windows recorded; likely ended '
            'externally (Task Manager, power loss, forced sign-out) or by a crash '
            'faulthandler would have logged.'
        ]
    return [describe(e) for e in events]


__all__ = ['build_query', 'describe', 'parse_events', 'probe_prior_session']
