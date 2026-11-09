"""Tests for the Chrome start and cleanup in model/fetch_idx.py.

No real Chrome and no network: a fake "Chrome" (a small Python script) takes the same arguments,
writes a SingletonLock like Chrome does and answers /json/version on the DevTools port, or not,
depending on FAKE_CHROME_MODE.

    python3 -m unittest discover -s tests -v
"""
import os
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model"))

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

    http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
    """
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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


class ChromeStartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="chrome-start-test-")).resolve()
        self.profile = self.tmp / "chrome_profile"
        self.chrome = self.tmp / "fake-chrome"
        self.chrome.write_text(FAKE_CHROME)
        self.chrome.chmod(0o755)
        self.port = free_port()
        self.procs = []
        self.env = mock.patch.dict(os.environ, {"FAKE_CHROME_MODE": "ok", "FAKE_CHROME_DELAY": "0"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait()
        subprocess.run(["rm", "-rf", str(self.tmp)], check=False)

    def plant_lock(self, target: str) -> None:
        self.profile.mkdir(parents=True, exist_ok=True)
        os.symlink(target, self.profile / "SingletonLock")
        os.symlink("/nonexistent/SingletonSocket", self.profile / "SingletonSocket")
        os.symlink("123456789", self.profile / "SingletonCookie")

    def singleton_files(self):
        return sorted(n for n in ("SingletonLock", "SingletonSocket", "SingletonCookie")
                      if os.path.lexists(self.profile / n))

    def start(self, timeout=10):
        proc = fetch_idx.ensure_chrome_running(self.port, self.profile, str(self.chrome), timeout=timeout)
        if proc is not None:
            self.procs.append(proc)
        return proc

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

    def test_lock_of_a_live_pid_that_is_not_chrome_on_this_profile_is_stale(self):
        # pid reuse: the number in the lock now belongs to an unrelated process (this test runner)
        self.plant_lock("%s-%d" % (socket.gethostname(), os.getpid()))
        self.assertTrue(fetch_idx.remove_stale_singleton(self.profile))
        self.assertEqual(self.singleton_files(), [])

    def test_lock_of_a_live_chrome_on_this_profile_is_refused(self):
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                                   "--user-data-dir=%s" % self.profile])
        self.procs.append(holder)
        self.plant_lock("%s-%d" % (socket.gethostname(), holder.pid))
        with self.assertRaisesRegex(fetch_idx.ChromeStartError, "pid %d" % holder.pid):
            fetch_idx.remove_stale_singleton(self.profile)
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

    def test_attaches_to_a_chrome_already_on_the_port_and_never_stops_it(self):
        os.environ["FAKE_CHROME_MODE"] = "ok"
        other = subprocess.Popen([str(self.chrome), "--remote-debugging-port=%d" % self.port,
                                  "--user-data-dir=%s" % (self.tmp / "other")])
        self.procs.append(other)
        for _ in range(100):
            if fetch_idx.debug_port_is_open(self.port):
                break
            time.sleep(0.1)
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

    # --- signals -------------------------------------------------------------------------------

    def test_sigterm_and_sighup_become_systemexit_so_cleanup_runs(self):
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
                print("cleanup ran", flush=True)
            """ % str(Path(fetch_idx.__file__).parent)
        )
        for sig, code_expected in ((signal.SIGTERM, 143), (signal.SIGHUP, 129)):
            p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
            self.procs.append(p)
            self.assertEqual(p.stdout.readline().strip(), "ready")
            p.send_signal(sig)
            out, _ = p.communicate(timeout=10)
            self.assertIn("cleanup ran", out)
            self.assertEqual(p.returncode, code_expected)

    def test_default_start_timeout_is_longer_than_the_old_30s(self):
        self.assertGreaterEqual(fetch_idx.CHROME_START_TIMEOUT_SECONDS, 90)


if __name__ == "__main__":
    unittest.main()
