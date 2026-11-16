# Transitory Email Cleanup — MVP Plan

Status: BUILT 2026-11-15 from this plan. See README.md to run and DESIGN.md for what shipped.
Timeline: build over 2 days, then a live demo of a few minutes.

---

## 1. What it is, in one paragraph

A Python command-line tool that reads an mbox email archive, classifies every
message against a named government retention schedule using a model that runs
locally on the same machine, flags any message containing PII / PHI / PCI,
writes a spreadsheet and a summary page for the director, and deletes only what
the director approves, moving deleted mail into a quarantine file first. Run
once over the backlog, then daily on new mail. No email content ever leaves the
machine.

## 2. What the demo shows, mapped to the prompt

| Prompt requirement | Demo moment |
|---|---|
| Review the archive, keep only non-transitory records | `scan` classifies a 20-year synthetic archive; summary page shows KEEP vs DELETE PROPOSED by category and year |
| Against the records retention policy | `policy.json` lists Florida GS1-SL series with item numbers; every KEEP row names its series |
| HIPAA / PII / PCI cannot leave the tenant | Model is Ollama on localhost; sensitive-flagged mail is never sent to it; `grep` shows the only network call targets 127.0.0.1 |
| Director is the final decision maker | Director edits the `action` column in `decisions.csv`, runs `approve`, then `apply`. Nothing is deleted without that step |
| Smaller archive, easier to search | `apply` prints before/after message count and size; quarantine mbox holds what was removed |
| Less daily transitory email stored | `generate --append 30` simulates a day of mail, `scan` processes only those 30 |

## 3. Fixed design decisions (no alternates)

- **Input:** one mbox file. Nothing else.
- **Model:** Ollama, model `llama3.2:3b`, on `http://127.0.0.1:11434`. Required.
  The README's install is two commands. The tool exits with a clear error if
  Ollama is not reachable. No rules-only fallback.
- **Schedule:** Florida GS1-SL (General Records Schedule for State and Local
  Government Agencies). Item numbers cited in `policy.json`; docs say to swap in
  the agency's own state schedule for production.
- **Transitory window:** 30 days, one number in `policy.json`.
- **Deletion:** always quarantine (copy to `quarantine/run-N.mbox`, then remove
  from source). No hard-delete option.
- **State:** one SQLite file, `retain.db`, stdlib `sqlite3`.
- **Dependencies:** Python 3.11+ standard library only, plus Ollama as a
  separate local program.

## 4. Classification pipeline

Every message goes through three stages in order. The first stage that
produces a result ends the pipeline for that message.

**Stage 1: sensitivity scan (regex, deterministic).**
Runs on subject + body + attachment filenames.

| Flag | Detector |
|---|---|
| SSN | `\b(?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b` |
| PCI_CARD | 13 to 19 digits with optional spaces/dashes, Luhn-valid, not inside a longer digit run |
| PHI | MRN token (`\bMRN[:#\s]*\d{5,}\b`), or an ICD-10 code (`\b[A-TV-Z]\d{2}(\.\d{1,4})?\b`) near a health keyword, or 3+ distinct health keywords |

Any flag means decision `REVIEW_SENSITIVE`. The message is not sent to the
model. Matched values are counted and discarded, never stored.

**Stage 2: header rules (deterministic).**
Mechanical transitory types that headers identify with certainty. Checked in
this order; first hit wins and sets `klass = TRANSITORY`.

| Category | Condition |
|---|---|
| CALENDAR | any MIME part is `text/calendar` |
| AUTO_REPLY | `Auto-Submitted` header present, or subject matches `^(automatic reply|out of office)` |
| BULK_LIST | `List-Unsubscribe` or `List-Id` header present, or `Precedence: bulk|list` |
| SYSTEM | sender local-part matches `^(no-?reply|do-?not-?reply|notifications?|alerts?|mailer-daemon|postmaster)$` |

**Stage 3: local model (everything else).**
One prompt per message to Ollama's `/api/generate` with `format: "json"`.
Payload: subject, sender domain, up to 1,500 chars of the message's own text
(quoted replies stripped), attachment filenames, and the policy's series list
(id + name + one-line description). The model returns:

```json
{"klass": "RECORD" | "TRANSITORY" | "UNSURE",
 "series": "<series id or null>",
 "reason": "<one sentence>"}
```

Malformed output after one retry is treated as UNSURE. The model is
instructed that when in doubt it must answer RECORD; the safe failure direction
is over-retention.

**Decision table (first row that matches wins):**

| Condition | decision | bucket |
|---|---|---|
| sensitivity flag | `REVIEW_SENSITIVE` | NEEDS REVIEW |
| klass RECORD | `RETAIN` (retain_until = date + series years) | KEEP |
| klass TRANSITORY, age > 30 days | `DELETE_PROPOSED` | DELETE PROPOSED |
| klass TRANSITORY, age <= 30 days | `RETAIN_PENDING` | KEEP for now |
| klass UNSURE | `REVIEW` | NEEDS REVIEW |

Every row stores a `reason` string: the header rule name, or the model's
sentence, or the flag names.

## 5. Policy file

```json
{
  "schedule": "Florida GS1-SL (General Records Schedule for State and Local Government Agencies)",
  "agency_domain": "cityofexample.gov",
  "transitory_retention_days": 30,
  "series": [
    {"id": "CONTRACTS",   "name": "Contracts, Leases and Agreements", "gs1sl_item": "65", "retention_years": 5,
     "description": "Agreements, amendments, statements of work, bids and awards"},
    {"id": "PERSONNEL",   "name": "Personnel Records", "gs1sl_item": "9", "retention_years": 50,
     "description": "Hiring, evaluations, discipline, leave, separation"},
    {"id": "FINANCIAL",   "name": "Financial Transaction Records", "gs1sl_item": "...", "retention_years": 5,
     "description": "Invoices, purchase orders, payments, budget, audit"},
    {"id": "LEGAL",       "name": "Litigation and Claims", "gs1sl_item": "...", "retention_years": 5,
     "description": "Lawsuits, claims, legal holds, attorney correspondence"},
    {"id": "RECORDS_REQ", "name": "Public Records Requests", "gs1sl_item": "...", "retention_years": 1,
     "description": "Requests for public records and responses"},
    {"id": "GOVERNANCE",  "name": "Council / Board Correspondence", "gs1sl_item": "...", "retention_years": 10,
     "description": "Agendas, minutes, resolutions, directives, policy decisions"},
    {"id": "PERMITS",     "name": "Permits, Licenses and Inspections", "gs1sl_item": "...", "retention_years": 10,
     "description": "Applications, approvals, inspection reports, code enforcement"},
    {"id": "CORRESPONDENCE", "name": "Administrative Correspondence", "gs1sl_item": "17", "retention_years": 3,
     "description": "Substantive correspondence on agency business that is not covered by another series"}
  ]
}
```

GS1-SL item numbers get filled in from the published schedule during block 1
and the docs name the edition used. Eight series is enough for a demo and
keeps the model prompt short.

## 6. Commands

Two commands. One is the product, one makes demo data.

| Command | What it does |
|---|---|
| `python -m retain run sample/archive.mbox` | The whole thing, start to finish, with a pause for the director. |
| `python -m retain generate sample/archive.mbox --count 300 --seed 1` | Builds the synthetic archive. `--append 30` adds a day of new mail. |

`run` is one linear script with clear prints at each step:

```
== Transitory Email Cleanup ==
Policy      Florida GS1-SL, 8 series, transitory window 30 days
Archive     sample/archive.mbox  (300 messages, 4.1 MB)
Model       llama3.2:3b @ 127.0.0.1:11434 ... reachable
State       retain.db  (0 messages seen before)

Scanning 300 new messages
  [  25/300]  calendar      TRANSITORY  "Accepted: Budget review"
  [  26/300]  SENSITIVE     REVIEW      "Re: benefits enrollment"           flags=SSN
  [  27/300]  model         RECORD      "Amendment 2 to paving contract"    CONTRACTS: mentions amendment to an executed agreement
  ...
Scan done in 2m41s   header rules 118   sensitive 14   model 168 (3 unsure)

Decisions
  KEEP              141   (RETAIN 112, RETAIN_PENDING 29)
  DELETE PROPOSED   130   3.1 MB
  NEEDS REVIEW       29   (14 sensitive, 15 unsure)

Wrote report/summary.html and report/decisions.csv

>> Director review: edit the "action" column in report/decisions.csv
>> (delete / retain / blank), save it, then press Enter.  Ctrl-C to stop here.

Reading approvals ... 128 delete, 2 retain, 170 unchanged
Will quarantine 128 messages (3.0 MB) to quarantine/run-1.mbox
Proceed? [y/N] y
Quarantined 128.  Archive: 300 -> 172 messages, 4.1 MB -> 1.1 MB
Audit written to retain.db (audit table, 428 rows)
```

Every line says what stage produced the decision and why, so the demo
narrates itself. The pause is the director gate: nothing is removed unless a
human edits the file and confirms. Ctrl-C at the pause leaves the report in
place; the next `run` picks up the edited CSV, so a director who reviews
over several days still works with the same command.

Defaults: `--policy policy.json`, `--db retain.db`, `--ollama http://127.0.0.1:11434`,
`--model llama3.2:3b`. No other flags.

## 7. Files

```
darwin/
  README.md             install (Python, Ollama, pull model), the four commands, demo script
  DESIGN.md             this plan, revised to what shipped, plus limitations
  policy.json
  retain/
    __main__.py         argparse, `run` (scan, pause, approve, apply) and `generate` entry (~150 lines)
    classify.py         mbox reading, feature extraction, regex scan, header rules, Ollama call, decision (~250 lines)
    store.py            sqlite schema + queries (~100 lines)
    report.py           decisions.csv + summary.html (~120 lines)
    generate.py         synthetic archive (~150 lines)
  tests/test_classify.py  unittest: one case per decision-table row, Luhn, quote stripping; Ollama mocked
  sample/               committed demo archive + a pre-generated report
```

About 770 lines of Python.

## 8. Storage

```sql
CREATE TABLE messages (
  msg_key TEXT PRIMARY KEY,   -- normalized Message-ID, else sha256 of raw bytes
  mbox_key INTEGER,           -- position in the mbox, used by apply
  date TEXT, from_addr TEXT, subject TEXT, size_bytes INTEGER,
  klass TEXT, category TEXT, decision TEXT, reason TEXT, flags TEXT,
  retain_until TEXT,
  approved_action TEXT, approved_by TEXT, approved_at TEXT,
  applied_at TEXT, quarantine_file TEXT
);
CREATE TABLE audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT, msg_key TEXT, action TEXT, detail TEXT
);
```

Two tables. Bodies are never stored. Audit rows are written for: classified,
sent_to_model, approved, quarantined.

## 9. Incremental daily mode

`run` skips any `msg_key` already in the table, so a re-run only classifies
new messages. It then recomputes `decision` for every stored row from its
stored `klass` and today's date, so `RETAIN_PENDING` rows age into
`DELETE_PROPOSED` without re-reading mail or re-querying the model. Rows with
an `approved_action` keep it.

## 10. Report

- `report/decisions.csv`: one row per not-yet-applied message. Columns:
  `msg_key, date, from, subject, size_kb, category, decision, flags, reason, action`.
  `action` is pre-filled `delete` on DELETE_PROPOSED rows, blank elsewhere.
  Sorted: NEEDS REVIEW first, then DELETE PROPOSED, then KEEP.
- `report/summary.html`: static, inline CSS, no JS. Tables for: totals by
  bucket with bytes; counts by category; messages per year with the
  delete-proposed share; sensitivity flag counts; top 10 senders in the
  delete-proposed set.

## 11. Synthetic archive

Deterministic from `--seed`. 300 messages across 20 years, weighted toward
recent years. Mix: 25% calendar, 15% list mail, 10% system notifications, 15%
courtesy replies and internal chatter (body-only transitory, so the model has
to catch them), 25% records spread across the eight series with realistic
subjects and attachment names, 5% ambiguous, 5% carrying a planted fake SSN,
Luhn-valid test card, or MRN. A handful have no Date header or a duplicate
Message-ID.

## 12. Demo script (about 5 minutes)

1. Open `policy.json`: schedule name, eight series, 30-day window. 20 s.
2. `generate --count 300`. Show the mbox size and the year spread. 20 s.
3. `run`. Per-message lines scroll while narrating the three stages. About 300
   messages, roughly 40% hit a header rule, 5% are sensitive, the remaining
   ~165 go to the model at about 1 s each. 2 to 3 min, narrated. It stops at
   the director pause.
4. Open `summary.html`. Buckets, categories, years. 30 s.
5. Open `decisions.csv`. Point at a sensitive row (flag, no model call), a
   model-classified KEEP with its reason, a DELETE_PROPOSED. Flip one proposed
   row to `retain`. Save. 40 s.
6. Back in the terminal, press Enter. It reads the approvals, shows the
   quarantine plan, asks to proceed, confirms. Before/after counts. Open the
   quarantine mbox. 40 s.
7. `generate --append 30`, then `run` again. Only 30 messages scanned. Ctrl-C
   at the pause. 30 s.
8. `grep -rn "http" retain/` shows one URL, 127.0.0.1. 10 s.

Total scan time is the one risk. Mitigation: pre-warm Ollama (`ollama run
llama3.2:3b` once before recording) so the model is in VRAM.

## 13. Build plan (2 days, about 8 working hours)

| # | Block | Hours |
|---|---|---|
| 1 | Install Ollama, pull model, confirm latency. Write `policy.json` with GS1-SL items. | 0.75 |
| 2 | `classify.py`: mbox read, features, regex scan, header rules, Ollama call, decision | 2.0 |
| 3 | `store.py` + `scan` command + incremental | 1.0 |
| 4 | `generate.py` | 1.0 |
| 5 | `report.py`, `approve`, `apply` | 1.5 |
| 6 | Tests, end-to-end run on the sample, tune the prompt until the sample classifies cleanly | 1.0 |
| 7 | README, DESIGN.md, record the video | 0.75 |

## 14. Cut from v2 and why

- Keyword scoring for series, fallback heuristics, legal holds, `report` and
  `status` commands, eml-directory input, `--hard`, `review.csv`, JSONL audit
  mirror, `llm_may_propose` flag, remote-host flag, any "if the model is not
  present" branch. Each was a second way to do something the single path
  already does.

## 15. Known limitations (go in DESIGN.md)

- Attachments are not opened; PII inside a PDF is not detected.
- A 3B model will misclassify some edge cases. It is instructed to prefer
  RECORD when unsure, and the director reviews every proposal.
- mbox removal rewrites the file. Fine for exports; production would call the
  archive product's delete API with the same approve/quarantine/audit pattern.
- Records past their retention period are kept and counted, not dispositioned.
- Throughput is model-bound: roughly 1 message per second after header rules.
  A 2-million-message backlog would run for days on this hardware; a bigger
  GPU or batching is the production answer.
