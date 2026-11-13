# Australian payroll (FY2026-27)

Built from **GPE-AU-LOGIC v1.3**, `Payroll_Logic_Australia_FY2026-27.xlsx` (prepared 08 Oct 2026, process
owner Vishal Sharma). The workbook asks IT to load tabs 02–08 as configuration, implement tab 09 as code,
and pass tabs 10–11 as automated tests. This page says how that was done and what is still open.

| Piece | File |
|---|---|
| Parameter tables (tabs 01-B, 02, 04, 05, 06, 08), keyed by the workbook's rule IDs | `eor/au_rules.json` |
| Algorithm (tab 09), decision table (03), special payments (11), validations (15) | `eor/au_payroll.py` |
| Acceptance tests (tabs 10 and 11) | `tests/test_au_payroll.py` |

## Acceptance results

| Workbook source | Result |
|---|---|
| Tab 10, TC01–TC16: scale, tax treatment code, weekly equivalent, base, Medicare adjustment, offsets, STSL, 53-pay extra, overrides, PAYG, net, QE, SG, Payday Super due date, payroll tax, workers' comp, leave and LSL provisions, CTC | **All 16 cases, 197 field checks, match** (including the ATO-published anchors TC01–TC04, TC10, TC11, TC14, TC15) |
| Tab 10 flag rows (SG / PT / WC / AL / LSL applies) | Derived from worker type alone; match |
| Tab 06-D employer payroll tax (NSW $294,300, VIC $200,666.67, QLD $99,833.33; effective and marginal rates) | Match |
| Tab 11-A Schedule 5 Method A, scenarios A1 and A2 | Match |
| Tab 11-B termination, B1 (genuine redundancy) and B2 (employer termination) | Match |

Run them with `python -m unittest tests.test_au_payroll -v`.

## Coverage by tab

| Tab | Status |
|---|---|
| 01 Country profile | NMW floor, casual loading, rounding rules, labour-hire licence states used. Employer settings live in `au_rules.json` under `employer_settings`, with the workbook's sample values. |
| 02 Worker types | W01–W14 with machine flags. Each conditional (C) flag takes a per-worker value, falling back to defaults that reproduce tab 10. W15 (death beneficiary) is not built. |
| 03 Scale selection | TS-01 to TS-16 in priority order, plus the six-character STP tax treatment code and TFN/ABN check digits. Schedule 5 back pay (Methods B(i)/B(ii)) is not built; TS-17 is out of scope per the spec. |
| 04 PAYG tables | Schedule 1 scales 1, 2, 3, 5 and 6, Schedule 8 STSL components, all four pay frequencies, Medicare levy adjustment, tax offsets, flat rates (Scale 4, no ABN, voluntary agreement, WHM bands), 53/27-pay extras. The engine uses the "base + separate STSL" method, which the spec allows; the alternative combined Schedule 8 tables are not used. |
| 05 Contributions | SG at 12% of QE (overtime excluded), annual maximum contribution base tracked year to date, Payday Super due date (7 business days, or 20 for a new fund). The under-18 30-hour test is a per-worker flag, not computed from timesheets. Choice/stapling and SG charge are not built. |
| 06 State rules | Payroll tax for all 8 jurisdictions (thresholds, phase-outs, tiers, surcharges, regional options), workers' comp rates, LSL loadings. Only national public holidays are loaded; state calendars are not. |
| 07 Pay codes | Payslip lines carry the main codes (E01/E02, P01, C01, C04, C05, C07, T01–T08, A09). The full catalogue with per-code STP categories is not loaded. |
| 08 Leave and termination | Annual and personal leave accrual, NES notice (+1 week over 45 with 2+ years), NES redundancy, genuine-redundancy tax-free limit, ETP and whole-of-income caps, Lump Sum A and Schedule 7. |
| 09 Algorithm | Steps B to F, in order, with reconciliation identities R1 and R3 asserted on every calculation. Child support with the protected earnings amount (E1) is not built. Step G (STP lodgement, bank and super files) needs real integrations. |
| 12–13 Remittances and returns | Not built: needs an STP sending service, SuperStream clearing house and ABA/NPP bank file. |
| 15 Validations | Built: V-TFN-01 to 04, V-WHM-01/02, V-RATE-01, V-SG-02, V-NET-01, V-PL-02, V-CON-02. Not built: the rest (casual conversion, fixed-term limits, leave balances, STP status, etc.). |
| 16 Payslip spec | Lines, rule IDs and the tax treatment code are stored on each payslip. A payslip document checked against the mandatory and prohibited content list is not built. |

## How it plugs into the platform

- Hire with `"country": "AU"` and an `au` profile, e.g.
  `{"state": "VIC", "tfn": "123456782", "tft": true, "worker_type": "W01"}`.
  Other fields are listed in `PROFILE_FIELDS`: residency, Medicare variations, STSL, tax offsets,
  salary sacrifice, visa, DOB, ABN and more.
- EOR defaults to W01. AOR defaults to W13 when an ABN is held, otherwise W14, which intake blocks (V-CON-02).
  W11/W12 can be chosen explicitly.
- Intake runs the AU validations; Block-level failures put the worker in `needs_attention`.
- Pay runs are monthly, with payday on the last business day of the month (AU-ES-007). Year-to-date QE and WHM
  income are summed from earlier payslips in the same July–June year, so the super cap and WHM bands carry across runs.
- Each payslip's `data.meta` holds the method, tax treatment code, PAYG, QE, SG, SG due date and the rule IDs used.
- Offboarding with `"cessation_type": "R"` treats it as a genuine redundancy. Notice, redundancy, unused leave and
  their Schedule 7/11 withholding go on the final payslip.
- `POST /api/quote` with `"country": "AU", "state": "QLD"` gives a monthly estimate. It assumes a resident who has
  quoted a TFN and claims the tax-free threshold.

## Simplifications to close before go-live

1. **Leave balances.** Unused annual leave at termination is estimated from length of service minus
   `leave_taken_days`; there is no hours-based leave ledger yet.
2. **Age.** Without a `dob`, the extra notice week for over-45s is not added and the under-preservation-age ETP
   rate (32%) is used.
3. **Part months.** Salaried pay for a part month is prorated by calendar days ("pro-rata per policy" in 09-B1).
   Confirm this with Lancesoft payroll policy.
4. **Employer settings.** Group and state wages, regional flags, licence numbers and workers' comp rates in
   `au_rules.json` are the workbook's sample or average values (yellow cells). They must be replaced with
   Lancesoft's actual figures.
5. **Items marked "To confirm" in the workbook** (portable LSL, the NT phase-out end-point, termination
   simplifications) carry over as-is.
