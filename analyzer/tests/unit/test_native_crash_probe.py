"""Unit tests for the Windows event-log probe of a prior session's death.

Crash reports for native deaths that bypass faulthandler (fail-fast, heap
corruption, a hung window the user ended) carry a runtime log that just stops.
``native_crash_probe`` recovers the cause from the Application event log on the
next launch. These tests pin the parsing against both EventData layouts, the
pid/app matching, the no-paths guarantee, and the failsafe paths.
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import native_crash_probe as ncp  # noqa: E402

pytestmark = pytest.mark.unit

PID = 4096  # 0x1000
STARTED = datetime(2026, 1, 15, 9, 0, 0)


def _event(event_id, data, time='2026-01-15T09:10:00.0000000Z', named=False):
    if named:
        body = ''.join(f"<Data Name='{k}'>{v}</Data>" for k, v in data)
    else:
        body = ''.join(f'<Data>{v}</Data>' for v in data)
    provider = 'Application Error' if event_id == 1000 else 'Application Hang'
    return (
        "<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'>"
        f"<System><Provider Name='{provider}'/><EventID>{event_id}</EventID>"
        f"<TimeCreated SystemTime='{time}'/></System>"
        f"<EventData>{body}</EventData></Event>"
    )


def _wrap(*events):
    return '<Events>' + ''.join(events) + '</Events>'


APP_ERROR_UNNAMED = [
    'ProjectKestrel.exe', '6.3.0.0', '68b5c1a2', 'DirectML.dll', '1.15.4.0',
    'aa11bb22', 'c0000409', '00000000000a1b2c', '0x1000', '0x01dd3a2b4c5d6e7f',
    r'C:\Users\someone\AppData\Local\Programs\ProjectKestrel\ProjectKestrel.exe',
    r'C:\Users\someone\AppData\Local\Programs\ProjectKestrel\_internal\DirectML.dll',
    'f0e1d2c3-0000-0000-0000-000000000000', '', '',
]


def test_unnamed_app_error_for_prior_pid_is_reported_without_paths():
    xml = _wrap(_event(1000, APP_ERROR_UNNAMED))
    events = ncp.parse_events(xml, PID, 'ProjectKestrel.exe')
    assert len(events) == 1
    line = ncp.describe(events[0])
    assert 'exception=0xc0000409' in line
    assert 'fail-fast' in line
    assert 'module=DirectML.dll' in line
    assert 'module_version=1.15.4.0' in line
    assert 'offset=00000000000a1b2c' in line
    assert 'someone' not in line and 'Users' not in line
    assert 'someone' not in repr(events[0])


def test_named_app_error_layout_is_preferred():
    data = [
        ('AppName', 'ProjectKestrel.exe'), ('AppVersion', '6.3.0.0'),
        ('ModuleName', 'onnxruntime.dll'), ('ModuleVersion', '1.20.1.0'),
        ('ExceptionCode', 'c0000005'), ('FaultingOffset', '0000000000123456'),
        ('ProcessId', '0x1000'), ('AppPath', r'C:\Users\someone\x.exe'),
    ]
    events = ncp.parse_events(_wrap(_event(1000, data, named=True)), PID, 'projectkestrel.exe')
    assert len(events) == 1
    assert 'exception=0xc0000005 (access violation)' in ncp.describe(events[0])
    assert 'AppPath' not in events[0]


def test_other_pid_or_other_app_is_ignored():
    other_pid = list(APP_ERROR_UNNAMED)
    other_pid[8] = '0x1001'
    other_app = list(APP_ERROR_UNNAMED)
    other_app[0] = 'chrome.exe'
    xml = _wrap(_event(1000, other_pid), _event(1000, other_app))
    assert ncp.parse_events(xml, PID, 'ProjectKestrel.exe') == []


def test_application_hang_for_prior_pid_is_reported():
    data = ['ProjectKestrel.exe', '6.3.0.0', '1000', '01dd3a2b4c5d6e7f', '4294967295',
            r'C:\Users\someone\ProjectKestrel.exe', 'guid', '', '', 'Unknown']
    events = ncp.parse_events(_wrap(_event(1002, data)), PID, 'ProjectKestrel.exe')
    assert len(events) == 1
    assert ncp.describe(events[0]).startswith('hang:')


def test_malformed_xml_returns_nothing():
    assert ncp.parse_events('<Events><Event>', PID, 'ProjectKestrel.exe') == []


def test_query_filters_providers_ids_and_time():
    q = ncp.build_query(STARTED)
    assert "@Name='Application Error'" in q and "@Name='Application Hang'" in q
    assert 'EventID=1000' in q and 'EventID=1002' in q
    assert "@SystemTime>='2026-01-15T09:00:00.000Z'" in q


class _Runner:
    def __init__(self, stdout='', returncode=0, exc=None):
        self.stdout, self.returncode, self.exc, self.calls = stdout, returncode, exc, []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, '')


def test_probe_reports_matching_crash():
    runner = _Runner(_wrap(_event(1000, APP_ERROR_UNNAMED)))
    lines = ncp.probe_prior_session(PID, STARTED, 'ProjectKestrel.exe', runner=runner, platform='win32')
    assert len(lines) == 1 and lines[0].startswith('crash: exception=0xc0000409')
    args, kwargs = runner.calls[0]
    assert args[:3] == ['wevtutil', 'qe', 'Application']
    # One minute of slack before the recorded session start.
    assert "@SystemTime>='2026-01-15T08:59:00.000Z'" in args[3]
    assert kwargs['timeout'] > 0 and kwargs['creationflags'] == 0x08000000


def test_probe_says_so_when_windows_recorded_nothing():
    lines = ncp.probe_prior_session(PID, STARTED, 'ProjectKestrel.exe',
                                    runner=_Runner(_wrap()), platform='win32')
    assert len(lines) == 1 and lines[0].startswith('none:')


@pytest.mark.parametrize('runner,expected', [
    (_Runner(returncode=5), 'event log query failed: wevtutil exit 5'),
    (_Runner(exc=subprocess.TimeoutExpired('wevtutil', 15)), 'event log query failed: TimeoutExpired'),
    (_Runner(exc=FileNotFoundError()), 'event log query failed: FileNotFoundError'),
])
def test_probe_never_raises(runner, expected):
    assert ncp.probe_prior_session(PID, STARTED, 'x.exe', runner=runner, platform='win32') == [expected]


@pytest.mark.parametrize('platform,pid,started', [
    ('darwin', PID, STARTED), ('linux', PID, STARTED), ('win32', 0, STARTED), ('win32', PID, None),
])
def test_probe_skips_when_not_applicable(platform, pid, started):
    runner = _Runner()
    assert ncp.probe_prior_session(pid, started, 'x.exe', runner=runner, platform=platform) == []
    assert runner.calls == []
