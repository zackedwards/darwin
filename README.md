# retain — Transitory Email Cleanup

Cleans a local-government email archive against a records retention schedule.
Every message is classified as a record to keep, a transitory message proposed
for deletion, or something a human must look at. Nothing is deleted until the
director approves it, and deleted mail goes to a quarantine file first.

No email content leaves the machine. Classification uses a model running
locally through Ollama on `127.0.0.1`; the tool refuses any other host.

## Install

1. Python 3.11 or newer. No packages to install; the tool is standard library only.
2. [Ollama](https://ollama.com/download), then pull the model:

```bash
ollama pull llama3.2:3b
```

Ollama runs as a background service after install. If `run` says it cannot
reach Ollama, start it from the Start menu or with `ollama serve`.

## Run

```bash
python -m retain run path/to/archive.mbox
```

That does everything, in order, with a print per message:

1. Checks the policy, the archive, the model, and what was seen on earlier runs.
2. Scans messages not seen before: sensitivity regex, then header rules, then the model.
3. Re-decides every stored message against today's date, so transitory mail ages into the deletion window.
4. Prints bucket totals and writes `report/summary.html` and `report/decisions.csv`.
5. **Pauses.** The director opens `decisions.csv`, sets the `action` column to `delete` or `retain` per row (proposed rows are pre-filled `delete`), saves, and presses Enter. Ctrl-C here is safe; the next run picks up the edited file.
6. Reads the approvals, shows what will be quarantined, asks `Proceed? [y/N]`.
7. Copies approved messages into `quarantine/run-N.mbox`, removes them from the archive, prints before/after, and logs every action in the `audit` table of `retain.db`.

Run it again tomorrow and it only classifies the new mail.

Options, all with defaults: `--policy policy.json`, `--db retain.db`, `--report report`,
`--quarantine quarantine`, `--ollama http://127.0.0.1:11434`, `--model llama3.2:3b`,
`--by "Director Name"` (recorded on approvals).

## Demo (about 5 minutes)

```bash
python -m retain generate sample/archive.mbox --count 300 --seed 1
```

```bash
python -m retain run sample/archive.mbox
```

The scan takes one to three minutes on a workstation GPU (about 120 of the 300
messages reach the model; the rest are decided by regex or headers). Run
`ollama run llama3.2:3b` once beforehand so the model is already loaded. While
it scans, point out the three stages in the per-message lines: `header`
decisions are free and certain, `sensitive` rows never reach the model, `model`
rows show the series and the model's one-sentence reason. At the pause, open
`report/summary.html` and `report/decisions.csv`, flip one `delete` to
`retain`, save, press Enter, confirm. Then simulate a day of new mail:

```bash
python -m retain generate sample/archive.mbox --append 30
```

```bash
python -m retain run sample/archive.mbox
```

Only the 30 new messages are classified. Ctrl-C at the pause.

To prove the data-residency claim:

```bash
grep -rn "http" retain/*.py
```

The only URL is the loopback default, and `classify.Model` rejects anything else.

## Test

```bash
python -m unittest
```

Fifteen cases: one per decision-table row, the Luhn check, the PHI regex, quote stripping. The model is faked; no Ollama needed.

## Policy file

`policy.json` is the retention schedule. The shipped one uses Florida GS1-SL
(2017 edition) with ten series and the Transitory Messages item (#146), each
with the item number and retention wording quoted from the schedule. To adapt
it: change `agency_domain`, edit `transitory.window_days`, and add or remove
series entries. The model reads `id`, `name`, and `description` from this file,
so a clear description is what makes a new series classify well. Verify item
numbers against your own state's current schedule before production use.

## Files

| Path | Purpose |
|---|---|
| `retain/__main__.py` | the `run` and `generate` commands |
| `retain/classify.py` | mbox parsing, sensitivity regex, header rules, Ollama call, decision table |
| `retain/store.py` | SQLite state and audit |
| `retain/report.py` | `decisions.csv` and `summary.html` |
| `retain/generate.py` | synthetic demo archive |
| `policy.json` | retention schedule |
| `tests/` | unit tests |
| `DESIGN.md` | design, decisions, limitations |

See `DESIGN.md` for how it works and what it deliberately does not do. Each
module's docstring explains its role; `retain/__init__.py` is the map and
`retain/__main__.py` is the control flow.
