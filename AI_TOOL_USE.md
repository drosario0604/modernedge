# AI tool use

I used Cursor (Composer) while building this. I directed the work, reviewed the diffs, and can explain or change every line.

**Assisted by the tool:** scaffolding (FastAPI/React/Compose), API client boilerplate, first-pass parsers, KPI formula wiring from the package docs, and draft docs that I then edited.

**Done / checked by me:** Schedule B and Corlett amendment headcount, Art. 14 coding in `callouts/rules.py` (no LLM on the code path), payroll dry-run behavior, margin and unbilled formulas against `kpi_definitions.md`, live runs against the mock APIs, and the decisions in `output/decisions_log.md`.

**At runtime:** with `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`, the app may call a model for call-out structuring and PAF OCR. Coding, notice, balances, and KPIs stay deterministic. Without a key, heuristics still produce outputs.
