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

# Redirect all print/log output to stderr so stdout stays clean for JSON-RPC.
# The MCP protocol uses stdout exclusively; any non-JSON line breaks clients.
_real_stderr = sys.stderr
class _StderrPrint:
    """Replacement for builtins.print that always writes to stderr."""
    def __call__(self, *args, **kwargs):
        kwargs['file'] = _real_stderr
        __builtins__['print'](*args, **kwargs) if isinstance(__builtins__, dict) else _orig_print(*args, **kwargs)

_orig_print = print
def print(*args, **kwargs):
    kwargs.setdefault('file', _real_stderr)
    _orig_print(*args, **kwargs)

_ANALYZER_DIR = str(Path(__file__).resolve().parent.parent / 'analyzer')
if _ANALYZER_DIR not in sys.path:
    sys.path.insert(0, _ANALYZER_DIR)

from mcp.server.fastmcp import FastMCP

from bridge import KestrelUIBridge

mcp = FastMCP("kestrel-ui-testing")

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
                '1) dismiss_overlays() to clear known overlays. '
                '2) Take a screenshot to identify what\'s blocking. '
                '3) Use ui_evaluate to remove the overlay via JS.')
    if 'not attached' in msg or 'detached' in msg:
        return 'Element was removed from DOM. Take a screenshot and retry.'
    return ''


# ── Core interaction tools ──────────────────────────────────────────────

@mcp.tool()
async def ui_start(folder_path: str = '') -> dict:
    """Start or restart the Kestrel app UI.

    Args:
        folder_path: Default folder for dialog intercepts (defaults to test_imgs)

    Returns dict with success status. The app dismisses legal/tutorial
    overlays automatically on start.
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
            'message': 'Kestrel UI started and ready',
            'screenshot': path,
        }
    except Exception as e:
        return _error_context(e)


@mcp.tool()
async def ui_screenshot(label: str = '') -> dict:
    """Take a screenshot of the current UI state.

    Args:
        label: Optional label for the screenshot filename

    Returns dict with the screenshot file path.
    """
    try:
        bridge = await _ensure_bridge()
        path = await bridge.screenshot(label or 'capture')
        return {'success': True, 'path': path}
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
        text: Visible button/link text to click (e.g. 'Start Analysis')
        timeout: Max wait time in ms (default 10000)

    Provide either selector OR text, not both. Returns a screenshot
    taken after the click, plus error diagnostics if it fails.
    """
    if not selector and not text:
        return {'success': False, 'error': 'Provide selector or text'}
    try:
        bridge = await _ensure_bridge()
        label = text or selector.replace('#', '').replace('.', '')
        path = await bridge.click(
            selector=selector or None,
            text=text or None,
            label=f'click_{label[:30]}',
            timeout=timeout,
        )
        return {'success': True, 'screenshot': path}
    except Exception as e:
        # Take a screenshot showing what went wrong
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
    """
    try:
        bridge = await _ensure_bridge()
        await bridge.fill(selector, value)
        path = await bridge.screenshot('typed')
        return {'success': True, 'screenshot': path}
    except Exception as e:
        return _error_context(e)


@mcp.tool()
async def ui_hover(selector: str) -> dict:
    """Hover over an element (useful for tooltips, dropdowns).

    Args:
        selector: CSS selector to hover over
    """
    try:
        bridge = await _ensure_bridge()
        await bridge.page.hover(selector)
        await asyncio.sleep(0.3)
        path = await bridge.screenshot('hover')
        return {'success': True, 'screenshot': path}
    except Exception as e:
        return _error_context(e)


@mcp.tool()
async def ui_select(selector: str, value: str) -> dict:
    """Select an option from a <select> dropdown.

    Args:
        selector: CSS selector for the <select> element
        value: Option value to select
    """
    try:
        bridge = await _ensure_bridge()
        await bridge.page.select_option(selector, value)
        path = await bridge.screenshot('selected')
        return {'success': True, 'screenshot': path}
    except Exception as e:
        return _error_context(e)


@mcp.tool()
async def ui_checkbox(selector: str, checked: bool = True) -> dict:
    """Set a checkbox state.

    Args:
        selector: CSS selector for the checkbox
        checked: True to check, False to uncheck
    """
    try:
        bridge = await _ensure_bridge()
        if checked:
            await bridge.page.check(selector)
        else:
            await bridge.page.uncheck(selector)
        path = await bridge.screenshot('checkbox')
        return {'success': True, 'screenshot': path}
    except Exception as e:
        return _error_context(e)


# ── Page inspection tools ───────────────────────────────────────────────

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
                    visible: el.offsetParent !== null || el.style.display !== 'none',
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
        elif text:
            await bridge.page.wait_for_function(
                f'document.body.innerText.includes({json.dumps(text)})',
                timeout=timeout)
        else:
            return {'success': False, 'error': 'Provide selector or text'}
        path = await bridge.screenshot('wait_done')
        return {'success': True, 'screenshot': path}
    except Exception as e:
        return _error_context(e)


# ── Dialog & overlay management ─────────────────────────────────────────

@mcp.tool()
async def ui_dismiss_overlays() -> dict:
    """Dismiss any known overlay: legal banner, tutorial, feedback consent.

    Call this when clicks are being blocked by overlays.
    """
    try:
        bridge = await _ensure_bridge()
        await bridge.dismiss_overlays()
        await bridge.dismiss_feedback_consent()
        path = await bridge.screenshot('overlays_dismissed')
        return {'success': True, 'screenshot': path}
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
        return {'success': True, 'folder': folder_path}
    except Exception as e:
        return _error_context(e)


# ── Analysis queue tools ────────────────────────────────────────────────

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
                    'processed': total_processed,
                    'total': total_total,
                    'screenshot': path,
                    'message': f'Reached {total_processed}/{total_total}',
                }

            if target_processed == 0 and not running and total_total > 0:
                path = await bridge.screenshot('analysis_complete')
                return {
                    'success': True,
                    'processed': total_processed,
                    'total': total_total,
                    'screenshot': path,
                    'message': f'Analysis complete: {total_processed}/{total_total}',
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


# ── Utility tools ───────────────────────────────────────────────────────

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
                // Check <dialog> elements
                document.querySelectorAll('dialog').forEach(d => {
                    if (d.open) results.push({
                        type: 'dialog', id: d.id,
                        text: (d.textContent || '').trim().slice(0, 200)
                    });
                });
                // Check overlay-style divs
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
                // Check for any element with high z-index covering the viewport
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
                            text: (center.textContent || '').trim().slice(0, 100),
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
        else:
            await bridge.evaluate(f'window.scrollBy({dx}, {dy})')

        await asyncio.sleep(0.2)
        path = await bridge.screenshot('scrolled')
        return {'success': True, 'screenshot': path}
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
        return {'success': True, 'message': 'Kestrel UI stopped'}
    except Exception as e:
        return _error_context(e)


if __name__ == '__main__':
    import logging
    logging.getLogger('mcp').setLevel(logging.WARNING)
    logging.getLogger('uvicorn').setLevel(logging.WARNING)
    mcp.run(transport='stdio')
