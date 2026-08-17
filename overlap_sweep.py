#!/usr/bin/env python3
"""
OVERLAP SWEEP

Tornado exit -> ? -> Railgun shield

The earlier design asked who paid the 56,960 intermediates. That question
has eleven billion answers and none of them discriminate. This asks the
question in the other direction.

    what did the Tornado exit set pay, ever

That set is bounded. Exits are mostly ordinary accounts making a handful
of onward payments, so the answer is millions of rows rather than
billions, and it is collected with fromAddress instead of toAddress.
Everything else is a set intersection done locally, at no cost.

One sweep answers all three depths:

    depth 0   exit is itself a shielder
    depth 1   exit paid a shielder directly
    depth 2   exit paid an intermediate that paid a shielder

No time windows anywhere. The only bound is causal, and it is stated
plainly at startup: an exit payment after the last block on which any
intermediate paid a shielder cannot complete a chain. Pass --to-head to
drop even that.

Ordering is reported, never applied. Chains where the exit paid after
the intermediate had already funded the shielder are counted separately
rather than silently removed, since that count is itself a finding.

    python3 overlap_sweep.py --check          what it will read, no node
    python3 overlap_sweep.py                  sweep, resumable, Ctrl-C safe
    python3 overlap_sweep.py --result         intersect and report
"""

import argparse
import gzip
import io
import os
import queue
import re
import sqlite3
import threading
import time

import requests

# ----------------------------------------------------------------------
# configuration

NODE = "http://88.218.224.19:8546"

# Where the Tornado exit addresses live. A sqlite file or a text/csv file
# of addresses. Table and column are auto-detected; set them explicitly
# below if the guess is wrong.
TORNADO_SOURCE = "tornado.db"
TORNADO_TABLE = None
TORNADO_COLUMN = None

RAILGUN_DB = "railgun.db"
HOP_DB = "onehop.db"
OUT_DB = "overlap.db"

WORKERS = 6
TIMEOUT = 900

# Tornado Cash first deployment. Nothing earlier can be an exit payment.
FALLBACK_START = 9_116_000

UNIT = 1_000
WINDOW_START = 100_000
WINDOW_MIN = 1_000
WINDOW_MAX = 1_000_000
GROW_AFTER = 3
PIECE = 200_000

# A payer producing more than this many outgoing payments is a service.
# Set to 0 to keep everything.
MAX_ROWS_PER_PAYER = 500_000

COMMIT_ROWS = 100_000
QUEUE_DEPTH = 200

ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
ADDRESS_ANY = re.compile(r"0x[0-9a-fA-F]{40}")
LIMIT_PATTERN = re.compile(r"(?:limit|filter)[^0-9]{0,30}(\d{2,})")

SKIP_CALL_TYPES = {"delegatecall", "callcode", "staticcall"}
OVERFLOW_HINTS = ("too many", "limit", "exceed", "too large", "out of memory",
                  "deadline", "timeout", "canceled", "cancelled")

# ----------------------------------------------------------------------

work = queue.Queue()
results = queue.Queue(maxsize=QUEUE_DEPTH)
stop_now = threading.Event()
warned = threading.Event()

counters = {"rows": 0, "blocks": 0, "failed": 0}
counter_lock = threading.Lock()

hot = set()
hot_counts = {}
hot_lock = threading.Lock()

server_limit = None
limit_lock = threading.Lock()
use_gzip = False


class Busy(Exception):
    pass


class Overflow(Exception):
    pass


def classify(error):
    text = str(error).lower()
    if any(hint in text for hint in OVERFLOW_HINTS):
        return Overflow(str(error)[:140])
    return Busy(str(error)[:140])


def parse_limit(text):
    match = LIMIT_PATTERN.search(str(text))
    if not match:
        return None
    value = int(match.group(1))
    return value if 100 <= value <= 10_000_000 else None


def note_limit(value):
    global server_limit
    if not value:
        return
    with limit_lock:
        if server_limit is None or value < server_limit:
            server_limit = value


def ceiling():
    return server_limit or WINDOW_MAX


# ----------------------------------------------------------------------


class Node:
    def __init__(self):
        self.session = None
        self._reset()

    def _reset(self):
        if self.session is not None:
            try:
                self.session.close()
            except Exception:
                pass
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def _send(self, payload, timeout):
        if use_gzip:
            import json
            raw = json.dumps(payload).encode()
            buffer = io.BytesIO()
            with gzip.GzipFile(fileobj=buffer, mode="wb", compresslevel=6) as f:
                f.write(raw)
            return self.session.post(
                NODE, data=buffer.getvalue(), timeout=timeout,
                headers={"Content-Encoding": "gzip",
                         "Content-Type": "application/json"})
        return self.session.post(NODE, json=payload, timeout=timeout)

    def _post(self, payload, timeout=None):
        wait, total = 1, 0
        while not stop_now.is_set():
            try:
                return self._send(payload, timeout or TIMEOUT).json()
            except requests.exceptions.ReadTimeout:
                raise Overflow("read timeout after {}s".format(timeout or TIMEOUT))
            except Exception:
                total += wait
                if total > 3600:
                    raise RuntimeError("node unreachable for an hour")
                time.sleep(wait)
                wait = min(wait * 2, 60)
                self._reset()
        raise RuntimeError("stopped")

    def call(self, method, params, timeout=None):
        answer = self._post({"jsonrpc": "2.0", "id": 0,
                             "method": method, "params": params}, timeout)
        if "error" in answer:
            raise classify(answer["error"])
        return answer["result"]


def probe_gzip(node, sample, start):
    """Compressed request bodies cut the address payload roughly fourfold."""
    global use_gzip
    use_gzip = True
    try:
        node.call("trace_filter", [{"fromBlock": hex(start),
                                    "toBlock": hex(start + 9),
                                    "fromAddress": [sample]}], timeout=60)
        return True
    except Exception:
        use_gzip = False
        return False


def discover_limit(node, sample, start):
    width = WINDOW_MAX
    while width > WINDOW_MIN:
        try:
            node.call("trace_filter", [{
                "fromBlock": hex(start), "toBlock": hex(start + width - 1),
                "fromAddress": [sample]}], timeout=180)
            note_limit(width)
            return width
        except Overflow as problem:
            stated = parse_limit(problem)
            if stated:
                note_limit(stated)
                return stated
            width //= 2
        except (Busy, RuntimeError):
            break
    note_limit(WINDOW_MIN)
    return WINDOW_MIN


# ----------------------------------------------------------------------
# loading


def sqlite_addresses(path, table=None, column=None):
    con = sqlite3.connect(path)
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    best = (0, None, None)
    for name in ([table] if table else tables):
        if name not in tables:
            continue
        columns = [r[1] for r in con.execute(
            "PRAGMA table_info({})".format(name))]
        for field in ([column] if column else columns):
            if field not in columns:
                continue
            try:
                sample = con.execute(
                    "SELECT {} FROM {} LIMIT 200".format(field, name)).fetchall()
            except Exception:
                continue
            hits = sum(1 for row in sample
                       if isinstance(row[0], str) and ADDRESS.match(row[0]))
            if hits > best[0]:
                best = (hits, name, field)

    if not best[1]:
        con.close()
        raise SystemExit(
            "\n  No address-shaped column found in {}.\n"
            "  Tables present: {}\n"
            "  Set TORNADO_TABLE and TORNADO_COLUMN at the top of this file."
            .format(path, ", ".join(tables) or "none"))

    _, name, field = best
    addresses = sorted({r[0].lower() for r in con.execute(
        "SELECT DISTINCT {} FROM {} WHERE {} IS NOT NULL"
        .format(field, name, field)) if isinstance(r[0], str)})

    earliest = None
    for candidate in [r[1] for r in con.execute(
            "PRAGMA table_info({})".format(name))]:
        if candidate == field:
            continue
        try:
            value = con.execute("SELECT MIN({}) FROM {} WHERE {} > 0"
                                .format(candidate, name, candidate)).fetchone()[0]
        except Exception:
            continue
        if isinstance(value, int) and 1_000_000 < value < 40_000_000:
            earliest = value if earliest is None else min(earliest, value)
    con.close()
    return addresses, "{}.{}".format(name, field), earliest


def text_addresses(path):
    with open(path, "r", errors="ignore") as handle:
        found = ADDRESS_ANY.findall(handle.read())
    return sorted({a.lower() for a in found}), path, None


def load_tornado():
    if not os.path.exists(TORNADO_SOURCE):
        raise SystemExit(
            "\n  {} not found. Set TORNADO_SOURCE at the top of this file to\n"
            "  wherever your Tornado exit addresses live. A sqlite database or\n"
            "  a text file of addresses both work."
            .format(TORNADO_SOURCE))
    if TORNADO_SOURCE.endswith((".db", ".sqlite", ".sqlite3")):
        return sqlite_addresses(TORNADO_SOURCE, TORNADO_TABLE, TORNADO_COLUMN)
    return text_addresses(TORNADO_SOURCE)


def load_shielders():
    con = sqlite3.connect(RAILGUN_DB)
    rows = {r[0].lower(): r[1] for r in con.execute(
        "SELECT sender, MIN(block_number) FROM senders GROUP BY sender")}
    con.close()
    return rows


def load_intermediates():
    """funder -> list of (shielder, block at which it paid that shielder)"""
    con = sqlite3.connect(HOP_DB)
    table = {}
    last = 0
    for shielder, funder, block in con.execute(
            "SELECT shielder, funder, block FROM funding"):
        table.setdefault(funder.lower(), []).append((shielder.lower(), block))
        last = max(last, block or 0)
    con.close()
    return table, last


# ----------------------------------------------------------------------
# storage


def setup():
    con = sqlite3.connect(OUT_DB)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA cache_size=-262144")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("CREATE TABLE IF NOT EXISTS paid "
                "(payer TEXT, target TEXT, block INTEGER)")
    con.execute("CREATE TABLE IF NOT EXISTS done_units "
                "(start_block INTEGER PRIMARY KEY)")
    con.execute("CREATE TABLE IF NOT EXISTS failed_ranges "
                "(low INTEGER, high INTEGER, reason TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS hot_payers "
                "(address TEXT PRIMARY KEY, seen INTEGER)")
    con.commit()
    return con


def writer_thread():
    con = sqlite3.connect(OUT_DB)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA cache_size=-262144")
    pending = 0
    while True:
        try:
            item = results.get(timeout=2)
        except queue.Empty:
            if stop_now.is_set() and results.empty():
                break
            continue
        if item is None:
            break
        kind, payload = item
        try:
            if kind == "span":
                low, high, rows = payload
                units = [(u,) for u in range(low, high + 1, UNIT)]
                try:
                    if rows:
                        con.executemany("INSERT INTO paid VALUES (?,?,?)", rows)
                    con.executemany(
                        "INSERT OR IGNORE INTO done_units VALUES (?)", units)
                    pending += len(rows) + len(units)
                except Exception as problem:
                    con.rollback()
                    pending = 0
                    con.execute("INSERT INTO failed_ranges VALUES (?,?,?)",
                                (low, high, "write failed: "
                                 + str(problem)[:80]))
                    con.commit()
                    with counter_lock:
                        counters["failed"] += 1
                    print("\n  write failed for blocks {:,} to {:,}: {}"
                          .format(low, high, str(problem)[:70]))
            elif kind == "failed":
                con.execute("INSERT INTO failed_ranges VALUES (?,?,?)", payload)
                con.commit()
                pending = 0
            elif kind == "hot":
                con.execute("INSERT OR REPLACE INTO hot_payers VALUES (?,?)",
                            payload)
                con.commit()
                pending = 0
            if pending >= COMMIT_ROWS:
                con.commit()
                pending = 0
        except Exception as problem:
            print("\n  writer problem: {}".format(str(problem)[:90]))
        results.task_done()
    con.commit()
    con.close()


# ----------------------------------------------------------------------
# sweeping


def extract(traces):
    rows = []
    for item in traces or []:
        if item.get("error"):
            continue
        action = item.get("action") or {}
        if action.get("callType") in SKIP_CALL_TYPES:
            continue
        if action.get("value") in ("0x0", "0x", "0x00", None):
            continue
        sender, target = action.get("from"), action.get("to")
        if not sender or not target:
            continue
        sender, target = sender.lower(), target.lower()
        if sender == target:
            continue
        block = item.get("blockNumber", 0)
        if isinstance(block, str):
            block = int(block, 16)
        rows.append((sender, target, block))
    return rows


def guard(rows):
    if not MAX_ROWS_PER_PAYER:
        return rows
    tally = {}
    for payer, _, _ in rows:
        tally[payer] = tally.get(payer, 0) + 1
    newly = []
    with hot_lock:
        for address, count in tally.items():
            total = hot_counts.get(address, 0) + count
            hot_counts[address] = total
            if total > MAX_ROWS_PER_PAYER and address not in hot:
                hot.add(address)
                newly.append((address, total))
    for entry in newly:
        results.put(("hot", entry))
    return [row for row in rows if row[0] not in hot] if hot else rows


def worker(exits, done):
    node = Node()
    window = min(WINDOW_START, ceiling())
    clean = 0
    live = list(exits)
    live_hot = 0

    while not stop_now.is_set():
        try:
            low, high = work.get(timeout=3)
        except queue.Empty:
            return

        current = low
        while current <= high and not stop_now.is_set():
            if len(hot) != live_hot:
                live = [a for a in exits if a not in hot]
                live_hot = len(hot)
                if not live:
                    break

            stop = min(current + window - 1, high)
            if all(u in done for u in range(current, stop + 1, UNIT)):
                with counter_lock:
                    counters["blocks"] += stop - current + 1
                current = stop + 1
                continue

            try:
                traces = node.call("trace_filter", [{
                    "fromBlock": hex(current), "toBlock": hex(stop),
                    "fromAddress": live}])
            except Overflow as problem:
                stated = parse_limit(problem)
                note_limit(stated)
                if window > WINDOW_MIN:
                    if not warned.is_set():
                        warned.set()
                        print("\n  node refused {:,} blocks: {}".format(
                            window, problem))
                        print("  cap is {:,}, using that\n".format(stated)
                              if stated else "  narrowing\n")
                    window = (max(WINDOW_MIN, min(stated, window)) if stated
                              else max(WINDOW_MIN, window // 2))
                    clean = 0
                    continue
                results.put(("failed", (current, stop, str(problem))))
                with counter_lock:
                    counters["failed"] += 1
                current = stop + 1
                continue
            except Busy as problem:
                results.put(("failed", (current, stop, str(problem))))
                with counter_lock:
                    counters["failed"] += 1
                current = stop + 1
                continue
            except RuntimeError:
                return

            rows = guard(extract(traces))
            results.put(("span", (current, stop, rows)))
            with counter_lock:
                counters["rows"] += len(rows)
                counters["blocks"] += stop - current + 1

            current = stop + 1
            clean += 1
            if clean >= GROW_AFTER and window < ceiling():
                window = min(ceiling(), window * 2)
                clean = 0

        work.task_done()


def format_duration(seconds):
    if seconds < 60:
        return "{:.0f}s".format(seconds)
    if seconds < 3600:
        return "{:.0f}m".format(seconds / 60)
    return "{:.1f}h".format(seconds / 3600)


# ----------------------------------------------------------------------


def describe_inputs():
    exits, where, earliest = load_tornado()
    shielders = load_shielders()
    intermediates, last_edge = load_intermediates()

    print("=" * 72)
    print("INPUTS")
    print("=" * 72)
    print("  tornado exits        {:>10,}   from {}".format(len(exits), where))
    print("  railgun shielders    {:>10,}   from {}".format(
        len(shielders), RAILGUN_DB))
    print("  intermediates        {:>10,}   from {}".format(
        len(intermediates), HOP_DB))
    if earliest:
        print("  earliest exit block  {:>10,}".format(earliest))
    print("  last funding block   {:>10,}".format(last_edge))
    return exits, shielders, intermediates, earliest, last_edge


def sweep(args):
    exits, shielders, intermediates, earliest, last_edge = describe_inputs()
    if not exits:
        raise SystemExit("\n  No exit addresses loaded.")

    con = setup()
    probe = Node()
    head = int(probe.call("eth_blockNumber", []), 16)

    start = args.from_block or earliest or FALLBACK_START
    start = (start // UNIT) * UNIT
    if args.to_head or not last_edge:
        end, bound = head, "chain tip"
    else:
        end = min(head, ((last_edge // UNIT) + 1) * UNIT - 1)
        bound = "last block any intermediate paid a shielder"

    compressed = probe_gzip(probe, exits[0], start)
    cap = discover_limit(probe, exits[0], start)

    done = {r[0] for r in con.execute("SELECT start_block FROM done_units")}
    for row in con.execute("SELECT address FROM hot_payers"):
        hot.add(row[0])
    live = [a for a in exits if a not in hot]

    pieces, cursor = 0, start
    while cursor <= end:
        stop = min(cursor + PIECE - 1, end)
        if not all(u in done for u in range(cursor, stop + 1, UNIT)):
            work.put((cursor, stop))
            pieces += 1
        cursor = stop + 1

    span = end - start + 1
    per_call = len(live) * 45 / 1_000_000.0
    if compressed:
        per_call /= 4.0
    calls = span / float(ceiling())

    print("\n" + "=" * 72)
    print("SWEEP")
    print("=" * 72)
    print("  direction            fromAddress, the exit set")
    print("  addresses in filter  {:>10,}".format(len(live)))
    print("  blocks               {:,} to {:,}".format(start, end))
    print("  upper bound          {}".format(bound))
    print("  server range cap     {:>10,} blocks".format(cap))
    print("  gzip request bodies  {}".format(
        "accepted, payload cut about fourfold" if compressed else "refused"))
    print("  calls for one pass   {:>10,.0f}".format(calls))
    print("  upload for one pass  {:>10,.1f} GB".format(per_call * calls / 1000))
    print("  pieces outstanding   {:>10,}".format(pieces))
    already = con.execute("SELECT COUNT(*) FROM paid").fetchone()[0]
    if already:
        print("  resuming, {:,} rows already collected".format(already))

    if pieces == 0:
        print("\n  Already complete. Run with --result.")
        con.close()
        return
    print("\n  Ctrl-C is safe. Progress saved continuously.\n")
    con.close()

    writer = threading.Thread(target=writer_thread, daemon=True)
    writer.start()
    threads = []
    for _ in range(args.workers):
        thread = threading.Thread(target=worker, args=(live, done), daemon=True)
        thread.start()
        threads.append(thread)

    started = time.time()
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(5)
            with counter_lock:
                blocks, rows, failed = (counters["blocks"], counters["rows"],
                                        counters["failed"])
            elapsed = time.time() - started
            rate = blocks / elapsed if elapsed else 0
            print("  {:,}/{:,} blocks  {:.2f}%  rows {:,}  failed {}  "
                  "{:,.0f} blk/s  eta {}    ".format(
                      blocks, span, blocks / span * 100 if span else 100,
                      rows, failed, rate,
                      format_duration((span - blocks) / rate if rate else 0)),
                  end="\r")
    except KeyboardInterrupt:
        print("\n\n  stopping, please wait...")
        stop_now.set()
        for thread in threads:
            thread.join(timeout=30)

    stop_now.set()
    results.put(None)
    writer.join(timeout=120)

    con = sqlite3.connect(OUT_DB)
    print("\n\n  rows            {:,}".format(
        con.execute("SELECT COUNT(*) FROM paid").fetchone()[0]))
    print("  failed ranges   {:,}".format(
        con.execute("SELECT COUNT(*) FROM failed_ranges").fetchone()[0]))
    print("  payers retired  {:,}".format(
        con.execute("SELECT COUNT(*) FROM hot_payers").fetchone()[0]))
    con.close()
    print("\n  Run with --result.")


# ----------------------------------------------------------------------


def result(args):
    exits, shielders, intermediates, _, _ = describe_inputs()
    exits = set(exits)

    con = sqlite3.connect(OUT_DB)
    con.execute("CREATE INDEX IF NOT EXISTS i_payer ON paid(payer)")
    con.execute("CREATE INDEX IF NOT EXISTS i_target ON paid(target)")
    con.commit()

    covered = con.execute("SELECT COUNT(*) FROM done_units").fetchone()[0] * UNIT
    missing = con.execute("SELECT COUNT(*) FROM failed_ranges").fetchone()[0]
    retired = con.execute("SELECT COUNT(*) FROM hot_payers").fetchone()[0]

    depth0 = exits & set(shielders)

    depth1, depth2 = set(), set()
    chains, out_of_order = [], 0
    reached = set()

    for payer, target, block in con.execute(
            "SELECT payer, target, block FROM paid"):
        if payer not in exits:
            continue
        reached.add(target)
        if target in shielders:
            depth1.add((payer, target))
        for shielder, funded_at in intermediates.get(target, ()):
            depth2.add((payer, target, shielder))
            ordered = block <= funded_at
            if not ordered:
                out_of_order += 1
            chains.append((payer, target, shielder, block, funded_at, ordered))
    con.close()

    print("\n" + "=" * 72)
    print("COVERAGE")
    print("=" * 72)
    print("  blocks swept         {:>10,}".format(covered))
    print("  failed ranges        {:>10,}".format(missing))
    print("  payers retired       {:>10,}".format(retired))
    if missing or retired:
        print("\n  Coverage is not complete. State this before quoting counts.")

    print("\n" + "=" * 72)
    print("REACH")
    print("=" * 72)
    print("  exits                {:>10,}".format(len(exits)))
    print("  addresses they paid  {:>10,}".format(len(reached)))
    print("  of those, intermediates {:>7,}".format(
        len(reached & set(intermediates))))

    print("\n" + "=" * 72)
    print("OVERLAP")
    print("=" * 72)
    print("  depth 0  exit is a shielder             {:>8,}".format(len(depth0)))
    print("  depth 1  exit paid a shielder           {:>8,} edges, "
          "{:,} exits".format(len(depth1), len({p for p, _ in depth1})))
    print("  depth 2  exit paid an intermediate      {:>8,} chains, "
          "{:,} exits".format(len(depth2), len({p for p, _, _ in depth2})))
    print("\n  of the depth 2 chains, {:,} have the exit paying after the"
          "\n  intermediate had already funded the shielder. Reported, not"
          "\n  removed.".format(out_of_order))

    if args.dump and chains:
        with open("chains.csv", "w") as handle:
            handle.write("exit,intermediate,shielder,paid_block,"
                         "funded_block,ordered\n")
            for row in sorted(chains, key=lambda r: r[3]):
                handle.write(",".join(str(x) for x in row) + "\n")
        print("\n  {:,} chains written to chains.csv".format(len(chains)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="report what will be read and exit")
    parser.add_argument("--result", action="store_true",
                        help="intersect the collected data and report")
    parser.add_argument("--dump", action="store_true",
                        help="with --result, write chains.csv")
    parser.add_argument("--to-head", action="store_true",
                        help="sweep to the tip, dropping the causal bound")
    parser.add_argument("--from-block", type=int, default=None)
    parser.add_argument("--workers", type=int, default=WORKERS)
    args = parser.parse_args()

    if args.check:
        describe_inputs()
        print("\n  Sources look readable. Run without --check to sweep.")
    elif args.result:
        result(args)
    else:
        sweep(args)


if __name__ == "__main__":
    main()
