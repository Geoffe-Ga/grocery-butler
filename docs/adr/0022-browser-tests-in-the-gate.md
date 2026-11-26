# ADR 0022: The dashboard's JavaScript is tested in a real browser, in the gate

- **Status:** Accepted
- **Date:** 2026-11-26
- **Decider:** Geoff (decided on #231: add the browser dependency, and make the tests gate merges)
- **Source:** Issue #231, from the end-to-end test audit of 2026-11-26; builds on ADR 0020 and the browser security baseline (#138)

## Context

The dashboard's Content Security Policy forbids inline script. Everything
interactive on the kitchen phone therefore depends on one file,
`static/js/app.js`, loading and working: the inventory status buttons, the
recipe form's extra rows, the delete confirmation, the menu. That file had
no test. Flask's test client checks that a page renders and that an
endpoint accepts a correct request. It cannot tell whether the page's own
script sends one. A typo in the script, a renamed data attribute in a
template, or a policy change that blocks the file leaves buttons that do
nothing while every test passes.

The project is Python only, and until now every gate ran without a browser.

## Decision

1. **`tests/browser` drives the real app in headless Chromium.** The app is
   served on a loopback port the operating system picks, with CSRF
   protection and the real Content Security Policy on. Tests click real
   controls and then check stored state, usually after a reload.
2. **Playwright is the tool**, pinned to an exact version in
   `requirements-dev.txt`. The package decides which Chromium build is
   used, so the pin fixes the browser too. `./scripts/install-browser.sh`
   downloads it.
3. **The tests are part of the gate.** They carry the `browser` marker and
   run in `./scripts/check-all.sh` with the rest of the suite, so in the
   `CI gate` on all three Python versions. The quick unit lane
   (`./scripts/test.sh`) leaves them out.
4. **A missing browser fails the run.** It never skips, and the failure
   names the install command. A gate that skips is no gate.
5. **Every test also fails on a page error.** Console errors, uncaught
   script errors and Content Security Policy violations are collected for
   each page a test opens. A test that provokes one on purpose says which.
6. **No waiting on time.** A test waits for one named thing, with the
   browser's own waiting: a response, an element's state, a dialog. There
   is no fixed delay and no retry. One bound exists, on how long a broken
   page may hold a test up before it is called stuck.
7. **`BROWSER_TESTS_CHROMIUM` may name a Chromium executable to use
   instead.** It is for machines that cannot reach Playwright's download
   host. CI does not set it.

## Consequences

- A broken script, a renamed attribute or a blocking policy now fails the
  gate. The first run found one: every page asked for `/favicon.ico` and
  got a 404. The base template now names an icon.
- Contributors need one more setup step, and the gate now downloads a
  browser on each CI run. The Python gate's time limit already had to move
  from 10 to 20 minutes for the suite as it stood.
- A flaky browser test would block every pull request. Decision 6 is the
  bar for adding one, and the rule on flaky tests in `CLAUDE.md` applies:
  fix the cause or delete the test, never retry it.
- The pinned Playwright version must be upgraded on purpose. A new version
  brings a new Chromium, and the install script must be run again.
- Mutation testing leaves these tests out. They check the JavaScript, not
  the Python being mutated, and a browser per mutant would take hours.
- These tests run on SQLite only. They are about the script; the
  end-to-end lane covers both dialects.

## Alternatives considered

- **A separate lane that does not gate**, like the container scan. No
  pull request is ever blocked by the browser. Rejected by Geoff: a red
  nightly is easy to ignore, and this is the household's main screen.
- **Test the script with a JavaScript test runner and a simulated DOM.**
  No real browser to install. Rejected: it adds a second language's
  toolchain to a Python-only project, and a simulated DOM does not
  enforce the Content Security Policy, which is half of what can break.
- **Selenium.** Mature and well known. Rejected: it needs a separately
  managed driver matched to the browser by hand; Playwright pins both
  with one version number and waits on events by default.
- **Keep relying on the test client and a manual check.** Free. Rejected:
  that is the gap the audit found.
