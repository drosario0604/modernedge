# Finance Reporting API

The client's ERP exposes reference data and a report service. Transactional data (invoices, receivables, general ledger) is only available as reports.

## Base URL

`http://localhost:8083` when the client systems are running (see `mock_systems/README.md`).

## Authentication

HTTP Basic authentication on every `/api/` request.

```
username: svc_assess
password: Q4-close!
```

## Reference data

### GET /api/v1/entities
Legal entities in the ERP.

### GET /api/v1/customers
Customer master. Parameters: `offset`, `limit` (up to 100), `entity_code`.

### GET /api/v1/sites
Finance site names and the ledger department each maps to.

## Reports

Reports run asynchronously. Submit a job, poll until it finishes, then download the file.

### POST /api/v1/reports

```json
{"report_code": "INV_REG", "entity_code": "NBS", "period": "2026-10"}
```

| Field | Notes |
|---|---|
| `report_code` | `INV_REG` (invoice register), `AR_OPEN` (open receivables), `GL_LABOR` (labor cost detail) |
| `entity_code` | Required. One of the codes from `/api/v1/entities` |
| `period` | `YYYY-MM` |

Returns `202` with a `job_id`.

### GET /api/v1/reports/{job_id}
Job status: `QUEUED`, `RUNNING`, `COMPLETE` or `FAILED`. A `COMPLETE` job includes `file_url`. A `FAILED` job includes `error`; submit the report again.

### GET /api/v1/reports/{job_id}/file
The report file, exactly as the ERP prints it.

| Report | Format |
|---|---|
| `INV_REG` | Comma separated, with report banner lines above the column header |
| `AR_OPEN` | Pipe delimited |
| `GL_LABOR` | Fixed width print layout |

## Errors

Errors return a JSON body with `faultcode` and `faultstring`.
