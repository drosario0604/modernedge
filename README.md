# ModernEdge Assessment

Data spine and call-out → payroll flow for Northgate building services (Oct 2026). React UI for building status, KPIs, chat, and review.

## Quick start

**1. Client systems** (do not modify `data/` or `mock_systems/`):

```bash
cd assessment_package/ModernEdge_AI_Solutions_Engineer_Assessment/mock_systems
docker compose up --build
```

Without Docker (Python 3.12): `.\scripts\run_local_mocks.ps1`

**2. Config**

```bash
cp .env.example .env
```

Set `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY`) if you want LLM call-out extraction and PAF OCR. Keep `PAYROLL_DRY_RUN=true` until payroll writes are verified.

**3. This app**

```bash
docker compose up --build
```

- API http://localhost:8000  
- UI http://localhost:5173  

`docker compose --profile mocks up --build` starts mocks from this compose file as well.

Local (SQLite) without Docker for the app:

```powershell
$env:PYTHONPATH = "$PWD\app\backend"
$env:DATA_DIR = "$PWD\assessment_package\ModernEdge_AI_Solutions_Engineer_Assessment\data"
$env:OUTPUT_DIR = "$PWD\output"
$env:DATABASE_URL = "sqlite:///./modernedge.db"
python -c "from app.ingest.pipeline import run_pipeline; print(run_pipeline())"
cd app\frontend; npm install; npm run dev
```

Or `POST /api/pipeline/run` / **Run pipeline** in the UI.

## Outputs (`OUTPUT_DIR`)

| File | Contents |
|---|---|
| `kpis_by_building.csv` | Building KPIs (Schedule B names) |
| `ar_aging.csv` | AR buckets as of 2026-10-31 |
| `payroll_submissions.csv` | Each coded absence (written or held) |
| `backup_notes/<posting_id>.txt` | Cost-plus coverage notes |
| `exceptions.csv` | Unresolved / flagged records |
| `decisions_log.md` | Conflicts and calls |

Env: `DATA_DIR`, `OUTPUT_DIR`, `PAYROLL_URL`, `DISPATCH_URL`, `FINANCE_URL`, `DATABASE_URL`, `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`, `PAYROLL_DRY_RUN`. See `.env.example`.

## Design

```
files + APIs → ingest → resolve → DB
  → KPIs / AR → output CSVs
  → call-out extract → Art. 14 coding → payroll (idempotent, dry-run)
  → backup notes → FastAPI → React
```

Absence codes, notice, balances, and KPI math are code. LLM is used only for structuring call-outs and OCR on scanned PAFs (heuristics if no key). Building/employee maps come from contracts and IDs, not hard-coded dataset rows.

## Margin and unbilled coverage

Gross margin = Oct revenue for the building − GL labor for its department(s). Margin % = margin ÷ revenue.

Unbilled coverage (cost-plus only): filled call-out/vacation posting with no matching coverage invoice line:

```
8 × base rate × OT mult (1.5 if OT else 1.0) × (1 + burden) × (1 + markup)
```

per shift, rounded to the cent, then summed. Burden 32% union / 18% non-union.

## Deferred

- Broader chatbot coverage beyond KPI/ops lookups
- Labor budget as a primary source (contracts, HR, and APIs win)
- UI polish past the operational panels

## Notes

- [output/decisions_log.md](output/decisions_log.md)
- [AI_TOOL_USE.md](AI_TOOL_USE.md)
