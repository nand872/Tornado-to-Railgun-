#!/usr/bin/env python3
"""
REPORT 11

Turns the three databases into RESULTS.md and two CSVs, ready to commit.

Adds two things analysis11.py does not compute.

A baseline. The raw post-designation share is uninformative on its own,
because Railgun's own adoption is concentrated after August 2022. If
almost every shielder first shielded after the designation, then Tornado
linked shielders doing the same is not a signal. This computes both
shares side by side so the comparison is explicit rather than implied.

A time series. First shield month for Tornado linked shielders against
all shielders, as a share, which is the series that would actually carry
an argument about migration.

    python3 report11.py
    python3 report11.py --threshold 50
"""

import argparse
import datetime as dt

from analysis11 import (DESIGNATION_BLOCK, LADDER, load_funding, load_payouts,
                        load_shielders)

OUT = "RESULTS.md"

# Anchored on the Merge, block 15,537,393 at 2022-09-15 06:42 UTC.
# Twelve seconds exactly after it, about 13.3 before. Approximate, and
# labelled as such wherever it appears.
MERGE_BLOCK = 15_537_393
MERGE_TIME = dt.datetime(2022, 9, 15, 6, 42)


def block_date(block):
    if block >= MERGE_BLOCK:
        return MERGE_TIME + dt.timedelta(seconds=12 * (block - MERGE_BLOCK))
    return MERGE_TIME - dt.timedelta(seconds=13.3 * (MERGE_BLOCK - block))


def month(block):
    return block_date(block).strftime("%Y-%m")


def compute(threshold):
    payers, hits, first_seen, blocks, failed = load_payouts()
    shields = load_shielders()
    funding, funding_rows = load_funding()

    exits = {a: first_seen[a] for a, n in hits.items() if n <= threshold}
    relayers = sorted(((n, a) for a, n in hits.items() if n > threshold),
                      reverse=True)

    depth0 = {a for a in shields if a in exits}
    depth0_backwards = {a for a in depth0
                        if exits[a] and shields[a] and exits[a] > shields[a]}

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

    linked = depth0 | {s for _, s in pairs}
    return dict(
        payers=payers, hits=hits, blocks=blocks, failed=failed,
        shields=shields, funding=funding, funding_rows=funding_rows,
        exits=exits, relayers=relayers, depth0=depth0,
        depth0_backwards=depth0_backwards, pairs=pairs, linked=linked,
        after_shield=sum(1 for (_, s), b in pairs.items()
                         if b > shields.get(s, 0)),
        before_withdrawal=sum(1 for (e, _), b in pairs.items()
                              if exits.get(e) and b < exits[e]))


def share(part, whole):
    return "{:.2f}%".format(part / whole * 100) if whole else "n/a"


def build(data, threshold):
    shields = data["shields"]
    exits = data["exits"]
    depth0 = data["depth0"]
    pairs = data["pairs"]
    linked = data["linked"]
    hits = data["hits"]

    depth1_shielders = {s for _, s in pairs}
    all_after = {a for a, b in shields.items() if b >= DESIGNATION_BLOCK}
    linked_after = {a for a in linked if shields.get(a, 0) >= DESIGNATION_BLOCK}

    lines = []
    add = lines.append

    add("# Tornado Cash to Railgun, one hop\n")
    add("Measured connectivity between Tornado Cash withdrawal addresses "
        "and Railgun shielding addresses, at depth zero and depth one, "
        "with no time window at any stage.\n")

    add("## Headline\n")
    add("| | Shielders | Share of all shielders |")
    add("|---|---:|---:|")
    add("| Reached at depth 0 or 1 | {:,} | {} |".format(
        len(linked), share(len(linked), len(shields))))
    add("| Depth 0, withdrew and shielded from the same address | {:,} | {} |"
        .format(len(depth0), share(len(depth0), len(shields))))
    add("| Depth 1, funded by a withdrawal address | {:,} | {} |".format(
        len(depth1_shielders), share(len(depth1_shielders), len(shields))))
    add("| All Railgun shielders | {:,} | 100% |\n".format(len(shields)))
    add("Depth one is {:.1f} times larger than depth zero. The direct "
        "pattern, withdrawing to a fresh address and shielding from it "
        "without an intervening transfer, is the minority case.\n".format(
            len(depth1_shielders) / len(depth0) if depth0 else 0))

    add("## The baseline\n")

    observed = [b for b in shields.values() if b]
    if observed:
        low, high = min(observed), max(observed)
        before = sum(1 for b in observed if b < DESIGNATION_BLOCK)
        pre_span = max(0, DESIGNATION_BLOCK - low)
        add("### Observation window\n")
        add("| | |")
        add("|---|---:|")
        add("| Earliest shield observed | block {:,}, {} |".format(
            low, block_date(low).strftime("%d %b %Y")))
        add("| Latest shield observed | block {:,}, {} |".format(
            high, block_date(high).strftime("%d %b %Y")))
        add("| Window before the designation | {:,} blocks, {} |".format(
            pre_span, share(pre_span, high - low + 1)))
        add("| Shielders first seen before it | {:,} of {:,}, {} |".format(
            before, len(observed), share(before, len(observed))))
        add("")
        if pre_span < 0.2 * (high - low + 1):
            add("**The window is asymmetric.** Only {} of the observed "
                "period precedes the designation, so a post-designation "
                "share close to 100% is largely what the coverage produces "
                "on its own. Any before-and-after comparison drawn from "
                "this rests on {:,} blocks of prior data, and Railgun "
                "launched earlier than the earliest shield recorded here. "
                "Extending the shielder extraction back to launch would "
                "widen that base substantially.\n".format(
                    share(pre_span, high - low + 1), pre_span))

    add("### Shares\n")
    add("The post-designation share of Tornado linked shielders is "
        "uninformative on its own, because Railgun adoption is itself "
        "concentrated after August 2022. Both shares are given here so the "
        "comparison is explicit.\n")
    add("| Population | First shielded on or after 8 Aug 2022 | Share |")
    add("|---|---:|---:|")
    add("| All shielders | {:,} of {:,} | {} |".format(
        len(all_after), len(shields), share(len(all_after), len(shields))))
    add("| Tornado linked | {:,} of {:,} | {} |".format(
        len(linked_after), len(linked), share(len(linked_after), len(linked))))
    add("")
    gap = ((len(linked_after) / len(linked) if linked else 0)
           - (len(all_after) / len(shields) if shields else 0)) * 100
    if abs(gap) < 2:
        add("The two shares differ by {:.2f} percentage points. Tornado "
            "linked shielders are **not** disproportionately "
            "post-designation relative to the shielding population as a "
            "whole. The raw post-designation count carries no inference "
            "about migration on its own.\n".format(gap))
    else:
        add("The two shares differ by {:.2f} percentage points. This gap is "
            "the quantity an argument about migration would have to rest "
            "on, not the raw count.\n".format(gap))

    add("## First shield by month\n")
    add("Approximate dates, derived from block height against the Merge. "
        "The share column is the one that matters, since the counts track "
        "Railgun's overall growth.\n")
    add("| Month | All shielders | Tornado linked | Linked share |")
    add("|---|---:|---:|---:|")
    buckets = {}
    for address, block in shields.items():
        if not block:
            continue
        key = month(block)
        entry = buckets.setdefault(key, [0, 0])
        entry[0] += 1
        if address in linked:
            entry[1] += 1
    for key in sorted(buckets):
        total, hit = buckets[key]
        label = "**{}**".format(key) if key == "2022-08" else key
        add("| {} | {:,} | {:,} | {} |".format(
            label, total, hit, share(hit, total)))
    add("\nAugust 2022 is the designation month, marked in bold.\n")

    add("## Depth 0\n")
    add("| | Count |")
    add("|---|---:|")
    add("| Shielders that withdrew from a Tornado pool | {:,} |".format(
        len(depth0)))
    add("| Withdrawal recorded after the shield | {:,} |".format(
        len(data["depth0_backwards"])))
    add("")
    add("The second row cannot be the sequence under study, since the "
        "address shielded before it ever withdrew. It is reported rather "
        "than removed.\n")

    add("## Depth 1\n")
    add("| | Count |")
    add("|---|---:|")
    add("| Exit to shielder pairs | {:,} |".format(len(pairs)))
    add("| Distinct shielders reached | {:,} |".format(len(depth1_shielders)))
    add("| Distinct withdrawal addresses involved | {:,} |".format(
        len({e for e, _ in pairs})))
    add("| Funded after that shielder's first shield | {:,} |".format(
        data["after_shield"]))
    add("| Funded before the funder's own withdrawal | {:,} |".format(
        data["before_withdrawal"]))
    add("")
    flagged = data["after_shield"] + data["before_withdrawal"]
    add("{:,} of {:,} pairs, {}, fail one of the two ordering checks. "
        "Neither is filtered out. Both are carried in `depth1.csv` with "
        "their block numbers so the ordering can be inspected per row.\n"
        .format(flagged, len(pairs), share(flagged, len(pairs))))

    add("## Relayer separation\n")
    add("A relayed withdrawal pays twice from the pool, once to the "
        "recipient and once to the relayer. The sweep did not retain "
        "values, so the two are separated by frequency instead.\n")
    add("| Cutoff | Exits | Relayers |")
    add("|---:|---:|---:|")
    for level in LADDER:
        below = sum(1 for n in hits.values() if n <= level)
        mark = " *(used)*" if level == threshold else ""
        add("| {:,}{} | {:,} | {:,} |".format(
            level, mark, below, len(hits) - below))
    add("")
    add("| Address | Payouts received |")
    add("|---|---:|")
    for count, address in data["relayers"][:10]:
        add("| `{}` | {:,} |".format(address, count))
    add("")

    add("## Sensitivity to the relayer cutoff\n")
    add("| Cutoff | Depth 0 | Depth 1 |")
    add("|---:|---:|---:|")
    funding = data["funding"]
    for level in LADDER:
        alt = {a for a, n in hits.items() if n <= level}
        zero = {a for a in shields if a in alt}
        one = {s for s, funders in funding.items()
               if s not in zero and any(f in alt for f, _ in funders)}
        mark = " *(used)*" if level == threshold else ""
        add("| {:,}{} | {:,} | {:,} |".format(level, mark, len(zero), len(one)))
    add("")
    spread0 = [len({a for a in shields if hits.get(a, 10**9) <= level})
               for level in LADDER if level >= 25]
    if spread0 and max(spread0) - min(spread0) < 0.05 * max(spread0):
        add("Above a cutoff of 25 the result is flat. The threshold is not "
            "carrying the finding.\n")

    add("## Coverage\n")
    add("| | |")
    add("|---|---:|")
    add("| Pool contracts observed paying | {:,} |".format(len(data["payers"])))
    add("| Payouts collected | {:,} |".format(sum(data["payers"].values())))
    add("| Distinct addresses paid | {:,} |".format(len(hits)))
    add("| Exit set at cutoff {:,} | {:,} |".format(threshold, len(exits)))
    add("| Blocks swept | {:,} |".format(data["blocks"]))
    add("| Funding edges examined | {:,} |".format(data["funding_rows"]))
    add("")
    if data["failed"]:
        add("**Incomplete.** The following ranges were not collected.\n")
        add("| From | To | Reason |")
        add("|---:|---:|---|")
        for low, high, reason in data["failed"]:
            add("| {:,} | {:,} | {} |".format(low, high, (reason or "")[:60]))
        add("")
    else:
        add("No failed ranges.\n")

    add("## What this does and does not establish\n")
    add("A funding edge is not an identity claim. Two addresses connected "
        "by a payment may or may not be controlled by the same person, and "
        "nothing on chain distinguishes the two cases.\n")
    add("The measured set is therefore neither an upper nor a lower bound "
        "on migration. It is not an upper bound, because any migrant who "
        "inserted a second hop, used an exchange, or bridged is absent from "
        "it. It is not a lower bound, because some of the connections "
        "counted here are incidental rather than the same actor moving "
        "between protocols.\n")
    add("The depth zero to depth one ratio is the more robust observation. "
        "Whatever the absolute numbers, connectivity grows sharply with "
        "each hop, and the candidate set at depth two is large enough that "
        "it cannot discriminate at all. That is a measurement ceiling "
        "rather than a gap in the data, and it does not close with a better "
        "node or a longer sweep.\n")

    add("---\n")
    add("Generated by `report11.py`. Relayer cutoff {:,}. "
        "Designation block {:,}.".format(threshold, DESIGNATION_BLOCK))
    return "\n".join(lines)


def write_csvs(data):
    shields, exits = data["shields"], data["exits"]
    with open("depth0.csv", "w") as handle:
        handle.write("address,withdrawal_block,first_shield_block\n")
        for address in sorted(data["depth0"], key=lambda a: shields[a]):
            handle.write("{},{},{}\n".format(
                address, exits[address], shields[address]))
    with open("depth1.csv", "w") as handle:
        handle.write("exit,shielder,funding_block,withdrawal_block,"
                     "first_shield_block,funded_before_shield,"
                     "funded_after_withdrawal\n")
        for (funder, shielder), block in sorted(
                data["pairs"].items(), key=lambda kv: kv[1]):
            shield_block = shields.get(shielder, 0)
            exit_block = exits.get(funder, 0)
            handle.write("{},{},{},{},{},{},{}\n".format(
                funder, shielder, block, exit_block, shield_block,
                block <= shield_block, block >= exit_block))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=int, default=100)
    args = parser.parse_args()

    data = compute(args.threshold)
    with open(OUT, "w") as handle:
        handle.write(build(data, args.threshold))
    write_csvs(data)

    print("  wrote {}".format(OUT))
    print("  wrote depth0.csv  {:,} rows".format(len(data["depth0"])))
    print("  wrote depth1.csv  {:,} rows".format(len(data["pairs"])))
    print("\n  Commit all three. They are small and they are the result.")


if __name__ == "__main__":
    main()
