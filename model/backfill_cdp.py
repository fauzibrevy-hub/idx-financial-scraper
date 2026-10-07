"""Backfill IDX XBRL filings for a year range via a running Chrome's CDP.

Why this exists: idx.co.id sits behind a WAF that returns 403 to plain HTTP
clients (curl/requests), but a real Chrome session fetches the same static URLs
with 200. Playwright's connect_over_cdp refuses this Chrome
("Browser context management is not supported"), so this script drives the CDP
websocket directly and runs `fetch(..., {credentials:'include'})` inside the page.

It only downloads — parsing is still `model/arelle_loader.py`.

Usage:
    python3 model/backfill_cdp.py --start-year 2020 --end-year 2022
    python3 model/backfill_cdp.py --start-year 2020 --end-year 2022 --tickers UNTR MSTI
"""
import argparse
import base64
import json
import logging
import time
import urllib.request
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
SCHEMA_VERSION_URL = Path(__file__).resolve().parents[1] / "Taxonomy.xsd"

PERIODS = [("TW1", "Q1"), ("TW2", "Q2"), ("TW3", "Q3"), ("Audit", "FY")]

IDX_URL_TEMPLATE = (
    "https://www.idx.co.id/Portals/0/StaticData/ListedCompanies/Corporate_Actions/"
    "New_Info_JSX/Jenis_Informasi/01_Laporan_Keuangan/02_Soft_Copy_Laporan_Keuangan/"
    "/Laporan%20Keuangan%20Tahun%20{year}/{period_folder}/{ticker}/instance.zip"
)

DEFAULT_DEBUG_PORT = 9222
REQUEST_DELAY_SECONDS = 1.5
PROGRESS_EVERY = 25


def cdp_endpoint(port: int) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3) as r:
        return json.load(r)["webSocketDebuggerUrl"]


def cdp_page(port: int) -> str:
    """Reuse an existing page target, or open a blank one."""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as r:
        targets = json.load(r)
    for t in targets:
        if t.get("type") == "page":
            return t["webSocketDebuggerUrl"]
    req = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT")
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.load(r)["webSocketDebuggerUrl"]

def _extract_instance(blob: bytes) -> bytes | None:
    """Return the XBRL XML from an IDX `instance.zip` payload.

    IDX serves `instance.zip` containing `instance.xbrl`; the rest of the repo
    stores the extracted XML at data/XBRL/.../*.xbrl. If the payload is already
    XML (some endpoints serve it directly), return it unchanged.
    """
    if blob[:2] == b"PK":  # zip magic
        import io
        import zipfile
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".xbrl")]
                if not names:
                    return None
                # Prefer the canonical instance.xbrl.
                pick = next((n for n in names if n.lower().endswith("instance.xbrl")), names[0])
                return zf.read(pick)
        except zipfile.BadZipFile:
            return None
    return blob


class CDP:
    """Minimal CDP client over a websocket, using only stdlib + a tiny shim."""

    def __init__(self, ws_url: str):
        # macOS ships no stdlib websocket client; use the `websockets` package
        # when present, else fall back to a raw socket implementation.
        try:
            import websockets  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "The 'websockets' package is required for backfill_cdp.py "
                "(pip install websockets)."
            ) from e
        self._websockets = websockets
        self._ws_url = ws_url
        self._conn = None
        self._id = 0
        self._loop = None

    async def __aenter__(self):
        import asyncio
        self._loop = asyncio.get_event_loop()
        self._conn = await self._websockets.connect(self._ws_url, max_size=None)
        return self

    async def __aexit__(self, *a):
        if self._conn:
            await self._conn.close()

    async def call(self, method: str, params: dict | None = None):
        self._id += 1
        mid = self._id
        await self._conn.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            raw = await self._conn.recv()
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} failed: {msg['error']}")
                return msg.get("result", {})

    async def evaluate(self, expression: str):
        res = await self.call("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
        })
        return res.get("result", {}).get("value")


async def download_one(cdp: CDP, ticker: str, url: str) -> bytes | None:
    expr = f"""(async () => {{
        try {{
            const r = await fetch({json.dumps(url)}, {{credentials:'include'}});
            if (r.status !== 200) return JSON.stringify({{status: r.status}});
            const b = await r.arrayBuffer();
            let bin = ''; const bytes = new Uint8Array(b); const chunk = 0x8000;
            for (let i = 0; i < bytes.length; i += chunk) {{
                bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
            }}
            return JSON.stringify({{status: 200, b64: btoa(bin)}});
        }} catch (e) {{ return JSON.stringify({{status: -1, err: String(e)}}); }}
    }})()"""
    raw = await cdp.evaluate(expr)
    if not raw:
        return None
    data = json.loads(raw)
    if data.get("status") != 200 or not data.get("b64"):
        return None
    return base64.b64decode(data["b64"])


def load_watchlist(path: Path) -> list[str]:
    import csv
    tickers = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            t = (row.get("ticker") or "").strip().upper()
            if t:
                tickers.append(t)
    return tickers


async def run(args):
    data_dir = args.data_dir
    xbrl_dir = data_dir / "XBRL"
    watchlist_path = args.watchlist or (data_dir / "watchlist.csv")
    failed_path = data_dir / "failed_downloads.csv"

    tickers = load_watchlist(watchlist_path)
    if args.tickers:
        wanted = {t.upper() for t in args.tickers}
        tickers = [t for t in tickers if t in wanted]
    if not tickers:
        logger.warning("No tickers to process.")
        return

    import csv as _csv
    failed = open(failed_path, "a", newline="", encoding="utf-8")
    failed_writer = _csv.writer(failed)

    ws_url = cdp_page(args.port)
    downloaded = skipped = missing = 0

    async with CDP(ws_url) as cdp:
        # Establish the IDX session in this tab (cookies) before fetching files.
        await cdp.call("Page.navigate", {"url": "https://www.idx.co.id/id"})
        import asyncio as _asyncio
        await _asyncio.sleep(6)

        for year in range(args.start_year, args.end_year + 1):
            for period_folder, period_tag in PERIODS:
                for ticker in tickers:
                    # Store under the canonical period folder the parser expects
                    # (Q1/Q2/Q3/FY), not IDX's own TW1/TW2/TW3/Audit labels — the
                    # existing 2023+ tree uses the canonical names.
                    dest = xbrl_dir / str(year) / period_tag / f"{ticker}_{year}_{period_tag}.xbrl"
                    if dest.exists() and not args.force:
                        skipped += 1
                        continue
                    url = IDX_URL_TEMPLATE.format(
                        year=year, period_folder=period_folder, ticker=ticker
                    )
                    body = await download_one(cdp, ticker, url)
                    if not body:
                        missing += 1
                        failed_writer.writerow([ticker, year, period_tag, url])
                        continue
                    # instance.zip wraps the real filing: it contains
                    # `instance.xbrl` (+ Taxonomy.xsd). Existing data/XBRL files
                    # are the extracted XML, so unwrap here to match that shape.
                    xml = _extract_instance(body)
                    if xml is None:
                        missing += 1
                        failed_writer.writerow([ticker, year, period_tag, url, "no instance.xbrl in zip"])
                        logger.warning("%s %s %s: zip had no instance.xbrl", ticker, year, period_tag)
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(xml)
                    downloaded += 1
                    if downloaded % PROGRESS_EVERY == 0:
                        logger.info("Downloaded %d so far…", downloaded)
                    await _asyncio.sleep(REQUEST_DELAY_SECONDS)
                logger.info("Finished %s %s", year, period_folder)

    failed.close()
    logger.info(
        "Backfill done: %d downloaded, %d already present, %d missing/blocked",
        downloaded, skipped, missing,
    )
    if missing:
        logger.info("Missing entries logged to %s", failed_path)


def main():
    parser = argparse.ArgumentParser(description="Backfill IDX XBRL via Chrome CDP")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--watchlist", type=Path, default=None)
    parser.add_argument("--start-year", type=int, required=True)
    parser.add_argument("--end-year", type=int, required=True)
    parser.add_argument("--tickers", nargs="+", default=None)
    parser.add_argument("--port", type=int, default=DEFAULT_DEBUG_PORT)
    parser.add_argument("--force", action="store_true", help="Re-download existing files")
    args = parser.parse_args()

    import asyncio
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
