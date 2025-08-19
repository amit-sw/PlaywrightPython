import subprocess
import time
import re
import os
import signal
from playwright.sync_api import sync_playwright, Playwright, Browser, Page, Error
import threading
import queue

class PlaywrightController:
    def __init__(self):
        self.mcp_server_process = None
        self.playwright: Playwright = None
        self.browser: Browser = None
        self.page: Page = None
        self.commands = []
        self.ws_endpoint = None
        self._owner_thread_id = None
        self._task_queue: queue.Queue = queue.Queue()
        self._result_queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._worker_thread: threading.Thread | None = None

    def launch_mcp_server(self):
        """
        Start the Playwright MCP server via npx and wait for its ws:// endpoint to appear in the log.
        This version is more robust: it increases the timeout, detects early process exit,
        and surfaces helpful diagnostics if startup fails.
        You can override the timeout by setting MCP_LAUNCH_TIMEOUT (seconds) in the environment.
        """
        if self.mcp_server_process and self.mcp_server_process.poll() is None:
            return "Server already running."

        log_file = "mcp_server.log"
        # Use npx with -y to avoid interactive prompts if Playwright isn't installed locally.
        # Redirect both stdout and stderr to the log for easier debugging.
        command = f"npx -y playwright@latest run-server --port 0 > {log_file} 2>&1"

        # Start the subprocess in its own process group so we can cleanly terminate it later.
        self.mcp_server_process = subprocess.Popen(command, shell=True, preexec_fn=os.setsid)

        # Allow more time for first-time setups (browser downloads, etc.)
        timeout = int(os.environ.get("MCP_LAUNCH_TIMEOUT", "75"))  # seconds
        start_time = time.time()
        last_size = 0

        # Poll for the ws endpoint while also detecting early failures.
        while time.time() - start_time < timeout:
            # If the process exited, surface the error from the log and stop.
            if self.mcp_server_process.poll() is not None:
                # Process ended before we found the endpoint.
                error_tail = ""
                if os.path.exists(log_file):
                    try:
                        with open(log_file, "r", errors="replace") as f:
                            lines = f.readlines()
                            error_tail = "".join(lines[-50:])  # last 50 lines for context
                    except Exception:
                        pass
                self.shutdown()
                raise RuntimeError(
                    "Failed to launch MCP server: process exited early.\n"
                    f"Last log lines:\n{error_tail}"
                )

            if os.path.exists(log_file):
                try:
                    with open(log_file, "r", errors="replace") as f:
                        content = f.read()
                except Exception:
                    content = ""

                # Look for a ws:// endpoint in the log
                match = re.search(r"ws://[^\s]+", content)
                if match:
                    self.ws_endpoint = match.group(0)
                    self.commands.append(f"# Launched MCP server at {self.ws_endpoint}")
                    return f"MCP server launched. Endpoint: {self.ws_endpoint}"

                # If the log is growing, extend patience a bit for first-time downloads
                if len(content) > last_size:
                    last_size = len(content)
                    # Heuristic: if we see signs of downloading/installing, allow more time once.
                    if any(k in content.lower() for k in ["download", "installing", "extracting", "browser"]):
                        timeout = max(timeout, int(time.time() - start_time) + 60)

            time.sleep(0.5)

        # Timed out without seeing the endpoint; include helpful diagnostics.
        error_tail = ""
        if os.path.exists(log_file):
            try:
                with open(log_file, "r", errors="replace") as f:
                    lines = f.readlines()
                    error_tail = "".join(lines[-50:])
            except Exception:
                pass

        self.shutdown()
        raise RuntimeError(
            "Failed to launch MCP server: timeout waiting for endpoint.\n"
            f"Hint: Ensure Node.js and npx are available, and try increasing MCP_LAUNCH_TIMEOUT.\n"
            f"Last log lines:\n{error_tail}"
        )

    def _bind_or_rebind_to_current_thread(self):
        """Ensure Playwright objects are used from the same thread that created them.
        If a different thread calls into this controller, cleanly restart Playwright/browser
        in the current thread to avoid greenlet 'cannot switch to a different thread' errors."""
        current = threading.get_ident()
        if self._owner_thread_id is None:
            self._owner_thread_id = current
            return
        if current != self._owner_thread_id:
            # Recreate Playwright/browser bound to this thread
            try:
                self.shutdown()
            except Exception:
                pass
            self._owner_thread_id = current
            # Do not auto-start here; caller will invoke connect/open as needed.

    def _ensure_worker(self):
        if self._worker_thread and self._worker_thread.is_alive():
            return
        # Start a dedicated thread that owns Playwright and the browser objects
        def _loop():
            self._owner_thread_id = threading.get_ident()
            while not self._stop_event.is_set():
                try:
                    item = self._task_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if item is None:
                    break
                func, args, kwargs = item
                try:
                    result = func(*args, **kwargs)
                    self._result_queue.put((True, result))
                except Exception as e:
                    self._result_queue.put((False, e))
        self._stop_event.clear()
        self._worker_thread = threading.Thread(target=_loop, name="PW-Owner", daemon=True)
        self._worker_thread.start()

    def _call_on_worker(self, func, *args, **kwargs):
        """Run the callable on the Playwright owner thread and return its result."""
        self._ensure_worker()
        # If already on owner thread, just run it
        if threading.get_ident() == self._owner_thread_id:
            return func(*args, **kwargs)
        self._task_queue.put((func, args, kwargs))
        ok, value = self._result_queue.get()
        if ok:
            return value
        raise value

    def connect(self):
        def _do():
            if self.playwright:
                return "Playwright instance already exists."
            self.playwright = sync_playwright().start()
            self.commands.append("p = sync_playwright().start()")
            return "Playwright context started."
        return self._call_on_worker(_do)

    def open_browser(self):
        def _do():
            if not self.ws_endpoint:
                return "MCP server not launched."
            if not self.playwright:
                # start Playwright on the same worker thread
                self.playwright = sync_playwright().start()
                self.commands.append("p = sync_playwright().start()")
            if self.browser and self.browser.is_connected():
                return "Browser is already open and connected."
            try:
                self.browser = self.playwright.chromium.connect(self.ws_endpoint)
                if self.browser.contexts and self.browser.contexts[0].pages:
                    self.page = self.browser.contexts[0].pages[0]
                else:
                    context = self.browser.new_context() if not self.browser.contexts else self.browser.contexts[0]
                    self.page = context.new_page()
                self.commands.append(f"browser = playwright.chromium.connect('{self.ws_endpoint}')")
                self.commands.append("page = browser.contexts[0].pages[0] if browser.contexts[0].pages else browser.contexts[0].new_page()")
                return "Browser opened and connected successfully."
            except Error as e:
                return f"Failed to connect to browser: {e}"
        return self._call_on_worker(_do)

    def goto(self, url: str):
        def _do():
            if not self.page or not self.browser or not self.browser.is_connected():
                return "Page not available. Please open a browser first."
            if not url.startswith("http"):
                url_to_go = "http://" + url
            else:
                url_to_go = url
            # Use a shorter nav plus a follow-up wait for network idle to avoid hangs
            self.page.goto(url_to_go, wait_until="domcontentloaded", timeout=60_000)
            self.commands.append(f"page.goto('{url_to_go}')")
            return f"Navigated to {url_to_go}"
        return self._call_on_worker(_do)

    def get_page_contents(self, max_chars: int = 100000) -> str | str:
        """Retrieve the full text content of the current page body. Returns a truncated string if too long."""
        def _do():
            if not self.page or not self.browser or not self.browser.is_connected():
                return "Page not available. Please open a browser first."
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:
                pass
            text = self.page.inner_text("body")
            if len(text) > max_chars:
                return text[:max_chars] + f"\n... [truncated, total length {len(text)} chars]"
            return text
        return self._call_on_worker(_do)

    def summarize_page(self, instructions: str = "Summarize the main points") -> str:
        """Produce a simple heuristic summary of the page content based on headings and paragraph text.
        This is a placeholder for passing the content to an external LLM for a true summary."""
        def _do():
            if not self.page or not self.browser or not self.browser.is_connected():
                return "Page not available. Please open a browser first."
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:
                pass
            text = self.page.inner_text("body")
            if len(text) > max_chars:
                text = text[:max_chars] + f"\n... [truncated, total length {len(text)} chars]"
            sample = paras[:5]
            summary = "Summary instructions: " + instructions + "\n"
            summary += "Headings found:\n" + "\n".join(headings[:10]) + "\n\n"
            summary += "Sample paragraphs:\n" + "\n\n".join(sample)
            return summary
        return self._call_on_worker(_do)

    def close_browser(self):
        def _do():
            if self.browser and self.browser.is_connected():
                self.browser.close()
            self.browser = None
            self.page = None
            self.commands.append("browser.close()")
            return "Browser closed."
        return self._call_on_worker(_do)

    def shutdown(self):
        def _do_close():
            if self.browser and self.browser.is_connected():
                self.browser.close()
            if self.playwright:
                self.playwright.stop()
            self.browser = None
            self.page = None
            self.playwright = None
            self.commands.append("# Shutdown Playwright")
            return True
        try:
            # Try to close on worker
            try:
                self._call_on_worker(_do_close)
            except Exception:
                pass
            # Stop MCP server regardless of worker state
            if self.mcp_server_process:
                os.killpg(os.getpgid(self.mcp_server_process.pid), signal.SIGTERM)
                try:
                    self.mcp_server_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(self.mcp_server_process.pid), signal.SIGKILL)
            self.mcp_server_process = None
            self.ws_endpoint = None
        finally:
            # Tear down worker thread
            if self._worker_thread and self._worker_thread.is_alive():
                self._stop_event.set()
                self._task_queue.put(None)
                self._worker_thread.join(timeout=2)
            self._worker_thread = None
            self._owner_thread_id = None
            self.commands.append("# Shutdown complete")
        return "Shutdown complete."

    def get_commands(self) -> list[str]:
        return self.commands

    def save_script(self, filename: str):
        if not filename.endswith(".py"):
            filename += ".py"

        script_content = "from playwright.sync_api import sync_playwright\n\n"
        script_content += "def run(playwright):\n"
        script_content += "    # This script launches a new browser instance and is not connected to the MCP server.\n"
        script_content += "    browser = playwright.chromium.launch(headless=False)\n"
        script_content += "    context = browser.new_context()\n"
        script_content += "    page = context.new_page()\n\n"

        user_commands = []
        for cmd in self.commands:
            if cmd.startswith("#") or "connect_over_cdp" in cmd or "playwright.stop" in cmd or "browser.close" in cmd or "p = sync_playwright" in cmd:
                continue
            if "browser.contexts[0].pages[0]" in cmd or "new_page()" in cmd:
                continue

            user_commands.append(f"    {cmd}")

        script_content += "\n".join(user_commands)
        script_content += "\n\n    print('Script finished. Closing browser.')\n"
        script_content += "    context.close()\n"
        script_content += "    browser.close()\n\n"
        script_content += "with sync_playwright() as playwright:\n"
        script_content += "    run(playwright)\n"

        with open(filename, "w") as f:
            f.write(script_content)

        return f"Script saved to {filename}"
