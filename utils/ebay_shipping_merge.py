"""
ebay_shipping_merge.py

Consolidates eBay packing slips for buyers with multiple separate orders.
Each buyer gets one page (or as few pages as needed) showing all their orders,
with the eBay/store branding header preserved from a template packing slip PDF.

The extracted header is cached as header.png in the working directory so it only
needs to be re-parsed when the template changes.

Usage:
    # First run — extract header from template and cache it
    python -m utils.ebay_shipping_merge orders.csv template.pdf -o packing_slips.pdf

    # Subsequent runs — skip extraction, use cached header.png
    python -m utils.ebay_shipping_merge orders.csv -o packing_slips.pdf

    # Force re-extraction (e.g. after updating your store branding)
    python -m utils.ebay_shipping_merge orders.csv template.pdf --refresh-header

Note: The QR code in the extracted header is order-specific (from the template),
so treat it as decorative branding only on consolidated slips.
"""

import argparse
import re
from collections import defaultdict
from textwrap import wrap

import fitz  # PyMuPDF
import pandas as pd
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

MARGIN = 50
RIGHT_MARGIN = 562  # 612 - 50
LINE_H = 16
SMALL_H = 14
DEFAULT_HEADER_INCHES = 2.5
HEADER_CACHE = "header.png"

# Shipping constraints
PRICE_LIMIT = 20.0   # combined sold-for value above this requires non-envelope packaging
WEIGHT_TIER_CARDS = 7  # card count at or above this threshold = 2oz postage (vs 1oz)


# ---------------------------------------------------------------------------
# Header extraction
# ---------------------------------------------------------------------------


def extract_header(template_pdf: str, header_height_pts: float) -> str:
    """
    Crop the top `header_height_pts` points from the first page of template_pdf,
    save it to HEADER_CACHE, and return the cache path.
    """
    doc = fitz.open(template_pdf)
    page = doc[0]
    crop = fitz.Rect(0, 0, page.rect.width, header_height_pts)
    pix = page.get_pixmap(dpi=300, clip=crop)
    pix.save(HEADER_CACHE)
    doc.close()
    return HEADER_CACHE


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------

_ORDER_RE = re.compile(r"^\d{2}-\d{5}-\d{5}$")


def load_orders(csv_file: str) -> dict:
    """
    Load an eBay orders report CSV.

    eBay exports have a blank first line, then column headers, then data rows,
    then a footer ("X record(s) downloaded", "Seller ID: ..."). We skip the
    blank first line with skiprows=1, then filter to rows with valid order numbers.

    Returns:
        {buyer_username: {order_number: [row, ...]}}
    """
    df = pd.read_csv(csv_file, skiprows=1, dtype=str).fillna("")
    # Strip whitespace from every cell (pandas >=2.1 uses DataFrame.map)
    df = df.apply(lambda col: col.map(lambda x: x.strip() if isinstance(x, str) else x))
    # Keep only rows with a valid eBay order number (drops blank rows and footer)
    df = df[df["Order Number"].map(lambda v: bool(_ORDER_RE.match(v)))]

    groups: dict = defaultdict(lambda: defaultdict(list))
    for _, row in df.iterrows():
        groups[row["Buyer Username"]][row["Order Number"]].append(row)

    return groups


# ---------------------------------------------------------------------------
# PDF drawing helpers
# ---------------------------------------------------------------------------


_HIGHLIGHT = "Reverse Holo"
_HIGHLIGHT_PLACEHOLDER = "Reverse\x1fHolo"  # \x1f is not whitespace; prevents wrap splitting the phrase


def _draw_item_line(c: canvas.Canvas, x: float, y: float, text: str, size: int = 10) -> None:
    """Draw one wrapped line of an item title, bolding 'Reverse Holo' in place."""
    text = text.replace(_HIGHLIGHT_PLACEHOLDER, _HIGHLIGHT)  # restore before drawing
    idx = text.find(_HIGHLIGHT)
    if idx == -1:
        c.setFont("Helvetica", size)
        c.drawString(x, y, text)
        return
    before = text[:idx]
    after = text[idx + len(_HIGHLIGHT):]
    if before:
        c.setFont("Helvetica", size)
        c.drawString(x, y, before)
        x += pdfmetrics.stringWidth(before, "Helvetica", size)
    c.setFont("Helvetica-Bold", size)
    c.drawString(x, y, _HIGHLIGHT)
    x += pdfmetrics.stringWidth(_HIGHLIGHT, "Helvetica-Bold", size)
    c.setFont("Helvetica", size)
    if after:
        c.drawString(x, y, after)


def _hline(
    c: canvas.Canvas, y: float, x0: float = MARGIN, x1: float = RIGHT_MARGIN
) -> None:
    c.setStrokeColorRGB(0.75, 0.75, 0.75)
    c.line(x0, y, x1, y)
    c.setStrokeColorRGB(0, 0, 0)


def _draw_ship_to(c: canvas.Canvas, row, y: float) -> float:
    """Draw the ship-to address block. Returns the y position after the block."""
    addr2 = row.get("Ship To Address 2", "").strip()
    city = row.get("Ship To City", "").strip()
    state = row.get("Ship To State", "").strip()
    zip_ = row.get("Ship To Zip", "").strip()
    lines = [
        row.get("Ship To Name", "").strip(),
        row.get("Ship To Address 1", "").strip(),
        addr2,
        f"{city}, {state} {zip_}".strip(", "),
        row.get("Ship To Country", "").strip(),
    ]

    c.setFont("Helvetica-Bold", 11)
    c.drawString(MARGIN, y, "SHIP TO:")
    y -= LINE_H

    c.setFont("Helvetica", 11)
    for line in lines:
        if line and line not in (",", ", "):
            c.drawString(MARGIN, y, line)
            y -= SMALL_H

    return y - 6


# Column x positions for the unified items table
_COL_ORDER = MARGIN
_COL_ITEM = MARGIN + 85
_COL_QTY = MARGIN + 355
_COL_SKU = MARGIN + 380
_COL_PRICE = RIGHT_MARGIN

# Minimum y space needed to render the breakdown sub-table without overflow
_BREAKDOWN_H = SMALL_H * 4 + LINE_H + 14  # 3 breakdown rows + total row + padding


def _draw_table_header(c: canvas.Canvas, y: float) -> float:
    """Draw the column header row and rule. Returns y below the rule."""
    c.setFont("Helvetica-Bold", 10)
    c.drawString(_COL_ORDER, y, "Order #")
    c.drawString(_COL_ITEM, y, "Item")
    c.drawString(_COL_QTY, y, "Qty")
    c.drawString(_COL_SKU, y, "SKU")
    c.setFont("Helvetica-Bold", 9)
    c.drawRightString(_COL_PRICE, y, "Price (Incl. Sales)")
    y -= 3
    _hline(c, y)
    y -= SMALL_H
    return y


def _draw_breakdown(
    c: canvas.Canvas, y: float, shipping: float, tax: float, total: float
) -> float:
    """Draw the price breakdown sub-table. Returns y below it."""
    _hline(c, y, x0=_COL_QTY - 10)
    y -= SMALL_H

    c.setFont("Helvetica", 10)
    for label, value in [
        ("Shipping", f"${shipping:.2f}"),
        ("Sales tax (eBay collected)", f"${tax:.2f}"),
    ]:
        c.drawString(_COL_QTY - 10, y, label)
        c.drawRightString(_COL_PRICE, y, value)
        y -= SMALL_H

    y -= 2
    c.setFont("Helvetica-Bold", 10)
    c.drawString(_COL_QTY - 10, y, "Order total")
    c.drawRightString(_COL_PRICE, y, f"${total:.2f}")
    y -= LINE_H

    return y


# ---------------------------------------------------------------------------
# Shipment grouping
# ---------------------------------------------------------------------------


def _compute_order_metrics(orders: dict) -> dict:
    """
    Returns {order_num: (total_price, card_count)} for each order.

    The eBay CSV has a summary row (rows[0]) with aggregate Total Price and
    Quantity, followed by individual item rows. We use only rows[0] to avoid
    double-counting the summary against the per-item rows.

    total_price (subtotal + tax) is used to enforce the $20 plain-envelope
    limit. card_count drives the 1oz/2oz postage tier (WEIGHT_TIER_CARDS).
    """
    metrics: dict = {}
    for order_num, rows in orders.items():
        r0 = rows[0]
        try:
            total_price = float(r0.get("Total Price", "0").lstrip("$"))
        except ValueError:
            total_price = 0.0
        try:
            card_count = int(r0.get("Quantity", "0"))
        except ValueError:
            card_count = 0
        metrics[order_num] = (total_price, card_count)
    return metrics


def group_orders(orders: dict) -> list:
    """
    Partition a buyer's orders into the fewest valid shipment groups.

    Hard constraint: no group's combined Total Price may exceed PRICE_LIMIT.
    Secondary goal: among equal-count partitions, keep as many groups as
    possible under WEIGHT_TIER_CARDS (1oz vs 2oz postage threshold).

    Uses best-fit-decreasing bin packing (sort orders by price descending,
    then place each into the fullest bin that still has room; among equally
    valid bins, prefer ones where adding the order stays under the weight tier).

    Returns a list of order-dicts: [{order_num: rows, ...}, ...]
    """
    metrics = _compute_order_metrics(orders)
    order_nums = sorted(orders.keys(), key=lambda o: metrics[o][0], reverse=True)

    # Each bin: [price_used, card_count, {order_num: rows}]
    bins: list = []

    for order_num in order_nums:
        price, cards = metrics[order_num]

        candidates = [i for i, b in enumerate(bins) if b[0] + price <= PRICE_LIMIT]

        if not candidates:
            bins.append([price, cards, {order_num: orders[order_num]}])
            continue

        # Prefer bins where adding this order keeps card count under the weight threshold
        under = [i for i in candidates if bins[i][1] + cards < WEIGHT_TIER_CARDS]
        pool = under if under else candidates

        # Best-fit: pick the bin with the highest current price (least remaining space)
        best = max(pool, key=lambda i: bins[i][0])
        bins[best][0] += price
        bins[best][1] += cards
        bins[best][2][order_num] = orders[order_num]

    return [b[2] for b in bins]


# ---------------------------------------------------------------------------
# PDF builder
# ---------------------------------------------------------------------------


def build_pdf(
    groups: dict, header_img: str, output: str, header_height_pts: float
) -> None:
    c = canvas.Canvas(output, pagesize=letter)
    width, height = letter  # 612 × 792 points

    def start_page(first: bool) -> float:
        """Start a new canvas page. Draws the header only on the buyer's first page."""
        if first:
            c.drawImage(
                header_img,
                0,
                height - header_height_pts,
                width=width,
                height=header_height_pts,
                preserveAspectRatio=False,
            )
            return height - header_height_pts - 10
        else:
            return height - MARGIN

    for buyer_orders in groups.values():
        for orders in group_orders(buyer_orders):
            y = start_page(first=True)

            _hline(c, y)
            y -= LINE_H

            # Ship-to address — drawn once per shipment group
            first_row = next(iter(orders.values()))[0]
            y = _draw_ship_to(c, first_row, y)

            if len(orders) > 1:
                c.setFont("Helvetica-Oblique", 10)
                c.drawRightString(
                    RIGHT_MARGIN,
                    y + SMALL_H,
                    f"{len(orders)} separate orders \u2014 combined shipment",
                )

            _hline(c, y)
            y -= LINE_H

            y = _draw_table_header(c, y)

            # Accumulators for the single combined breakdown
            shipping = tax = total = 0.0

            for order_num, rows in orders.items():
                for row in rows:
                    # Overflow: ensure room for at least this row + the breakdown
                    if y < _BREAKDOWN_H + SMALL_H * 2:
                        c.showPage()
                        y = start_page(first=False)
                        y = _draw_table_header(c, y)

                    title = row.get("Item Title", "")
                    qty = row.get("Quantity", "")
                    sku = row.get("Custom Label", "")
                    price = row.get("Sold For", "").lstrip("$")

                    wrapped = wrap(title.replace(_HIGHLIGHT, _HIGHLIGHT_PLACEHOLDER), 44)
                    for i, line in enumerate(wrapped):
                        if i == 0:
                            c.setFont("Helvetica", 9)
                            c.drawString(_COL_ORDER, y, order_num)
                            _draw_item_line(c, _COL_ITEM, y, line)
                            c.drawString(_COL_QTY, y, qty)
                            c.drawString(_COL_SKU, y, sku)
                            c.drawRightString(_COL_PRICE, y, f"${price}")
                        else:
                            _draw_item_line(c, _COL_ITEM, y, line)
                        y -= SMALL_H

                # Accumulate per-order shipping and tax (one value per order row[0])
                r0 = rows[0]
                try:
                    shipping += float(r0.get("Shipping And Handling", "0").lstrip("$"))
                    tax += float(r0.get("eBay Collected Tax", "0").lstrip("$"))
                    total += float(r0.get("Total Price", "0").lstrip("$"))
                except ValueError:
                    pass

            y = _draw_breakdown(c, y, shipping, tax, total)

            c.showPage()

    c.save()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Consolidate eBay packing slips for buyers with multiple orders"
    )
    parser.add_argument("csv", help="eBay orders report CSV file")
    parser.add_argument(
        "template_pdf",
        nargs="?",
        help=(
            "A sample eBay packing slip PDF to extract the store/branding header from. "
            f"Required on first run or with --refresh-header; otherwise {HEADER_CACHE} is reused."
        ),
    )
    parser.add_argument(
        "-o", "--output", default="packing_slips.pdf", help="Output PDF filename"
    )
    parser.add_argument(
        "--refresh-header",
        action="store_true",
        help="Re-extract the header from template_pdf even if a cached header.png already exists",
    )
    parser.add_argument(
        "--header-height",
        type=float,
        default=DEFAULT_HEADER_INCHES,
        metavar="INCHES",
        help=f"Height (in inches) of header to crop from template PDF (default: {DEFAULT_HEADER_INCHES})",
    )
    args = parser.parse_args()

    import os

    needs_extract = args.refresh_header or not os.path.exists(HEADER_CACHE)

    if needs_extract:
        if not args.template_pdf:
            parser.error(
                f"template_pdf is required when {HEADER_CACHE} does not exist or --refresh-header is set"
            )
        header_pts = args.header_height * 72
        print(f'Extracting header ({args.header_height}") from {args.template_pdf} ...')
        header_img = extract_header(args.template_pdf, header_pts)
    else:
        header_pts = args.header_height * 72
        header_img = HEADER_CACHE
        print(f"Using cached header: {HEADER_CACHE}")

    print(f"Loading orders from {args.csv} ...")
    groups = load_orders(args.csv)

    slips = [group_orders(orders) for orders in groups.values()]
    total_slips = sum(len(s) for s in slips)
    split_buyers = sum(1 for s in slips if len(s) > 1)
    multi = sum(1 for orders in groups.values() if len(orders) > 1)
    single = len(groups) - multi
    print(
        f"  {len(groups)} buyers: {multi} with multiple orders, {single} with a single order"
    )
    print(
        f"  {total_slips} packing slip(s) total"
        + (f" ({split_buyers} buyer(s) split due to >${PRICE_LIMIT:.0f} limit)" if split_buyers else "")
    )

    print("Building PDF ...")
    build_pdf(groups, header_img, args.output, header_pts)
    print(f"Done -> {args.output}")


if __name__ == "__main__":
    main()
