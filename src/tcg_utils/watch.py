"""
watch.py

Watches a folder for new eBay orders report CSVs and automatically
runs merge.py to produce a combined packing slip PDF.

Usage (CLI):
    tcg-watch
    tcg-watch --watch "C:/Users/Alex/Downloads" --out "C:/Users/Alex/Documents/packing_slips"
    tcg-watch --header-height 3.0

Can also be driven programmatically via the Watcher class (used by the native host).
"""

import argparse
import logging
import os
import time
from pathlib import Path
from typing import Callable

from watchdog.events import FileCreatedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from tcg_utils.merge import (
    DEFAULT_HEADER_INCHES,
    build_pdf,
    get_header_cache,
    load_orders,
)

EBAY_CSV_PREFIX = "eBay-OrdersReport"


# ---------------------------------------------------------------------------
# Watchdog event handler
# ---------------------------------------------------------------------------


class _OrdersReportHandler(FileSystemEventHandler):
    def __init__(self, out_dir: str, header_height_pts: float, log_fn: Callable):
        self._out_dir = out_dir
        self._header_height_pts = header_height_pts
        self._log = log_fn

    def on_created(self, event: FileCreatedEvent):
        if event.is_directory:
            return
        path = event.src_path
        filename = os.path.basename(path)
        if not (filename.startswith(EBAY_CSV_PREFIX) and filename.endswith(".csv")):
            return

        # Wait briefly for the download to finish writing
        time.sleep(1)
        self._log(f"Detected: {filename}", "info")
        self._process(path, filename)

    def _process(self, csv_path: str, filename: str):
        header_cache = get_header_cache()
        if not header_cache.exists():
            self._log(
                f"No cached header found ({header_cache}). "
                "Run tcg-merge once with a template PDF to generate it.",
                "error",
            )
            return

        out_filename = os.path.splitext(filename)[0] + ".pdf"
        out_path = os.path.join(self._out_dir, out_filename)

        try:
            groups = load_orders(csv_path)
            multi = sum(1 for o in groups.values() if len(o) > 1)
            single = len(groups) - multi
            self._log(
                f"  {len(groups)} buyers: {multi} multi-order, {single} single-order",
                "info",
            )
            build_pdf(groups, str(header_cache), out_path, self._header_height_pts)
            self._log(f"  Saved -> {out_path}", "info")
        except Exception as e:
            self._log(f"  Failed to process {filename}: {e}", "error")


# ---------------------------------------------------------------------------
# Watcher class (programmatic interface used by the native host)
# ---------------------------------------------------------------------------


class Watcher:
    """
    Manages a watchdog Observer that processes eBay CSV files as they appear.

    Parameters
    ----------
    watch_dir : str
        Directory to monitor for new eBay order CSV files.
    output_dir : str
        Directory to write generated PDF packing slips into.
    header_height_pts : float
        Header crop height in PDF points (inches × 72).
    log_fn : callable, optional
        Callback ``log_fn(message: str, level: str)`` for status messages.
        Defaults to a no-op. Level is one of "info", "warning", "error".
    """

    def __init__(
        self,
        watch_dir: str,
        output_dir: str,
        header_height_pts: float,
        log_fn: Callable | None = None,
    ):
        self._watch_dir = watch_dir
        self._output_dir = output_dir
        self._header_height_pts = header_height_pts
        self._log = log_fn or (lambda msg, level="info": None)
        self._observer: Observer | None = None

    def start(self) -> None:
        """Start watching. Raises if already running."""
        if self.running:
            raise RuntimeError("Watcher is already running")

        Path(self._output_dir).mkdir(parents=True, exist_ok=True)

        handler = _OrdersReportHandler(
            out_dir=self._output_dir,
            header_height_pts=self._header_height_pts,
            log_fn=self._log,
        )
        self._observer = Observer()
        self._observer.schedule(handler, path=self._watch_dir, recursive=False)
        self._observer.start()

        self._log(f"Watching: {self._watch_dir}", "info")
        self._log(f"Output:   {self._output_dir}", "info")

    def stop(self) -> None:
        """Stop watching. Safe to call even if not running."""
        if self._observer is not None:
            self._observer.stop()
            self._observer.join()
            self._observer = None
            self._log("Watcher stopped.", "info")

    @property
    def running(self) -> bool:
        return self._observer is not None and self._observer.is_alive()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger(__name__)

    default_watch = str(Path.home() / "Downloads")
    default_out = str(Path.home() / "Documents" / "packing_slips")

    parser = argparse.ArgumentParser(
        description="Watch a folder for eBay orders report CSVs and auto-generate combined packing slip PDFs"
    )
    parser.add_argument(
        "--watch",
        default=default_watch,
        metavar="DIR",
        help=f"Folder to watch for new eBay orders report CSVs (default: {default_watch})",
    )
    parser.add_argument(
        "--out",
        default=default_out,
        metavar="DIR",
        help=f"Folder to write combined packing slip PDFs into (default: {default_out})",
    )
    parser.add_argument(
        "--header-height",
        type=float,
        default=DEFAULT_HEADER_INCHES,
        metavar="INCHES",
        help=f"Header crop height in inches (default: {DEFAULT_HEADER_INCHES})",
    )
    args = parser.parse_args()

    header_cache = get_header_cache()
    if not header_cache.exists():
        print(
            f"Warning: no cached header found ({header_cache}). "
            "Run 'tcg-merge' once with a template PDF before starting the watcher."
        )

    watcher = Watcher(
        watch_dir=args.watch,
        output_dir=args.out,
        header_height_pts=args.header_height * 72,
        log_fn=lambda msg, level: log.info(msg),
    )
    watcher.start()
    log.info("Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        watcher.stop()


if __name__ == "__main__":
    main()
