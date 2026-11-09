"""Tests for the Chrome start and cleanup in model/fetch_idx.py and the signal hand-off in run.py.

No real Chrome and no network: a fake "Chrome" (a small Python script) takes the same arguments,
writes a SingletonLock like Chrome does and answers /json/version on the DevTools port, or not,
depending on FAKE_CHROME_MODE.

    python3 -m unittest discover -s tests -v
"""
import logging
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
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"Browser": "FakeChrome/1.0"}')

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
            fetch_idx.remove_stale_singleton(self.profile)
        self.assertEqual(self.singleton_files(), ["SingletonLock"])

    def test_live_chrome_with_a_relative_profile_path_is_refused(self):
        h = self.holder("chrome_profile", cwd=str(self.tmp))
        with self.assertRaisesRegex(fetch_idx.ChromeStartError, "pid %d" % h.pid):
            fetch_idx.remove_stale_singleton(self.profile)
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
                    fetch_idx.remove_stale_singleton(checked)
                self.assertEqual(self.singleton_files(real), ["SingletonLock"])
                h.kill()
                h.wait()
                (real / "SingletonLock").unlink()

    def test_live_chrome_with_a_trailing_slash_is_refused(self):
        h = self.holder(str(self.profile) + "/")
        with self.assertRaisesRegex(fetch_idx.ChromeStartError, "pid %d" % h.pid):
            fetch_idx.remove_stale_singleton(self.profile)

    def test_lock_changed_while_checked_is_left_in_place(self):
        self.plant_lock("%s-%d" % (socket.gethostname(), os.getpid()))
        replacement = "%s-%d" % (socket.gethostname(), 424242)

        def another_chrome_takes_the_profile(_pid):
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

    def test_start_timeout_must_be_positive(self):
        for value in ("0", "-5", "nan", "abc"):
            with self.subTest(value=value):
                r = subprocess.run(self.fetch_cmd("--chrome-start-timeout", value),
                                   capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn("--chrome-start-timeout", r.stderr)

    def test_run_py_passes_term_and_hup_to_the_download_step(self):
        # run.py has no --port: fetch_idx.py uses the default 9222, which must be free here.
        if fetch_idx.debug_port_is_open(fetch_idx.DEFAULT_DEBUG_PORT):
            self.skipTest("something listens on port %d" % fetch_idx.DEFAULT_DEBUG_PORT)
        for sig, code_expected in ((signal.SIGTERM, 143), (signal.SIGHUP, 129)):
            with self.subTest(signal=sig):
                data = self.tmp / ("run-%d" % sig)
                (self.tmp / "watchlist.csv").write_text("ticker\nUNTR\n")
                p = subprocess.Popen(
                    [sys.executable, str(REPO / "run.py"), "--skip-master", "--skip-parse",
                     "--data-dir", str(data), "--watchlist", str(self.tmp / "watchlist.csv"),
                     "--chrome-path", str(self.chrome)],
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


if __name__ == "__main__":
    unittest.main()
