#!/usr/bin/env python3
"""
ANALYSIS 11

Reads what you already have. No node calls, runs in seconds.

    overlap.db   payments out of the Tornado pool contracts
    railgun.db   shielders
    onehop.db    who funded each shielder

The sweep that produced overlap.db filtered on the pool contracts rather
than on the exit addresses, so every row is a payout from a pool. That
makes it the withdrawal set, one level below what the file names suggest,
and it is what this reads.

    depth 0   a pool paid an address, that address shielded
              B withdraws to a fresh address and shields from it

    depth 1   a pool paid an address, that address funded a different
              address, that address shielded
              B withdraws, sends to a fresh C, C shields

Relayers are the complication. A relayed withdrawal pays twice from the
pool, once to the recipient and once to the relayer as its fee, and both
look identical here because the sweep did not keep values. They separate
cleanly on frequency instead: an exit is paid once or a handful of times,
a relayer thousands. The threshold is reported at several levels so you
can see how much the answer moves, rather than being asked to trust one.

No time window anywhere. Ordering is computed and reported, never used to
drop a row.

    python3 analysis11.py
    python3 analysis11.py --threshold 50 --dump
"""

import argparse
import sqlite3
from collections import Counter

OVERLAP_DB = "overlap.db"
RAILGUN_DB = "railgun.db"
HOP_DB = "onehop.db"

# A target paid more than this many times by pool contracts is treated as
# a relayer rather than an exit.
THRESHOLD = 100

# OFAC designation of Tornado Cash, 8 August 2022.
DESIGNATION_BLOCK = 15_307_000

LADDER = (5, 10, 25, 50, 100, 250, 1000)


def load_payouts():
    con = sqlite3.connect(OVERLAP_DB)
    payers = Counter()
    hits = Counter()
    first_seen = {}
    for payer, target, block in con.execute(
            "SELECT payer, target, block FROM paid"):
        payers[payer] += 1
        hits[target] += 1
        if target not in first_seen or block < first_seen[target]:
            first_seen[target] = block
    units = con.execute("SELECT COUNT(*) FROM done_units").fetchone()[0]
    failed = con.execute(
        "SELECT low, high, reason FROM failed_ranges").fetchall()
    con.close()
    return payers, hits, first_seen, units * 1000, failed


def load_shielders():
    con = sqlite3.connect(RAILGUN_DB)
    shields = {r[0].lower(): r[1] for r in con.execute(
        "SELECT sender, MIN(block_number) FROM senders GROUP BY sender")}
    con.close()
    return shields


def load_funding():
    """shielder -> list of (funder, block)"""
    con = sqlite3.connect(HOP_DB)
    table = {}
    rows = 0
    for shielder, funder, block in con.execute(
            "SELECT shielder, funder, block FROM funding"):
        table.setdefault(shielder.lower(), []).append((funder.lower(), block))
        rows += 1
    con.close()
    return table, rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=int, default=THRESHOLD,
                        help="payments from pools above which a target is "
                             "treated as a relayer")
    parser.add_argument("--dump", action="store_true")
    args = parser.parse_args()

    payers, hits, first_seen, blocks, failed = load_payouts()
    shields = load_shielders()
    funding, funding_rows = load_funding()

    print("=" * 70)
    print("SOURCE")
    print("=" * 70)
    print("  pool contracts paying    {:>10,}".format(len(payers)))
    print("  payouts                  {:>10,}".format(sum(payers.values())))
    print("  distinct addresses paid  {:>10,}".format(len(hits)))
    print("  blocks swept             {:>10,}".format(blocks))
    print("  railgun shielders        {:>10,}".format(len(shields)))
    print("  funding edges            {:>10,}".format(funding_rows))
    if failed:
        print("\n  {} range(s) failed and were not collected:".format(
            len(failed)))
        for low, high, reason in failed[:5]:
            print("    {:,} to {:,}  {}".format(low, high, (reason or "")[:60]))
        print("  Rerun overlap_sweep.py to retry them before quoting counts.")

    # relayer separation --------------------------------------------
    print("\n" + "=" * 70)
    print("RELAYER SEPARATION")
    print("=" * 70)
    print("  A relayed withdrawal pays the recipient and the relayer from")
    print("  the same pool. Frequency separates them.\n")
    print("  {:>8}  {:>12}  {:>12}".format("cutoff", "exits", "relayers"))
    print("  " + "-" * 36)
    for level in LADDER:
        below = sum(1 for count in hits.values() if count <= level)
        print("  {:>8,}  {:>12,}  {:>12,}{}".format(
            level, below, len(hits) - below,
            "   <- using" if level == args.threshold else ""))

    exits = {address: first_seen[address] for address, count in hits.items()
             if count <= args.threshold}
    relayers = {a for a, count in hits.items() if count > args.threshold}
    print("\n  exit set at cutoff {:,}   {:,} addresses".format(
        args.threshold, len(exits)))
    if relayers:
        print("\n  heaviest addresses excluded as relayers")
        for address, count in sorted(
                ((a, hits[a]) for a in relayers),
                key=lambda kv: -kv[1])[:8]:
            print("    {}  {:,} payouts".format(address, count))

    # depth 0 --------------------------------------------------------
    depth0 = {a for a in shields if a in exits}
    depth0_after = {a for a in depth0 if shields[a] >= DESIGNATION_BLOCK}
    depth0_backwards = {a for a in depth0
                        if exits[a] and shields[a] and exits[a] > shields[a]}

    print("\n" + "=" * 70)
    print("DEPTH 0   withdrawal address shields directly")
    print("=" * 70)
    print("  shielders that withdrew from a pool   {:>8,}".format(len(depth0)))
    print("  shielding on or after 8 Aug 2022      {:>8,}".format(
        len(depth0_after)))
    print("  withdrawal recorded after the shield  {:>8,}".format(
        len(depth0_backwards)))
    if depth0_backwards:
        print("    cannot be the described sequence, counted separately")

    # depth 1 --------------------------------------------------------
    pairs = {}
    for shielder, funders in funding.items():
        if shielder in depth0:
            continue
        for funder, block in funders:
            if funder not in exits:
                continue
            key = (funder, shielder)
            if key not in pairs or block < pairs[key]:
                pairs[key] = block

    depth1_shielders = {s for _, s in pairs}
    depth1_exits = {e for e, _ in pairs}
    depth1_after = {s for _, s in pairs
                    if shields.get(s, 0) >= DESIGNATION_BLOCK}
    after_shield = sum(1 for (_, s), b in pairs.items()
                       if b > shields.get(s, 0))
    before_withdrawal = sum(1 for (e, _), b in pairs.items()
                            if exits.get(e) and b < exits[e])

    print("\n" + "=" * 70)
    print("DEPTH 1   withdrawal address funds a fresh address which shields")
    print("=" * 70)
    print("  exit to shielder pairs                {:>8,}".format(len(pairs)))
    print("  distinct shielders reached            {:>8,}".format(
        len(depth1_shielders)))
    print("  distinct exits involved               {:>8,}".format(
        len(depth1_exits)))
    print("  shielding on or after 8 Aug 2022      {:>8,}".format(
        len(depth1_after)))
    print("\n  reported, not filtered")
    print("    funded after that first shield      {:>8,}".format(after_shield))
    print("    funded before the withdrawal        {:>8,}".format(
        before_withdrawal))

    # sensitivity ----------------------------------------------------
    print("\n" + "=" * 70)
    print("SENSITIVITY TO THE RELAYER CUTOFF")
    print("=" * 70)
    print("  {:>8}  {:>10}  {:>10}".format("cutoff", "depth 0", "depth 1"))
    print("  " + "-" * 32)
    for level in LADDER:
        alt = {a for a, count in hits.items() if count <= level}
        zero = {a for a in shields if a in alt}
        one = {s for s, funders in funding.items()
               if s not in zero and any(f in alt for f, _ in funders)}
        print("  {:>8,}  {:>10,}  {:>10,}{}".format(
            level, len(zero), len(one),
            "   <- using" if level == args.threshold else ""))
    print("\n  If these barely move, the cutoff is not doing the work and the")
    print("  result is robust to it. If they move a lot, say so in the paper.")

    print("\n" + "=" * 70)
    print("TOTAL")
    print("=" * 70)
    print("  shielders reached at depth 0 or 1     {:>8,} of {:,}".format(
        len(depth0 | depth1_shielders), len(shields)))

    if args.dump:
        with open("depth0_11.csv", "w") as handle:
            handle.write("address,withdrawal_block,first_shield_block\n")
            for address in sorted(depth0, key=lambda a: shields[a]):
                handle.write("{},{},{}\n".format(
                    address, exits[address], shields[address]))
        with open("depth1_11.csv", "w") as handle:
            handle.write("exit,shielder,funding_block,withdrawal_block,"
                         "first_shield_block\n")
            for (funder, shielder), block in sorted(
                    pairs.items(), key=lambda kv: kv[1]):
                handle.write("{},{},{},{},{}\n".format(
                    funder, shielder, block, exits.get(funder, 0),
                    shields.get(shielder, 0)))
        print("\n  written depth0_11.csv ({:,}) and depth1_11.csv ({:,})".format(
            len(depth0), len(pairs)))


if __name__ == "__main__":
    main()
