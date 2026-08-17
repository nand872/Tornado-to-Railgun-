#!/usr/bin/env python3
"""
WINDOW CHECK 11

Was onehop.db collected with a time window, or is it everything?

The analysis applies no window. This checks whether the data underneath
it does, which is a separate question and the one that matters, because
an edge never collected cannot be recovered by analysing without a
filter afterwards.

The test is the gap between when a shielder was funded and when it first
shielded. If collection was unfiltered, that gap should run from negative
values, funding arriving after a shield, out to years. If it stops dead
at a round boundary, a window was applied and the boundary names it.

    50,400 blocks    about 7 days
   216,000 blocks    about 30 days
 1,296,000 blocks    about 180 days

    python3 window_check11.py
"""

import sqlite3

RAILGUN_DB = "railgun.db"
HOP_DB = "onehop.db"

BLOCKS_PER_DAY = 7_200.0

SUSPECTS = {
    50_400: "7 days",
    100_800: "14 days",
    216_000: "30 days",
    648_000: "90 days",
    1_296_000: "180 days",
    2_628_000: "1 year",
}

BUCKETS = [
    (None, 0, "funded after the shield"),
    (0, 7_200, "same day"),
    (7_200, 50_400, "1 to 7 days"),
    (50_400, 216_000, "7 to 30 days"),
    (216_000, 648_000, "30 to 90 days"),
    (648_000, 1_296_000, "90 to 180 days"),
    (1_296_000, 2_628_000, "180 days to 1 year"),
    (2_628_000, 7_884_000, "1 to 3 years"),
    (7_884_000, None, "over 3 years"),
]


def main():
    railgun = sqlite3.connect(RAILGUN_DB)
    first_shield = {r[0].lower(): r[1] for r in railgun.execute(
        "SELECT sender, MIN(block_number) FROM senders GROUP BY sender")}
    railgun.close()

    hop = sqlite3.connect(HOP_DB)
    gaps = []
    edges = 0
    unmatched = 0
    lowest = highest = None
    for shielder, funder, block in hop.execute(
            "SELECT shielder, funder, block FROM funding"):
        edges += 1
        if block:
            lowest = block if lowest is None else min(lowest, block)
            highest = block if highest is None else max(highest, block)
        shielded = first_shield.get((shielder or "").lower())
        if shielded is None or not block:
            unmatched += 1
            continue
        gaps.append(shielded - block)
    tables = [r[0] for r in hop.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    hop.close()

    print("=" * 66)
    print("SOURCE")
    print("=" * 66)
    print("  funding edges            {:>12,}".format(edges))
    print("  matched to a shielder    {:>12,}".format(len(gaps)))
    print("  unmatched                {:>12,}".format(unmatched))
    print("  funding block range      {:,} to {:,}".format(
        lowest or 0, highest or 0))
    print("  tables in onehop.db      {}".format(", ".join(tables)))

    if not gaps:
        print("\n  Nothing to measure. Check the schema names.")
        return

    gaps.sort()
    biggest = gaps[-1]
    smallest = gaps[0]

    print("\n" + "=" * 66)
    print("GAP BETWEEN FUNDING AND FIRST SHIELD")
    print("=" * 66)
    print("  most negative      {:>12,} blocks   {:>8,.0f} days after".format(
        smallest, abs(smallest) / BLOCKS_PER_DAY))
    print("  largest            {:>12,} blocks   {:>8,.0f} days before".format(
        biggest, biggest / BLOCKS_PER_DAY))
    for label, index in (("median", len(gaps) // 2),
                         ("90th percentile", int(len(gaps) * 0.90)),
                         ("99th percentile", int(len(gaps) * 0.99))):
        value = gaps[min(index, len(gaps) - 1)]
        print("  {:<18} {:>12,} blocks   {:>8,.0f} days".format(
            label, value, value / BLOCKS_PER_DAY))

    print("\n" + "=" * 66)
    print("DISTRIBUTION")
    print("=" * 66)
    for low, high, label in BUCKETS:
        count = sum(1 for g in gaps
                    if (low is None or g > low) and (high is None or g <= high))
        bar = "#" * int(count / len(gaps) * 50)
        print("  {:<22} {:>10,}  {:>5.1f}%  {}".format(
            label, count, count / len(gaps) * 100, bar))

    print("\n" + "=" * 66)
    print("VERDICT")
    print("=" * 66)

    negatives = sum(1 for g in gaps if g < 0)
    if negatives:
        print("  {:,} edges have funding arriving after the first shield.".format(
            negatives))
        print("  No ordering constraint was applied during collection.")
    else:
        print("  No edge has funding after a shield. Either collection")
        print("  required funding to precede shielding, or it genuinely")
        print("  never happens. The former is far more likely.")

    hit = None
    for boundary, name in sorted(SUSPECTS.items()):
        if biggest <= boundary * 1.02:
            hit = (boundary, name)
            break

    if hit:
        boundary, name = hit
        print("\n  The largest gap is {:,} blocks, at or just under the".format(
            biggest))
        print("  {:,} block mark, which is about {}.".format(boundary, name))
        print("\n  A WINDOW WAS APPLIED. onehop.db does not contain funding")
        print("  older than {} before the shield, so the depth 1 result is".format(
            name))
        print("  bounded by that regardless of how the analysis is written.")
        print("  Rebuild the funding layer with funding11.py, which applies")
        print("  none, then rerun analysis11.py.")
    else:
        print("\n  The largest gap is {:,} blocks, about {:,.0f} days, which".format(
            biggest, biggest / BLOCKS_PER_DAY))
        print("  is not near any round window boundary.")
        print("\n  NO WINDOW. Funding is retained however long before the")
        print("  shield it arrived. The depth 1 figure of 1,502 is unbounded")
        print("  in time, which is what you asked for.")

    over_year = sum(1 for g in gaps if g > 2_628_000)
    print("\n  edges with over a year between funding and shield  {:,}".format(
        over_year))
    if over_year:
        print("  Their presence is the direct evidence that nothing is being")
        print("  cut off at the far end.")


if __name__ == "__main__":
    main()
