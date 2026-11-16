"""The two files the director works from: decisions.csv to edit, summary.html to read.

Both are regenerated from the database on every run.  The CSV is the approval
interface: its `action` column is read back by `run` after the pause.  Because
the file is rewritten each run, write_csv() carries forward whatever the
director already typed (via read_actions) so an interrupted review is not lost.

Everything here is plain stdlib: csv for the sheet, string formatting for the
HTML.  No templating library, no JavaScript; the summary is a static page that
opens anywhere and prints cleanly.
"""
from __future__ import annotations

import csv
import html
import json
import os
from collections import Counter, defaultdict

# Decision -> the bucket the director thinks in.  Kept here (not in policy.json)
# because the six decisions are fixed by classify.decide().
BUCKET = {"REVIEW_SENSITIVE": "NEEDS REVIEW", "REVIEW": "NEEDS REVIEW", "DELETE_PROPOSED": "DELETE PROPOSED",
          "RETAIN_PENDING": "KEEP", "RETAIN": "KEEP"}
# Sort order for the CSV: things needing eyes first, proposals next, keeps last.
ORDER = ["REVIEW_SENSITIVE", "REVIEW", "DELETE_PROPOSED", "RETAIN_PENDING", "RETAIN"]
CSV_COLUMNS = ["msg_key", "date", "from", "subject", "size_kb", "category", "decision", "flags", "reason", "action"]


def _sorted(rows):
    """Review first, then proposals, then keeps; oldest first within each."""
    return sorted(rows, key=lambda r: (ORDER.index(r["decision"]), r["date"] or ""))


def read_actions(path: str) -> dict:
    """msg_key -> action column of an existing decisions.csv, so a director's edits survive a re-run.

    Returns {} when the file does not exist yet (first run).  Values are
    lowercased and stripped; validation of the value happens in `run`.
    """
    if not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {r["msg_key"]: (r.get("action") or "").strip().lower() for r in csv.DictReader(f)}


def write_csv(rows, path: str, prior: dict) -> None:
    """Write decisions.csv.  The `action` column is the director's input.

    Pre-fill rule, in priority order:
        1. whatever the director typed last time (prior), even blank
        2. an approval already recorded in the database
        3. "delete" for DELETE_PROPOSED rows, "" for everything else
    So the first run proposes, and later runs never overwrite a human's edit.
    """
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for r in _sorted(rows):
            default = r["approved_action"] or ("delete" if r["decision"] == "DELETE_PROPOSED" else "")
            action = prior[r["msg_key"]] if r["msg_key"] in prior else default
            w.writerow([r["msg_key"], (r["date"] or "")[:10], r["from_addr"], r["subject"], round(r["size_bytes"] / 1024, 1),
                        r["category"], r["decision"], " ".join(json.loads(r["flags"])), r["reason"], action])


def _table(headers, body_rows):
    """One HTML table.  Every cell is escaped; subjects and senders are untrusted text."""
    h = "".join(f"<th>{html.escape(str(x))}</th>" for x in headers)
    b = "".join("<tr>" + "".join(f"<td>{html.escape(str(x))}</td>" for x in row) + "</tr>" for row in body_rows)
    return f"<table><tr>{h}</tr>{b}</table>"


def _mb(b):
    return f"{b / 1_048_576:.1f} MB"


def _year(r):
    """Group key for the by-year table; undated messages get their own row instead of a bogus year."""
    return r["date"][:4] if r["date"] else "no date"


def write_html(rows, policy: dict, applied: int, path: str) -> None:
    """Write summary.html: six small tables that answer the director's questions in order.

    1. How much is there and how much would go?        (by bucket)
    2. Which decisions make up each bucket?            (by decision)
    3. What kinds of records were found, under which   (by category, with GS1-SL item)
       schedule item?
    4. Which years would the deletions come from?      (by year)
    5. Was anything sensitive found?                   (flag counts, never values)
    6. Who sends the mail being proposed for deletion?  (top senders; a quick sanity check
                                                        that proposals are newsletters and
                                                        bots, not the City Attorney)
    """
    # Each accumulator is name -> [count, bytes]; by_year rows also collect a
    # trailing 1 per DELETE_PROPOSED message so the table can show that count.
    by_bucket = defaultdict(lambda: [0, 0])
    by_decision = defaultdict(lambda: [0, 0])
    by_cat = defaultdict(lambda: [0, 0])
    by_year = defaultdict(lambda: [0, 0])
    flags = Counter()
    senders = Counter()
    for r in rows:
        b = BUCKET[r["decision"]]
        for d, k in ((by_bucket, b), (by_decision, r["decision"]), (by_cat, r["category"]), (by_year, _year(r))):
            d[k][0] += 1
            d[k][1] += r["size_bytes"]
        if r["decision"] == "DELETE_PROPOSED":
            by_year[_year(r)].append(1)
            senders[r["from_addr"]] += 1
        for fl in json.loads(r["flags"]):
            flags[fl] += 1
    total_n = sum(v[0] for v in by_bucket.values())
    total_b = sum(v[1] for v in by_bucket.values())
    # Category id -> "Name (GS1-SL #item)" so the report cites the schedule, not just our ids.
    series_name = {s["id"]: f'{s["name"]} (GS1-SL #{s["gs1sl_item"]})' for s in policy["series"]}
    transitory_cats = ("CALENDAR", "AUTO_REPLY", "BULK_LIST", "SYSTEM", "TRANSITORY")   # all fall under the one transitory item

    sections = [
        ("Totals by bucket", _table(["Bucket", "Messages", "Size", "Share"],
            [(k, v[0], _mb(v[1]), f"{100 * v[0] / total_n:.0f}%") for k, v in sorted(by_bucket.items())] + [("Total in archive", total_n, _mb(total_b), "")])),
        ("By decision", _table(["Decision", "Messages", "Size"], [(k, *by_decision[k][:1], _mb(by_decision[k][1])) for k in ORDER if k in by_decision])),
        ("By category", _table(["Category", "Schedule item", "Messages", "Size"],
            sorted([(k, series_name.get(k, policy["transitory"]["title"] + f" (GS1-SL #{policy['transitory']['gs1sl_item']})" if k in transitory_cats else ""), v[0], _mb(v[1])) for k, v in by_cat.items()], key=lambda x: -x[2]))),
        ("By year", _table(["Year", "Messages", "Delete proposed"],
            [(y, v[0], sum(v[2:])) for y, v in sorted(by_year.items())])),
        ("Sensitivity flags (values never stored)", _table(["Flag", "Messages"], sorted(flags.items())) if flags else "<p>None detected.</p>"),
        ("Top senders in DELETE PROPOSED", _table(["Sender", "Messages"], senders.most_common(10)) if senders else "<p>None.</p>"),
    ]
    body = "".join(f"<h2>{html.escape(t)}</h2>{c}" for t, c in sections)
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>Email Cleanup Summary</title>
<style>body{{font:14px/1.4 system-ui,sans-serif;max-width:900px;margin:2em auto;padding:0 1em;color:#222}}
table{{border-collapse:collapse;margin:0 0 1.5em}}th,td{{border:1px solid #ccc;padding:4px 10px;text-align:left}}th{{background:#f3f3f3}}
h1{{font-size:22px}}h2{{font-size:16px;margin-top:1.5em}}.meta{{color:#555}}</style></head><body>
<h1>Email Cleanup Summary</h1>
<p class="meta">Schedule: {html.escape(policy["schedule"])}<br>
Transitory window: {policy["transitory"]["window_days"]} days &nbsp;|&nbsp; Already quarantined in earlier runs: {applied} messages</p>
{body}
<p class="meta">Decisions are proposals. Nothing is removed until the director edits decisions.csv and confirms.</p>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


def write_all(rows, policy, applied, out_dir):
    """Write both files into out_dir, carrying forward any actions already in the existing CSV."""
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, "decisions.csv")
    write_csv(rows, csv_path, read_actions(csv_path))
    write_html(rows, policy, applied, os.path.join(out_dir, "summary.html"))
