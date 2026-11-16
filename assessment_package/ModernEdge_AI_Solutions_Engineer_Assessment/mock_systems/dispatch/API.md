# Dispatch API v2

The dispatch app is where coordinators post open shifts and assign coverage. This API gives read access to sites, workers and shift postings.

## Base URL

`http://localhost:8082` when the client systems are running (see `mock_systems/README.md`).

## Authentication

OAuth 2.0 client credentials. Request a token, then send it as a bearer token on every `/v2/` request.

```
POST /oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id=me-assess&client_secret=d1sp4tch-s3cret
```

```json
{"access_token": "...", "token_type": "Bearer", "expires_in": 300}
```

Tokens expire after `expires_in` seconds. An expired token returns `401` with `"error": "invalid_token"`.

## Pagination

List endpoints return `data` and `next_cursor`. Pass `next_cursor` back as `cursor` to get the next page. `limit` accepts 1 to 50.

## Rate limits

Rate limits apply. A client over the limit receives `429` with a `Retry-After` header.

## Endpoints

### GET /v2/sites
All sites, including inactive ones.

| Field | Notes |
|---|---|
| `site_id` | Dispatch site ID |
| `name` | Site name in the dispatch app |
| `timezone` | IANA time zone of the site |
| `active` | Whether the site can receive postings |

### GET /v2/workers
Every person with a dispatch login.

| Field | Notes |
|---|---|
| `worker_id` | Dispatch worker ID |
| `display_name` | Name shown in the app |
| `hr_employee_no` | HR employee number, entered when the login was created |
| `phone_last4` | |
| `active` | |

### GET /v2/workers/{worker_id}

### GET /v2/postings
Shift postings.

| Parameter | Notes |
|---|---|
| `limit`, `cursor` | Pagination |
| `updated_since` | ISO 8601 UTC timestamp |
| `site_id` | Filter by site |
| `status` | Filter by status |

| Field | Notes |
|---|---|
| `posting_id` | |
| `site_id` | |
| `shift_date` | Local date of the shift |
| `shift` | `D` (day) or `N` (night) |
| `start_local` | Local start time |
| `reason` | `CALLOUT` or `VACATION` |
| `absent_employee_text` | Free text typed by the coordinator |
| `status` | `OPEN`, `FILLED`, `EXPIRED` (shift started unfilled) or `CANCELLED` |
| `created_at`, `updated_at` | UTC |
| `events` | Ordered history of the posting |

Event types: `POSTED`, `OFFERED`, `DECLINED`, `ASSIGNED`, `UNASSIGNED`, `ESCALATED`, `EXPIRED`, `CANCELLED`. `ASSIGNED` carries `worker_id` and `fill_type` (`STANDARD` for a floater or temp, `OT` for an employee working an extra shift on overtime).

### GET /v2/postings/{posting_id}
