#!/usr/bin/env python3

import argparse
import csv
import sys
import time
import urllib.request
from pathlib import Path
from io import StringIO
from utils import SHEET_URL

# ---------- CONFIG ----------
CACHE_FILE = Path(".shipping_cost_cache.csv")
CACHE_TTL = 24 * 60 * 60  # 24 hours

# ---------- COST TABLE LOADER ----------
def load_cost_table(csv_url, force_refresh=False):
    """Load cost table from Google Sheets CSV, with caching."""
    now = time.time()

    def cache_is_valid():
        return CACHE_FILE.exists() and (now - CACHE_FILE.stat().st_mtime) < CACHE_TTL

    data = None

    if not force_refresh and cache_is_valid():
        data = CACHE_FILE.read_text()
    else:
        try:
            with urllib.request.urlopen(csv_url, timeout=10) as response:
                data = response.read().decode("utf-8")
            CACHE_FILE.write_text(data)
        except Exception as e:
            if CACHE_FILE.exists():
                print("WARNING: Network error — using cached cost data")
                data = CACHE_FILE.read_text()
            else:
                print(f"Error downloading cost data: {e}")
                sys.exit(1)

    # Parse CSV and detect header row
    reader = csv.reader(StringIO(data))
    header = None
    rows = []

    for row in reader:
        if row and row[0].strip().lower() == "cards":
            header = [col.strip() for col in row]
            break

    if not header:
        print("Could not find header row starting with 'Cards'")
        sys.exit(1)

    for row in reader:
        if len(row) < len(header):
            continue
        rows.append(row)

    # Identify columns
    try:
        cards_idx = header.index("Cards")
        cost_idx = header.index("Total Ship Cost")
    except ValueError as e:
        print(f"Missing required column: {e}")
        sys.exit(1)

    # Build cost table
    cost_table = {}
    for row in rows:
        try:
            cards = int(row[cards_idx])
            cost_str = row[cost_idx].replace("$", "").strip()
            cost = float(cost_str)
            cost_table[cards] = cost
        except (ValueError, IndexError):
            continue

    if not cost_table:
        print("No valid cost data found in sheet.")
        sys.exit(1)

    return cost_table

# ---------- SHIPPING OPTIMIZER ----------
def min_shipping_with_envelopes(order_size, cost_table):
    """Compute minimum shipping cost and envelope breakdown for a given order,
       favoring roughly equal-sized envelopes when costs are tied.
    """
    max_env = max(cost_table.keys())
    max_check = order_size + max_env

    # dp[i] = (total_cost, total_envelopes)
    dp = [(float("inf"), float("inf"))] * (max_check + 1)
    choice = [-1] * (max_check + 1)

    dp[0] = (0, 0)

    for i in range(1, max_check + 1):
        for env_size, cost in cost_table.items():
            if i - env_size >= 0:
                prev_cost, prev_count = dp[i - env_size]
                candidate = (prev_cost + cost, prev_count + 1)

                # Lexicographic comparison:
                # 1) lower cost
                # 2) fewer envelopes (more even sizing)
                if candidate < dp[i]:
                    dp[i] = candidate
                    choice[i] = env_size

    # Find best total shipped cards >= order_size
    best_total = min(
        range(order_size, max_check + 1),
        key=lambda x: dp[x]
    )

    # Reconstruct envelope breakdown
    envelopes = {}
    current = best_total
    while current > 0:
        env = choice[current]
        envelopes[env] = envelopes.get(env, 0) + 1
        current -= env

    total_cost, _ = dp[best_total]
    return total_cost, envelopes, best_total

# ---------- BATCH PROCESS ----------
def process_batch(input_csv, output_csv, cost_table):
    """Process a batch of orders from CSV."""
    results = []
    with open(input_csv, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                order_size = int(row["Cards"])
            except (KeyError, ValueError):
                continue

            total_cost, envelopes, _ = min_shipping_with_envelopes(order_size, cost_table)
            envelope_str = "; ".join(f"{qty}x{size}" for size, qty in sorted(envelopes.items(), reverse=True))

            results.append({
                "OrderSize": order_size,
                "TotalCost": total_cost,
                "EnvelopeBreakdown": envelope_str
            })

    keys = ["OrderSize", "TotalCost", "EnvelopeBreakdown"]
    with open(output_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(results)

    print(f"Batch results written to {output_csv}")

# ---------- MAIN CLI ----------
def main():
    parser = argparse.ArgumentParser(
        description="Optimize shipping cost using Google Sheets pricing (single or batch)"
    )
    parser.add_argument("--sheet", required=False, default=SHEET_URL, help="Google Sheets CSV export URL")
    parser.add_argument("--refresh", action="store_true", help="Force refresh of cached cost data")
    parser.add_argument("--batch", help="Path to CSV of orders for batch processing")
    parser.add_argument("--output", help="Output CSV file for batch processing")
    parser.add_argument("order_size", nargs="?", type=int, help="Number of cards in a single order")

    args = parser.parse_args()

    cost_table = load_cost_table(args.sheet, args.refresh)

    # Batch mode
    if args.batch:
        if not args.output:
            print("Please specify --output for batch processing")
            sys.exit(1)
        process_batch(args.batch, args.output, cost_table)
        return

    # Single order mode
    if args.order_size is None:
        print("Please specify order_size for single order, or use --batch")
        sys.exit(1)

    total_cost, envelopes, shipped_cards = min_shipping_with_envelopes(args.order_size, cost_table)

    print("\nShipping Optimization Result")
    print("-" * 40)
    print(f"Order size:        {args.order_size} cards")
    print(f"Cards shipped:     {shipped_cards}")
    print(f"Total cost:        ${total_cost:.2f}")
    print("\nEnvelope breakdown:")
    for size in sorted(envelopes, reverse=True):
        print(f"  {envelopes[size]} × {size}-card envelope")
    print("-" * 40)

if __name__ == "__main__":
    main()
