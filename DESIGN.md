# Design

## The problem

A local government archive holds about 20 years of email and grows by hundreds
of messages a day. Only spam is deleted today. The records retention policy
says transitory mail (scheduling, acknowledgements, newsletters, notifications)
can go once its short-term value has passed, while records (contracts,
personnel, finance, legal, public records requests, official correspondence)
must be kept for their scheduled period. HIPAA, PII and PCI content cannot be
sent to outside services. A central services director is the final decision
maker.

## What the tool does

One command, `python -m retain run archive.mbox`, runs this pipeline and stops
once for the director:

```
mbox ──> parse ──> [1] sensitivity regex ──> [2] header rules ──> [3] local model ──> decision
                                                                                        │
                         retain.db (SQLite) <────────────────────────────────────────────┘
                                │
                   report/summary.html + report/decisions.csv
                                │
                   director edits "action" column, presses Enter, confirms
                                │
                   quarantine/run-N.mbox  +  audit table
```

Every message lands in one of three buckets:

| Bucket | Decisions | What happens |
|---|---|---|
| KEEP | `RETAIN` (a record, with its retention end date), `RETAIN_PENDING` (transitory but inside the 30-day window) | stays |
| DELETE PROPOSED | `DELETE_PROPOSED` (transitory, older than the window) | pre-filled `delete` in the CSV; removed only if the director leaves it that way and confirms |
| NEEDS REVIEW | `REVIEW_SENSITIVE` (PII/PHI/PCI detected), `REVIEW` (model unsure, or no date) | listed with the reason; never auto-proposed |

## The three stages

**Stage 1, sensitivity regex.** Runs over subject, body, and attachment
filenames. SSN in dashed form with the invalid-prefix exclusions; 13 to 19
digit card numbers that pass the Luhn checksum; PHI as an MRN token, an ICD-10
code near a health keyword, or three or more distinct health keywords. Any hit
ends the pipeline: the message goes to review and is never sent to the model.
Only the flag names are stored, never the matched values.

**Stage 2, header rules.** Four mechanical transitory types that headers
identify with certainty: a `text/calendar` part, an `Auto-Submitted` header or
out-of-office subject, `List-Unsubscribe` / `List-Id` / bulk precedence, and
no-reply or notification senders. These cost nothing and need no model.

**Stage 3, local model.** Everything else goes to Ollama on `127.0.0.1` with a
prompt that lists the policy's series (id, name, description) and asks for
strict JSON: `klass` of RECORD, TRANSITORY or UNSURE, a series id when RECORD,
and a one-sentence reason. The model is told to answer RECORD when torn, so the
failure direction is over-retention. The payload is the subject, sender,
attachment names, and up to 1,500 characters of the message's own text with
quoted replies stripped. Temperature is zero. Unusable output gets one retry
and then becomes UNSURE. An invalid series id on a RECORD answer falls back to
the policy's `default_series` (administrative correspondence) rather than
dropping the record.

**Decision table**, first matching row wins:

| Condition | Decision |
|---|---|
| sensitivity flag | `REVIEW_SENSITIVE` |
| RECORD | `RETAIN`, retain_until = date + series years (null when permanent) |
| no Date header | `REVIEW` |
| TRANSITORY, older than the window | `DELETE_PROPOSED` |
| TRANSITORY, inside the window | `RETAIN_PENDING` |
| UNSURE | `REVIEW` |

## Retention schedule

`policy.json` uses Florida GS1-SL, the General Records Schedule for State and
Local Government Agencies, 2017 edition, because it is public, widely adopted,
and has an explicit Transitory Messages item. Item numbers and retention wording
are quoted from the schedule's published table:

| Series id | GS1-SL item | Retention as written |
|---|---|---|
| TRANSITORY | 146 | Retain until obsolete, superseded, or administrative value is lost. |
| CONTRACTS | 65 | 5 fiscal years after completion or termination of contract/lease/agreement. |
| PERSONNEL | 162 | 50 fiscal years after any manner of separation or termination of employment. |
| DISBURSEMENT | 340 | 5 fiscal years after transaction completed. |
| LITIGATION | 27 | 5 anniversary years after case closed or appeal process expired. |
| INFO_REQUEST | 23 | 1 fiscal year. |
| MINUTES | 32 | Permanent. |
| PERMITS | 286 | 10 anniversary years. |
| GRANTS | 422 | 5 fiscal years after completion of grant cycle or project. |
| POLICY_CORR | 338 | 5 fiscal years. |
| ADMIN_CORR | 17 | 3 fiscal years. |

The schedule's transitory item says "until administrative value is lost"; the
agency sets that as a number of days (`transitory.window_days`, default 30).
An agency in another state swaps the series entries for its own schedule. The
code reads everything about a series from this file and hardcodes none of it.

## Data residency

- The only network code is the Ollama call. Its host is checked and must be
  `127.0.0.1`, `localhost` or `::1`; anything else aborts the run.
- Sensitive-flagged messages are decided by regex alone and are never sent to
  the model.
- The database stores subjects, senders, sizes, decisions and reasons. Never
  bodies, never matched sensitive values.
- The report and database contain internal subjects and addresses and belong
  on the tenant's own storage next to the archive.
- Quarantine before removal, always. Every classification, model query,
  approval and quarantine is a row in the `audit` table.

## State and daily operation

SQLite, two tables. `messages` keyed by normalized `Message-ID` (sha256 of the
raw bytes when missing). `run` skips keys it has seen, so a daily run
classifies only the new mail. It then recomputes every pending row's decision
from its stored class and today's date, which is how `RETAIN_PENDING` becomes
`DELETE_PROPOSED` 30 days later without re-reading mail or re-querying the
model. Approvals survive re-runs: a `retain` approval pins the message as KEEP
on every future run, and applied rows are never touched again. If the director
stops at the pause, the `action` column they edited is carried into the next
run's `decisions.csv`, but approvals are only recorded when someone presses
Enter in an interactive run, so an unattended scheduled run can never approve
anything. The mbox position of every message is refreshed on each run because
removal renumbers the file.

## Synthetic data

There is no real export to test on, so `generate` builds one: 300 messages
over 20 years weighted toward recent years, in roughly the mix a city inbox
would have, plus planted edge cases (missing Date header, duplicate
Message-ID, a fake SSN, a Luhn-valid test card number, an MRN). The seed makes
it reproducible.

## What was deliberately left out

- **Direct archive connectors.** Exchange, Microsoft Graph, Barracuda and the
  like all export mbox; the logic is proven on that. A connector would replace
  the mbox read and the mbox rewrite with API calls and keep everything else.
- **Rules-only fallback.** One path only. If Ollama is down the tool stops and
  says so.
- **Disposition of expired records.** Records past `retain_until` are kept.
  Formal disposition usually needs an authorization form; the data to drive it
  is in the database.
- **Thread reasoning.** A one-line "thanks" that quotes a contract is
  classified on its own text; the parent message is its own record.
- **Attachment contents.** Filenames are read, bodies are not. PII inside a PDF
  is not detected.

## Known limits

- A 3B model will misjudge edge cases. Over-retention is the chosen failure
  direction, and the director reviews every proposal.
- Throughput is model-bound at roughly one message per second after header
  rules. A multi-million-message backlog needs a bigger GPU, a batched model
  server, or a first pass that only runs stages 1 and 2.
- Removing from an mbox rewrites the file. Fine for exports; production would
  call the archive product's delete API.
- Only dashed SSNs are detected, to avoid matching phone and invoice numbers.

## Reading the code

Every module opens with a docstring that says what it owns and how it fits;
start with `retain/__init__.py` for the map, then `retain/__main__.py`, whose
`cmd_run` is the product's control flow top to bottom. Function docstrings say
what each one returns and why it exists; inline comments are reserved for
decisions that are not obvious from the code (why SSNs must be dashed, why the
host check avoids `urllib.parse`, why removal copies before it deletes).

`policy.json` carries its own field guide under the `_about` and `_fields`
keys; the code ignores keys that start with an underscore.

Deliberate shortcuts with a known ceiling are marked `ponytail:` in the source
so they can be found with one grep. There is one: retention end dates use
365-day years rather than the schedule's fiscal-year boundaries.
