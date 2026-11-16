# Data Guide

This is what the client handed over at discovery. Each source comes from a different system or team, and they were never designed to agree. Names, identifiers, building references, formats and time zones differ from one source to the next. Column descriptions below are as the client's teams described them. They are not guarantees.

## Files

### hr/hr_roster.csv (HR system)
One row per employee record, exported October 31, 2026.

| Column | Meaning |
|---|---|
| `employee_id` | HR employee ID |
| `last_name`, `first_name`, `middle_initial` | Legal name |
| `job_class` | Cleaner, Porter, Handyperson or Supervisor |
| `union_member` | `Y` or `N` |
| `local_member_no` | Union membership number |
| `home_location` | HR location code, or `FLOAT` for the float pool |
| `department` | Ledger department the employee is charged to |
| `shift` | `DAY`, `NIGHT`, or `FLEX` for floaters and temps |
| `employment_type` | `PERMANENT`, `FLOATER` or `TEMP` |
| `hire_date`, `termination_date` | |
| `status` | `ACTIVE` or `TERMINATED` |
| `sick_days_remaining_2026_10_01` | Paid sick days left for 2026 as of October 1 |
| `clinic_day_used_2026` | Whether the annual clinic day had been used before October |
| `primary_language` | |

### hr/personnel_action_forms/ (scanned paper forms)
Personnel Action Forms received by HR in October, scanned from paper. HR does not always update the roster from them.

### time_attendance/ (building time clocks)
Punches for October. The clock vendor pushed a firmware update on October 19, which changed the export layout.

- `ta_punches_2026-10-01_to_2026-10-18.csv`: `badge_no`, `employee_name`, `site`, `punch_type`, `punch_time` (local), `device_id`
- `ta_punches_2026-10-19_to_2026-10-31.csv`: `EmpBadge`, `Name`, `SiteCode`, `Event`, `Timestamp`, `Terminal`
- `ta_site_codes.csv`: site codes introduced by the update

### labor_budget/labor_budget_FY26.xlsx (operations spreadsheet)
The labor budget operations used to maintain by hand. Nobody has owned it since the administrator left.

### callouts/callout_log_october_2026.xlsx (reception spreadsheet)
Calls about attendance that reached reception, as reception typed them. Live calls are answered 8:30 a.m. to 5:30 p.m. on weekdays. Outside those hours calls go to voicemail, which reception transcribes the next working morning. `VM Received` is the phone system's timestamp for a voicemail, when it recorded one.

### callouts/night_ops_group_chat_export.txt (messaging app export)
The area managers' group chat for October, exported from a phone. Some employees text their area manager instead of calling reception.

### contracts/ (signed documents)
- One master services agreement per client, with Schedule B (buildings, posts and pricing) and, for cost-plus clients, Exhibit C (coverage backup requirements)
- Amendments to those agreements
- The collective bargaining agreement's wage schedule, with the employer's rate card for non-union staff and the labor burden

## Systems reached by API

See `mock_systems/README.md`.

- **Dispatch app:** sites, workers and every shift posting with its event history.
- **Finance ERP:** legal entities, the customer master, the finance site to department map, and three reports per legal entity: invoice register, open receivables and labor cost detail from the general ledger.
- **Payroll:** the system of record for absences. You read employees and write absences.

## Reference documents (docs/)

- `union_rules.pdf`: Article 14 of the collective bargaining agreement and the payroll coding guide
- `kpi_definitions.md`: how every KPI is calculated
- `billing_note_formats.md`: the two backup note templates
