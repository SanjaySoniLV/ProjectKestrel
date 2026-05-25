"""Playwright-based UI automation bridge for Project Kestrel.

Starts the app's HTTP server, launches headless Chromium via Playwright,
and injects a fake window.pywebview.api that proxies JS calls to the real
Python Api class. This enables full UI testing in headless environments.

Usage:
    xvfb-run -a python3 perch_ui_harness/bridge.py \\
        --folder /path/to/images --timeout 300
"""

import asyncio
import json
import os
import sys
import threading
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_ANALYZER_DIR = str(Path(__file__).resolve().parent.parent / 'analyzer')
if _ANALYZER_DIR not in sys.path:
    sys.path.insert(0, _ANALYZER_DIR)

from playwright.async_api import async_playwright

HOST = '127.0.0.1'
DEFAULT_PORT = 18765
DIALOG_METHODS = {'choose_directory', 'choose_directories', 'choose_application'}


class KestrelUIBridge:
    """Manages the Playwright browser + Python Api bridge.

    Provides methods to interact with the Project Kestrel UI:
    - click(selector=...) / click(text=...) — click buttons/elements
    - screenshot(label) — capture the current viewport
    - fill(selector, value) — type into input fields
    - evaluate(js) — run arbitrary JavaScript
    - dismiss_overlays() — clear legal banners and tutorials
    - set_folder_override(path) — change the folder returned by dialogs
    """

    def __init__(self, port=DEFAULT_PORT, folder_override=None, screenshots_dir=None):
        self.port = port
        self.folder_override = folder_override
        self.screenshots_dir = Path(screenshots_dir or '/tmp/kestrel_screenshots')
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self._screenshot_counter = 0
        self._server = None
        self._server_thread = None
        self._api = None
        self._browser = None
        self._page = None
        self._playwright = None

    @property
    def page(self):
        return self._page

    @property
    def api(self):
        return self._api

    async def start(self):
        """Start the HTTP server, browser, and bridge."""
        self._start_http_server()
        await self._start_browser()
        await self._inject_bridge()
        await self._wait_for_app_ready()

    async def stop(self):
        """Shut down browser and server cleanly."""
        if self._page:
            try:
                await self._page.close()
            except Exception:
                pass
            self._page = None
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None

    # ── Internal setup ──────────────────────────────────────────────────

    def _start_http_server(self):
        os.chdir(_ANALYZER_DIR)
        analyzer_dir = _ANALYZER_DIR

        class Handler(SimpleHTTPRequestHandler):
            def do_GET(self):
                if self.path in ('/', '/index.html'):
                    self.path = '/visualizer.html'
                return super().do_GET()

            def translate_path(self, path):
                resolved = super().translate_path(path)
                if os.path.exists(resolved):
                    return resolved
                alt = os.path.join(analyzer_dir, path.lstrip('/'))
                if os.path.exists(alt):
                    return alt
                return resolved

            def end_headers(self):
                self.send_header('Cache-Control', 'no-store')
                super().end_headers()

            def log_message(self, fmt, *args):
                pass

        self._server = ThreadingHTTPServer((HOST, self.port), Handler)
        self._server_thread = threading.Thread(
            target=self._server.serve_forever, daemon=True)
        self._server_thread.start()
        print(f'[bridge] HTTP server on http://{HOST}:{self.port}/')

    async def _start_browser(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-gpu'],
        )
        self._page = await self._browser.new_page(
            viewport={'width': 1440, 'height': 900})
        print('[bridge] Chromium launched')

    async def _inject_bridge(self):
        from api_bridge import Api
        self._api = Api()
        self._api._server_port = self.port

        await self._page.expose_function(
            '__kestrel_bridge_call__', self._handle_bridge_call)

        await self._page.add_init_script("""
        (() => {
            window.__kestrel_ready__ = false;
            window.pywebview = {
                api: new Proxy({}, {
                    get(target, prop) {
                        if (prop === 'then' || prop === Symbol.toPrimitive
                            || prop === 'toJSON' || typeof prop === 'symbol') {
                            return undefined;
                        }
                        return async function(...args) {
                            const r = await window.__kestrel_bridge_call__(
                                String(prop), JSON.stringify(args));
                            return JSON.parse(r);
                        };
                    }
                })
            };
            setTimeout(() => {
                window.dispatchEvent(new Event('pywebviewready'));
                window.__kestrel_ready__ = true;
            }, 50);
        })();
        """)

        await self._page.goto(
            f'http://{HOST}:{self.port}/', wait_until='domcontentloaded')
        print('[bridge] Page loaded with pywebview.api bridge')

    async def _handle_bridge_call(self, method_name: str, args_json: str):
        try:
            args = json.loads(args_json)
            if method_name in DIALOG_METHODS:
                return self._handle_dialog_call(method_name, args)

            method = getattr(self._api, method_name, None)
            if method is None:
                return json.dumps({'error': f'Unknown method: {method_name}'})

            result = method(*args)
            return json.dumps(result, default=str)
        except Exception as e:
            print(f'[bridge] Error in {method_name}: {e}')
            return json.dumps({'error': str(e)})

    def _handle_dialog_call(self, method_name, args):
        if method_name == 'choose_directory':
            path = self.folder_override or ''
            print(f'[bridge] choose_directory -> {path}')
            return json.dumps(path)
        elif method_name == 'choose_directories':
            paths = [self.folder_override] if self.folder_override else []
            print(f'[bridge] choose_directories -> {paths}')
            return json.dumps(paths)
        elif method_name == 'choose_application':
            return json.dumps('')
        return json.dumps(None)

    async def _wait_for_app_ready(self):
        try:
            await self._page.wait_for_function(
                'window.__kestrel_ready__ === true', timeout=10000)
            await asyncio.sleep(1)
            print('[bridge] App ready')
        except Exception as e:
            print(f'[bridge] App ready wait: {e}')

    # ── Public interaction API ──────────────────────────────────────────

    async def screenshot(self, label=''):
        """Take a screenshot and return the file path."""
        self._screenshot_counter += 1
        name = f'{self._screenshot_counter:03d}'
        if label:
            name += f'_{label}'
        path = self.screenshots_dir / f'{name}.png'
        await self._page.screenshot(path=str(path), full_page=False)
        print(f'[bridge] Screenshot: {path}')
        return str(path)

    async def click(self, selector=None, text=None, label='', timeout=10000):
        """Click an element by CSS selector or visible text."""
        if text:
            locator = self._page.get_by_role('button', name=text)
            if await locator.count() == 0:
                locator = self._page.get_by_text(text, exact=False)
            await locator.first.click(timeout=timeout)
        elif selector:
            await self._page.click(selector, timeout=timeout)
        else:
            raise ValueError('Provide selector or text')
        await asyncio.sleep(0.3)
        return await self.screenshot(label or 'click')

    async def fill(self, selector, value):
        """Type into an input field."""
        await self._page.fill(selector, value)

    async def get_text(self, selector):
        """Get text content of an element."""
        return await self._page.text_content(selector)

    async def wait_for(self, selector, timeout=30000):
        """Wait for an element to appear."""
        await self._page.wait_for_selector(selector, timeout=timeout)

    async def evaluate(self, js_expression):
        """Run arbitrary JavaScript in the page context."""
        return await self._page.evaluate(js_expression)

    async def set_folder_override(self, path):
        """Change the folder returned by intercepted dialog calls."""
        self.folder_override = path

    async def dismiss_overlays(self):
        """Dismiss legal banner and tutorial overlay if present."""
        for selector, name in [
            ('#legalAgreeBtn', 'legal banner'),
        ]:
            try:
                loc = self._page.locator(selector)
                if await loc.is_visible(timeout=2000):
                    await loc.click()
                    print(f'[bridge] Dismissed {name}')
                    await asyncio.sleep(0.5)
            except Exception:
                pass

        try:
            skip = self._page.get_by_text('Skip Tour', exact=True)
            if await skip.is_visible(timeout=2000):
                await skip.click()
                print('[bridge] Skipped tutorial')
                await asyncio.sleep(0.5)
        except Exception:
            pass

        try:
            await self._page.wait_for_selector(
                '#tutorialOverlay:not(.active)', timeout=3000)
        except Exception:
            await self._page.evaluate("""
                const o = document.getElementById('tutorialOverlay');
                if (o) o.classList.remove('active', 'has-backdrop');
            """)

    async def dismiss_feedback_consent(self):
        """Dismiss the analytics/feedback consent dialog if present."""
        try:
            btn = self._page.locator('#analyticsDecline')
            if await btn.is_visible(timeout=1000):
                await btn.click()
                print('[bridge] Dismissed feedback consent')
                await asyncio.sleep(0.3)
        except Exception:
            pass

    def get_queue_info(self):
        """Get current analysis queue status from the Python Api."""
        info = self._api.get_queue_status()
        if not isinstance(info, dict):
            return {'running': False, 'items': []}
        return info


async def run_analysis_flow(folder_path, screenshots_dir=None, analysis_timeout=300):
    """Full UI flow: load app -> analyze folder -> capture screenshots."""
    sdir = screenshots_dir or '/tmp/kestrel_screenshots'
    bridge = KestrelUIBridge(
        folder_override=folder_path,
        screenshots_dir=sdir,
    )

    await bridge.start()
    await bridge.screenshot('app_loaded')

    await bridge.dismiss_overlays()
    await bridge.screenshot('ready')

    await bridge.click(selector='#analyzeQueueBtn', label='analyze_dialog')
    await asyncio.sleep(1)

    await bridge.click(
        selector='#analyzeDlgLoadFolders', label='add_folders')
    await asyncio.sleep(3)
    await bridge.screenshot('folders_loaded')

    # Ensure checkboxes are checked and Start button enabled
    await bridge.evaluate("""
        (() => {
            const boxes = document.querySelectorAll(
                '#analyzeDlgTree input[type="checkbox"]');
            boxes.forEach(cb => {
                cb.checked = true;
                cb.dispatchEvent(new Event('change', {bubbles: true}));
            });
            const btn = document.getElementById('analyzeDlgAdd');
            if (btn) btn.disabled = false;
        })()
    """)
    await asyncio.sleep(0.5)
    await bridge.screenshot('ready_to_start')

    await bridge.click(selector='#analyzeDlgAdd', label='analysis_started')
    await asyncio.sleep(2)
    await bridge.dismiss_feedback_consent()
    await bridge.screenshot('analysis_running')

    # Poll until analysis completes
    start_time = asyncio.get_event_loop().time()
    poll_count = 0
    while (asyncio.get_event_loop().time() - start_time) < analysis_timeout:
        await asyncio.sleep(5)
        poll_count += 1
        await bridge.dismiss_feedback_consent()
        await bridge.screenshot(f'progress_{poll_count:03d}')

        try:
            info = bridge.get_queue_info()
            running = info.get('running', False)
            items = info.get('items', [])
            processed = sum(i.get('processed', 0) for i in items)
            total = sum(i.get('total', 0) for i in items)
            statuses = [i.get('status', '') for i in items]
            print(f'[flow] {processed}/{total} {statuses}')

            if not running and poll_count > 2:
                print('[flow] Analysis complete')
                break

            status = await bridge.get_text('#status') or ''
            if 'complete' in status.lower() or 'error' in status.lower():
                break
        except Exception:
            pass

    await bridge.screenshot('final')
    print(f'[flow] Done. {bridge._screenshot_counter} screenshots in {sdir}')
    return bridge


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Kestrel UI Bridge')
    parser.add_argument('--folder', default='/home/user/ProjectKestrel/test_imgs')
    parser.add_argument('--screenshots', default='/tmp/kestrel_screenshots')
    parser.add_argument('--timeout', type=int, default=300)
    args = parser.parse_args()

    async def main():
        bridge = await run_analysis_flow(
            args.folder, args.screenshots, args.timeout)
        await bridge.stop()

    asyncio.run(main())
