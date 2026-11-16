# Payroll Absence API

Version 2.3.1

This is the absence interface to the payroll system of record. It is the only supported way to record a call-out absence in payroll. It runs locally for this assessment.

## Base URL

`http://localhost:8081` when the client systems are running (see `mock_systems/README.md`). Data persists in a Docker volume between restarts. To start clean, call `POST /v1/admin/reset`.

## Authentication

Every request under `/v1/` except `/v1/health` must carry the header:

```
X-API-Key: assess-7f3c9a
```

## Endpoints

### GET /v1/health
Returns `{"status": "ok"}`.

### GET /v1/codes
Returns the absence codes payroll accepts.

### GET /v1/employees
Lists employees as payroll holds them.

| Parameter | Default | Notes |
|---|---|---|
| `page` | 1 | |
| `page_size` | 25 | |

Response fields: `employee_id` (integer), `name`, `status`.

### POST /v1/absences
Records one absence for one employee on one date.

```json
{
  "employee_id": 10023,
  "date": "2026-10-14",
  "code": "SKP",
  "hours": 8,
  "note": "Call-out received 10/14 09:12"
}
```

| Field | Type | Rules |
|---|---|---|
| `employee_id` | integer | Payroll employee ID |
| `date` | string | `YYYY-MM-DD`, inside the open pay period (October 2026) |
| `code` | string | One of the codes from `/v1/codes` |
| `hours` | number | Greater than 0 and at most 12, up to 2 decimals |
| `note` | string | Optional |

Returns `202 Accepted`:

```json
{"submission_id": "SUB-3F9A0C12BE", "status": "RECEIVED"}
```

Validation errors return `422` with an `error` code (`INVALID_DATE`, `PERIOD_CLOSED`, `INVALID_CODE`, `INVALID_HOURS`).

### GET /v1/absences/{submission_id}
Returns one submission and its current `status` (`RECEIVED` or `POSTED`).

Submissions are processed in batches shortly after they are received.

### GET /v1/absences
Lists submissions.

| Parameter | Notes |
|---|---|
| `employee_id` | Filter by employee |
| `date` | Filter by date, `YYYY-MM-DD` |
| `status` | Filter by status |
| `page`, `page_size` | Default page size 50 |

### DELETE /v1/absences/{submission_id}
Voids a submission. A voided submission no longer counts in payroll.

### POST /v1/admin/reset
Deletes all submissions. For development use.

## Availability

The service may return `503 Service Unavailable` during processing windows. Clients should retry.
