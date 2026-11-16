# Decisions log

## Buildings and identity

1. Canonical building names are Schedule B of each MSA (e.g. `Meridian One`, `Bramwell Annex`). Outputs use those names, not HR codes or clock site labels.
2. Aliases via normalized fuzzy match (`Meridian 1` ↔ `Meridian One`, `BRAMWELL ANX` ↔ `Bramwell Annex`, etc.). `S-106` and `S-116` both map to Bramwell Annex.
3. HR `BLD-0x` → building from labor-budget majority vote on employees; FLOAT stays unassigned.
4. Corlett Amendment No. 2 (eff. 2026-09-01): night posts 7 → 8. Contracted headcount on 2026-10-31 is **11**.
5. Scanned PAFs: OCR when an API key is set; apply home-building change only if all three approvals are present. Otherwise exception, no roster change.

## Employees and rates

6. Payroll match: HR `E10001` → payroll `10001`, then name keys.
7. Dispatch: `hr_employee_no`, then display name.
8. Covering employee = `ASSIGNED` `worker_id` linked via `dispatch_worker_id` (OT $ and backup notes).
9. 2026 CBA rates for union; employer rate card for non-union/temp. Burden 32% / 18%.
10. Paid hours are 8 for a worked shift; punches are attendance evidence only.

## Call-outs and payroll

11. Sources: reception log (`reception_log` + row) and area-manager chat (`area_manager_chat` + timestamp). Non-absences dropped.
12. One call-out shift per employee per date across channels.
13. Notice: phone = log time; voicemail = `VM Received` when present; chat = message time (or explicit time in text).
14. Shift starts Day 07:00 / Night 16:00. Under four hours notice → LTN when the leave would otherwise be paid.
15. Sick/clinic: union + ≥90 days. BRV/JRY all employees. Family → FEM. Personal / ineligible clinic → PER.
16. Sick balance from roster as of Oct 1, consumed in date order. One clinic day max.
17. Vacation is not a call-out; vacation postings still count for fill rate and may be billable at cost-plus sites.
18. Low confidence or missing payroll ID → `held_for_review`; no invented write.
19. Default dry-run (`PAYROLL_DRY_RUN=true`). Live mode: POST absences with 503 retries; skip if employee+date+code already exists.

## Financial KPIs

20. Revenue: INV_REG lines with service period 2026-10 for the building, both entities.
21. Labor cost: GL_LABOR for the building’s department(s), 2026-10, both entities.
22. AR aging as of 2026-10-31 by days past due (`current`, `1_30`, `31_60`, `61_90`, `over_90`).
23. Unbilled coverage only for cost-plus (Meridian 15%, Corlett 12%). Fixed-fee buildings → 0.
24. Backup notes: Meridian Template A; Corlett Template B. Never name the absent employee.

## Other

25. Chat answers only from stored data; says when it cannot.
26. Finance `FAILED` jobs (e.g. ORA-01555) are resubmitted per API.md until COMPLETE.
