"""retain: clean a government email archive against a retention schedule, with the director deciding.

  python -m retain run archive.mbox         scan, report, pause for director, quarantine approved
  python -m retain generate archive.mbox    build a synthetic demo archive

`run` is one linear pass with one stop.  Reading it top to bottom is reading
the product:

    header      print what we are about to do and fail fast if Ollama is missing
    scan        classify every message not seen before (classify.classify_one)
    decisions   recompute all pending decisions, print bucket totals, write report/
    gate        pause; director edits decisions.csv; read the action column back
    apply       show what will go, ask y/N, copy to quarantine, remove from mbox, audit

Ctrl-C is safe anywhere.  Nothing leaves the archive until after "Proceed? y".
"""
from __future__ import annotations

import argparse
import csv
import json
import mailbox
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone

from . import classify, generate, report, store

# The three buckets the director thinks in, and which decisions roll into each.
# Same mapping as report.BUCKET, laid out as ordered pairs for printing.
BUCKETS = (("KEEP", ("RETAIN", "RETAIN_PENDING")), ("DELETE PROPOSED", ("DELETE_PROPOSED",)), ("NEEDS REVIEW", ("REVIEW_SENSITIVE", "REVIEW")))


def _mb(b):
    return f"{b / 1_048_576:.1f} MB"


def cmd_run(a):
    policy = json.load(open(a.policy, encoding="utf-8"))
    now = datetime.now(timezone.utc)
    model = classify.Model(a.ollama, a.model, policy)   # raises SystemExit if the host is not loopback
    model.check()                                       # raises SystemExit if Ollama or the model is missing
    con = store.open_db(a.db)
    box = mailbox.mbox(a.mbox)
    total = len(box)
    known = store.seen(con)

    # ---- header
    print("== Transitory Email Cleanup ==")
    print(f"Policy      {policy['schedule']}")
    print(f"            {len(policy['series'])} series, transitory window {policy['transitory']['window_days']} days")
    print(f"Archive     {a.mbox}  ({total} messages, {_mb(os.path.getsize(a.mbox))})")
    print(f"Model       {a.model} @ {a.ollama} ... reachable")
    print(f"State       {a.db}  ({len(known)} messages seen before)\n")

    # ---- scan
    # Every message is parsed (cheap) so its current mbox position can be
    # recorded, but only unseen keys go through the pipeline.  That is what
    # makes a daily run take seconds on a 20-year archive.
    t0, new, via = time.time(), 0, Counter()
    print(f"Scanning {total} messages")
    for i, k in enumerate(box.iterkeys(), 1):
        msg = classify.parse(box.get_bytes(k), k)
        if msg.key in known:
            store.set_mbox_key(con, msg.key, k)
            continue
        known.add(msg.key)      # also de-duplicates within this run (same Message-ID twice in one mbox)
        res = classify.classify_one(msg, policy, model, now)
        new += 1
        via[res["via"]] += 1
        # One line per message is the demo's narration: the via column shows which stage decided.
        print(f"  [{i:>4}/{total}]  {res['via']:<9} {res['klass']:<10} {msg.subject[:48]!r:<50} {res['category']}: {res['reason'][:80]}")
        store.insert(con, msg, res)
        store.audit(con, msg.key, "classified", f"{res['via']} -> {res['decision']}", now)
        if res["via"] == "model":
            store.audit(con, msg.key, "sent_to_model", a.model, now)   # the audit shows exactly which messages the model saw
        if new % 25 == 0:
            con.commit()        # a Ctrl-C mid-scan loses at most 25 classifications
    store.redecide(con, policy, now)
    con.commit()
    print(f"Scan done in {time.time() - t0:.0f}s   new {new}   header rules {via['header']}   sensitive {via['sensitive']}   model {via['model']}\n")

    # ---- decisions
    counts = store.counts(con)
    print("Decisions")
    for name, decisions in BUCKETS:
        n = sum(counts.get(d, (0, 0))[0] for d in decisions)
        b = sum(counts.get(d, (0, 0))[1] for d in decisions)
        detail = ", ".join(f"{d} {counts[d][0]}" for d in decisions if d in counts)
        print(f"  {name:<16} {n:>5}   {_mb(b):>8}   ({detail})")
    rows = store.pending(con)
    report.write_all(rows, policy, store.applied_count(con), a.report)
    print(f"\nWrote {a.report}/summary.html and {a.report}/decisions.csv")

    # ---- director gate
    # The pause is the approval mechanism.  Approvals are only read after a
    # human presses Enter here, so a scheduled run with no TTY (EOFError) or a
    # Ctrl-C (KeyboardInterrupt) exits having changed nothing.  The CSV they
    # may have half-edited is carried into the next run by report.read_actions.
    try:
        input(f'\n>> Director review: edit the "action" column in {a.report}/decisions.csv (delete / retain / blank),\n>> save it, then press Enter.  Ctrl-C to stop here; a later run picks up the edited file.\n')
    except (KeyboardInterrupt, EOFError):
        print("\nStopped before approvals. Nothing removed.")
        return
    now = datetime.now(timezone.utc)    # approval timestamps should reflect when Enter was pressed, not when the scan started
    applied = Counter()
    with open(os.path.join(a.report, "decisions.csv"), newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            action = (row.get("action") or "").strip().lower()
            # Only the two known words count; anything else (blank, typo) leaves the row untouched.
            if action in ("delete", "retain") and store.set_approval(con, row["msg_key"], action, a.by, now):
                store.audit(con, row["msg_key"], "approved", f"{action} by {a.by}", now)
                applied[action] += 1
    con.commit()
    print(f"Reading approvals ... {applied['delete']} delete, {applied['retain']} retain, {len(rows) - sum(applied.values())} unchanged")

    # ---- apply
    targets = store.approved_deletes(con)
    if not targets:
        print("Nothing approved for deletion.")
        return
    os.makedirs(a.quarantine, exist_ok=True)
    qpath = os.path.join(a.quarantine, f"run-{len(os.listdir(a.quarantine)) + 1}.mbox")   # run-1, run-2, ... one file per approval batch
    print(f"Will quarantine {len(targets)} messages ({_mb(sum(r['size_bytes'] for r in targets))}) to {qpath}")
    try:
        ok = input("Proceed? [y/N] ").strip().lower() == "y"
    except (KeyboardInterrupt, EOFError):
        ok = False
    if not ok:
        print("\nStopped. Nothing removed.")
        return
    before_n, before_b = len(box), os.path.getsize(a.mbox)
    # Copy first, then remove.  mailbox.mbox buffers removals until flush(), so
    # the mbox_key positions recorded during the scan stay valid for this loop.
    q = mailbox.mbox(qpath)
    for r in targets:
        q.add(box.get_bytes(r["mbox_key"]))
        box.remove(r["mbox_key"])
        store.mark_applied(con, r["msg_key"], now, qpath)
        store.audit(con, r["msg_key"], "quarantined", qpath, now)
    q.flush()
    q.close()
    box.flush()     # this is the moment the archive file is rewritten
    box.close()
    con.commit()
    after_n, after_b = len(mailbox.mbox(a.mbox)), os.path.getsize(a.mbox)
    print(f"Quarantined {len(targets)}.  Archive: {before_n} -> {after_n} messages, {_mb(before_b)} -> {_mb(after_b)}")
    print(f"Audit written to {a.db} (audit table, {con.execute('SELECT COUNT(*) FROM audit').fetchone()[0]} rows)")
    report.write_all(store.pending(con), policy, store.applied_count(con), a.report)   # report now reflects the smaller archive


def cmd_generate(a):
    count = a.append or a.count     # --append N overrides --count
    n = generate.build(a.mbox, count, a.seed, bool(a.append))
    print(f"{'Appended' if a.append else 'Wrote'} {count} messages -> {a.mbox} now holds {n} messages, {_mb(os.path.getsize(a.mbox))}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="retain", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="scan, report, pause for the director, quarantine what was approved")
    r.add_argument("mbox")
    r.add_argument("--policy", default="policy.json", help="retention schedule (default: policy.json)")
    r.add_argument("--db", default="retain.db", help="SQLite state and audit (default: retain.db)")
    r.add_argument("--report", default="report", help="folder for summary.html and decisions.csv (default: report)")
    r.add_argument("--quarantine", default="quarantine", help="folder for run-N.mbox files (default: quarantine)")
    r.add_argument("--ollama", default="http://127.0.0.1:11434", help="Ollama URL; must be loopback (default: http://127.0.0.1:11434)")
    r.add_argument("--model", default="llama3.2:3b", help="Ollama model name (default: llama3.2:3b)")
    r.add_argument("--by", default="director", help="name recorded on approvals (default: director)")
    r.set_defaults(fn=cmd_run)
    g = sub.add_parser("generate", help="build a synthetic demo archive")
    g.add_argument("mbox")
    g.add_argument("--count", type=int, default=300, help="messages to generate (default: 300)")
    g.add_argument("--seed", type=int, default=1, help="random seed; same seed, same archive (default: 1)")
    g.add_argument("--append", type=int, metavar="N", help="add N messages dated in the last 3 days to an existing archive (simulates a day of mail)")
    g.set_defaults(fn=cmd_generate)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
