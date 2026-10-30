"""Country rule tables plus the pure calculators that use them: payroll gross-to-net,
contractor (AOR) misclassification risk, offboarding settlements and cost quotes."""
import calendar
import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

_RULES = {k: v for k, v in json.loads((Path(__file__).parent / "countries.json").read_text()).items()
          if not k.startswith("_")}

CENT = Decimal("0.01")


def D(x):
    return Decimal(str(x if x not in (None, "") else 0))


def money(x):
    return D(x).quantize(CENT, ROUND_HALF_UP)


def country(code):
    return _RULES.get((code or "").upper())


def countries():
    return {k: {"name": v["name"], "currency": v["currency"]} for k, v in _RULES.items()}


def parse_date(s):
    if s is None or isinstance(s, date):
        return s
    return date.fromisoformat(s)


def period_bounds(period):
    y, m = map(int, period.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


# ---------------------------------------------------------------- payroll

def progressive_tax(taxable, brackets):
    tax = Decimal(0)
    for i, (lo, rate) in enumerate(brackets):
        lo = D(lo)
        if taxable <= lo:
            break
        hi = D(brackets[i + 1][0]) if i + 1 < len(brackets) else None
        top = taxable if hi is None else min(taxable, hi)
        tax += (top - lo) * D(rate)
    return tax


def contribution(annual, item):
    base = annual if item.get("cap") is None else min(annual, D(item["cap"]))
    return max(base - D(item.get("floor", 0)), Decimal(0)) * D(item["rate"])


def withholding(rules, gross_month):
    """Monthly statutory amounts, computed on the annualised gross and divided by 12."""
    annual = gross_month * 12
    employee = [(c["name"], money(contribution(annual, c) / 12)) for c in rules["employee_contributions"]]
    taxable = max(annual - D(rules.get("tax_free_allowance", 0)), Decimal(0))
    income_tax = money(progressive_tax(taxable, rules["income_tax_brackets"]) / 12)
    employer = [(c["name"], money(contribution(annual, c) / 12)) for c in rules["employer_contributions"]]
    if rules.get("statutory_bonus_months"):
        employer.append(("Statutory bonus accrual", money(gross_month * D(rules["statutory_bonus_months"]) / 12)))
    return income_tax, employee, employer


def employed_days(worker, start, end):
    s = max(parse_date(worker["start_date"]), start)
    e = min(parse_date(worker["end_date"]) if worker.get("end_date") else end, end)
    return max((e - s).days + 1, 0)


def calc_payslip(worker, rules, period, timesheets=(), extra_earnings=()):
    """Gross-to-net for one worker and one monthly period. Returns Decimal amounts."""
    start, end = period_bounds(period)
    days = employed_days(worker, start, end)
    frac = D(days) / D((end - start).days + 1)
    lines = []

    def line(kind, label, amount):
        amount = money(amount)
        if amount:
            lines.append({"kind": kind, "label": label, "amount": amount})

    hours = sum((D(t["hours"]) for t in timesheets), Decimal(0))
    expenses = sum((D(t["expenses"]) for t in timesheets), Decimal(0))

    if worker["engagement_type"] == "EOR":
        line("earning", "Base salary", D(worker["salary_annual"]) / 12 * frac)
        for label, amount in extra_earnings:
            line("earning", label, amount)
        gross = sum((l["amount"] for l in lines), Decimal(0))
        income_tax, employee, employer = withholding(rules, gross)
        line("deduction", "Income tax", income_tax)
        for name, amt in employee:
            line("deduction", name, amt)
        for name, amt in employer:
            line("employer", name, amt)
    else:
        if worker.get("rate_hourly"):
            line("earning", f"Contractor fees ({hours} h)", hours * D(worker["rate_hourly"]))
        else:
            line("earning", "Contractor retainer", D(worker["salary_annual"]) / 12 * frac)
        gross = sum((l["amount"] for l in lines), Decimal(0))

    line("reimbursement", "Expense reimbursement", expenses)
    deductions = sum((l["amount"] for l in lines if l["kind"] == "deduction"), Decimal(0))
    employer_cost = sum((l["amount"] for l in lines if l["kind"] == "employer"), Decimal(0))
    reimb = sum((l["amount"] for l in lines if l["kind"] == "reimbursement"), Decimal(0))
    return {
        "currency": rules["currency"], "days": days, "lines": lines,
        "gross": money(gross), "net": money(gross - deductions + reimb),
        "employer_cost": money(employer_cost), "total_cost": money(gross + employer_cost + reimb),
    }


def quote(country_code, engagement_type, salary_annual):
    rules = country(country_code)
    if not rules:
        raise ValueError(f"unsupported country {country_code}")
    worker = {"engagement_type": engagement_type, "salary_annual": salary_annual,
              "start_date": "2000-01-01", "end_date": None}
    slip = calc_payslip(worker, rules, "2025-01")
    return {"country": country_code.upper(), "currency": rules["currency"], "engagement_type": engagement_type,
            "monthly_gross": slip["gross"], "monthly_net": slip["net"],
            "monthly_employer_cost": slip["employer_cost"], "monthly_total_cost": slip["total_cost"],
            "lines": slip["lines"], "notice_days": rules["notice_days"],
            "annual_leave_days": rules["annual_leave_days"], "benefits": rules["benefits"]}


# ---------------------------------------------------------------- AOR classification

CLASSIFICATION_FACTORS = {
    "client_controls_schedule": 20,
    "works_exclusively_for_client": 15,
    "integrated_into_core_business": 15,
    "client_provides_equipment": 10,
    "paid_regular_salary_like": 10,
    "engagement_over_12_months": 10,
    "cannot_subcontract": 10,
    "receives_employee_benefits": 10,
}


def classify(answers, rules):
    """Misclassification risk score 0-100. Unanswered factors count half (conservative)."""
    answers = answers or {}
    score, hits = 0.0, []
    for factor, weight in CLASSIFICATION_FACTORS.items():
        val = answers.get(factor)
        if val is True:
            score += weight
            hits.append(factor)
        elif val is None:
            score += weight / 2
            hits.append(f"{factor} (unanswered)")
    if rules.get("strict_classification"):
        score += 15
        hits.append("strict local classification regime")
    score = min(round(score), 100)
    level = "high" if score >= 60 else "medium" if score >= 35 else "low"
    rec = {"high": "Convert to EOR employment or obtain legal sign-off",
           "medium": "Proceed as contractor; re-assess at renewal",
           "low": "Proceed as contractor"}[level]
    return {"score": score, "level": level, "factors": hits, "recommendation": rec}


# ---------------------------------------------------------------- offboarding

def settlement(worker, rules, notice_given_days, initiated_by):
    """Final-pay extras for an EOR employee: unused leave payout and pay in lieu of notice."""
    if worker["engagement_type"] != "EOR":
        return []
    salary = D(worker["salary_annual"])
    start, end = parse_date(worker["start_date"]), parse_date(worker["end_date"])
    months = D((end - start).days + 1) / D("30.4375")
    accrued = D(rules["annual_leave_days"]) * months / 12
    unused = max(accrued - D(worker["data"].get("leave_taken_days", 0)), Decimal(0))
    items = []
    if unused:
        items.append((f"Unused leave payout ({unused.quantize(Decimal('0.1'))} days)", money(unused * salary / 260)))
    shortfall = rules["notice_days"] - notice_given_days
    if initiated_by == "employer" and shortfall > 0:
        items.append((f"Pay in lieu of notice ({shortfall} days)", money(D(shortfall) * salary / 365)))
    return items
