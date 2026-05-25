"""MCP server for interactive Project Kestrel UI automation.

Exposes tools that let an AI agent drive the real Kestrel desktop UI
through a Playwright bridge: take screenshots, click elements, type text,
evaluate JS, and monitor analysis progress — all interactively.

Start with:
    xvfb-run -a python3 perch_ui_harness/mcp_server.py [--folder PATH]
"""

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path

_ANALYZER_DIR = str(Path(__file__).resolve().parent.parent / 'analyzer')
if _ANALYZER_DIR not in sys.path:
    sys.path.insert(0, _ANALYZER_DIR)

from mcp.server.fastmcp import FastMCP

from bridge import KestrelUIBridge

mcp = FastMCP("perch-ui")

_bridge: KestrelUIBridge | None = None
_bridge_lock = asyncio.Lock()
_screenshots_dir = os.environ.get(
    'PERCH_SCREENSHOTS_DIR', '/tmp/kestrel_screenshots')
_default_folder = os.environ.get(
    'PERCH_TEST_FOLDER',
    str(Path(__file__).resolve().parent.parent / 'test_imgs'))


async def _ensure_bridge() -> KestrelUIBridge:
    """Lazily start the bridge on first tool call."""
    global _bridge
    async with _bridge_lock:
        if _bridge is None:
            _bridge = KestrelUIBridge(
                folder_override=_default_folder,
                screenshots_dir=_screenshots_dir,
            )
            await _bridge.start()
            await _bridge.dismiss_overlays()
        return _bridge


def _error_context(e: Exception) -> dict:
    """Build a structured error response with diagnostics."""
    return {
        'success': False,
        'error': str(e),
        'type': type(e).__name__,
        'hint': _get_hint(e),
    }


def _get_hint(e: Exception) -> str:
    msg = str(e).lower()
    if 'timeout' in msg:
        return ('Element not found within timeout. Try: '
                '1) Take a screenshot to see current state. '
                '2) Check if a dialog/overlay is blocking. '
                '3) Use a different selector.')
    if 'intercepts pointer' in msg:
        return ('An overlay is blocking clicks. Try: '
                '1) ui_dismiss_overlays() to clear known overlays. '
                '2) Take a screenshot to identify what\'s blocking. '
                '3) Use ui_evaluate to remove the overlay via JS.')
    if 'not attached' in msg or 'detached' in msg:
        return 'Element was removed from DOM. Take a screenshot and retry.'
    return ''


# ── Response helpers ───────────────────────────────────────────────────

async def _get_element_info(bridge, selector):
    """Get details about a DOM element by CSS selector."""
    return await bridge.evaluate(f"""
        (() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return null;
            return {{
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
                text: (el.textContent || el.value || '').trim().slice(0, 100),
                classes: (typeof el.className === 'string'
                          ? el.className : '') || '',
                type: el.type || null,
            }};
        }})()
    """)


def _describe_element(info, action='Clicked'):
    """Build a human-readable description from element info."""
    if not info:
        return f'{action} element'
    tag = info.get('tag', '?')
    eid = info.get('id')
    text = info.get('text', '')
    parts = [action, tag]
    if eid:
        parts.append(f"'#{eid}'")
    if text:
        display = text[:60] + ('...' if len(text) > 60 else '')
        parts.append(f'with text "{display}"')
    return ' '.join(parts)


async def _find_elements_by_text(bridge, text):
    """Find visible interactive elements whose text contains the query."""
    return await bridge.evaluate(f"""
        (() => {{
            const searchText = {json.dumps(text)}.toLowerCase();
            const matches = [];
            const candidates = document.querySelectorAll(
                'button, a, [role="button"], input[type="submit"], '
                + 'input[type="button"], [onclick], label, .btn');

            function getSelector(el) {{
                if (el.id) return '#' + CSS.escape(el.id);
                let path = el.tagName.toLowerCase();
                if (el.className && typeof el.className === 'string') {{
                    const cls = el.className.trim().split(/\\s+/)
                        .map(c => CSS.escape(c)).join('.');
                    if (cls) path += '.' + cls;
                }}
                if (document.querySelectorAll(path).length === 1) return path;
                const parent = el.parentElement;
                if (parent) {{
                    const siblings = Array.from(parent.children);
                    const index = siblings.indexOf(el) + 1;
                    path += ':nth-child(' + index + ')';
                }}
                return path;
            }}

            candidates.forEach(el => {{
                const content = (el.textContent || el.value || '').trim();
                if (content.toLowerCase().includes(searchText)
                    && el.offsetParent !== null) {{
                    matches.push({{
                        index: matches.length,
                        tag: el.tagName.toLowerCase(),
                        id: el.id || null,
                        text: content.slice(0, 100),
                        classes: (typeof el.className === 'string'
                                  ? el.className : '') || '',
                        selector: getSelector(el),
                        disabled: el.disabled || false,
                    }});
                }}
            }});
            return matches;
        }})()
    """)


async def _post_interaction(bridge, description, label):
    """Standard post-interaction: wait 1s, screenshot, return response."""
    await asyncio.sleep(1)
    path = await bridge.screenshot(label)
    return {
        'success': True,
        'action': description,
        'screenshot': path,
    }


# ── Core interaction tools ─────────────────────────────────────────────

@mcp.tool()
async def ui_start(folder_path: str = '') -> dict:
    """Start or restart the Kestrel app UI.

    Args:
        folder_path: Default folder for dialog intercepts (defaults to test_imgs)

    Returns screenshot after startup. Legal/tutorial overlays are
    dismissed automatically.
    """
    global _bridge
    try:
        async with _bridge_lock:
            if _bridge is not None:
                await _bridge.stop()
                _bridge = None
        if folder_path:
            os.environ['PERCH_TEST_FOLDER'] = folder_path

        bridge = await _ensure_bridge()
        path = await bridge.screenshot('started')
        return {
            'success': True,
            'action': 'Kestrel UI started and ready',
            'screenshot': path,
        }
    except Exception as e:
        return _error_context(e)


@mcp.tool()
async def ui_screenshot(label: str = '') -> dict:
    """Take a screenshot of the current UI state.

    Args:
        label: Optional label for the screenshot filename

    Returns the screenshot file path.
    """
    try:
        bridge = await _ensure_bridge()
        path = await bridge.screenshot(label or 'capture')
        return {
            'success': True,
            'action': 'Screenshot captured',
            'screenshot': path,
        }
    except Exception as e:
        return _error_context(e)


@mcp.tool()
async def ui_click(
    selector: str = '',
    text: str = '',
    timeout: int = 10000,
) -> dict:
    """Click a UI element by CSS selector or visible text.

    Args:
        selector: CSS selector (e.g. '#analyzeQueueBtn', '.primary')
        text: Visible button/link text to click (e.g. 'Analyze Folders')
        timeout: Max wait time in ms (default 10000)

    Provide either selector OR text, not both.

    When using text: if multiple elements match, returns a disambiguation
    list with selectors instead of clicking. Re-call with the desired
    selector. If exactly one element is an exact text match among multiple
    partial matches, it clicks that one automatically.

    Returns: action description of exactly what was clicked + screenshot.
    """
    if not selector and not text:
        return {'success': False, 'error': 'Provide selector or text'}
    try:
        bridge = await _ensure_bridge()

        if text:
            matches = await _find_elements_by_text(bridge, text)

            if len(matches) == 0:
                err_path = await bridge.screenshot('click_no_match')
                return {
                    'success': False,
                    'error': (f'No visible interactive elements found '
                              f'matching "{text}"'),
                    'screenshot': err_path,
                    'hint': ('Use ui_get_elements("button") or '
                             'ui_screenshot to see what\'s on screen.'),
                }

            if len(matches) > 1:
                exact = [m for m in matches
                         if m['text'].strip().lower() == text.strip().lower()]
                if len(exact) == 1:
                    match = exact[0]
                else:
                    return {
                        'success': False,
                        'needs_disambiguation': True,
                        'message': (
                            f'Found {len(matches)} elements matching '
                            f'"{text}". Call ui_click(selector=...) '
                            f'with one of these selectors:'),
                        'candidates': matches,
                    }
            else:
                match = matches[0]

            target_selector = match['selector']
            await bridge.page.click(target_selector, timeout=timeout)
            description = _describe_element(match, 'Clicked')
            return await _post_interaction(
                bridge, description, f'click_{text[:30]}')

        else:
            info = await _get_element_info(bridge, selector)
            await bridge.page.click(selector, timeout=timeout)
            description = _describe_element(info, 'Clicked')
            label = selector.replace('#', '').replace('.', '')[:30]
            return await _post_interaction(
                bridge, description, f'click_{label}')

    except Exception as e:
        try:
            bridge = await _ensure_bridge()
            err_path = await bridge.screenshot('click_failed')
        except Exception:
            err_path = None
        result = _error_context(e)
        result['screenshot'] = err_path
        return result


@mcp.tool()
async def ui_type(selector: str, value: str) -> dict:
    """Type text into an input field.

    Args:
        selector: CSS selector for the input (e.g. '#adlgDetectionThreshold')
        value: Text to type

    Returns action description and screenshot after typing.
    """
    try:
        bridge = await _ensure_bridge()
        info = await _get_element_info(bridge, selector)
        await bridge.fill(selector, value)
        desc = _describe_element(info, f'Typed "{value}" into')
        return await _post_interaction(bridge, desc, 'typed')
    except Exception as e:
        return _error_context(e)


@mcp.tool()
async def ui_hover(selector: str) -> dict:
    """Hover over an element (useful for tooltips, dropdowns).

    Args:
        selector: CSS selector to hover over

    Returns action description and screenshot after hover.
    """
    try:
        bridge = await _ensure_bridge()
        info = await _get_element_info(bridge, selector)
        await bridge.page.hover(selector)
        desc = _describe_element(info, 'Hovered over')
        return await _post_interaction(bridge, desc, 'hover')
    except Exception as e:
        return _error_context(e)


@mcp.tool()
async def ui_select(selector: str, value: str) -> dict:
    """Select an option from a <select> dropdown.

    Args:
        selector: CSS selector for the <select> element
        value: Option value to select

    Returns action description and screenshot after selection.
    """
    try:
        bridge = await _ensure_bridge()
        info = await _get_element_info(bridge, selector)
        await bridge.page.select_option(selector, value)
        desc = _describe_element(info, f'Selected "{value}" in')
        return await _post_interaction(bridge, desc, 'selected')
    except Exception as e:
        return _error_context(e)


@mcp.tool()
async def ui_checkbox(selector: str, checked: bool = True) -> dict:
    """Set a checkbox state.

    Args:
        selector: CSS selector for the checkbox
        checked: True to check, False to uncheck

    Returns action description and screenshot after toggling.
    """
    try:
        bridge = await _ensure_bridge()
        info = await _get_element_info(bridge, selector)
        if checked:
            await bridge.page.check(selector)
        else:
            await bridge.page.uncheck(selector)
        action = 'Checked' if checked else 'Unchecked'
        desc = _describe_element(info, action)
        return await _post_interaction(bridge, desc, 'checkbox')
    except Exception as e:
        return _error_context(e)


# ── Page inspection tools ──────────────────────────────────────────────

@mcp.tool()
async def ui_get_text(selector: str) -> dict:
    """Get the text content of an element.

    Args:
        selector: CSS selector (e.g. '#status', '.queue-item-status')
    """
    try:
        bridge = await _ensure_bridge()
        text = await bridge.get_text(selector)
        return {'success': True, 'text': text}
    except Exception as e:
        return _error_context(e)


@mcp.tool()
async def ui_get_elements(selector: str) -> dict:
    """List elements matching a selector with their text and attributes.

    Args:
        selector: CSS selector (e.g. 'button', '.toolbar button', 'dialog[open]')

    Returns a list of elements with tag, id, text, classes, and visibility.
    Useful for discovering what's on screen.
    """
    try:
        bridge = await _ensure_bridge()
        elements = await bridge.evaluate(f"""
            (() => {{
                const els = document.querySelectorAll({json.dumps(selector)});
                return Array.from(els).slice(0, 50).map(el => ({{
                    tag: el.tagName.toLowerCase(),
                    id: el.id || null,
                    text: (el.textContent || '').trim().slice(0, 100),
                    classes: el.className || '',
                    visible: el.offsetParent !== null
                             || el.style.display !== 'none',
                    disabled: el.disabled || false,
                    checked: el.checked || false,
                    type: el.type || null,
                    value: el.value || null,
                    href: el.href || null,
                }}));
            }})()
        """)
        return {'success': True, 'count': len(elements), 'elements': elements}
    except Exception as e:
        return _error_context(e)


@mcp.tool()
async def ui_evaluate(js_code: str) -> dict:
    """Execute arbitrary JavaScript in the page and return the result.

    Args:
        js_code: JavaScript expression or IIFE to evaluate

    Use for advanced interactions: reading state, triggering events,
    removing overlays, inspecting the DOM, etc.
    """
    try:
        bridge = await _ensure_bridge()
        result = await bridge.evaluate(js_code)
        return {'success': True, 'result': result}
    except Exception as e:
        return _error_context(e)


@mcp.tool()
async def ui_wait_for(
    selector: str = '',
    text: str = '',
    state: str = 'visible',
    timeout: int = 30000,
) -> dict:
    """Wait for an element or condition.

    Args:
        selector: CSS selector to wait for
        text: Wait for this text to appear anywhere on the page
        state: 'visible', 'hidden', 'attached', or 'detached'
        timeout: Max wait time in ms
    """
    try:
        bridge = await _ensure_bridge()
        if selector:
            await bridge.page.wait_for_selector(
                selector, state=state, timeout=timeout)
            desc = f'Element "{selector}" is now {state}'
        elif text:
            await bridge.page.wait_for_function(
                f'document.body.innerText.includes({json.dumps(text)})',
                timeout=timeout)
            desc = f'Text "{text}" appeared on page'
        else:
            return {'success': False, 'error': 'Provide selector or text'}
        path = await bridge.screenshot('wait_done')
        return {'success': True, 'action': desc, 'screenshot': path}
    except Exception as e:
        return _error_context(e)


# ── Dialog & overlay management ────────────────────────────────────────

@mcp.tool()
async def ui_dismiss_overlays() -> dict:
    """Dismiss any known overlay: legal banner, tutorial, feedback consent.

    Call this when clicks are being blocked by overlays.
    Returns screenshot after dismissal.
    """
    try:
        bridge = await _ensure_bridge()
        await bridge.dismiss_overlays()
        await bridge.dismiss_feedback_consent()
        return await _post_interaction(
            bridge, 'Dismissed overlays (legal/tutorial/consent)',
            'overlays_dismissed')
    except Exception as e:
        return _error_context(e)


@mcp.tool()
async def ui_set_folder(folder_path: str) -> dict:
    """Set the folder path returned when the app opens a folder picker dialog.

    Args:
        folder_path: Absolute path to the folder

    The next time JS calls choose_directory() or choose_directories(),
    this path will be returned instead of opening a native OS dialog.
    """
    try:
        bridge = await _ensure_bridge()
        await bridge.set_folder_override(folder_path)
        return {
            'success': True,
            'action': f'Folder override set to {folder_path}',
        }
    except Exception as e:
        return _error_context(e)


# ── Analysis queue tools ───────────────────────────────────────────────

@mcp.tool()
async def ui_get_queue_status() -> dict:
    """Get the current analysis queue status from the Python backend.

    Returns running state, per-folder progress (processed/total),
    current filename, elapsed time, errors, etc.
    """
    try:
        bridge = await _ensure_bridge()
        info = bridge.get_queue_info()
        return {'success': True, **info}
    except Exception as e:
        return _error_context(e)


@mcp.tool()
async def ui_wait_for_analysis(
    target_processed: int = 0,
    timeout: int = 600,
    poll_interval: int = 5,
) -> dict:
    """Wait for the analysis queue to reach a target or complete.

    Args:
        target_processed: Stop when this many images are processed (0 = wait for completion)
        timeout: Max wait time in seconds
        poll_interval: Seconds between status checks

    Takes a screenshot when the target is reached.
    """
    try:
        bridge = await _ensure_bridge()
        start = asyncio.get_event_loop().time()

        while (asyncio.get_event_loop().time() - start) < timeout:
            info = bridge.get_queue_info()
            items = info.get('items', [])
            total_processed = sum(i.get('processed', 0) for i in items)
            total_total = sum(i.get('total', 0) for i in items)
            running = info.get('running', False)

            if target_processed > 0 and total_processed >= target_processed:
                path = await bridge.screenshot(
                    f'reached_{total_processed}_of_{total_total}')
                return {
                    'success': True,
                    'action': (f'Reached {total_processed}/{total_total} '
                               'processed'),
                    'processed': total_processed,
                    'total': total_total,
                    'screenshot': path,
                }

            if target_processed == 0 and not running and total_total > 0:
                path = await bridge.screenshot('analysis_complete')
                return {
                    'success': True,
                    'action': (f'Analysis complete: '
                               f'{total_processed}/{total_total}'),
                    'processed': total_processed,
                    'total': total_total,
                    'screenshot': path,
                }

            await bridge.dismiss_feedback_consent()
            await asyncio.sleep(poll_interval)

        path = await bridge.screenshot('timeout')
        info = bridge.get_queue_info()
        items = info.get('items', [])
        return {
            'success': False,
            'error': 'Timeout waiting for analysis',
            'processed': sum(i.get('processed', 0) for i in items),
            'total': sum(i.get('total', 0) for i in items),
            'screenshot': path,
        }
    except Exception as e:
        return _error_context(e)


# ── Utility tools ──────────────────────────────────────────────────────

@mcp.tool()
async def ui_wait_and_screenshot(
    seconds: int = 5,
    label: str = '',
) -> dict:
    """Wait a specified number of seconds, then take a screenshot.

    Args:
        seconds: Seconds to wait (1-120, default 5)
        label: Optional label for the screenshot filename

    Use after triggering async operations (analysis start, loading,
    animations) to let the UI settle before capturing state.
    """
    try:
        bridge = await _ensure_bridge()
        seconds = max(1, min(120, seconds))
        await asyncio.sleep(seconds)
        path = await bridge.screenshot(label or f'wait_{seconds}s')
        return {
            'success': True,
            'action': f'Waited {seconds}s',
            'screenshot': path,
        }
    except Exception as e:
        return _error_context(e)


@mcp.tool()
async def ui_get_visible_dialogs() -> dict:
    """List all currently open/visible dialogs and overlays.

    Useful for understanding what's blocking interaction.
    """
    try:
        bridge = await _ensure_bridge()
        dialogs = await bridge.evaluate("""
            (() => {
                const results = [];
                document.querySelectorAll('dialog').forEach(d => {
                    if (d.open) results.push({
                        type: 'dialog', id: d.id,
                        text: (d.textContent || '').trim().slice(0, 200)
                    });
                });
                ['tutorialOverlay', 'legalNotice', 'analyticsConsent',
                 'feedbackConsentDlg'].forEach(id => {
                    const el = document.getElementById(id);
                    if (el && (el.classList.contains('active') ||
                               el.classList.contains('visible') ||
                               !el.classList.contains('hidden'))) {
                        results.push({
                            type: 'overlay', id,
                            classes: el.className,
                            text: (el.textContent || '').trim().slice(0, 200),
                        });
                    }
                });
                const center = document.elementFromPoint(
                    window.innerWidth / 2, window.innerHeight / 2);
                if (center && center !== document.body &&
                    center !== document.documentElement) {
                    const z = getComputedStyle(center).zIndex;
                    if (z !== 'auto' && parseInt(z) > 100) {
                        results.push({
                            type: 'high-z-element',
                            tag: center.tagName,
                            id: center.id,
                            classes: center.className,
                            zIndex: z,
                            text: (center.textContent || '')
                                  .trim().slice(0, 100),
                        });
                    }
                }
                return results;
            })()
        """)
        return {'success': True, 'dialogs': dialogs}
    except Exception as e:
        return _error_context(e)


@mcp.tool()
async def ui_scroll(
    selector: str = '',
    direction: str = 'down',
    amount: int = 300,
) -> dict:
    """Scroll the page or a specific element.

    Args:
        selector: CSS selector to scroll within (empty = whole page)
        direction: 'up', 'down', 'left', or 'right'
        amount: Pixels to scroll

    Returns action description and screenshot after scrolling.
    """
    try:
        bridge = await _ensure_bridge()
        dx, dy = 0, 0
        if direction == 'down': dy = amount
        elif direction == 'up': dy = -amount
        elif direction == 'right': dx = amount
        elif direction == 'left': dx = -amount

        if selector:
            await bridge.evaluate(f"""
                document.querySelector({json.dumps(selector)})
                    ?.scrollBy({dx}, {dy})
            """)
            desc = f'Scrolled {direction} {amount}px in "{selector}"'
        else:
            await bridge.evaluate(f'window.scrollBy({dx}, {dy})')
            desc = f'Scrolled {direction} {amount}px on page'

        return await _post_interaction(bridge, desc, 'scrolled')
    except Exception as e:
        return _error_context(e)


@mcp.tool()
async def ui_stop() -> dict:
    """Stop the Kestrel app and close the browser."""
    global _bridge
    try:
        async with _bridge_lock:
            if _bridge:
                await _bridge.stop()
                _bridge = None
        return {'success': True, 'action': 'Kestrel UI stopped'}
    except Exception as e:
        return _error_context(e)


if __name__ == '__main__':
    mcp.run(transport='stdio')
