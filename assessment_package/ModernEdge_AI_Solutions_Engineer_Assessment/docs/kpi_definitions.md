# KPI Definitions

These definitions govern every figure in your KPI panel and in `output/kpis_by_building.csv`. Where a definition and the data appear to disagree, follow the definition and record what you found in your decisions log.

## Scope

- **Period:** October 1 to October 31, 2026. Shifts run Monday to Friday only.
- **Shifts:** Day runs 07:00 to 15:00 and Night runs 16:00 to 00:00. Every shift is 8 paid hours. A night shift belongs to the date it starts, even though its last punch falls after midnight.
- **Buildings:** identify each building by its name exactly as written in Schedule B of its client agreement. Use that name in every output.
- **Contract terms:** the client agreements in `data/contracts/`, as amended. An amendment overrides the agreement from its effective date.
- **Pay rates:** the 2026 rates in the wage schedule for union employees, and the rate card for non-union and temporary employees. Burden is set out in the same document.
- **Home building:** the building in the HR roster, updated by any Personnel Action Form that carries all three approvals. A form missing an approval has no effect.
- **Paid hours:** a shift is paid at its scheduled 8 hours when the employee worked it. Punches establish attendance, not paid hours.

## Operations

**Contracted headcount**
Day posts plus night posts for the building under the agreement in force on October 31, 2026.

**Active headcount**
Permanent employees whose home building on October 31, 2026 is this building, hired on or before that date and not terminated. Floaters and temps are excluded.

**Headcount variance**
Active headcount minus contracted headcount.

**Scheduled shifts**
For every permanent employee, each October weekday on which they were employed (hire date through termination date, both inclusive), counted at the employee's home building on that day.

**Call-out shifts**
Shifts lost to a call-out, at the building the employee was assigned to that day. A call-out counts whichever way it reached the company: the reception log or the area managers' chat. One call-out shift per employee per date: the same absence reported twice, or through two channels, counts once. Calls that are not absences are excluded. Planned vacation is not a call-out.

**Call-out rate**
Call-out shifts divided by scheduled shifts, as a decimal.

**Fill rate**
Filled postings divided by all postings for the building in the dispatch app, as a decimal. Call-out and vacation postings both count. Cancelled postings are excluded.

**Overtime hours**
Every coverage shift filled on overtime adds 8 overtime hours, because the covering employee works beyond 8 hours that day. Overtime is attributed to the building where the coverage shift was worked.

**Overtime dollars**
Overtime hours multiplied by the covering employee's base rate multiplied by 1.5. Wages only: burden is excluded.

## Financial

**Revenue**
The sum of every invoice line with service period 2026-10 for the building. That includes contract, coverage, sundry and credit lines, whatever the invoice date and whichever legal entity issued the invoice.

**Labor cost**
The building's department's labor cost in the general ledger for period 2026-10, across every account and both legal entities.

**Gross margin**
Revenue minus labor cost. Gross margin percent is gross margin divided by revenue, as a decimal.

**Unbilled coverage**
At buildings whose agreement makes coverage labor billable, a filled coverage posting (call-out or vacation) with no matching coverage line on any invoice. Report the count and the billable amount.

The billable amount of one coverage shift is:

```
8 hours × base rate × overtime multiplier (1.5 if filled on overtime, otherwise 1.0)
        × (1 + burden) × (1 + cost-plus markup)
```

Base rate and burden belong to the covering employee. Round each shift to the cent before adding.

**AR aging**
Open accounts receivable as of October 31, 2026, across both legal entities. The open balance of an invoice is its amount less the payments applied to it. A credit memo is open at its own amount and is aged from its document date. Age every open item by days past its due date:

| Bucket | Days past due |
|---|---|
| `current` | 0 or fewer (not yet due) |
| `1_30` | 1 to 30 |
| `31_60` | 31 to 60 |
| `61_90` | 61 to 90 |
| `over_90` | more than 90 |
