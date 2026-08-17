#!/usr/bin/env python3
"""
TIMELINE 11

When did the overlapping addresses appear, month by month.

An overlapping address is one that both withdrew from a Tornado ETH pool
and shielded into Railgun, the depth 0 set from analysis11.py. This dates
each of them and counts them by month, starting from the first.

Three columns rather than one, because a bare count cannot be read. Six
overlaps in a month when Railgun gained forty new shielders is a very
different observation from six in a month when it gained four thousand.
The share column is the one that carries meaning.

Dates come from real block timestamps fetched from the node, not from
estimating a date out of a block height. They are cached in
blocktimes11.db, so the first run takes a couple of minutes and every
run after that is instant.

    python3 timeline11.py
    python3 timeline11.py --threshold 25
    python3 timeline11.py --by withdrawal
"""

import argparse
import datetime as dt
import sqlite3
import sys

import requests

from analysis11 import load_payouts, load_shielders

NODE = "http://88.218.224.19:8546"
CACHE_DB = "blocktimes11.db"
THRESHOLD = 100
BATCH = 100

DESIGNATION = "2022-08"


def depth0_set(threshold):
    _, hits, first_seen, _, failed = load_payouts()
    shields = load_shielders()
    exits = {a: first_seen[a] for a, n in hits.items() if n <= threshold}
    overlap = {a for a in shields if a in exits}
    return overlap, exits, shields, failed


def cache():
    con = sqlite3.connect(CACHE_DB)
    con.execute("CREATE TABLE IF NOT EXISTS times "
                "(block INTEGER PRIMARY KEY, ts INTEGER)")
    con.commit()
    return con


def block_times(blocks):
    """Real timestamps, fetched once and cached."""
    con = cache()
    known = {r[0]: r[1] for r in con.execute("SELECT block, ts FROM times")}
    missing = sorted({b for b in blocks if b and b not in known})

    if missing:
        print("  fetching {:,} block timestamps ({:,} already cached)".format(
            len(missing), len(known)))
        session = requests.Session()
        session.headers.update({"Content-Type": "application/json"})
        fetched = 0
        for start in range(0, len(missing), BATCH):
            group = missing[start:start + BATCH]
            payload = [{"jsonrpc": "2.0", "id": index,
                        "method": "eth_getBlockByNumber",
                        "params": [hex(block), False]}
                       for index, block in enumerate(group)]
            try:
                answers = session.post(NODE, json=payload, timeout=180).json()
            except Exception as problem:
                print("\n  fetch failed at {}: {}".format(
                    start, str(problem)[:70]))
                continue
            if isinstance(answers, dict):
                answers = [answers]
            batch = []
            for answer in answers:
                block = group[answer.get("id", 0)]
                result = answer.get("result")
                if result and result.get("timestamp"):
                    batch.append((block, int(result["timestamp"], 16)))
            if batch:
                con.executemany(
                    "INSERT OR REPLACE INTO times VALUES (?,?)", batch)
                con.commit()
                fetched += len(batch)
                known.update(dict(batch))
            print("    {:,}/{:,}    ".format(
                min(start + BATCH, len(missing)), len(missing)), end="\r")
        print("\n  fetched {:,}\n".format(fetched))
    con.close()
    return known


def month_of(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=int, default=THRESHOLD)
    parser.add_argument("--by", choices=("shield", "withdrawal"),
                        default="shield",
                        help="which event dates the overlap")
    args = parser.parse_args()

    overlap, exits, shields, failed = depth0_set(args.threshold)
    if not overlap:
        raise SystemExit("  No overlapping addresses found.")

    needed = set()
    for address in overlap:
        needed.add(exits[address])
        needed.add(shields[address])
    for block in shields.values():
        needed.add(block)

    print("=" * 74)
    print("TIMELINE OF OVERLAPPING ADDRESSES")
    print("=" * 74)
    print("  overlapping addresses    {:,}".format(len(overlap)))
    print("  all railgun shielders    {:,}".format(len(shields)))
    print("  relayer cutoff           {:,}".format(args.threshold))
    print("  dated by                 {} block\n".format(args.by))

    times = block_times(needed)
    if not times:
        raise SystemExit("  No timestamps available. Is the node reachable?")

    # monthly counts -------------------------------------------------
    overlap_months = {}
    withdrawal_months = {}
    undated = 0
    for address in overlap:
        shield_ts = times.get(shields[address])
        exit_ts = times.get(exits[address])
        if not shield_ts or not exit_ts:
            undated += 1
            continue
        overlap_months[month_of(shield_ts)] = \
            overlap_months.get(month_of(shield_ts), 0) + 1
        withdrawal_months[month_of(exit_ts)] = \
            withdrawal_months.get(month_of(exit_ts), 0) + 1

    baseline = {}
    for block in shields.values():
        ts = times.get(block)
        if ts:
            baseline[month_of(ts)] = baseline.get(month_of(ts), 0) + 1

    primary = overlap_months if args.by == "shield" else withdrawal_months
    if not primary:
        raise SystemExit("  Nothing could be dated.")

    first = min(primary)
    last = max(max(primary), max(baseline) if baseline else first)
    months = []
    year, mon = int(first[:4]), int(first[5:])
    while "{:04d}-{:02d}".format(year, mon) <= last:
        months.append("{:04d}-{:02d}".format(year, mon))
        mon += 1
        if mon > 12:
            year, mon = year + 1, 1

    peak = max(primary.values())

    print("=" * 74)
    print("BY MONTH, FROM THE FIRST OVERLAPPING ADDRESS")
    print("=" * 74)
    print("  {:<9} {:>9} {:>7} {:>10} {:>8}  {}".format(
        "month", "overlaps", "cumul", "shielders", "share", ""))
    print("  " + "-" * 70)
    running = 0
    for key in months:
        count = primary.get(key, 0)
        running += count
        total = baseline.get(key, 0)
        pct = "{:.2f}%".format(count / total * 100) if total else "-"
        bar = "#" * int(count / peak * 22) if peak else ""
        mark = " <- designation" if key == DESIGNATION else ""
        print("  {:<9} {:>9,} {:>7,} {:>10,} {:>8}  {}{}".format(
            key, count, running, total, pct, bar, mark))

    print("\n  first overlap    {}".format(first))
    print("  busiest month    {} with {:,}".format(
        max(primary, key=primary.get), peak))
    if undated:
        print("  undated          {:,} addresses had no timestamp".format(
            undated))
    if failed:
        print("\n  {} uncollected range(s) in overlap.db. Early months may"
              "\n  be understated.".format(len(failed)))

    # lag ------------------------------------------------------------
    lags = []
    for address in overlap:
        shield_ts = times.get(shields[address])
        exit_ts = times.get(exits[address])
        if shield_ts and exit_ts:
            lags.append((shield_ts - exit_ts) / 86400.0)
    lags.sort()

    print("\n" + "=" * 74)
    print("TIME FROM WITHDRAWAL TO SHIELD")
    print("=" * 74)
    buckets = [
        (None, 0, "shielded before withdrawing"),
        (0, 1, "same day"),
        (1, 7, "1 to 7 days"),
        (7, 30, "7 to 30 days"),
        (30, 90, "30 to 90 days"),
        (90, 365, "90 days to 1 year"),
        (365, None, "over a year"),
    ]
    for low, high, label in buckets:
        count = sum(1 for d in lags
                    if (low is None or d > low) and (high is None or d <= high))
        bar = "#" * int(count / len(lags) * 40) if lags else ""
        print("  {:<28} {:>6,}  {:>5.1f}%  {}".format(
            label, count, count / len(lags) * 100 if lags else 0, bar))
    if lags:
        print("\n  median {:,.1f} days      longest {:,.0f} days".format(
            lags[len(lags) // 2], lags[-1]))

    # files ----------------------------------------------------------
    with open("overlap_timeline.csv", "w") as handle:
        handle.write("month,overlaps_by_shield,overlaps_by_withdrawal,"
                     "cumulative_by_shield,all_new_shielders,share_percent\n")
        running = 0
        for key in months:
            count = overlap_months.get(key, 0)
            running += count
            total = baseline.get(key, 0)
            handle.write("{},{},{},{},{},{}\n".format(
                key, count, withdrawal_months.get(key, 0), running, total,
                "{:.4f}".format(count / total * 100) if total else ""))

    with open("TIMELINE.md", "w") as handle:
        handle.write("# Overlapping addresses by month\n\n")
        handle.write("An overlapping address both withdrew from a Tornado "
                     "ETH pool and shielded into Railgun. Dated by its first "
                     "shield, using block timestamps from an archive node.\n\n")
        handle.write("The share column is the one to read. Absolute counts "
                     "track Railgun's overall growth, so they rise and fall "
                     "with the protocol rather than with anything about "
                     "Tornado.\n\n")
        handle.write("| Month | Overlapping | Cumulative | All new shielders "
                     "| Share |\n|---|---:|---:|---:|---:|\n")
        running = 0
        for key in months:
            count = overlap_months.get(key, 0)
            running += count
            total = baseline.get(key, 0)
            label = "**{}**".format(key) if key == DESIGNATION else key
            handle.write("| {} | {:,} | {:,} | {:,} | {} |\n".format(
                label, count, running, total,
                "{:.2f}%".format(count / total * 100) if total else "-"))
        handle.write("\nAugust 2022 is the designation month, in bold.\n")

    print("\n  wrote overlap_timeline.csv and TIMELINE.md")


if __name__ == "__main__":
    main()
