# Client Systems

Three of the client's systems are reachable only through their APIs. This folder runs local copies of them.

| System | Base URL | Docs |
|---|---|---|
| Payroll (system of record for absences) | `http://localhost:8081` | `payroll/API.md` |
| Dispatch app (shift postings and coverage) | `http://localhost:8082` | `dispatch/API.md` |
| Finance ERP (reference data and reports) | `http://localhost:8083` | `finance/API.md` |

## Start

```
cd mock_systems
docker compose up --build
```

To run without Docker you need Python 3.12 exactly:

```
pip install -r requirements.txt
export SEED_BUNDLE=$PWD/seed.bin PAYROLL_DB=$PWD/payroll.db
uvicorn app:app --app-dir payroll --port 8081 &
uvicorn app:app --app-dir dispatch --port 8082 &
uvicorn app:app --app-dir finance --port 8083 &
```

## Rules

- Treat these as the client's production systems. Your system must get its data through the APIs, as it would at the client. Do not attempt to read `seed.bin` or decompile the services.
- Payroll is the only system you write to. Use `POST /v1/admin/reset` to clear your test writes.
- Your own `docker compose` must reach these services through the environment variables `PAYROLL_URL`, `DISPATCH_URL` and `FINANCE_URL`, so we can point it at our copies when we evaluate.
