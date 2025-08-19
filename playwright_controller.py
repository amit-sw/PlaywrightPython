import subprocess
import time
import re
import os
import signal
from playwright.sync_api import sync_playwright, Playwright, Browser, Page, Error

class PlaywrightController:
    def __init__(self):
        self.mcp_server_process = None
        self.playwright: Playwright = None
        self.browser: Browser = None
        self.page: Page = None
        self.commands = []
        self.ws_endpoint = None

    def launch_mcp_server(self):
        if self.mcp_server_process and self.mcp_server_process.poll() is None:
            return "Server already running."

        log_file = "mcp_server.log"
        # The use of shell=True can be a security risk if the command were constructed from user input.
        # Here, the command is static, so it's acceptable for this use case.
        # preexec_fn=os.setsid is used to create a new process group, allowing us to terminate the server and its children.
        command = f"npx playwright run-server --port 0 > {log_file} 2>&1"
        self.mcp_server_process = subprocess.Popen(command, shell=True, preexec_fn=os.setsid)

        timeout = 15  # seconds
        start_time = time.time()
        while time.time() - start_time < timeout:
            if os.path.exists(log_file):
                with open(log_file, "r") as f:
                    content = f.read()
                    match = re.search(r'ws://[^\s]+', content)
                    if match:
                        self.ws_endpoint = match.group(0)
                        self.commands.append(f"# Launched MCP server at {self.ws_endpoint}")
                        return f"MCP server launched. Endpoint: {self.ws_endpoint}"
            time.sleep(0.5)

        self.shutdown()
        raise RuntimeError("Failed to launch MCP server: timeout waiting for endpoint.")

    def connect(self):
        if self.playwright:
            return "Playwright instance already exists."
        self.playwright = sync_playwright().start()
        self.commands.append("p = sync_playwright().start()")
        return "Playwright context started."

    def open_browser(self):
        if not self.ws_endpoint:
            return "MCP server not launched."
        if not self.playwright:
            self.connect()

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

    def goto(self, url: str):
        if not self.page or not self.browser.is_connected():
            return "Page not available. Please open a browser first."

        if not url.startswith("http"):
            url = "http://" + url
        self.page.goto(url)
        self.commands.append(f"page.goto('{url}')")
        return f"Navigated to {url}"

    def close_browser(self):
        if self.browser and self.browser.is_connected():
            self.browser.close()
        self.browser = None
        self.page = None
        self.commands.append("browser.close()")
        return "Browser closed."

    def shutdown(self):
        if self.browser and self.browser.is_connected():
            self.browser.close()
        if self.playwright:
            self.playwright.stop()
        if self.mcp_server_process:
            os.killpg(os.getpgid(self.mcp_server_process.pid), signal.SIGTERM)
            try:
                self.mcp_server_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(self.mcp_server_process.pid), signal.SIGKILL)

        self.mcp_server_process = None
        self.playwright = None
        self.browser = None
        self.page = None
        self.ws_endpoint = None
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
