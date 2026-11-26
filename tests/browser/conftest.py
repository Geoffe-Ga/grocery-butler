"""Fixtures for the browser lane (issue #231, ADR 0022).

The dashboard's Content Security Policy forbids inline script, so every
interactive control depends on ``static/js/app.js`` loading and working.
Flask's test client can't tell whether it does. These fixtures serve the
real app on a real socket and drive it with a real headless Chromium.

Nothing here waits for time to pass. A test waits for one named thing (a
response, an element's state, a dialog) with the browser's own waiting,
and the server listens on a port the operating system chose, so two runs
can't collide.
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import Browser, ConsoleMessage, Error, Page, sync_playwright
from werkzeug.serving import make_server

from grocery_butler.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from flask import Flask

#: Set to a Chromium executable to use it instead of the one Playwright
#: downloads. For machines that can't reach Playwright's download host.
CHROMIUM_ENV = "BROWSER_TESTS_CHROMIUM"

#: How long a single wait may take before the page is called stuck.
STUCK_AFTER_MS = 10_000

INSTALL_HINT = (
    "The browser tests need Chromium. Install it with"
    " ./scripts/install-browser.sh, or point BROWSER_TESTS_CHROMIUM at a"
    " Chromium executable. These tests are part of the gate and do not skip."
)


@pytest.fixture(scope="package")
def browser() -> Iterator[Browser]:
    """Start one headless Chromium for the browser tests, and stop it after.

    Package scope, not session scope, on purpose. Playwright's synchronous
    API keeps an asyncio event loop running in this thread for as long as
    it is open. Held for the whole session, that loop makes every later
    test that starts its own (the bot's and the auth middleware's) fail
    with "this event loop is already running". Closing it when this
    package finishes leaves the rest of the suite as it was.

    A missing browser fails the run with the command that installs it.
    It never skips: a gate that skips is no gate.
    """
    with sync_playwright() as playwright:
        try:
            launched = playwright.chromium.launch(
                executable_path=os.environ.get(CHROMIUM_ENV) or None
            )
        except Error as err:
            pytest.fail(f"{INSTALL_HINT}\n\n{err}", pytrace=False)
        yield launched
        launched.close()


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a fresh SQLite file; create_app migrates it."""
    return str(tmp_path / "browser.db")


@pytest.fixture()
def app(db_path: str, monkeypatch: pytest.MonkeyPatch) -> Flask:
    """Create the real app, with CSRF protection and the real CSP on."""
    monkeypatch.setenv("FLASK_SECRET_KEY", "browser-test-session-key")
    monkeypatch.delenv("APP_ENV", raising=False)
    return create_app(db_path=db_path)


@pytest.fixture()
def base_url(app: Flask) -> Iterator[str]:
    """Serve the app on a loopback port the operating system picks."""
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=10)
        server.server_close()


class PageProblems:
    """What went wrong in the page while a test ran."""

    def __init__(self) -> None:
        """Start with nothing recorded and nothing expected."""
        self.found: list[str] = []
        self.expected: list[str] = []

    def expect(self, fragment: str) -> None:
        """Allow console errors containing this text, for a deliberate failure."""
        self.expected.append(fragment)

    def record(self, text: str) -> None:
        """Keep a problem unless the test said to expect it."""
        if not any(fragment in text for fragment in self.expected):
            self.found.append(text)


@pytest.fixture()
def problems() -> PageProblems:
    """Return this test's record of console errors and script failures."""
    return PageProblems()


@pytest.fixture()
def page(browser: Browser, base_url: str, problems: PageProblems) -> Iterator[Page]:
    """Open a fresh browser profile on the app, and fail on any page error.

    Every console error, uncaught script error and Content Security Policy
    violation is collected, and the test fails at the end if there were
    any it did not say to expect. So each test also proves the script
    loaded and ran cleanly under the real policy.
    """
    context = browser.new_context(base_url=base_url)
    # A bound on how long a broken page can hold a test up, not a wait any
    # passing test spends: every wait here ends the moment its event arrives.
    context.set_default_timeout(STUCK_AFTER_MS)
    opened = context.new_page()

    def on_console(message: ConsoleMessage) -> None:
        if message.type == "error":
            where = message.location.get("url", "")
            problems.record(f"console: {message.text} ({where})")

    opened.on("console", on_console)
    opened.on("pageerror", lambda error: problems.record(f"script: {error}"))
    yield opened
    context.close()
    assert problems.found == [], "the page reported errors"
