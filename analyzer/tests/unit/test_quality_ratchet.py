"""The quality ratchet fails when a count grows and passes when it holds or drops."""
import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[3] / "tools" / "quality_ratchet.py"
_spec = importlib.util.spec_from_file_location("quality_ratchet", _SCRIPT)
ratchet = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ratchet)


def _base(**over):
    b = {"big_file_lines": {"analyzer/big.py": 2000}, "js_empty_catch": 10,
         "py_long_functions": 5, "tombstone_comments": 3}
    b.update(over)
    return b


def test_equal_counts_pass():
    assert ratchet.compare(_base(), _base()) == []


def test_lower_counts_pass():
    now = _base(js_empty_catch=9, big_file_lines={"analyzer/big.py": 1900})
    assert ratchet.compare(_base(), now) == []


def test_count_that_grows_fails():
    problems = ratchet.compare(_base(), _base(tombstone_comments=4))
    assert len(problems) == 1 and "tombstone_comments" in problems[0]


def test_big_file_that_grows_fails():
    problems = ratchet.compare(_base(), _base(big_file_lines={"analyzer/big.py": 2001}))
    assert len(problems) == 1 and "analyzer/big.py" in problems[0]


def test_new_big_file_fails():
    now = _base(big_file_lines={"analyzer/big.py": 2000, "analyzer/new.py": 1600})
    problems = ratchet.compare(_base(), now)
    assert len(problems) == 1 and "analyzer/new.py" in problems[0]


def test_long_functions_are_report_only():
    assert ratchet.compare(_base(), _base(py_long_functions=50)) == []



def test_inline_style_pattern_counts_attributes_only():
    html = '<div style="x"></div><p STYLE=\'y\'></p><style>.a{}</style><div data-style="z"></div>'
    assert len(ratchet.INLINE_STYLE.findall(html)) == 2


def test_new_inline_style_fails():
    problems = ratchet.compare(_base(html_inline_styles=0), _base(html_inline_styles=1))
    assert len(problems) == 1 and "html_inline_styles" in problems[0]
