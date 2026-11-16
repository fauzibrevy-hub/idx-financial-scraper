"""Tests for the Chrome start and cleanup in model/fetch_idx.py and the signal hand-off in run.py.

No network. Most tests use a fake "Chrome" (a small Python script) that takes the same arguments,
writes a SingletonLock like Chrome does and answers /json/version on the DevTools port, or not,
depending on FAKE_CHROME_MODE. The in-flight signal tests need a real Playwright call, so they run
the installed Google Chrome headless against a local HTTP server (skipped when Chrome is missing).

    python3 -m unittest discover -s tests -v
"""
import http.server
import importlib
import logging
import threading
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "model"))
sys.path.insert(1, str(REPO))  # run.py

import fetch_idx  # noqa: E402

FAKE_CHROME = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import http.server, os, signal, socket, sys, time
    args = dict(a.split("=", 1) for a in sys.argv[1:] if a.startswith("--") and "=" in a)
    port = int(args["--remote-debugging-port"])
    profile = args["--user-data-dir"]
    mode = os.environ.get("FAKE_CHROME_MODE", "ok")
    if mode == "exit3":
        sys.exit(3)
    os.makedirs(profile, exist_ok=True)
    lock = os.path.join(profile, "SingletonLock")
    if os.path.lexists(lock):
        os.unlink(lock)
    os.symlink("%s-%d" % (socket.gethostname(), os.getpid()), lock)
    if mode == "ignore_term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(float(os.environ.get("FAKE_CHROME_DELAY", "0")))
    if mode == "never":
        while True:
            time.sleep(1)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/devtools/"):
                # hang_ws: the websocket handshake of connect_over_cdp never completes
                open(os.path.join(profile, "ws-requested"), "w").close()
                time.sleep(3600)
                return
            body = '{"Browser": "FakeChrome/1.0"}'
            if mode == "hang_ws":
                body = ('{"Browser": "FakeChrome/1.0", "webSocketDebuggerUrl": '
                        '"ws://127.0.0.1:%d/devtools/browser/fake"}' % port)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *a):
            pass

    class Server(http.server.HTTPServer):
        allow_reuse_port = True  # the test keeps the port reserved with SO_REUSEPORT

    Server(("127.0.0.1", port), Handler).serve_forever()
    """
)


def reserve_port():
    """(socket, port): a port bound with SO_REUSEPORT and held, not listening, for the whole test.

    No other process can take it in between (unlike bind, read the number, close); the fake
    Chrome binds the same port with SO_REUSEPORT and listens on it.
    """
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("127.0.0.1", 0))
    return s, s.getsockname()[1]


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def dead_pid() -> int:
    """A pid that belonged to a process which has exited and been reaped."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


REAL_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def free_port_for_real_chrome() -> int:
    """Real Chrome binds without SO_REUSEPORT, so its port cannot stay reserved: bind, read, close."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SlowSite:
    """A local stand-in for idx.co.id. /file/... (the download fetch) blocks until released or 60 s.

    /home blocks too when `block_home`, except its first request (Chrome's start page), so that the
    request that blocks is page.goto() from open_idx_home().
    """

    def __init__(self, block_home: bool):
        self.release = threading.Event()
        self.home_requests = 0
        self.home_blocked = threading.Event()
        self.file_blocked = threading.Event()
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/home"):
                    site.home_requests += 1
                    if block_home and site.home_requests > 1:
                        site.home_blocked.set()
                        site.release.wait(60)
                    body = b"<html><head><title>IDX test</title></head><body>ok</body></html>"
                    status = 200
                else:
                    site.file_blocked.set()
                    site.release.wait(60)
                    body, status = b"", 404
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


def wait_until(predicate, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


class ChromeStartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="chrome-start-test-")).resolve()
        self.profile = self.tmp / "chrome_profile"
        self.chrome = self.tmp / "fake-chrome"
        self.chrome.write_text(FAKE_CHROME)
        self.chrome.chmod(0o755)
        self.reserved, self.port = reserve_port()
        self.procs = []
        self.extra_dirs = []
        self.env = mock.patch.dict(os.environ, {"FAKE_CHROME_MODE": "ok", "FAKE_CHROME_DELAY": "0",
                                                "PYTHONDONTWRITEBYTECODE": "1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()
        self.reserved.close()
        for d in [self.tmp, *self.extra_dirs]:
            subprocess.run(["rm", "-rf", str(d)], check=False)

    def plant_lock(self, target: str, profile=None) -> None:
        profile = profile or self.profile
        profile.mkdir(parents=True, exist_ok=True)
        os.symlink(target, profile / "SingletonLock")
        os.symlink("/nonexistent/SingletonSocket", profile / "SingletonSocket")
        os.symlink("123456789", profile / "SingletonCookie")

    def singleton_files(self, profile=None):
        profile = profile or self.profile
        return sorted(n for n in fetch_idx.SINGLETON_FILES if os.path.lexists(profile / n))

    def start(self, timeout=10):
        proc = fetch_idx.ensure_chrome_running(self.port, self.profile, str(self.chrome), timeout=timeout)
        if proc is not None:
            self.procs.append(proc)
        return proc

    def holder(self, user_data_dir_arg: str, cwd=None) -> subprocess.Popen:
        """A live fake Chrome holding a profile (its own lock), debug port never opened."""
        h = subprocess.Popen([str(self.chrome), "--remote-debugging-port=1",
                              "--user-data-dir=%s" % user_data_dir_arg], cwd=cwd,
                             env=dict(os.environ, FAKE_CHROME_MODE="never"),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(h)
        lock = Path(cwd or "/") / user_data_dir_arg / "SingletonLock"
        self.assertTrue(wait_until(lambda: os.path.lexists(lock)), "holder never wrote its lock")
        return h

    # --- the SingletonLock --------------------------------------------------------------------

    def test_lock_owner_parses_host_with_dashes(self):
        self.plant_lock("build-host-01.local-19868")
        self.assertEqual(fetch_idx.singleton_lock_owner(self.profile), ("build-host-01.local", 19868))

    def test_lock_owner_none_without_lock_or_with_garbage(self):
        self.assertIsNone(fetch_idx.singleton_lock_owner(self.profile))
        self.plant_lock("no-pid-here")
        self.assertEqual(fetch_idx.singleton_lock_owner(self.profile), ("no-pid-here", None))

    def test_stale_lock_of_a_dead_pid_is_removed(self):
        self.plant_lock("%s-%d" % (socket.gethostname(), dead_pid()))
        self.assertTrue(fetch_idx.remove_stale_singleton(self.profile))
        self.assertEqual(self.singleton_files(), [])

    def test_removal_log_says_dead_or_reused(self):
        pid = dead_pid()
        self.plant_lock("%s-%d" % (socket.gethostname(), pid))
        with self.assertLogs(fetch_idx.logger, level=logging.INFO) as logs:
            fetch_idx.remove_stale_singleton(self.profile)
        self.assertIn("pid %d is dead or reused" % pid, "\n".join(logs.output))

    def test_lock_of_a_live_non_chrome_process_is_stale(self):
        # pid reuse: the number in the lock now belongs to an unrelated program
        other = subprocess.Popen(["sleep", "30"])
        self.procs.append(other)
        self.plant_lock("%s-%d" % (socket.gethostname(), other.pid))
        self.assertTrue(fetch_idx.remove_stale_singleton(self.profile))
        self.assertEqual(self.singleton_files(), [])

    def test_lock_of_a_live_chrome_on_this_profile_is_refused(self):
        h = self.holder(str(self.profile))
        with self.assertRaisesRegex(fetch_idx.ChromeStartError, "pid %d" % h.pid):
            fetch_idx.remove_stale_singleton(self.profile, chrome_path=str(self.chrome))
        self.assertEqual(self.singleton_files(), ["SingletonLock"])

    def test_live_chrome_with_a_relative_profile_path_is_refused(self):
        h = self.holder("chrome_profile", cwd=str(self.tmp))
        with self.assertRaisesRegex(fetch_idx.ChromeStartError, "pid %d" % h.pid):
            fetch_idx.remove_stale_singleton(self.profile, chrome_path=str(self.chrome))
        self.assertEqual(self.singleton_files(), ["SingletonLock"])

    def test_live_chrome_via_tmp_or_private_tmp_is_refused(self):
        base = Path(tempfile.mkdtemp(prefix="chrome-start-tmp-", dir="/tmp"))
        self.extra_dirs.append(Path(os.path.realpath(base)))
        short = Path("/tmp") / base.name / "chrome_profile"           # /tmp/... (a symlink on macOS)
        real = Path(os.path.realpath(base)) / "chrome_profile"         # /private/tmp/... on macOS
        for holder_path, checked in ((real, short), (short, real)):
            with self.subTest(holder=str(holder_path), checked=str(checked)):
                h = self.holder(str(holder_path))
                with self.assertRaisesRegex(fetch_idx.ChromeStartError, "pid %d" % h.pid):
                    fetch_idx.remove_stale_singleton(checked, chrome_path=str(self.chrome))
                self.assertEqual(self.singleton_files(real), ["SingletonLock"])
                h.kill()
                h.wait()
                (real / "SingletonLock").unlink()

    def test_live_chrome_with_a_trailing_slash_is_refused(self):
        h = self.holder(str(self.profile) + "/")
        with self.assertRaisesRegex(fetch_idx.ChromeStartError, "pid %d" % h.pid):
            fetch_idx.remove_stale_singleton(self.profile, chrome_path=str(self.chrome))

    # --- which process counts as a Chrome -----------------------------------------------------

    def test_program_name_is_the_executable_or_the_interpreted_script(self):
        py = "/opt/py/Python.app/Contents/MacOS/Python"
        cases = [
            (REAL_CHROME, REAL_CHROME + " --remote-debugging-port=9222", "Google Chrome"),
            ("/Applications/My -Apps/Chromium.app/Contents/MacOS/Chromium",
             "/Applications/My -Apps/Chromium.app/Contents/MacOS/Chromium --user-data-dir=/p", "Chromium"),
            (py, py + " /tmp/x/fake-chrome --remote-debugging-port=1", "fake-chrome"),
            ("/usr/bin/env", "/usr/bin/env python3 /tmp/x/fake-chrome --x=1", "fake-chrome"),
            ("/bin/sleep", "sleep 30", "sleep"),
            ("", "", ""),
        ]
        for comm, args, want in cases:
            with self.subTest(comm=comm):
                self.assertEqual(fetch_idx._program_name(comm, args), want)

    def test_only_chrome_chromium_or_the_configured_browser_count(self):
        is_chrome = fetch_idx._is_chrome_program
        for name in ("Google Chrome", "Google Chrome Canary", "Google Chrome for Testing", "Chromium",
                     "chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome.exe"):
            with self.subTest(name=name):
                self.assertTrue(is_chrome(name))
        for name in ("Google Chrome Helper", "Google Chrome Helper (Renderer)", "Chromium Helper (GPU)",
                     "Python", "sleep", "Brave Browser", "chrome-headless-shell", ""):
            with self.subTest(name=name):
                self.assertFalse(is_chrome(name))
        brave = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
        self.assertTrue(is_chrome("Brave Browser", chrome_path=brave))
        self.assertTrue(is_chrome("kiosk", chrome_path="/opt/browser/bin/kiosk"))
        self.assertFalse(is_chrome("Google Chrome Helper (Renderer)", chrome_path=REAL_CHROME))

    def test_a_script_not_named_like_chrome_is_not_a_chrome_without_chrome_path(self):
        h = self.holder(str(self.profile))
        self.assertTrue(fetch_idx.remove_stale_singleton(self.profile))
        self.assertTrue(pid_alive(h.pid))

    def test_lock_of_a_reused_pid_that_is_a_chrome_helper_is_stale(self):
        helpers = subprocess.run(["pgrep", "-f", "Google Chrome Helper"], capture_output=True, text=True).stdout.split()
        if not helpers:
            self.skipTest("no Google Chrome Helper process running")
        self.plant_lock("%s-%s" % (socket.gethostname(), helpers[0]))
        self.assertTrue(fetch_idx.remove_stale_singleton(self.profile, chrome_path=REAL_CHROME))
        self.assertEqual(self.singleton_files(), [])

    def test_a_live_real_chrome_holding_the_profile_is_refused(self):
        if not os.path.exists(REAL_CHROME):
            self.skipTest("Google Chrome not installed")
        h = subprocess.Popen([REAL_CHROME, "--headless=new", "--remote-debugging-port=%d" % free_port_for_real_chrome(),
                              "--user-data-dir=%s" % self.profile, "--no-first-run", "about:blank"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(h)
        self.assertTrue(wait_until(lambda: os.path.lexists(self.profile / "SingletonLock"), 30))
        with self.assertRaisesRegex(fetch_idx.ChromeStartError, "pid %d" % h.pid):
            fetch_idx.remove_stale_singleton(self.profile)
        fetch_idx.stop_chrome(h, self.profile)
        self.assertEqual(self.singleton_files(), [])

    def test_lock_changed_while_checked_is_left_in_place(self):
        self.plant_lock("%s-%d" % (socket.gethostname(), os.getpid()))
        replacement = "%s-%d" % (socket.gethostname(), 424242)

        def another_chrome_takes_the_profile(*_args):
            os.unlink(self.profile / "SingletonLock")
            os.symlink(replacement, self.profile / "SingletonLock")
            return False

        with mock.patch.object(fetch_idx, "_process_is_chrome", side_effect=another_chrome_takes_the_profile):
            self.assertFalse(fetch_idx.remove_stale_singleton(self.profile))
        self.assertEqual(os.readlink(self.profile / "SingletonLock"), replacement)
        self.assertEqual(len(self.singleton_files()), 3)

    def test_lock_from_another_host_is_refused(self):
        self.plant_lock("some-other-mac.local-4242")
        with self.assertRaisesRegex(fetch_idx.ChromeStartError, "some-other-mac.local"):
            fetch_idx.remove_stale_singleton(self.profile)
        self.assertEqual(len(self.singleton_files()), 3)

    def test_unparseable_lock_is_refused_not_removed(self):
        self.plant_lock("no-pid-here")
        with self.assertRaises(fetch_idx.ChromeStartError):
            fetch_idx.remove_stale_singleton(self.profile)
        self.assertEqual(len(self.singleton_files()), 3)

    def test_no_lock_nothing_to_do(self):
        self.assertFalse(fetch_idx.remove_stale_singleton(self.profile))

    # --- start, attach, stop ---------------------------------------------------------------------

    def test_start_and_stop_leave_no_process_and_no_lock(self):
        proc = self.start()
        self.assertIsNotNone(proc)
        self.assertTrue(fetch_idx.debug_port_is_open(self.port))
        self.assertEqual(self.singleton_files(), ["SingletonLock"])
        fetch_idx.stop_chrome(proc, self.profile)
        self.assertIsNotNone(proc.poll())
        self.assertFalse(pid_alive(proc.pid))
        self.assertEqual(self.singleton_files(), [])

    def test_planted_stale_lock_does_not_block_the_start(self):
        self.plant_lock("%s-%d" % (socket.gethostname(), dead_pid()))
        proc = self.start()
        self.assertIsNotNone(proc)
        self.assertEqual(os.readlink(self.profile / "SingletonLock"),
                         "%s-%d" % (socket.gethostname(), proc.pid))
        fetch_idx.stop_chrome(proc, self.profile)

    def test_slow_start_within_the_timeout_succeeds(self):
        os.environ["FAKE_CHROME_DELAY"] = "2.5"
        proc = self.start(timeout=8)
        self.assertIsNotNone(proc)
        fetch_idx.stop_chrome(proc, self.profile)

    def test_timeout_stops_the_chrome_it_started_and_says_why(self):
        os.environ["FAKE_CHROME_MODE"] = "never"
        started = time.time()
        with self.assertRaises(fetch_idx.ChromeStartError) as ctx:
            self.start(timeout=2)
        self.assertLess(time.time() - started, 20)
        msg = str(ctx.exception)
        self.assertIn("did not open debug port %d within 2s" % self.port, msg)
        self.assertIn("stopped it", msg)
        pid = int(msg.split("pid ")[1].split(")")[0])
        self.assertFalse(pid_alive(pid))
        self.assertEqual(self.singleton_files(), [])

    def test_chrome_that_exits_early_fails_fast_with_its_exit_code(self):
        os.environ["FAKE_CHROME_MODE"] = "exit3"
        started = time.time()
        with self.assertRaisesRegex(fetch_idx.ChromeStartError, "exited with code 3"):
            self.start(timeout=30)
        self.assertLess(time.time() - started, 10)

    def test_missing_chrome_is_a_chrome_start_error(self):
        with self.assertRaisesRegex(fetch_idx.ChromeStartError, "not found"):
            fetch_idx.ensure_chrome_running(self.port, self.profile, str(self.tmp / "no-such-chrome"))

    def test_attaches_to_a_chrome_already_on_the_port_and_never_stops_it(self):
        other = subprocess.Popen([str(self.chrome), "--remote-debugging-port=%d" % self.port,
                                  "--user-data-dir=%s" % (self.tmp / "other")])
        self.procs.append(other)
        self.assertTrue(wait_until(lambda: fetch_idx.debug_port_is_open(self.port)),
                        "the Chrome to attach to never opened its port")
        self.assertIsNone(self.start())
        fetch_idx.stop_chrome(None, self.profile)
        self.assertIsNone(other.poll())

    def test_stop_escalates_to_sigkill_when_chrome_ignores_sigterm(self):
        os.environ["FAKE_CHROME_MODE"] = "ignore_term"
        proc = self.start()
        fetch_idx.stop_chrome(proc, self.profile, timeout=1)
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        self.assertEqual(self.singleton_files(), [])

    def test_stop_is_safe_when_chrome_is_already_gone(self):
        proc = self.start()
        proc.kill()
        proc.wait()
        fetch_idx.stop_chrome(proc, self.profile)
        self.assertEqual(self.singleton_files(), [])

    def test_stop_does_not_raise_when_the_killed_chrome_is_not_reaped(self):
        stuck = mock.Mock(pid=4242)
        stuck.poll.return_value = None
        stuck.wait.side_effect = subprocess.TimeoutExpired("chrome", 1)
        with self.assertLogs(fetch_idx.logger, level=logging.WARNING) as logs:
            fetch_idx.stop_chrome(stuck, self.profile, timeout=0.1)
        stuck.kill.assert_called_once()
        self.assertIn("still running 10 s after SIGKILL", "\n".join(logs.output))

    # --- signals -------------------------------------------------------------------------------

    def run_with_handlers(self, body: str):
        code = textwrap.dedent(
            """\
            import os, signal, sys, time
            sys.path.insert(0, %r)
            import fetch_idx
            fetch_idx.install_signal_handlers()
            try:
                print("ready", flush=True)
                time.sleep(30)
            finally:
            %s
                print("cleanup ran", flush=True)
            """
        ) % (str(REPO / "model"), textwrap.indent(body, "    "))
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.procs.append(p)
        self.assertEqual(p.stdout.readline().strip(), "ready")
        return p

    def test_sigterm_and_sighup_become_systemexit_so_cleanup_runs(self):
        for sig, code_expected in ((signal.SIGTERM, 143), (signal.SIGHUP, 129)):
            p = self.run_with_handlers("pass")
            p.send_signal(sig)
            out, _ = p.communicate(timeout=10)
            self.assertIn("cleanup ran", out)
            self.assertEqual(p.returncode, code_expected)

    def test_a_second_signal_does_not_break_the_cleanup(self):
        p = self.run_with_handlers('print("cleaning", flush=True)\ntime.sleep(1.5)')
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.stdout.readline().strip(), "cleaning")
        p.send_signal(signal.SIGTERM)
        p.send_signal(signal.SIGHUP)
        out, _ = p.communicate(timeout=10)
        self.assertIn("cleanup ran", out)
        self.assertEqual(p.returncode, 143)

    def test_default_start_timeout_is_longer_than_the_old_30s(self):
        self.assertGreaterEqual(fetch_idx.CHROME_START_TIMEOUT_SECONDS, 90)

    # --- main() and run.py, as processes ---------------------------------------------------------

    def fetch_cmd(self, *extra):
        (self.tmp / "watchlist.csv").write_text("ticker\nUNTR\n")
        return [sys.executable, str(REPO / "model" / "fetch_idx.py"), "--data-dir", str(self.tmp / "data"),
                "--watchlist", str(self.tmp / "watchlist.csv"), "--start-year", "2025", "--end-year", "2025",
                "--periods", "FY", *extra]

    def test_main_stops_chrome_when_playwright_cannot_connect(self):
        # The fake answers /json/version without a webSocketDebuggerUrl, so connect_over_cdp fails.
        r = subprocess.run(self.fetch_cmd("--port", str(self.port), "--chrome-path", str(self.chrome)),
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(len(re.findall(r"\[ERROR\]", r.stderr)), 1, r.stderr)
        self.assertIn("Could not connect to Chrome on debug port %d" % self.port, r.stderr)
        pid = int(re.search(r"open after [0-9.]+s \(pid (\d+)\)", r.stderr).group(1))
        self.assertFalse(pid_alive(pid))
        self.assertIn("Stopped the Chrome this run started (pid %d)" % pid, r.stderr)
        self.assertEqual(self.singleton_files(self.tmp / "data" / "chrome_profile"), [])

    def test_main_reports_a_missing_chrome_in_one_line(self):
        r = subprocess.run(self.fetch_cmd("--port", str(self.port), "--chrome-path", str(self.tmp / "nope")),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(len(re.findall(r"\[ERROR\]", r.stderr)), 1, r.stderr)

    def test_empty_ps_output_counts_as_a_chrome_only_while_the_pid_lives(self):
        gone = dead_pid()
        calls = []

        def empty_ps(cmd, **_kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with mock.patch.object(fetch_idx.subprocess, "run", side_effect=empty_ps):
            self.assertTrue(fetch_idx._process_is_chrome(os.getpid()))  # alive, unreadable: keep the lock
            self.assertFalse(fetch_idx._process_is_chrome(gone))        # gone: stale
        self.assertTrue(calls)
        self.assertTrue(all(c[0] == "/bin/ps" for c in calls), calls)

    def test_port_must_be_1_to_65535(self):
        for value in ("0", "-1", "65536", "x"):
            with self.subTest(script="fetch_idx.py", value=value):
                r = subprocess.run(self.fetch_cmd("--port", value), capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn("--port", r.stderr)
            with self.subTest(script="run.py", value=value):
                r = subprocess.run([sys.executable, str(REPO / "run.py"), "--skip-master", "--skip-parse",
                                    "--data-dir", str(self.tmp / "data"), "--watchlist", str(self.tmp / "watchlist.csv"),
                                    "--chrome-path", str(self.chrome), "--port", value],
                                   capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn("--port", r.stderr)

    # --- a TERM that finds no Playwright call to cancel (stub Playwright, fake Chrome) -------------

    STUB_PLAYWRIGHT = textwrap.dedent(
        """\
        class _Chromium:
            def connect_over_cdp(self, url):
                if TERM_DURING_CONNECT:
                    os.kill(os.getpid(), signal.SIGTERM)
                    time.sleep(0.3)
                raise fetch_idx.PlaywrightError("stub connect failure")

        class _PW:
            chromium = _Chromium()

        class _SyncPlaywright:
            def __enter__(self):
                if TERM_DURING_START:
                    os.kill(os.getpid(), signal.SIGTERM)
                    time.sleep(0.3)
                return _PW()

            def __exit__(self, *a):
                return False

        fetch_idx.sync_playwright = _SyncPlaywright
        # As if the TERM landed inside Playwright's event loop while no call was in flight.
        fetch_idx._in_playwright_dispatcher = lambda: True
        """
    )

    def run_main_with_stub(self, prelude: str):
        (self.tmp / "watchlist.csv").write_text("ticker\nUNTR\n")
        args = ["--data-dir", str(self.tmp / "data"), "--watchlist", str(self.tmp / "watchlist.csv"),
                "--start-year", "2025", "--end-year", "2025", "--periods", "FY",
                "--port", str(self.port), "--chrome-path", str(self.chrome)]
        code = ("import os, signal, sys, time\nsys.path.insert(0, %r)\nimport fetch_idx\n" % str(REPO / "model")
                + textwrap.dedent(prelude) + self.STUB_PLAYWRIGHT
                + "sys.argv = ['fetch_idx.py'] + %r\nfetch_idx.main()\n" % args)
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)

    def started_chrome_pid(self, log: str) -> int:
        return int(re.search(r"open after [0-9.]+s \(pid (\d+)\)", log).group(1))

    def test_term_with_nothing_to_cancel_stops_before_the_next_call(self):
        r = self.run_main_with_stub("TERM_DURING_START, TERM_DURING_CONNECT = True, False\n")
        self.assertEqual(r.returncode, 143, r.stderr)
        self.assertIn("[WARNING]", r.stderr)
        self.assertIn("no browser call to cancel", r.stderr)
        self.assertNotIn("stub connect failure", r.stderr)  # connect_over_cdp never ran
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(pid_alive(self.started_chrome_pid(r.stderr)))
        self.assertEqual(self.singleton_files(self.tmp / "data" / "chrome_profile"), [])

    def test_term_then_a_connect_failure_exits_143_not_1(self):
        r = self.run_main_with_stub("TERM_DURING_START, TERM_DURING_CONNECT = False, True\n")
        self.assertEqual(r.returncode, 143, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(pid_alive(self.started_chrome_pid(r.stderr)))

    def test_a_failing_stop_chrome_after_a_term_keeps_exit_143(self):
        r = self.run_main_with_stub(
            "TERM_DURING_START, TERM_DURING_CONNECT = True, False\n"
            "_real_stop = fetch_idx.stop_chrome\n"
            "def _stop_then_fail(proc, profile_dir, **kw):\n"
            "    _real_stop(proc, profile_dir, **kw)\n"
            "    raise OSError('simulated failure while cleaning up')\n"
            "fetch_idx.stop_chrome = _stop_then_fail\n")
        self.assertEqual(r.returncode, 143, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("simulated failure while cleaning up", r.stderr)
        self.assertFalse(pid_alive(self.started_chrome_pid(r.stderr)))

    def test_term_during_connect_over_cdp_exits_143(self):
        os.environ["FAKE_CHROME_MODE"] = "hang_ws"
        log = self.tmp / "main.log"
        with open(log, "w") as fh:
            p = subprocess.Popen(self.fetch_cmd("--port", str(self.port), "--chrome-path", str(self.chrome)),
                                 stdout=subprocess.DEVNULL, stderr=fh)
        self.procs.append(p)
        marker = self.tmp / "data" / "chrome_profile" / "ws-requested"
        self.assertTrue(wait_until(marker.exists, 60), "connect_over_cdp never reached the fake:\n" + log.read_text())
        time.sleep(0.3)
        sent = time.monotonic()
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.fail("still running 30 s after TERM:\n" + log.read_text())
        text = log.read_text()
        self.assertEqual(p.returncode, 143, text)
        self.assertLess(time.monotonic() - sent, 10, text)
        self.assertNotIn("Traceback", text)
        self.assertFalse(pid_alive(self.started_chrome_pid(text)))
        self.assertEqual(self.singleton_files(self.tmp / "data" / "chrome_profile"), [])

    def test_start_timeout_must_be_positive(self):
        for value in ("0", "-5", "nan", "inf", "-inf", "abc"):
            with self.subTest(value=value):
                r = subprocess.run(self.fetch_cmd("--chrome-start-timeout", value),
                                   capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn("--chrome-start-timeout", r.stderr)

    def test_run_py_passes_term_and_hup_to_the_download_step(self):
        for sig, code_expected in ((signal.SIGTERM, 143), (signal.SIGHUP, 129)):
            with self.subTest(signal=sig):
                data = self.tmp / ("run-%d" % sig)
                (self.tmp / "watchlist.csv").write_text("ticker\nUNTR\n")
                p = subprocess.Popen(
                    [sys.executable, str(REPO / "run.py"), "--skip-master", "--skip-parse",
                     "--data-dir", str(data), "--watchlist", str(self.tmp / "watchlist.csv"),
                     "--chrome-path", str(self.chrome), "--port", str(self.port)],
                    env=dict(os.environ, FAKE_CHROME_DELAY="30"), stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True)
                self.procs.append(p)
                lock = data / "chrome_profile" / "SingletonLock"
                self.assertTrue(wait_until(lambda: os.path.lexists(lock), 30), "fake Chrome never started")
                chrome_pid = int(os.readlink(lock).rpartition("-")[2])
                p.send_signal(sig)
                out, _ = p.communicate(timeout=90)
                self.assertEqual(p.returncode, code_expected, out)
                self.assertIn("Stopped the Chrome this run started (pid %d)" % chrome_pid, out)
                self.assertFalse(pid_alive(chrome_pid))
                self.assertFalse(os.path.lexists(lock))
                left = subprocess.run(["pgrep", "-f", "fetch_idx.py --data-dir %s" % data],
                                      capture_output=True, text=True).stdout.strip()
                self.assertEqual(left, "", "fetch_idx.py still running")

    def test_run_py_installs_its_handlers_before_the_step_starts(self):
        """A TERM that arrives while Popen is still starting the step must not be lost."""
        run = importlib.import_module("run")
        saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in saved.items()])
        step = self.tmp / "step.py"
        step.write_text("import time\ntime.sleep(30)\n")
        real_popen = subprocess.Popen
        seen = {}

        def popen_with_a_signal_in_flight(*a, **kw):
            handler = signal.getsignal(signal.SIGTERM)
            seen["handler"] = handler
            proc = real_popen(*a, **kw)
            self.procs.append(proc)
            if callable(handler):
                handler(signal.SIGTERM, None)  # the TERM lands before run_step has the Popen object
            return proc

        with mock.patch.object(run.subprocess, "Popen", side_effect=popen_with_a_signal_in_flight):
            with self.assertRaises(SystemExit) as ctx:
                run.run_step("test step", str(step), [])
        self.assertTrue(callable(seen["handler"]), "SIGTERM had the default action while Popen ran")
        self.assertEqual(ctx.exception.code, 143)
        self.assertEqual(self.procs[-1].returncode, -signal.SIGTERM)

    # --- TERM while a real Playwright call is in flight (headless Google Chrome, local site) ------

    def start_real_main(self, site, prelude: str = ""):
        """fetch_idx.main() with headless Google Chrome against `site`; (process, log path)."""
        if not os.path.exists(REAL_CHROME):
            self.skipTest("Google Chrome not installed")
        wrapper = self.tmp / "headless-chrome"
        wrapper.write_text('#!/bin/sh\nexec "%s" --headless=new "$@"\n' % REAL_CHROME)
        wrapper.chmod(0o755)
        (self.tmp / "watchlist.csv").write_text("ticker\nUNTR\n")
        base = "http://127.0.0.1:%d" % site.port
        args = ["--data-dir", str(self.tmp / "data"), "--watchlist", str(self.tmp / "watchlist.csv"),
                "--start-year", "2025", "--end-year", "2025", "--periods", "FY",
                "--port", str(free_port_for_real_chrome()), "--chrome-path", str(wrapper)]
        code = ("import os, signal, sys, threading, time\nsys.path.insert(0, %r)\nimport fetch_idx\n"
                "fetch_idx.IDX_HOME = %r\nfetch_idx.IDX_URL_TEMPLATE = %r\n"
                % (str(REPO / "model"), base + "/home", base + "/file/{year}/{period_folder}/{ticker}/instance.zip")
                + textwrap.dedent(prelude)
                + "sys.argv = ['fetch_idx.py'] + %r\nfetch_idx.main()\n" % args)
        log = self.tmp / "main.log"
        with open(log, "w") as fh:  # Popen keeps its own copy of the descriptor
            p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=fh)
        self.procs.append(p)
        return p, log

    def run_main_until_blocked(self, site, blocked: threading.Event):
        """fetch_idx.main() against `site`; (process, Chrome pid, log path) once `blocked` is set."""
        p, log = self.start_real_main(site)
        self.assertTrue(blocked.wait(90), "the Playwright call never reached the site:\n" + log.read_text())
        time.sleep(0.5)
        chrome_pid = int(re.search(r"open after [0-9.]+s \(pid (\d+)\)", log.read_text()).group(1))
        return p, chrome_pid, log

    def assert_term_ends_it(self, p, chrome_pid, log):
        # Whatever happens, never leave the real Chrome behind.
        self.addCleanup(lambda: pid_alive(chrome_pid) and os.kill(chrome_pid, signal.SIGKILL))
        sent = time.monotonic()
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.fail("still running 15 s after TERM:\n" + log.read_text())
        text = log.read_text()
        print("\n[term->exit %.1fs] " % (time.monotonic() - sent) + " | ".join(
            l[11:] for l in text.splitlines()[-5:]), file=sys.stderr)
        self.assertEqual(p.returncode, 143, text)
        self.assertLess(time.monotonic() - sent, 10, text)  # 0.1-0.7 s measured; room for a loaded Mac
        self.assertNotIn("Traceback", text)
        self.assertIn("Stopped the Chrome this run started (pid %d)" % chrome_pid, text)
        self.assertFalse(pid_alive(chrome_pid))
        self.assertEqual(self.singleton_files(self.tmp / "data" / "chrome_profile"), [])

    def test_term_during_page_goto_exits_143_and_stops_chrome(self):
        site = SlowSite(block_home=True)  # open_idx_home's page.goto() is the call in flight
        self.addCleanup(site.close)
        self.assert_term_ends_it(*self.run_main_until_blocked(site, site.home_blocked))

    def test_term_during_page_evaluate_exits_143_and_stops_chrome(self):
        site = SlowSite(block_home=False)  # the download fetch, page.evaluate(), is in flight
        self.addCleanup(site.close)
        self.assert_term_ends_it(*self.run_main_until_blocked(site, site.file_blocked))

    def test_term_during_the_playwright_driver_start_exits_143_without_navigating(self):
        """A TERM while sync_playwright() starts its driver (nothing to cancel yet) must not let the
        next call, page.goto() to the IDX home, run: here that goto would block for 60 s."""
        site = SlowSite(block_home=True)
        self.addCleanup(site.close)
        sent_file = self.tmp / "term-sent"
        p, log = self.start_real_main(site, prelude=(
            "_real_sync_playwright = fetch_idx.sync_playwright\n"
            "class _SyncPlaywright:\n"
            "    def __enter__(self):\n"
            "        self.cm = _real_sync_playwright()\n"
            "        def term():\n"
            "            open(%r, 'w').write(repr(time.time()))\n"
            "            os.kill(os.getpid(), signal.SIGTERM)\n"
            "        threading.Timer(0.05, term).start()\n"
            "        return self.cm.__enter__()\n"
            "    def __exit__(self, *a):\n"
            "        return self.cm.__exit__(*a)\n"
            "fetch_idx.sync_playwright = _SyncPlaywright\n" % str(sent_file)))
        try:
            p.wait(timeout=180)
        except subprocess.TimeoutExpired:
            self.fail("still running:\n" + log.read_text())
        text = log.read_text()
        took = time.time() - float(sent_file.read_text())
        chrome_pid = int(re.search(r"open after [0-9.]+s \(pid (\d+)\)", text).group(1))
        self.addCleanup(lambda: pid_alive(chrome_pid) and os.kill(chrome_pid, signal.SIGKILL))
        self.assertEqual(p.returncode, 143, text)
        self.assertLess(took, 10, text)
        self.assertLessEqual(site.home_requests, 1, "page.goto() ran after the TERM:\n" + text)
        self.assertNotIn("Traceback", text)
        self.assertFalse(pid_alive(chrome_pid))
        self.assertEqual(self.singleton_files(self.tmp / "data" / "chrome_profile"), [])


if __name__ == "__main__":
    unittest.main()
