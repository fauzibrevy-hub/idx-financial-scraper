import argparse
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[0] / "data"

MASTER_SCRIPT = "model/master-data.py"
FETCH_SCRIPT = "model/fetch_idx.py"
PARSE_SCRIPT = "model/arelle_loader.py"
EXPORT_SCRIPT = "model/sqlite_export.py"
# How long a step may take to clean up after it was told to stop (fetch_idx.py stops its Chrome:
# up to 15 s, then 10 s after a SIGKILL).
STEP_STOP_TIMEOUT_SECONDS = 60
# model/backfill_cdp.py is NOT part of this pipeline: it needs a Chrome with a
# remote-debugging port already running, and drives CDP directly (see the module
# docstring). Run it separately to pull historical years, then re-run this
# pipeline so the new XBRL is parsed and exported.

# Environment override for the Chrome executable used by the IDX download step.
# Resolution order: --chrome-path flag, then this variable, then the standard
# install locations for the current platform.
CHROME_PATH_ENV = "IDX_CHROME_PATH"


def chrome_candidates() -> List[Path]:
    """Standard Chrome install locations for the current platform."""
    if sys.platform == "darwin":
        return [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    if sys.platform.startswith("linux"):
        names = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]
        return [Path(p) for p in (shutil.which(n) for n in names) if p]
    roots = [os.environ.get(v) for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    return [Path(r) / "Google" / "Chrome" / "Application" / "chrome.exe" for r in roots if r]


def normalize_chrome_path(path: Path) -> Path:
    """Expand ~ and map a macOS .app bundle to the executable inside it."""
    path = path.expanduser()
    if path.suffix == ".app" and path.is_dir():
        return path / "Contents" / "MacOS" / path.stem
    return path


def resolve_chrome_path(explicit: Optional[str]) -> Optional[Path]:
    """Return the Chrome executable to pass to the download step, or None.

    An explicit flag or environment value must point at an executable file; a
    wrong value is an error so a typo fails fast instead of falling back to a
    different browser. When neither is set, the first existing standard location
    wins. None means "let fetch_idx.py decide": it attaches to a Chrome already
    listening on the debug port without needing the path, and raises its own
    error otherwise.
    """
    env_value = os.environ.get(CHROME_PATH_ENV, "").strip()
    if explicit is not None and explicit.strip():
        chosen, source = Path(explicit.strip()), "--chrome-path"
    elif env_value:
        chosen, source = Path(env_value), CHROME_PATH_ENV
    else:
        chosen, source = None, ""
    if chosen is not None:
        chosen = normalize_chrome_path(chosen)
        if not chosen.is_file() or not os.access(chosen, os.X_OK):
            raise FileNotFoundError(f"Chrome executable from {source} not found or not executable: {chosen}")
        return chosen
    for candidate in chrome_candidates():
        if candidate.is_file():
            return candidate
    return None


def _wait_for_step(proc: subprocess.Popen) -> None:
    """Wait for a step that was told to stop; SIGKILL it after STEP_STOP_TIMEOUT_SECONDS."""
    try:
        proc.wait(timeout=STEP_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        logger.error("Step pid %d did not stop within %ss, killing it", proc.pid, STEP_STOP_TIMEOUT_SECONDS)
        proc.kill()
        proc.wait()


def run_step(name: str, script: str, args: list) -> None:
    command = [sys.executable, str(SCRIPT_DIR / script), *args]
    logger.info("Step started: %s", name)
    started = time.time()

    # A TERM or HUP to this process (deploy/scraper.sh, launchd) is passed on to the running step
    # as TERM, so it can clean up (fetch_idx.py stops the Chrome it started); this process waits
    # for it, up to STEP_STOP_TIMEOUT_SECONDS, and exits with 128 + the signal. Further TERM/HUP
    # are ignored meanwhile. The handlers are in place before the step starts, so a signal that
    # arrives while Popen runs is not lost. The handler only sends the signal: waiting inside it
    # would re-enter Popen.wait and deadlock on its lock, so the loop below does the waiting.
    received = []
    step = {"proc": None}

    def forward(signum, _frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        if received:
            return
        received.append(signum)
        logger.error("Received signal %d, stopping step '%s'", signum, name)
        if step["proc"] is not None and step["proc"].poll() is None:
            step["proc"].terminate()

    previous = {s: signal.signal(s, forward) for s in (signal.SIGTERM, signal.SIGHUP)}
    stop_deadline = None
    try:
        proc = subprocess.Popen(command)
        step["proc"] = proc
        if received and proc.poll() is None:
            proc.terminate()  # the signal came while the step was being started
        while proc.poll() is None:
            if received:
                if stop_deadline is None:
                    stop_deadline = time.monotonic() + STEP_STOP_TIMEOUT_SECONDS
                elif time.monotonic() > stop_deadline:
                    logger.error("Step '%s' did not stop within %ss, killing it", name, STEP_STOP_TIMEOUT_SECONDS)
                    proc.kill()
                    proc.wait()
                    break
            time.sleep(0.2)
    except KeyboardInterrupt:
        # Ctrl-C reaches the step too (same process group); let it finish its own cleanup.
        if step["proc"] is not None:
            _wait_for_step(step["proc"])
        raise
    finally:
        for s, handler in previous.items():
            if signal.getsignal(s) is forward:
                signal.signal(s, handler)
    if received:
        raise SystemExit(128 + received[0])
    returncode = proc.returncode
    elapsed = time.time() - started

    if returncode != 0:
        raise RuntimeError(f"Step '{name}' failed with exit code {returncode}")

    logger.info("Step finished: %s (%.1f seconds)", name, elapsed)


def validate_inputs(args: argparse.Namespace, watchlist_path: Path) -> None:
    needs_watchlist = not args.skip_master or not args.skip_fetch
    if needs_watchlist and not watchlist_path.exists():
        raise FileNotFoundError(f"Watchlist not found: {watchlist_path}")

    if not args.skip_parse:
        mappings_path = args.data_dir / "taxonomy.csv"
        if not mappings_path.exists():
            raise FileNotFoundError(f"Taxonomy mappings not found: {mappings_path}")

    if args.skip_master and not args.skip_parse:
        emitens_path = args.data_dir / "emitens.csv"
        if not emitens_path.exists():
            raise FileNotFoundError(
                f"Issuer list not found: {emitens_path}. Run the master data step first."
            )


def port_number(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port number: {value!r}") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"must be 1-65535, got {port}")
    return port


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the full pipeline: master data, IDX download, XBRL parsing."
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help="Base data directory (default: %(default)s)")
    parser.add_argument("--watchlist", type=Path, default=None,
                        help="CSV file with a 'ticker' column (default: <data-dir>/watchlist.csv)")
    parser.add_argument("--start-year", type=int, default=None,
                        help="First year for download and parsing")
    parser.add_argument("--end-year", type=int, default=None,
                        help="Last year for download and parsing")
    parser.add_argument("--chrome-path", type=str, default=None,
                        help=f"Chrome executable for the IDX download step "
                             f"(default: ${CHROME_PATH_ENV}, then the standard install locations)")
    parser.add_argument("--port", type=port_number, default=None,
                        help="Chrome remote debugging port for the IDX download step (default: the step's own)")
    parser.add_argument("--skip-master", action="store_true", help="Skip the master data step")
    parser.add_argument("--skip-fetch", action="store_true", help="Skip the IDX download step")
    parser.add_argument("--skip-parse", action="store_true", help="Skip the XBRL parsing step")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    watchlist_path = args.watchlist or (args.data_dir / "watchlist.csv")

    try:
        validate_inputs(args, watchlist_path)
        chrome_path = resolve_chrome_path(args.chrome_path) if not args.skip_fetch else None
    except FileNotFoundError as e:
        logger.error("%s", e)
        return 1

    base_args = ["--data-dir", str(args.data_dir)]
    watchlist_args = ["--watchlist", str(watchlist_path)]
    chrome_args = []
    if chrome_path is not None:
        logger.info("Using Chrome at %s", chrome_path)
        chrome_args = ["--chrome-path", str(chrome_path)]
    elif not args.skip_fetch:
        logger.warning(
            "Chrome not found in the standard locations; the download step will attach to a "
            "running Chrome on the debug port or fail. Set %s or pass --chrome-path.",
            CHROME_PATH_ENV,
        )
    if args.port is not None:
        chrome_args += ["--port", str(args.port)]

    year_args = []
    if args.start_year is not None:
        year_args += ["--start-year", str(args.start_year)]
    if args.end_year is not None:
        year_args += ["--end-year", str(args.end_year)]

    steps = []
    if not args.skip_master:
        steps.append(("Master data", MASTER_SCRIPT, base_args + watchlist_args))
    if not args.skip_fetch:
        steps.append(("IDX download", FETCH_SCRIPT, base_args + watchlist_args + year_args + chrome_args))
    if not args.skip_parse:
        steps.append(("XBRL parsing", PARSE_SCRIPT, base_args + year_args))
    # Export runs whenever parsing did: it is a pure, idempotent rebuild of
    # data/idx.sqlite from the CSV (plan idx-data-mc-design.md, Option A).
    if not args.skip_parse:
        steps.append(("SQLite export", EXPORT_SCRIPT, base_args))

    if not steps:
        logger.warning("All steps were skipped, nothing to do")
        return 0

    started = time.time()
    try:
        for name, script, step_args in steps:
            run_step(name, script, step_args)
    except RuntimeError as e:
        logger.error("%s", e)
        logger.error("Pipeline stopped")
        return 1
    except KeyboardInterrupt:
        logger.error("Pipeline interrupted")
        return 130

    logger.info("Pipeline finished in %.1f seconds", time.time() - started)
    logger.info("Output: %s", args.data_dir / "financial_reports.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())