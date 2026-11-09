import argparse
import base64
import csv
import io
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data"

IDX_HOME = "https://www.idx.co.id/id"

PERIODS = [
    ("TW1", "Q1"),
    ("TW2", "Q2"),
    ("TW3", "Q3"),
    ("Audit", "FY"),
]
PERIOD_TAGS = [tag for _, tag in PERIODS]

IDX_URL_TEMPLATE = (
    "https://www.idx.co.id/Portals/0/StaticData/ListedCompanies/Corporate_Actions/"
    "New_Info_JSX/Jenis_Informasi/01_Laporan_Keuangan/02_Soft_Copy_Laporan_Keuangan/"
    "/Laporan%20Keuangan%20Tahun%20{year}/{period_folder}/{ticker}/instance.zip"
)

CHROME_CANDIDATES = [
    os.environ.get("PROGRAMFILES", r"C:\Program Files") + r"\Google\Chrome\Application\chrome.exe",
    os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)") + r"\Google\Chrome\Application\chrome.exe",
    os.environ.get("LOCALAPPDATA", "") + r"\Google\Chrome\Application\chrome.exe",
]

DEFAULT_DEBUG_PORT = 9222
# A cold start of the real Chrome took 13 s on an idle Mac and more than 30 s on 2026-11-08 (first
# start after a Chrome update, under heavy load, as a background LaunchAgent). Waiting longer costs
# nothing when Chrome is fast; --chrome-start-timeout overrides it.
CHROME_START_TIMEOUT_SECONDS = 90
CHROME_STOP_TIMEOUT_SECONDS = 15
SINGLETON_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie")

REQUEST_DELAY_SECONDS = 2.0
PERIOD_DELAY_SECONDS = 10
PROGRESS_EVERY = 25
MAX_CHALLENGE_RETRIES = 2

FETCH_JS = """
async (url) => {
    const response = await fetch(url, { credentials: 'include' });
    const result = { status: response.status, b64: null };
    if (response.status === 200) {
        const bytes = new Uint8Array(await response.arrayBuffer());
        let binary = '';
        const chunk = 0x8000;
        for (let i = 0; i < bytes.length; i += chunk) {
            binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
        }
        result.b64 = btoa(binary);
    }
    return result;
}
"""


class BlockedError(RuntimeError):
    pass


class ChromeStartError(RuntimeError):
    pass


def debug_port_is_open(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2):
            return True
    except Exception:
        return False


def find_chrome(explicit_path):
    if explicit_path:
        path = Path(explicit_path)
        if not path.exists():
            raise FileNotFoundError(f"Chrome executable not found: {path}")
        return str(path)
    for candidate in CHROME_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
    raise FileNotFoundError(
        "Chrome not found in the standard locations. Pass --chrome-path with the full path to chrome.exe."
    )


def singleton_lock_owner(profile_dir: Path):
    """(host, pid) from the profile's SingletonLock, (target, None) when it cannot be parsed, or None.

    Chrome writes SingletonLock as a symlink to "<hostname>-<pid>" and removes it only on a normal
    quit; a Chrome stopped by a signal (launchd tearing down the job, a kill) leaves it behind.
    """
    try:
        target = os.readlink(profile_dir / "SingletonLock")
    except OSError:
        return None
    host, sep, pid = target.rpartition("-")
    if not sep or not host or not pid.isdigit():
        return (target, None)
    return (host, int(pid))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_is_chrome(pid: int) -> bool:
    """True when the live process `pid` is a Chrome or Chromium (or cannot be checked).

    Decided by the program, never by --user-data-dir: a Chrome may name the profile with a
    relative path, through a symlink (/tmp vs /private/tmp) or with a trailing slash. The program
    is the command line before its first option, so a Chrome started through a script or
    interpreter ("python3 .../fake-chrome --...") counts too.
    """
    try:
        args = subprocess.run(["ps", "-ww", "-o", "args=", "-p", str(pid)], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return True  # cannot tell: treat it as a Chrome rather than delete a live lock
    if not args:
        return False  # gone between the liveness check and ps
    program = args.split(" -", 1)[0].lower()
    return "chrome" in program or "chromium" in program


def _remove_singleton_if_unchanged(profile_dir: Path, target: str) -> bool:
    """Delete the Singleton files only while SingletonLock still points at `target`."""
    try:
        if os.readlink(profile_dir / "SingletonLock") != target:
            logger.warning("Chrome profile lock in %s changed while it was checked; left in place",
                           profile_dir)
            return False
    except OSError:
        return False  # already gone
    for name in SINGLETON_FILES:
        try:
            (profile_dir / name).unlink()
        except FileNotFoundError:
            pass
    return True


def remove_stale_singleton(profile_dir: Path) -> bool:
    """Remove a SingletonLock left by a Chrome that is gone; True when something was removed.

    Stale: the lock names this host and its pid is dead, or the pid now belongs to a process that
    is not Chrome or Chromium (pid reuse). Raises ChromeStartError when a live Chrome or Chromium
    has that pid (whatever profile path it was given), when the lock names another host, or when
    the lock cannot be read: those are left alone.
    """
    try:
        target = os.readlink(profile_dir / "SingletonLock")
    except OSError:
        return False
    owner = singleton_lock_owner(profile_dir)
    if owner is None:
        return False
    host, pid = owner
    if pid is None:
        raise ChromeStartError(
            f"Unreadable SingletonLock ({host!r}) in {profile_dir}; quit any Chrome using this profile, "
            "then delete SingletonLock, SingletonSocket and SingletonCookie there."
        )
    if host != socket.gethostname():
        raise ChromeStartError(
            f"The Chrome profile {profile_dir} is locked by host {host} (pid {pid}), not this Mac; "
            "quit Chrome there or delete its SingletonLock."
        )
    if _pid_alive(pid) and _process_is_chrome(pid):
        raise ChromeStartError(
            f"Chrome pid {pid} holds the profile lock in {profile_dir} and is still running, but "
            "nothing answers on the debug port; quit that Chrome and run again."
        )
    if not _remove_singleton_if_unchanged(profile_dir, target):
        return False
    logger.info("Removed a stale Chrome profile lock (pid %d is dead or reused)", pid)
    return True


def stop_chrome(proc, profile_dir: Path, timeout: float = CHROME_STOP_TIMEOUT_SECONDS) -> None:
    """Stop a Chrome that ensure_chrome_running started (None: nothing was started, nothing to do).

    SIGTERM, then SIGKILL after `timeout` seconds; then the lock it leaves behind is removed.
    """
    if proc is None:
        return
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("Chrome pid %d did not stop within %ss, killing it", proc.pid, timeout)
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                logger.error("Chrome pid %d still running 10 s after SIGKILL; profile lock left in place",
                             proc.pid)
                return
        logger.info("Stopped the Chrome this run started (pid %d)", proc.pid)
    try:
        remove_stale_singleton(profile_dir)
    except ChromeStartError as e:
        logger.warning("Chrome profile lock left in place: %s", e)


def ensure_chrome_running(port: int, profile_dir: Path, chrome_path,
                          timeout: float = CHROME_START_TIMEOUT_SECONDS):
    """Attach to a Chrome on `port`, or start one; the started process, or None when attached.

    The caller stops a started Chrome with stop_chrome(). On any failure here the Chrome this call
    started is stopped before ChromeStartError is raised, so no window and no lock is left behind.
    """
    if debug_port_is_open(port):
        logger.info("Chrome already listening on debug port %d, attaching", port)
        return None

    try:
        exe = find_chrome(chrome_path)
    except FileNotFoundError as e:
        raise ChromeStartError(str(e)) from None
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ChromeStartError(f"Cannot create the Chrome profile folder {profile_dir}: {e}") from None
    remove_stale_singleton(profile_dir)

    logger.info("Starting Chrome with profile %s", profile_dir)
    started = time.monotonic()
    proc = subprocess.Popen(
        [
            exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            IDX_HOME,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        while time.monotonic() - started < timeout:
            if debug_port_is_open(port):
                logger.info("Chrome debug port %d open after %.1fs (pid %d)",
                            port, time.monotonic() - started, proc.pid)
                return proc
            code = proc.poll()
            if code is not None:
                raise ChromeStartError(
                    f"Chrome (pid {proc.pid}) exited with code {code} after {time.monotonic() - started:.1f}s "
                    f"before opening debug port {port}. Profile: {profile_dir}."
                )
            time.sleep(0.5)
        raise ChromeStartError(
            f"Chrome (pid {proc.pid}) did not open debug port {port} within {timeout:g}s; stopped it. "
            "The Mac may be busy (another render or build), asleep, or this is the first start after a "
            "Chrome update; run again, or raise --chrome-start-timeout."
        )
    except BaseException:
        stop_chrome(proc, profile_dir)
        raise


def install_signal_handlers() -> None:
    """SIGTERM and SIGHUP raise SystemExit (143, 129) so `finally` blocks run and stop Chrome.

    After the first one both are ignored: a second TERM must not interrupt the cleanup it started.
    """
    def handler(signum, _frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGHUP, handler)


def load_watchlist(path: Path) -> list:
    if not path.exists():
        raise FileNotFoundError(
            f"Watchlist not found: {path}. Create a CSV file with a 'ticker' header."
        )

    tickers = []
    seen = set()
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "ticker" not in reader.fieldnames:
            raise ValueError(f"{path} must contain a 'ticker' column")
        for row in reader:
            ticker = (row["ticker"] or "").strip().upper()
            if ticker and ticker not in seen:
                seen.add(ticker)
                tickers.append(ticker)
    return tickers


def log_failed_download(log_path: Path, ticker: str, year: int, period_tag: str, reason: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    is_new_file = not log_path.exists()
    with open(log_path, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if is_new_file:
            writer.writerow(["ticker", "year", "period", "reason", "logged_at"])
        writer.writerow([ticker, year, period_tag, reason, datetime.now().isoformat()])


def extract_xbrl(content: bytes):
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        names = z.namelist()
        xbrl_name = next((n for n in names if n.lower().endswith(".xbrl")), None)
        if xbrl_name is None:
            xbrl_name = next((n for n in names if n.lower().endswith(".xml")), None)
        return z.read(xbrl_name) if xbrl_name else None


def open_idx_home(page) -> None:
    try:
        page.goto(IDX_HOME, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        logger.warning("Navigation to IDX home did not finish cleanly: %s", e)


def page_is_challenge(page) -> bool:
    try:
        title = (page.title() or "").lower()
    except Exception:
        return False
    return "just a moment" in title or "attention required" in title


def wait_for_manual_challenge(page) -> None:
    if not sys.stdin or not sys.stdin.isatty():
        raise BlockedError(
            "Cloudflare challenge detected and no interactive terminal is available. "
            "Run this script directly from a terminal once to clear the challenge."
        )

    open_idx_home(page)
    print()
    print("Cloudflare verification may be required.")
    print("In the Chrome window, make sure the IDX website loads normally.")
    answer = input("Press Enter to continue, or type q to quit: ").strip().lower()
    if answer == "q":
        raise BlockedError("Stopped by user at the verification prompt.")


def fetch_in_browser(page, url: str):
    result = page.evaluate(FETCH_JS, url)
    status = result["status"]
    content = base64.b64decode(result["b64"]) if result.get("b64") else None
    return status, content


def bulk_download(
    page,
    tickers: list,
    xbrl_dir: Path,
    failed_log_path: Path,
    year: int,
    period_folder: str,
    period_tag: str,
) -> None:
    logger.info("Starting download: year=%s period=%s tickers=%d", year, period_tag, len(tickers))

    target_dir = xbrl_dir / str(year) / period_tag
    counts = {"saved": 0, "skipped_existing": 0, "not_found": 0, "blocked": 0, "http_error": 0, "errors": 0}

    for index, ticker in enumerate(tickers, start=1):
        target_file = target_dir / f"{ticker}_{year}_{period_tag}.xbrl"

        if target_file.exists():
            counts["skipped_existing"] += 1
            continue

        url = IDX_URL_TEMPLATE.format(year=year, period_folder=period_folder, ticker=ticker)
        challenge_attempts = 0

        while True:
            try:
                status, content = fetch_in_browser(page, url)
            except Exception as e:
                logger.error("Failed to download %s: %s", ticker, e)
                log_failed_download(failed_log_path, ticker, year, period_tag, str(e))
                counts["errors"] += 1
                break

            is_zip = content is not None and content[:2] == b"PK"

            if status == 200 and is_zip:
                try:
                    xbrl_bytes = extract_xbrl(content)
                except zipfile.BadZipFile as e:
                    logger.error("%s: invalid zip archive: %s", ticker, e)
                    log_failed_download(failed_log_path, ticker, year, period_tag, "bad_zip")
                    counts["errors"] += 1
                    break

                if xbrl_bytes is None:
                    logger.warning("%s: zip archive contained no XBRL file", ticker)
                    log_failed_download(failed_log_path, ticker, year, period_tag, "no_xbrl_in_zip")
                    counts["errors"] += 1
                else:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    target_file.write_bytes(xbrl_bytes)
                    logger.info("Saved %s [%s %s]", ticker, period_tag, year)
                    counts["saved"] += 1
                break

            if status == 404:
                counts["not_found"] += 1
                break

            if status in (403, 429) or status == 200:
                if challenge_attempts >= MAX_CHALLENGE_RETRIES:
                    logger.warning("%s: still blocked after %d verification attempts", ticker, challenge_attempts)
                    log_failed_download(failed_log_path, ticker, year, period_tag, f"blocked_http_{status}")
                    counts["blocked"] += 1
                    raise BlockedError(f"Repeated blocking at {ticker} [{period_tag} {year}].")
                logger.warning("%s: blocked (HTTP %s)", ticker, status)
                wait_for_manual_challenge(page)
                challenge_attempts += 1
                continue

            logger.warning("%s: unexpected HTTP %s", ticker, status)
            log_failed_download(failed_log_path, ticker, year, period_tag, f"http_{status}")
            counts["http_error"] += 1
            break

        if index % PROGRESS_EVERY == 0:
            logger.info("Progress %s %s: %d/%d tickers", period_tag, year, index, len(tickers))

        time.sleep(REQUEST_DELAY_SECONDS)

    logger.info(
        "Finished year=%s period=%s: %d saved, %d already on disk, %d not found (404), "
        "%d blocked, %d other HTTP errors, %d errors",
        year, period_tag, counts["saved"], counts["skipped_existing"], counts["not_found"],
        counts["blocked"], counts["http_error"], counts["errors"],
    )


def parse_args() -> argparse.Namespace:
    current_year = datetime.now().year
    parser = argparse.ArgumentParser(description="Download IDX XBRL filings for a watchlist.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help="Base data directory (default: %(default)s)")
    parser.add_argument("--watchlist", type=Path, default=None,
                        help="CSV file with a 'ticker' column (default: <data-dir>/watchlist.csv)")
    parser.add_argument("--start-year", type=int, default=2023)
    parser.add_argument("--end-year", type=int, default=current_year)
    parser.add_argument("--tickers", nargs="+", default=None,
                        help="Optional subset of tickers, e.g. --tickers AALI ADRO")
    parser.add_argument("--periods", nargs="+", choices=PERIOD_TAGS, default=None,
                        help="Optional subset of periods, e.g. --periods Q1 FY")
    parser.add_argument("--port", type=int, default=DEFAULT_DEBUG_PORT,
                        help="Chrome remote debugging port (default: %(default)s)")
    parser.add_argument("--chrome-path", type=str, default=None,
                        help="Full path to chrome.exe if it is not in a standard location")
    parser.add_argument("--chrome-start-timeout", type=positive_seconds, default=CHROME_START_TIMEOUT_SECONDS,
                        help="Seconds to wait for a started Chrome to open its debug port (default: %(default)s)")
    return parser.parse_args()


def positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from None
    if not seconds > 0:  # also rejects nan
        raise argparse.ArgumentTypeError(f"must be more than 0 seconds, got {value}")
    return seconds


def main() -> None:
    args = parse_args()

    data_dir = args.data_dir
    watchlist_path = args.watchlist or (data_dir / "watchlist.csv")
    xbrl_dir = data_dir / "XBRL"
    failed_log_path = data_dir / "failed_downloads.csv"
    profile_dir = data_dir / "chrome_profile"

    tickers = load_watchlist(watchlist_path)
    if args.tickers:
        wanted = {t.strip().upper() for t in args.tickers}
        tickers = [t for t in tickers if t in wanted]
    if not tickers:
        logger.warning("Watchlist is empty, nothing to download.")
        return

    periods = [p for p in PERIODS if args.periods is None or p[1] in args.periods]

    logger.info("Loaded %d tickers from %s", len(tickers), watchlist_path)

    # A TERM or HUP (launchd stopping the job, deploy/scraper.sh forwarding a signal) must still
    # stop the Chrome this run started: as SystemExit it runs the `finally` below.
    install_signal_handlers()
    try:
        chrome = ensure_chrome_running(args.port, profile_dir, args.chrome_path,
                                       timeout=args.chrome_start_timeout)
    except ChromeStartError as e:
        logger.error("%s", e)
        sys.exit(1)

    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.pages[0] if context.pages else context.new_page()
            except PlaywrightError as e:
                first_line = (str(e).strip().splitlines() or ["no detail"])[0]
                logger.error("Could not connect to Chrome on debug port %d: %s", args.port, first_line)
                sys.exit(1)

            try:
                if "idx.co.id" not in page.url or page_is_challenge(page):
                    open_idx_home(page)

                if page_is_challenge(page):
                    wait_for_manual_challenge(page)

                for year in range(args.start_year, args.end_year + 1):
                    for period_folder, period_tag in periods:
                        bulk_download(
                            page, tickers, xbrl_dir, failed_log_path,
                            year, period_folder, period_tag,
                        )
                        logger.info("Pausing before next period")
                        time.sleep(PERIOD_DELAY_SECONDS)

                logger.info("All periods from %s to %s processed", args.start_year, args.end_year)
            except BlockedError as e:
                logger.error("%s Re-run later; existing files are skipped automatically.", e)
            except KeyboardInterrupt:
                logger.error("Interrupted by user; existing files are skipped on the next run.")
            finally:
                browser.close()  # over CDP this only disconnects; Chrome keeps running
    finally:
        stop_chrome(chrome, profile_dir)

    if failed_log_path.exists():
        logger.info("Some downloads failed; see %s", failed_log_path)


if __name__ == "__main__":
    main()
