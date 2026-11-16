# ModernEdge AI Solutions Engineer Assessment: Candidate Package

Read the assessment brief first. This package contains everything the brief refers to.

```
data/              files the client handed over (see docs/data_guide.md)
mock_systems/      the client's payroll, dispatch and finance systems, run locally
docs/              union rules, KPI definitions, backup note templates, data guide
```

## Getting started

1. Start the client systems: `cd mock_systems && docker compose up --build`.
2. Read `docs/data_guide.md`, then each API's `API.md`.
3. Build your system in your own repository. Do not modify `data/` or `mock_systems/`.

## Required outputs

Your system must start with one command (`docker compose up`) and, on a fresh run, read every input from `./data` and the three APIs, then write the files below into `./output`. We will run it against a second dataset with the same structure, so nothing from this dataset may be hard-coded.

Your system must take these settings from environment variables:

| Variable | Meaning |
|---|---|
| `DATA_DIR` | Folder holding the client files (default `./data`) |
| `PAYROLL_URL`, `DISPATCH_URL`, `FINANCE_URL` | Base URLs of the three client systems |
| `ANTHROPIC_API_KEY` or your model provider's key | If your system calls a model |

### output/kpis_by_building.csv

One row per building, with these columns in this order:

```
building, contracted_headcount, active_headcount, headcount_variance, scheduled_shifts,
callout_shifts, callout_rate, fill_rate, overtime_hours, overtime_dollars, revenue,
labor_cost, gross_margin, gross_margin_pct, unbilled_coverage_count, unbilled_coverage_dollars
```

`building` is the name in the client agreement. Rates and percentages are decimals (0.0425, not 4.25%). Dollar figures have two decimals and no symbols or separators.

### output/ar_aging.csv

```
bucket, open_amount
```

One row each for `current`, `1_30`, `31_60`, `61_90`, `over_90`.

### output/payroll_submissions.csv

One row per absence your system decided on, whether or not it was written to payroll:

```
source, source_ref, employee_id, date, code, decision, submission_id, final_status, reason
```

- `source`: `reception_log` or `area_manager_chat`
- `source_ref`: the log row number or the chat message timestamp
- `employee_id`: the payroll employee ID
- `decision`: `written` or `held_for_review`
- `final_status`: the status payroll reports after processing (blank if held)

### output/backup_notes/

One text file per billable coverage shift, named `<posting_id>.txt`, in the template its client requires.

### output/exceptions.csv

Every record your system could not resolve or chose to flag:

```
source, record_ref, issue, action_taken, confidence
```

### output/decisions_log.md

Every place the sources conflicted or the materials were ambiguous, what you decided and why.

## Writing to payroll

Payroll is a system of record. Every absence must end up in payroll exactly once with the correct code, or be held for review with a reason. When we evaluate, we reset payroll, run your system once from a clean start, and then inspect every submission payroll holds.
