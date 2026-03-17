"""
ebay_watch.py

Watches a folder for new eBay orders report CSVs and automatically
runs ebay_shipping_merge to produce a combined packing slip PDF.

Usage:
    python -m utils.ebay_watch --watch "C:/Users/Alex/Downloads" --out "C:/Users/Alex/Documents/packing_slips"

The output filename mirrors the CSV name with a .pdf extension, e.g.:
    eBay-OrdersReport-Mar-16-2026-....csv  ->  eBay-OrdersReport-Mar-16-2026-....pdf
"""

import argparse
import os
import time
import logging

from watchdog.events import FileSystemEventHandler, FileCreatedEvent
from watchdog.observers import Observer

from utils.ebay_shipping_merge import extract_header, load_orders, build_pdf, HEADER_CACHE, DEFAULT_HEADER_INCHES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

EBAY_CSV_PREFIX = "eBay-OrdersReport"


class OrdersReportHandler(FileSystemEventHandler):

    def __init__(self, out_dir: str, header_height_pts: float):
        self.out_dir = out_dir
        self.header_height_pts = header_height_pts

    def on_created(self, event: FileCreatedEvent):
        if event.is_directory:
            return
        path = event.src_path
        filename = os.path.basename(path)
        if not (filename.startswith(EBAY_CSV_PREFIX) and filename.endswith(".csv")):
            return

        # Wait briefly for the download to finish writing
        time.sleep(1)

        log.info(f"Detected: {filename}")
        self._process(path, filename)

    def _process(self, csv_path: str, filename: str):
        if not os.path.exists(HEADER_CACHE):
            log.error(
                f"No cached header found ({HEADER_CACHE}). "
                "Run the script manually once with a template PDF to generate it."
            )
            return

        out_filename = os.path.splitext(filename)[0] + ".pdf"
        out_path = os.path.join(self.out_dir, out_filename)

        try:
            groups = load_orders(csv_path)
            multi = sum(1 for o in groups.values() if len(o) > 1)
            single = len(groups) - multi
            log.info(f"  {len(groups)} buyers: {multi} multi-order, {single} single-order")

            build_pdf(groups, HEADER_CACHE, out_path, self.header_height_pts)
            log.info(f"  Saved -> {out_path}")
        except Exception as e:
            log.error(f"  Failed to process {filename}: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Watch a folder for eBay orders report CSVs and auto-generate combined packing slip PDFs"
    )
    parser.add_argument(
        "--watch",
        default=r"C:\Users\Alex\Downloads",
        metavar="DIR",
        help="Folder to watch for new eBay orders report CSVs (default: Downloads)",
    )
    parser.add_argument(
        "--out",
        default=r"C:\Users\Alex\Documents\packing_slips",
        metavar="DIR",
        help="Folder to write combined packing slip PDFs into (default: Documents/packing_slips)",
    )
    parser.add_argument(
        "--header-height",
        type=float,
        default=DEFAULT_HEADER_INCHES,
        metavar="INCHES",
        help=f"Header crop height in inches (default: {DEFAULT_HEADER_INCHES})",
    )
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)

    if not os.path.exists(HEADER_CACHE):
        print(
            f"Warning: no cached header found ({HEADER_CACHE}). "
            "Run ebay_shipping_merge manually once with a template PDF before starting the watcher."
        )

    header_pts = args.header_height * 72
    handler = OrdersReportHandler(out_dir=args.out, header_height_pts=header_pts)

    observer = Observer()
    observer.schedule(handler, path=args.watch, recursive=False)
    observer.start()

    log.info(f"Watching: {args.watch}")
    log.info(f"Output:   {args.out}")
    log.info("Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()

    observer.join()


if __name__ == "__main__":
    main()
