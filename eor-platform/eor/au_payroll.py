"""Australian payroll engine, FY2026-27.

Implements the Lancesoft specification GPE-AU-LOGIC v1.3 (Payroll_Logic_Australia_FY2026-27.xlsx):
  03  withholding-method decision table and STP tax treatment code
  04  Schedule 1 / 8 coefficients, frequency conversion, Medicare adjustment, flat rates, 53/27 pays
  05  super guarantee with maximum contribution base and Payday Super due date
  06  payroll tax (employer-level effective rate), workers' comp, long service leave
  08  leave accrual, NES notice and redundancy, termination tax parameters
  09  gross-to-net and gross-to-CTC algorithm, steps A-G, in the order given
  11  Schedule 5 Method A (bonus) and Schedules 7 / 11 (termination)
  15  the validation rules that can be evaluated from payroll inputs
Parameters live in au_rules.json; nothing statutory is hard-coded here. Every amount
carries the rule ID that produced it so the payslip has an audit trail.
"""
import calendar
import json
from dataclasses import dataclass, field, fields
from datetime import date, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from fractions import Fraction
from pathlib import Path
from typing import Optional

RULES = json.loads((Path(__file__).parent / "au_rules.json").read_text())
STATES = ("NSW", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT")
FLAG_KEYS = ("EMP", "SG", "PT", "WC", "AL", "LSL")


def D(x):
    if isinstance(x, str) and "/" in x:
        f = Fraction(x)
        return Decimal(f.numerator) / Decimal(f.denominator)
    return Decimal(str(x if x not in (None, "") else 0))


def r0(x):
    return D(x).quantize(Decimal(1), ROUND_HALF_UP)


def r2(x):
    return D(x).quantize(Decimal("0.01"), ROUND_HALF_UP)


def floor(x):
    return D(x).to_integral_value(ROUND_FLOOR)


# ---------------------------------------------------------------- inputs

@dataclass
class AUInput:
    """One employee, one pay event. Field names follow tab 10 / tab 14."""
    worker_type: str = "W01"
    state: str = "NSW"
    frequency: str = "M"
    payday: Optional[date] = None
    residency: str = "R"                 # R resident, F foreign resident, WHM working holiday maker
    tfn_provided: bool = True
    tft: bool = True                     # tax-free threshold claimed (M04 second job -> False)
    medicare_exemption: str = "N"        # N, H (half), F (full)
    medicare_reduction: bool = False
    children: int = 0
    stsl: bool = False
    tax_offsets_annual: Decimal = Decimal(0)
    mls_tier: str = "X"                  # Medicare levy surcharge position of the tax treatment code
    whm_ytd: Decimal = Decimal(0)        # WHM payments this FY before this pay
    x53: bool = False                    # elected 53/27-pay extra withholding
    upward_variation: Decimal = Decimal(0)
    variation: Optional[dict] = None     # approved ATO variation {"rate": ..} or {"amount": ..}
    cir: Optional[Decimal] = None        # Commissioner's instalment rate (voluntary agreements)
    ordinary: Decimal = Decimal(0)       # E01/E02
    casual_loading: Decimal = Decimal(0) # E03
    overtime: Decimal = Decimal(0)       # E04 (not QE)
    penalties: Decimal = Decimal(0)      # E05/E06
    leave: Decimal = Decimal(0)          # E07-E12
    allowances: Decimal = Decimal(0)     # A05-A08 (QE)
    bonus: Decimal = Decimal(0)          # B01/B02 paid with normal pay -> Schedule 5 Method A
    salary_sacrifice: Decimal = Decimal(0)
    post_tax: Decimal = Decimal(0)
    qe_ytd: Decimal = Decimal(0)
    hours: Decimal = Decimal(0)          # ordinary hours this period (leave accrual)
    rate: Decimal = Decimal(0)           # base hourly rate (leave accrual)
    leave_loading: Optional[Decimal] = None
    new_fund: bool = False               # first contribution to a new fund -> 20 business days
    flags: dict = field(default_factory=dict)   # explicit Y/N for conditional (C) flags, e.g. {"SG": True}

    @classmethod
    def build(cls, **kw):
        names = {f.name for f in fields(cls)}
        unknown = set(kw) - names
        if unknown:
            raise ValueError(f"unknown AU input field(s): {', '.join(sorted(unknown))}")
        out = cls(**kw)
        for f in fields(cls):
            if f.type in ("Decimal", Decimal) and not isinstance(getattr(out, f.name), Decimal):
                setattr(out, f.name, D(getattr(out, f.name)))
        return out


# ---------------------------------------------------------------- identifiers (03-C)

TFN_SPECIAL = {"000000000": "declined", "111111111": "applied", "333333333": "under18", "444444444": "pensioner"}


def valid_tfn(tfn):
    s = "".join(ch for ch in str(tfn) if ch.isdigit())
    if s in TFN_SPECIAL:
        return True
    if len(s) != 9:
        return False
    return sum(int(d) * w for d, w in zip(s, (1, 4, 3, 7, 5, 8, 6, 9, 10))) % 11 == 0


def valid_abn(abn):
    s = "".join(ch for ch in str(abn) if ch.isdigit())
    if len(s) != 11:
        return False
    digits = [int(s[0]) - 1] + [int(c) for c in s[1:]]
    return sum(d * w for d, w in zip(digits, (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19))) % 89 == 0


# ---------------------------------------------------------------- flags (02)

def resolve_flags(inp, rules=RULES):
    wt = rules["worker_types"][inp.worker_type]
    defaults = rules["worker_types"]["conditional_defaults"]
    out = {}
    for key, ch in zip(FLAG_KEYS, wt["flags"]):
        if key in inp.flags:
            out[key] = bool(inp.flags[key])
        elif ch == "C":
            out[key] = defaults.get(key, {}).get(inp.worker_type, False)
        else:
            out[key] = ch == "Y"
    return out


# ---------------------------------------------------------------- PAYG (03, 04)

def select_method(inp, rules=RULES):
    """03-A decision table, first match wins. Returns (method, rule_id)."""
    wt = inp.worker_type
    if wt == "W13":
        return "NONE", "02-W13"
    if wt == "W14":
        return "NOABN", "TS-01"
    if wt == "W12":
        return "VA", "TS-02"
    if inp.variation:
        return "DV", "TS-03"
    if not inp.tfn_provided:
        return ("S4F", "TS-05") if inp.residency in ("F", "WHM") else ("S4R", "TS-04")
    if inp.residency == "WHM":
        return ("WHM", "TS-06") if rules["employer_settings"]["AU-ES-004"]["whm_registered"] else ("S3", "TS-07")
    if inp.residency == "F":
        return "S3", "TS-08"
    if not inp.tft:
        return "S1", "TS-09"
    if inp.medicare_exemption == "F":
        return "S5", "TS-10"
    if inp.medicare_exemption == "H":
        return "S6", "TS-11"
    return "S2", "TS-12"


def tax_treatment_code(inp, method):
    """03-B six-character STP Phase 2 code."""
    s = "S" if inp.stsl else "X"
    if method in ("S2", "S5", "S6"):
        ex = {"F": "F", "H": "H"}.get(inp.medicare_exemption, "X")
        red = "X"
        if inp.medicare_reduction:
            red = "0" if inp.children == 0 else ("A" if inp.children >= 10 else str(inp.children))
        return f"RT{s}{inp.mls_tier}{ex}{red}"
    if method == "S1":
        return f"RN{s}XXX"
    if method == "S3":
        return "HUXXXX" if inp.residency == "WHM" else f"FF{s}XXX"
    return {"WHM": "HRXXXX", "S4R": "NAXXXX", "S4F": "HFXXXX" if inp.residency == "WHM" else "NFXXXX",
            "DV": "DVXXXX", "VA": "VCXXXX" if inp.cir else "VOXXXX"}.get(method)


def weekly_equivalent(amount, freq):
    """04-B / 09-D2: whole dollars of the weekly equivalent plus 99 cents."""
    t = D(amount)
    if freq == "W":
        base = floor(t)
    elif freq == "F":
        base = floor(t / 2)
    elif freq == "M":
        if (t * 100) % 100 == 33:
            t += Decimal("0.01")
        base = floor(t * 3 / 13)
    elif freq == "Q":
        base = floor(t / 13)
    else:
        raise ValueError(f"unsupported pay frequency {freq}")
    return base + Decimal("0.99")


def to_period(weekly, freq):
    """09-D4: convert rounded weekly withholding back to the pay period."""
    w = D(weekly)
    return {"W": w, "F": 2 * w, "Q": 13 * w}.get(freq) if freq != "M" else r0(w * 13 / 3)


def coefficient(table, x, rules=RULES):
    for lo, hi, a, b in rules["payg"][table]:
        if x >= lo and (hi is None or x < hi):
            return D(a), D(b)
    raise ValueError(f"no {table} band for x={x}")


def weekly_y(table, x, rules=RULES):
    a, b = coefficient(table, x, rules)
    return max(r0(a * x - b), Decimal(0))


def medicare_adjustment(scale, x, children, rules=RULES):
    """04-C weekly levy adjustment (WLA), rounded to whole dollars."""
    p = {k: D(v) for k, v in rules["medicare_adjustment"][scale].items()}
    wft = r2((p["family"] + p["child"] * children) / 52)
    sop = floor(wft * p["mult"] / p["div"])
    if x < p["west"]:
        wla = Decimal(0)
    elif x < p["shade_in"]:
        wla = (x - p["factor"]) * p["mult"]
    elif x < wft:
        wla = x * p["rate"]
    elif x < sop:
        wla = wft * p["rate"] - (x - wft) * p["div"]
    else:
        wla = Decimal(0)
    return r0(max(wla, Decimal(0))), wft, sop


def coefficient_withholding(taxable, scale, freq, stsl, rules=RULES):
    """Base scale + STSL component for a pay period (no offsets / Medicare adjustment)."""
    x = weekly_equivalent(taxable, freq)
    base = to_period(weekly_y(scale, x, rules), freq)
    st = Decimal(0)
    if stsl:
        st = to_period(weekly_y("STSL_NT" if scale == "S1" else "STSL_TF", x, rules), freq)
    return base, st


def whm_rate(ytd_before, rules=RULES):
    for limit, rate in rules["flat_rates"]["WHM"]:
        if limit is None or ytd_before <= limit:
            return D(rate)


def flat_withholding(method, taxable, inp, rules=RULES):
    """09-D9 overrides. Returns (amount, rule_id)."""
    fr = rules["flat_rates"]
    whole = floor(taxable)
    if method == "NONE":
        return Decimal(0), "02-W13"
    if method == "NOABN":
        return (Decimal(0) if taxable <= 75 else floor(whole * D(fr["NOABN"]))), "NOABN"
    if method == "S4R":
        return floor(whole * D(fr["NOTFN_RES"])), "NOTFN_RES"
    if method == "S4F":
        return floor(whole * D(fr["NOTFN_FOR"])), "NOTFN_FOR"
    if method == "WHM":
        rate = whm_rate(inp.whm_ytd, rules)
        amt = floor(whole * rate) if rate >= D("0.45") else r0(whole * rate)
        return amt, "WHM_T"
    if method == "VA":
        return r0(taxable * max(D(fr["VA_MIN"]), D(inp.cir or 0))), "VA_MIN"
    if method == "DV":
        v = inp.variation
        return (r0(D(v["amount"])) if "amount" in v else r0(taxable * D(v["rate"]))), "TS-03"
    raise ValueError(method)


def extra_53_27(taxable, freq, rules=RULES):
    for lo, hi, amt in rules["extra_53_27"].get(freq, []):
        if taxable >= lo and (hi is None or taxable < hi):
            return D(amt)
    return Decimal(0)


def schedule5_bonus(normal_taxable, bonus, scale, freq, stsl, rules=RULES):
    """11-A Schedule 5 Method A: spread over the year, withhold the difference, cap at 47%."""
    periods = rules["frequencies"][freq]["periods"]
    spread = floor(D(bonus) / periods)
    b1, s1 = coefficient_withholding(normal_taxable, scale, freq, stsl, rules)
    b2, s2 = coefficient_withholding(D(normal_taxable) + spread, scale, freq, stsl, rules)
    calc = (b2 + s2 - b1 - s1) * periods
    cap = floor(D(bonus) * D("0.47"))
    return {"spread": spread, "normal": b1 + s1, "with_spread": b2 + s2, "diff": b2 + s2 - b1 - s1,
            "calc": calc, "cap": cap, "withholding": min(calc, cap)}


# ---------------------------------------------------------------- employer side (05, 06)

def add_business_days(start, n, holidays):
    d, left = start, n
    while left:
        d += timedelta(days=1)
        if d.weekday() < 5 and d.isoformat() not in holidays:
            left -= 1
    return d


def last_business_day(year, month, holidays=()):
    d = date(year, month, calendar.monthrange(year, month)[1])
    while d.weekday() >= 5 or d.isoformat() in holidays:
        d -= timedelta(days=1)
    return d


def payroll_tax_table(rules=RULES):
    """06-D: annual group payroll tax per state, effective rate and marginal rate."""
    es = rules["employer_settings"]
    auw = D(es["AU-ES-005"]["group_wages_annual"])
    p = {s: {k: (v if isinstance(v, list) else D(v)) for k, v in rules["payroll_tax"][s].items()} for s in STATES}
    out = {}
    for s in STATES:
        w = D(es["state_wages_annual"].get(s, 0))
        share = w / auw if auw > 0 else Decimal(0)
        c = p[s]
        surcharge, extra_marginal = Decimal(0), Decimal(0)

        def phased(threshold, ratio, start=None):
            start = threshold if start is None else start
            return threshold if auw <= start else max(Decimal(0), threshold - (auw - start) * ratio)

        if s == "NSW":
            ded, rate = c["threshold"] * share, c["rate"]
        elif s == "VIC":
            ded = phased(c["threshold"], c["phase_out_ratio"], c["phase_out_start"]) * share
            rate = c["regional_rate"] if es.get("vic_regional") else c["rate"]
        elif s == "QLD":
            ded = phased(c["threshold"], c["phase_out_ratio"]) * share
            rate = (c["rate2"] if auw > c["tier"] else c["rate1"]) - (c["regional_discount"] if es.get("qld_regional") else 0)
        elif s == "WA":
            ded, rate = phased(c["threshold"], c["phase_out_ratio"]) * share, c["rate"]
        elif s == "SA":
            ded = c["deduction"] * share
            rate = (Decimal(0) if auw <= c["shade_lo"] else
                    c["rate"] * (auw - c["shade_lo"]) / (c["shade_hi"] - c["shade_lo"]) if auw < c["shade_hi"] else c["rate"])
        elif s == "TAS":
            ded = c["threshold"] * share
            rate = c["rate2"] if auw > c["threshold"] + c["band"] else c["rate1"]
        elif s == "ACT":
            ded = c["threshold"] * share
            rate = next(D(r) for lim, r in c["tiers"] if lim is None or auw <= D(lim))
        else:  # NT
            ded = phased(c["threshold"], c["phase_out_ratio"]) * share
            rate = c["rate2"] if auw >= c["rate2_from"] else c["rate1"]

        taxable = max(Decimal(0), w - ded)
        if s == "TAS":
            base = c["rate1"] * min(taxable, c["band"] * share) + c["rate2"] * max(Decimal(0), taxable - c["band"] * share)
        else:
            base = taxable * rate
        for tier, srate in c.get("surcharges", []):
            if auw > D(tier):
                surcharge += D(srate) * max(Decimal(0), w - D(tier) * share)
                extra_marginal += D(srate)
        total = r2(base + surcharge)
        out[s] = {"wages": w, "share": share, "deduction": ded, "taxable": taxable, "rate": rate,
                  "total": total, "effective": total / w if w > 0 else Decimal(0), "marginal": rate + extra_marginal}
    return out


def payroll_tax_rate(state, rules=RULES):
    """06 CTC allocation rule: effective rate of the state, or its marginal rate if it has no wages yet."""
    row = payroll_tax_table(rules)[state]
    return row["effective"] if row["wages"] > 0 else row["marginal"]


# ---------------------------------------------------------------- validations (15)

def validate(inp, today=None, tfn=None, tfn_applied_on=None, visa=None, home_country=None,
             on_hire=False, rules=RULES):
    """Rules from tab 15 that can be checked from payroll inputs. Returns [(rule, severity, message)]."""
    out = []
    if inp.worker_type not in rules["worker_types"] or inp.worker_type.startswith("_"):
        out.append(("02", "Block", f"Unknown worker type {inp.worker_type}"))
    if inp.state not in STATES:
        out.append(("06", "Block", f"Unknown state {inp.state}"))
    if tfn and not valid_tfn(tfn):
        out.append(("V-TFN-01", "Block", "Invalid TFN (fails check digit)"))
    if tfn and "".join(c for c in str(tfn) if c.isdigit()) == "111111111" and tfn_applied_on and today \
            and (today - tfn_applied_on).days > 28:
        out.append(("V-TFN-02", "Warn", "TFN applied for more than 28 days ago; Scale 4 now applies"))
    if inp.medicare_exemption != "N" or inp.medicare_reduction:
        if not inp.tft:
            out.append(("V-TFN-03", "Block", "Medicare variation claimed without the tax-free threshold"))
    if inp.medicare_exemption == "F" and inp.medicare_reduction:
        out.append(("V-TFN-04", "Block", "Full Medicare exemption cannot be combined with a reduction"))
    if inp.residency == "WHM":
        if not home_country or str(visa) not in ("417", "462"):
            out.append(("V-WHM-01", "Block", "WHM needs a 417/462 visa and home country code"))
        if inp.stsl:
            out.append(("V-WHM-02", "Warn", "WHM recorded with an STSL debt"))
    if inp.worker_type in ("W01", "W02", "W03", "W04", "W08", "W09") and inp.hours > 0:
        hourly = inp.ordinary / inp.hours
        if hourly < D(rules["country"]["AU-CP-013"]["nmw_hourly"]):
            out.append(("V-RATE-01", "Block", f"Effective hourly rate {r2(hourly)} is below the National Minimum Wage"))
    flags = resolve_flags(inp, rules)
    if flags["SG"] and inp.qe_ytd + inp.ordinary > D(rules["super"]["SG_MCB"]):
        out.append(("V-SG-02", "Warn", "Qualifying earnings exceed the maximum contribution base this year"))
    if on_hire and inp.state in rules["country"]["AU-CP-024"]["labour_hire_licence_states"] \
            and not rules["employer_settings"]["labour_hire_licences"].get(inp.state):
        out.append(("V-PL-02", "Block", f"No labour-hire licence recorded for {inp.state}"))
    return out


# ---------------------------------------------------------------- algorithm (09)

def calculate(inp, rules=RULES):
    """Gross-to-net and gross-to-CTC for one pay event (09 steps B-F). Amounts are Decimal."""
    used = []
    flags = resolve_flags(inp, rules)
    freq = inp.frequency
    sup = rules["super"]

    # B. Earnings
    gross = inp.ordinary + inp.casual_loading + inp.overtime + inp.penalties + inp.leave + inp.allowances + inp.bonus
    # C. Pre-tax
    taxable = gross - inp.salary_sacrifice - inp.bonus

    # D. PAYG
    method, rule = select_method(inp, rules)
    used.append(rule)
    x = weekly_equivalent(taxable, freq)
    res = {"method": method, "ttc": tax_treatment_code(inp, method), "x": x, "base": Decimal(0), "mla": Decimal(0),
           "offsets": Decimal(0), "stsl": Decimal(0), "extra": Decimal(0), "override": Decimal(0),
           "bonus_withholding": Decimal(0), "wft": Decimal(0), "sop": Decimal(0)}
    if method in ("S1", "S2", "S3", "S5", "S6"):
        res["base"], res["stsl"] = coefficient_withholding(taxable, method, freq, inp.stsl, rules)
        used.append(f"PAYG-{method}")
        if inp.stsl:
            used.append("STSL-NT" if method == "S1" else "STSL-TF")
        if method in ("S2", "S6") and inp.medicare_reduction:
            wla, res["wft"], res["sop"] = medicare_adjustment(method, x, inp.children, rules)
            res["mla"] = to_period(wla, freq)
            used.append("MLA")
        if inp.tax_offsets_annual:
            res["offsets"] = r0(inp.tax_offsets_annual * D(rules["frequencies"][freq]["offset_factor"]))
            used.append("AU-PAYG-OFFSET")
        if inp.x53:
            res["extra"] = extra_53_27(taxable, freq, rules)
            used.append("TS-13")
        res["extra"] += inp.upward_variation
        payg = max(Decimal(0), res["base"] - res["mla"] - res["offsets"]) + res["stsl"] + res["extra"]
        if inp.bonus:
            b = schedule5_bonus(taxable, inp.bonus, method, freq, inp.stsl, rules)
            res["bonus_withholding"] = b["withholding"]
            payg += b["withholding"]
            used.append("TS-15")
    else:
        res["override"], r = flat_withholding(method, taxable + inp.bonus, inp, rules)
        used.append(r)
        payg = res["override"]
    res["payg"] = payg

    # E. Net
    net = gross - inp.salary_sacrifice - payg - inp.post_tax
    if net < 0:
        raise ValueError("V-NET-01: negative net pay")

    # F. Employer costs
    qe = (inp.ordinary + inp.casual_loading + inp.penalties + inp.leave + inp.allowances + inp.bonus) if flags["SG"] else Decimal(0)
    qe_sg = min(qe, max(Decimal(0), D(sup["SG_MCB"]) - inp.qe_ytd))
    sg = r2(qe_sg * D(sup["SG_RATE"]))
    if flags["SG"]:
        used += ["AU-CON-004", "SG_MCB"]
    payday = inp.payday or date.today()
    sg_due = add_business_days(payday, sup["SG_NEW_BD"] if inp.new_fund else sup["SG_DUE_BD"],
                               set(rules["public_holidays"]))
    pt_base = gross + sg if flags["PT"] else Decimal(0)
    pt_rate = payroll_tax_rate(inp.state, rules)
    pt = r2(pt_base * pt_rate)
    wc_rate = D(rules["workers_comp_rates"][inp.state])
    wc = r2((gross + sg) * wc_rate) if flags["WC"] else Decimal(0)
    lv = rules["leave"]
    al_hours = inp.hours * D(lv["AL_ACCRUAL"]) if flags["AL"] else Decimal(0)
    pcl_hours = inp.hours * D(lv["PCL_ACCRUAL"]) if flags["AL"] else Decimal(0)
    loading = D(lv["AL_LOADING"]) if inp.leave_loading is None else inp.leave_loading
    al_cost = r2(al_hours * inp.rate * (1 + loading))
    lsl_pct = D(rules["lsl_weeks_per_year"][inp.state]) / 52
    lsl_cost = r2(inp.ordinary * lsl_pct) if flags["LSL"] else Decimal(0)
    ctc = gross + sg + pt + wc + al_cost + lsl_cost
    used += ["06-D", "06-E", "LV-AL", "06-F"]

    res.update(flags=flags, gross=gross, taxable=taxable, net=net, qe=qe, qe_sg=qe_sg, sg=sg, sg_due=sg_due,
               super_total=sg + inp.salary_sacrifice, pt_base=pt_base, pt_rate=pt_rate, pt=pt, wc_rate=wc_rate, wc=wc,
               al_hours=al_hours, al_cost=al_cost, pcl_hours=pcl_hours, lsl_pct=lsl_pct, lsl_cost=lsl_cost, ctc=ctc,
               rules=used)
    # 09-H reconciliation identities R1 and R3
    assert res["net"] == gross - inp.salary_sacrifice - payg - inp.post_tax
    assert res["ctc"] == gross + sg + pt + wc + al_cost + lsl_cost
    return res


# ---------------------------------------------------------------- termination (08, 11-B)

def _band(bands, years):
    out = 0
    for start, weeks in bands:
        if years >= start:
            out = weeks
    return out


def notice_weeks(years, age, rules=RULES):
    w = _band(rules["notice_weeks"]["bands"], years)
    return w + (1 if age is not None and age > 45 and years >= 2 else 0)


def redundancy_weeks(years, rules=RULES):
    return _band(rules["redundancy_weeks"]["bands"], years)


def termination(years, age, genuine_redundancy, weekly_rate, unused_leave, normal_weekly, scale="S2",
                pay_in_lieu=True, notice_given_weeks=0, extra=0, unused_lsl=0, other_income=0, prior_etp=0,
                tfn_provided=True, small_business=False, rules=RULES):
    """11-B: redundancy, notice and unused leave with Schedule 7 and 11 withholding."""
    tt = {k: (D(v) if isinstance(v, str) else v) for k, v in rules["termination_tax"].items()}
    weekly_rate, extra = D(weekly_rate), D(extra)
    notice = notice_weeks(years, age, rules)
    piln = max(notice - notice_given_weeks, 0) * weekly_rate if pay_in_lieu else Decimal(0)
    red_w = redundancy_weeks(years, rules) if genuine_redundancy and not small_business else 0
    red = red_w * weekly_rate + (extra if genuine_redundancy else 0)
    tfl = tt["GR_BASE"] + tt["GR_YEAR"] * years
    in_rate = tt["ETP_R_OVER"] if age is not None and age >= tt["PRES_AGE"] else tt["ETP_R_UNDER"]

    def etp_wh(amount, cap):
        if not tfn_provided:
            return r0(amount * tt["ETP_NOTFN"])
        cap = max(cap, Decimal(0))
        return r0(min(amount, cap) * in_rate + max(Decimal(0), amount - cap) * tt["ETP_R_EXCESS"])

    if genuine_redundancy:
        lsd = min(red + piln, tfl)
        etp_r, etp_o = red + piln - lsd, Decimal(0)
    else:
        lsd, etp_r, etp_o = Decimal(0), Decimal(0), piln + extra
    cap_r = tt["ETP_CAP"] - D(prior_etp)
    cap_o = min(tt["ETP_CAP"] - D(prior_etp), tt["WOI_CAP"] - D(other_income))
    wh_r, wh_o = etp_wh(etp_r, cap_r), etp_wh(etp_o, cap_o)

    lv = D(unused_leave) + D(unused_lsl)
    spread = floor(lv / 52)
    w1, _ = coefficient_withholding(normal_weekly, scale, "W", False, rules)
    w2, _ = coefficient_withholding(D(normal_weekly) + spread, scale, "W", False, rules)
    wh_leave = r0(lv * tt["LUMP_A"]) if genuine_redundancy else (w2 - w1) * 52

    total = red + piln + (0 if genuine_redundancy else extra) + lv
    total_wh = wh_r + wh_o + wh_leave
    components = []
    if genuine_redundancy:
        components += [("T05", "Genuine redundancy - tax-free (Lump Sum D)", lsd, Decimal(0)),
                       ("T06", "Genuine redundancy - excess (ETP R)", etp_r, wh_r),
                       ("T02", "Unused leave - genuine redundancy (Lump Sum A)", lv, wh_leave)]
    else:
        components += [("T07", "Payment in lieu of notice (ETP O)", piln, None),
                       ("T08", "Ex-gratia / settlement (ETP O)", extra, None),
                       ("T01", "Unused annual leave + loading (Schedule 7)", lv, wh_leave)]
        if etp_o:  # Schedule 11 withholding is computed on PILN + ex-gratia together
            components.append(("ETP-O", "PAYG on non-excluded ETP (Schedule 11)", Decimal(0), wh_o))
    components = [c for c in components if c[2] or (c[3] or 0)]
    return {"notice_weeks": notice, "piln": piln, "redundancy_weeks": red_w, "redundancy": red, "tax_free_limit": tfl,
            "lump_sum_d": lsd, "etp_r": etp_r, "etp_o": etp_o, "etp_rate": in_rate, "cap_r": cap_r, "cap_o": cap_o,
            "wh_r": wh_r, "wh_o": wh_o, "leave": lv, "leave_spread": spread, "leave_w1": w1, "leave_w2": w2,
            "wh_leave": wh_leave, "total": total, "total_withholding": total_wh, "net": total - total_wh,
            "sg_on_piln": r2(piln * D(rules["super"]["SG_RATE"])), "components": components}


# ---------------------------------------------------------------- platform adapter

EOR_TYPES = {f"W{n:02d}" for n in range(1, 11)}
AOR_TYPES = {"W11", "W12", "W13", "W14"}
PROFILE_FIELDS = {"worker_type", "state", "residency", "tfn", "tfn_applied_on", "tft", "medicare_exemption",
                  "medicare_reduction", "children", "stsl", "tax_offsets_annual", "mls_tier", "x53",
                  "upward_variation", "variation", "cir", "salary_sacrifice", "post_tax", "leave_loading",
                  "new_fund", "flags", "abn", "visa", "home_country", "on_hire", "dob", "hours_per_week"}


def profile(worker):
    """AU employee master from worker.data['au'] with defaults. AOR maps to W13 when an ABN is held."""
    p = dict(worker.get("data", {}).get("au") or {})
    if "worker_type" not in p:
        if worker["engagement_type"] == "EOR":
            p["worker_type"] = "W01"
        else:
            p["worker_type"] = "W13" if p.get("abn") else "W14"
    p.setdefault("state", "NSW")
    return p


def platform_validate(worker, today=None):
    """Block-level problems for intake, as human-readable strings."""
    p = profile(worker)
    errs = []
    unknown = set(p) - PROFILE_FIELDS
    if unknown:
        errs.append(f"Unknown AU field(s): {', '.join(sorted(unknown))}")
    wt = p["worker_type"]
    if worker["engagement_type"] == "EOR" and wt not in EOR_TYPES:
        errs.append(f"{wt} is not an employee type; EOR needs W01-W10")
    if worker["engagement_type"] == "AOR" and wt not in AOR_TYPES:
        errs.append(f"{wt} is not a contractor type; AOR needs W11-W14")
    if wt in ("W12", "W13") and not (p.get("abn") and valid_abn(p["abn"])):
        errs.append("A valid ABN is required for ABN contractors (03-C)")
    if wt == "W14":
        errs.append("V-CON-02: contractor has no ABN and no labour-hire TFN declaration")
    if worker["start_date"] and str(worker["start_date"]) < RULES["effective_from"]:
        errs.append(f"AU payroll rules are loaded for payments from {RULES['effective_from']}")
    try:
        inp = _input(worker, p, "M", date.fromisoformat(RULES["effective_from"]), Decimal(0), Decimal(0), Decimal(0))
        errs += [f"{rid}: {msg}" for rid, sev, msg in validate(
            inp, today=today, tfn=p.get("tfn"), visa=p.get("visa"), home_country=p.get("home_country"),
            on_hire=p.get("on_hire", False)) if sev == "Block" and rid != "V-RATE-01"]
    except (ValueError, KeyError) as e:
        errs.append(str(e))
    return errs


def _tfn_provided(p, payday):
    tfn = "".join(c for c in str(p.get("tfn") or "") if c.isdigit())
    if p["worker_type"] in ("W12", "W13", "W14"):
        return True
    if not tfn or tfn == "000000000":
        return False
    if tfn == "111111111" and p.get("tfn_applied_on"):
        return (payday - date.fromisoformat(p["tfn_applied_on"])).days <= 28
    return True


def _input(worker, p, freq, payday, ordinary, qe_ytd, whm_ytd, hours=None, rate=None):
    kw = {k: p[k] for k in ("residency", "tft", "medicare_exemption", "medicare_reduction", "children", "stsl",
                            "tax_offsets_annual", "mls_tier", "x53", "upward_variation", "variation", "cir",
                            "salary_sacrifice", "post_tax", "leave_loading", "new_fund", "flags") if k in p}
    return AUInput.build(worker_type=p["worker_type"], state=p["state"], frequency=freq, payday=payday,
                         tfn_provided=_tfn_provided(p, payday), ordinary=ordinary, qe_ytd=qe_ytd,
                         whm_ytd=whm_ytd, hours=hours or 0, rate=rate or 0, **kw)


def platform_payslip(worker, period, timesheets=(), extra_earnings=(), context=None):
    """Monthly payslip for the EOR/AOR platform, in its payslip format."""
    from .rules import employed_days, period_bounds
    p = profile(worker)
    context = context or {}
    start, end = period_bounds(period)
    payday = last_business_day(start.year, start.month, set(RULES["public_holidays"]))
    if not (RULES["effective_from"] <= payday.isoformat() <= RULES["effective_to"]):
        raise ValueError(f"no AU rule pack effective for payday {payday}")
    frac = D(employed_days(worker, start, end)) / D((end - start).days + 1)
    hours_ts = sum((D(t["hours"]) for t in timesheets), Decimal(0))
    expenses = sum((D(t["expenses"]) for t in timesheets), Decimal(0))
    std_hours = D(p.get("hours_per_week", RULES["country"]["standard_hours_week"]))
    if worker.get("rate_hourly"):
        ordinary, hours, rate = r2(hours_ts * D(worker["rate_hourly"])), hours_ts, D(worker["rate_hourly"])
    else:
        salary = D(worker["salary_annual"])
        ordinary = r2(salary / 12 * frac)
        hours = r2(std_hours * 52 / 12 * frac)
        rate = r2(salary / (std_hours * 52))
    inp = _input(worker, p, "M", payday, ordinary, D(context.get("qe_ytd", 0)), D(context.get("whm_ytd", 0)),
                 hours, rate)
    res = calculate(inp)

    lines = []

    def line(kind, label, amount, rule):
        if amount:
            lines.append({"kind": kind, "label": label, "amount": r2(amount), "rule": rule})

    line("earning", "Ordinary pay (E01/E02)", inp.ordinary, "09-B1")
    payg_label = f"PAYG withholding {res['method']} [{res['ttc'] or 'no STP code'}]"
    payg_rules = [r for r in res["rules"] if r.startswith(("TS-", "PAYG-", "STSL-", "MLA", "AU-PAYG", "NOTFN", "NOABN", "WHM", "VA_"))]
    line("deduction", payg_label, res["payg"], " ".join(payg_rules))
    line("deduction", "Salary sacrifice to super (P01)", inp.salary_sacrifice, "09-C1")
    line("deduction", "Post-tax deductions (D04-D07)", inp.post_tax, "09-E2")

    term_wh = Decimal(0)
    for item in extra_earnings:
        label, amount = item[0], D(item[1])
        code = item[2] if len(item) > 2 else ""
        line("earning", f"{label} ({code})" if code else label, amount, code or "08")
        if len(item) > 3 and item[3] not in (None, "", "None"):
            term_wh += D(item[3])
    line("deduction", "PAYG on termination payments (Sch 7/11)", term_wh, "TS-16")

    line("employer", "Super guarantee 12% (C01)", res["sg"], "AU-CON-004")
    line("employer", f"Payroll tax {p['state']} (C04)", res["pt"], "06-D")
    line("employer", f"Workers' comp {p['state']} (C05)", res["wc"], "06-E")
    line("employer", "Annual leave provision (C07)", res["al_cost"], "LV-AL")
    line("employer", "Long service leave provision (C07)", res["lsl_cost"], "06-F")
    line("reimbursement", "Expense reimbursement (A09)", expenses, "07-A09")

    extra_total = sum((D(i[1]) for i in extra_earnings), Decimal(0))
    gross = res["gross"] + extra_total
    employer = res["sg"] + res["pt"] + res["wc"] + res["al_cost"] + res["lsl_cost"]
    net = res["net"] + extra_total - term_wh + expenses
    return {"currency": "AUD", "days": employed_days(worker, start, end), "lines": lines,
            "gross": r2(gross), "net": r2(net), "employer_cost": r2(employer),
            "total_cost": r2(gross + employer + expenses),
            "meta": {"engine": "AU FY2026-27", "worker_type": p["worker_type"], "state": p["state"],
                     "method": res["method"], "tax_treatment_code": res["ttc"], "payday": payday.isoformat(),
                     "payg": str(res["payg"] + term_wh), "qe": str(res["qe"]), "sg": str(res["sg"]),
                     "sg_due": res["sg_due"].isoformat(), "whm_gross": str(res["gross"]) if p.get("residency") == "WHM" else "0",
                     "al_hours": str(r2(res["al_hours"])), "pcl_hours": str(r2(res["pcl_hours"])),
                     "rules": res["rules"]}}


def platform_settlement(worker, notice_given_days, initiated_by, cessation=None):
    """Final-pay components for an AU employee, with their termination withholding."""
    from .rules import parse_date
    if worker["engagement_type"] != "EOR":
        return []
    p = profile(worker)
    start, end = parse_date(worker["start_date"]), parse_date(worker["end_date"])
    years = (end - start).days // 365
    age = None
    if p.get("dob"):
        dob = date.fromisoformat(p["dob"])
        age = end.year - dob.year - ((end.month, end.day) < (dob.month, dob.day))
    salary = D(worker["salary_annual"])
    weekly = r2(salary / 52)
    lv = RULES["leave"]
    loading = D(p.get("leave_loading", lv["AL_LOADING"]))
    weeks_service = D((end - start).days + 1) / 7
    accrued_weeks = weeks_service * D(lv["AL_WEEKS"]) / 52
    taken_weeks = D(worker["data"].get("leave_taken_days", 0)) / 5
    unused = r2(max(accrued_weeks - taken_weeks, Decimal(0)) * weekly * (1 + loading))
    gr = cessation == "R"
    res = termination(years, age, gr, weekly, unused, weekly,
                      scale="S2" if p.get("tft", True) else "S1",
                      pay_in_lieu=initiated_by == "employer", notice_given_weeks=notice_given_days // 7)
    return [[label, str(r2(amount)), code, str(wh) if wh is not None else ""]
            for code, label, amount, wh in res["components"]]
