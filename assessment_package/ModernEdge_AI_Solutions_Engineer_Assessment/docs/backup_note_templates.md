# Coverage Backup Note Templates

Cost-plus clients pay for coverage labor only when the invoice line carries a backup note for the shift. Exhibit C of each cost-plus client agreement says what that client requires. ModernEdge uses two templates to meet those requirements. Choose the right template for each building from its agreement, and produce one note per billable coverage shift.

## Template A: full detail

For clients that require the replacement's name and actual time on site.

```
COVERAGE BACKUP NOTE
Client:            <client name>
Building:          <building name>
Date of service:   <MM/DD/YYYY>
Shift:             <Day | Night>, <scheduled start> to <scheduled end>
Position covered:  <job class of the absent employee>
Coverage type:     Replacement | Overtime
Replacement:       <covering employee full name>
On site:           <first clock-in> to <last clock-out>
Billable hours:    8.00
Billable amount:   $<amount>
Posting reference: <dispatch posting id>
```

## Template B: no names

For clients that require confirmation of coverage without the names of personnel.

```
COVERAGE BACKUP NOTE
Client:            <client name>
Building:          <building name>
Date of service:   <MM/DD/YYYY>
Shift:             <Day | Night>
Position covered:  <job class of the absent employee>
Coverage:          Replacement provided
Billable hours:    8.00
Billable amount:   $<amount>
Posting reference: <dispatch posting id>
```

## Rules for both templates

- Never name the absent employee and never state why they were absent.
- The billable amount follows the KPI definitions: 8 hours × the covering employee's base rate × 1.5 if on overtime × (1 + burden) × (1 + markup), rounded to the cent.
- When a clock-in or clock-out is missing for a Template A note, write `Not recorded` for that time and add the shift to your exceptions.
