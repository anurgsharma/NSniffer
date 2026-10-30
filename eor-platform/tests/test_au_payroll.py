"""Acceptance tests from Payroll_Logic_Australia_FY2026-27.xlsx (GPE-AU-LOGIC v1.3).

TC01-TC16 are tab 10's columns; A1/A2 and B1/B2 are tab 11's scenarios. Expected values are
the workbook's computed results, including the ATO-published MATCH anchors (TC01-TC04,
TC10, TC11, TC14, TC15)."""
import unittest
from datetime import date
from decimal import Decimal

from eor import au_payroll as au
from eor.au_payroll import AUInput

JUL31, JUL16 = date(2026, 7, 31), date(2026, 7, 16)

# id: (inputs, expected)
CASES = {
    "TC01": (dict(state="NSW", frequency="M", ordinary="5400.33", tax_offsets_annual=1365, hours="164.67", rate="32.8", payday=JUL31),
             dict(method="S2", ttc="RTXXXX", x="1246.99", base=940, offsets=113, payg=827, net="4573.33", qe="5400.33", sg="648.04",
                  sg_due="2026-08-11", pt="296.67", wc="120.36", al_cost="488.18", lsl_cost="90.01", ctc="7043.59")),
    "TC02": (dict(state="NSW", frequency="W", ordinary="1333.45", medicare_reduction=True, children=5, hours=38, rate="35.09", payday=JUL16),
             dict(method="S2", ttc="RTXXX5", x="1333.99", base=245, wft="1325.54", sop=1656, mla=26, payg=219, net="1114.45",
                  sg="160.01", sg_due="2026-07-27", pt="73.25", wc="29.72", al_cost="120.52", lsl_cost="22.22", ctc="1739.17")),
    "TC03": (dict(worker_type="W02", state="VIC", frequency="F", ordinary="1299.3", medicare_exemption="F", tax_offsets_annual=1645,
                  hours=50, rate="25.99", payday=JUL16),
             dict(method="S5", ttc="RTXXFX", x="649.99", base=86, offsets=63, payg=23, net="1276.3", sg="155.92", pt="73",
                  wc="26.19", al_cost="117.45", lsl_cost="21.66", ctc="1693.52")),
    "TC04": (dict(state="QLD", frequency="M", ordinary="10627.88", tft=False, stsl=True, hours="164.67", rate="64.54", payday=JUL31),
             dict(method="S1", ttc="RNSXXX", x="2452.99", base=3155, stsl=979, payg=4134, net="6493.88", sg="1275.35",
                  pt="594.17", wc="159.86", al_cost="960.59", lsl_cost="177.13", ctc="13794.98")),
    "TC05": (dict(state="NSW", frequency="M", ordinary=10000, qe_ytd=30000, hours="164.67", rate="60.73", payday=JUL31),
             dict(method="S2", ttc="RTXXXX", x="2307.99", base=2414, payg=2414, net=7586, sg=1200, pt="549.36", wc="222.88",
                  al_cost="903.88", lsl_cost="166.67", ctc="13042.79")),
    "TC06": (dict(state="VIC", frequency="M", ordinary=15000, stsl=True, salary_sacrifice=1000, qe_ytd=45000, hours="164.67",
                  rate="91.09", payday=JUL31),
             dict(method="S2", ttc="RTSXXX", x="3230.99", base=3887, stsl=1296, payg=5183, net=8817, qe=15000, sg=1800,
                  super_total=2800, pt="842.8", wc="302.4", al_cost="1355.75", lsl_cost=250, ctc="19550.95")),
    "TC07": (dict(worker_type="W03", state="QLD", frequency="W", ordinary=1330, casual_loading="332.5", overtime="367.5",
                  qe_ytd=20000, hours=38, rate=35, leave_loading=0, payday=JUL16),
             dict(method="S2", x="2030.99", base=468, payg=468, net=1562, qe="1662.5", sg="199.5", pt="111.29", wc="29.94",
                  al_hours=0, al_cost=0, lsl_cost="22.17", ctc="2392.9")),
    "TC08": (dict(worker_type="W04", state="WA", frequency="F", ordinary=3200, tft=False, qe_ytd=12000, hours=48, rate="66.67", payday=JUL16),
             dict(method="S1", ttc="RNXXXX", x="1600.99", base=882, payg=882, net=2318, sg=384, pt="197.12", wc="69.21",
                  al_cost="289.25", lsl_cost="53.33", ctc="4192.91")),
    "TC09": (dict(worker_type="W08", state="ACT", frequency="M", residency="F", tft=False, ordinary=9000, qe_ytd=27000,
                  hours="164.67", rate="54.65", payday=JUL31),
             dict(method="S3", ttc="FFXXXX", x="2076.99", base=2700, payg=2700, net=6300, sg=1080, pt="680.4", wc="163.3",
                  al_cost="813.39", lsl_cost=150, ctc="11887.09")),
    "TC10": (dict(worker_type="W07", state="NSW", frequency="W", residency="WHM", tft=False, ordinary=1200, whm_ytd=44500,
                  qe_ytd=44500, hours=38, rate="31.58", payday=JUL16),
             dict(method="WHM", ttc="HRXXXX", override=180, payg=180, net=1020, sg=144, pt="65.92", wc="26.75",
                  al_cost="108.47", lsl_cost=20, ctc="1565.14")),
    "TC11": (dict(state="NSW", frequency="W", tfn_provided=False, tft=False, ordinary=1000, qe_ytd=4000, hours=38, rate="26.32", payday=JUL16),
             dict(method="S4R", ttc="NAXXXX", override=470, payg=470, net=530, sg=120, pt="54.94", wc="22.29",
                  al_cost="90.4", lsl_cost="16.67", ctc="1304.3")),
    "TC12": (dict(state="NSW", frequency="M", ordinary=25000, qe_ytd=250000, hours="164.67", rate="151.82", payday=JUL31),
             dict(method="S2", x="5769.99", base=8909, payg=8909, net=16091, qe_sg=20830, sg="2499.6", pt="1348.86",
                  wc="547.24", al_cost="2259.63", lsl_cost="416.67", ctc="32072")),
    "TC13": (dict(worker_type="W11", state="VIC", frequency="M", ordinary=8000, qe_ytd=16000, hours=160, rate=50, leave_loading=0, payday=JUL31),
             dict(method="S2", x="1846.99", base=1772, payg=1772, net=6228, sg=960, pt="449.49", wc=0, al_cost=0,
                  lsl_cost=0, ctc="9409.49")),
    "TC14": (dict(worker_type="W12", state="NSW", frequency="M", tft=False, ordinary=10000, payday=JUL31),
             dict(method="VA", ttc="VOXXXX", override=2000, payg=2000, net=8000, qe=0, sg=0, pt="490.5", wc=0, ctc="10490.5")),
    "TC15": (dict(worker_type="W14", state="NSW", frequency="M", tfn_provided=False, tft=False, ordinary=2000, payday=JUL31),
             dict(method="NOABN", ttc=None, override=940, payg=940, net=1060, sg=0, pt=0, wc=0, ctc=2000)),
    "TC16": (dict(state="NSW", frequency="W", ordinary=1000, x53=True, qe_ytd=4000, hours=38, rate="26.32", payday=JUL16),
             dict(method="S2", x="1000.99", base=138, extra=3, payg=141, net=859, sg=120, pt="54.94", wc="22.29",
                  al_cost="90.4", lsl_cost="16.67", ctc="1304.3")),
}

# Tab 10 flag rows (SGAPP, PTAPP, WCAPP, ALAPP, LSLAPP) must follow from the worker type alone.
FLAGS = {
    "TC07": dict(SG=True, PT=True, WC=True, AL=False, LSL=True),
    "TC13": dict(SG=True, PT=True, WC=False, AL=False, LSL=False),
    "TC14": dict(SG=False, PT=True, WC=False, AL=False, LSL=False),
    "TC15": dict(SG=False, PT=False, WC=False, AL=False, LSL=False),
    "TC10": dict(SG=True, PT=True, WC=True, AL=True, LSL=True),
}


def money(v):
    return None if v is None else Decimal(str(v))


class WorkbookTestCases(unittest.TestCase):
    def test_all_cases(self):
        for tc, (inputs, expected) in CASES.items():
            res = au.calculate(AUInput.build(**inputs))
            for key, want in expected.items():
                got = res[key]
                with self.subTest(tc=tc, field=key):
                    if key in ("method", "ttc"):
                        self.assertEqual(got, want)
                    elif key == "sg_due":
                        self.assertEqual(got.isoformat(), want)
                    else:
                        self.assertEqual(Decimal(got).quantize(Decimal("0.01")), money(want).quantize(Decimal("0.01")))

    def test_worker_type_flags(self):
        for tc, want in FLAGS.items():
            res = au.calculate(AUInput.build(**CASES[tc][0]))
            with self.subTest(tc=tc):
                self.assertEqual({k: res["flags"][k] for k in want}, want)

    def test_payroll_tax_effective_rates(self):
        t = au.payroll_tax_table()
        self.assertEqual(t["NSW"]["total"], Decimal("294300.00"))
        self.assertEqual(t["VIC"]["total"], Decimal("200666.67"))
        self.assertEqual(t["QLD"]["total"], Decimal("99833.33"))
        self.assertEqual(t["NSW"]["effective"], Decimal("0.04905"))
        self.assertEqual(t["VIC"]["marginal"], Decimal("0.0585"))
        self.assertEqual(t["QLD"]["marginal"], Decimal("0.052"))
        self.assertEqual(au.payroll_tax_rate("WA"), Decimal("0.055"))
        self.assertEqual(au.payroll_tax_rate("ACT"), Decimal("0.0675"))
        self.assertEqual(sum(r["total"] for r in t.values()), Decimal("594800.00"))


class SpecialPayments(unittest.TestCase):
    def test_schedule5_bonus_A1(self):
        b = au.schedule5_bonus(10000, 12000, "S2", "M", False)
        self.assertEqual((b["spread"], b["normal"], b["with_spread"], b["diff"], b["calc"], b["cap"], b["withholding"]),
                         (1000, 2414, 2734, 320, 3840, 5640, 3840))

    def test_schedule5_bonus_A2_with_stsl(self):
        b = au.schedule5_bonus(4000, 3000, "S2", "F", True)
        self.assertEqual((b["spread"], b["normal"], b["with_spread"], b["calc"], b["cap"], b["withholding"]),
                         (115, 918 + 200, 954 + 216, 1352, 1410, 1352))

    def test_bonus_in_pay_event(self):
        res = au.calculate(AUInput.build(frequency="M", ordinary=10000, bonus=12000, payday=JUL31))
        self.assertEqual(res["payg"], 2414 + 3840)
        self.assertEqual(res["qe"], 22000)

    def test_termination_B1_genuine_redundancy(self):
        t = au.termination(years=3, age=50, genuine_redundancy=True, weekly_rate=2300, unused_leave=8000,
                           normal_weekly=2300, extra=25000, other_income=60000)
        got = {k: t[k] for k in ("notice_weeks", "piln", "redundancy_weeks", "redundancy", "tax_free_limit", "lump_sum_d",
                                 "etp_r", "etp_o", "wh_r", "wh_o", "leave_spread", "leave_w1", "leave_w2", "wh_leave",
                                 "total", "total_withholding", "net", "sg_on_piln")}
        self.assertEqual(got, dict(notice_weeks=4, piln=9200, redundancy_weeks=7, redundancy=41100, tax_free_limit=34001,
                                   lump_sum_d=34001, etp_r=16299, etp_o=0, wh_r=5216, wh_o=0, leave_spread=153,
                                   leave_w1=555, leave_w2=604, wh_leave=2560, total=58300, total_withholding=7776,
                                   net=50524, sg_on_piln=1104))
        self.assertEqual(t["cap_o"], 120000)

    def test_termination_B2_not_redundancy(self):
        t = au.termination(years=3, age=50, genuine_redundancy=False, weekly_rate=2300, unused_leave=8000,
                           normal_weekly=2300, extra=5000, other_income=60000)
        self.assertEqual((t["redundancy"], t["etp_o"], t["wh_o"], t["wh_leave"], t["total"], t["total_withholding"], t["net"]),
                         (0, 14200, 4544, 2548, 22200, 7092, 15108))


class Identifiers(unittest.TestCase):
    def test_tfn(self):
        self.assertTrue(au.valid_tfn("123 456 782"))
        self.assertFalse(au.valid_tfn("123456789"))
        self.assertTrue(au.valid_tfn("111111111"))

    def test_abn(self):
        self.assertTrue(au.valid_abn("51 824 753 556"))
        self.assertFalse(au.valid_abn("51824753557"))


class Validations(unittest.TestCase):
    def ids(self, **kw):
        extra = {k: kw.pop(k) for k in ("tfn", "visa", "home_country", "on_hire") if k in kw}
        return {r for r, _, _ in au.validate(AUInput.build(**kw), **extra)}

    def test_rules(self):
        self.assertIn("V-TFN-01", self.ids(tfn="123456789"))
        self.assertIn("V-TFN-03", self.ids(tft=False, medicare_exemption="H"))
        self.assertIn("V-TFN-04", self.ids(medicare_exemption="F", medicare_reduction=True))
        self.assertIn("V-WHM-01", self.ids(residency="WHM", visa="500"))
        self.assertNotIn("V-WHM-01", self.ids(residency="WHM", visa="417", home_country="GB"))
        self.assertIn("V-RATE-01", self.ids(ordinary=900, hours=38, frequency="W"))
        self.assertIn("V-PL-02", self.ids(state="QLD", on_hire=True))
        self.assertNotIn("V-PL-02", self.ids(state="NSW", on_hire=True))

    def test_negative_net_blocks(self):
        with self.assertRaises(ValueError):
            au.calculate(AUInput.build(ordinary=100, post_tax=500, payday=JUL31))

    def test_unknown_field_rejected(self):
        with self.assertRaises(ValueError):
            AUInput.build(salary=1)


class PlatformIntegration(unittest.TestCase):
    def setUp(self):
        from tests.test_platform import active_client, platform
        self.p = platform()
        self.addCleanup(self.p.store.conn.close)
        self.client = active_client(self.p)

    def test_au_employee_end_to_end(self):
        p = self.p
        w = p.create_worker({"client_id": self.client["id"], "full_name": "Ava", "email": "ava@x.example",
                             "country": "AU", "salary_annual": 120000, "start_date": "2026-07-01",
                             "au": {"state": "NSW", "tfn": "123456782"}})
        p.engine.tick(date(2026, 7, 1))
        self.assertEqual(p.store.get("workers", w["id"])["status"], "active")
        p.engine.tick(date(2026, 7, 20))
        p.engine.tick(date(2026, 8, 20))
        jul, aug = (p.store.find("payslips", worker_id=w["id"], order="created_at")[i] for i in (0, 1))
        # Same as workbook TC05: $10,000 monthly, Scale 2 -> PAYG 2414, SG 1200
        self.assertEqual(jul["gross"], "10000.00")
        self.assertEqual(jul["net"], "7586.00")
        self.assertEqual(jul["data"]["meta"]["tax_treatment_code"], "RTXXXX")
        self.assertEqual(jul["data"]["meta"]["sg"], "1200.00")
        self.assertEqual(jul["data"]["meta"]["sg_due"], "2026-08-11")
        self.assertEqual(Decimal(jul["total_cost"]), Decimal("13042.79"))
        self.assertEqual(aug["data"]["meta"]["payday"], "2026-08-31")
        self.assertTrue(all(r["status"] == "closed" for r in p.store.find("pay_runs")))

    def test_super_cap_tracked_across_pay_runs(self):
        p = self.p
        w = p.create_worker({"client_id": self.client["id"], "full_name": "Hi", "email": "hi@x.example",
                             "country": "AU", "salary_annual": 600000, "start_date": "2026-07-01",
                             "au": {"state": "NSW", "tfn": "123456782"}})
        for y, m in [(2026, m) for m in range(7, 13)] + [(2027, 1)]:
            p.engine.tick(date(y, m, 20))
        sg = [Decimal(s["data"]["meta"]["sg"]) for s in p.store.find("payslips", worker_id=w["id"], order="created_at")]
        # $50k QE a month: five full months, December takes the last $20,830 of the base, January gets none
        self.assertEqual(sg, [Decimal("6000.00")] * 5 + [Decimal("2499.60"), Decimal(0)])
        self.assertEqual(sum(sg), Decimal("32499.60"))  # SG_MAX: 12% of the $270,830 base

    def test_intake_validation(self):
        p = self.p
        bad = p.create_worker({"client_id": self.client["id"], "full_name": "Bad", "email": "b@x.example",
                               "country": "AU", "salary_annual": 90000, "start_date": "2026-07-01",
                               "au": {"state": "NSW", "tfn": "123456789"}})
        abn = p.create_worker({"client_id": self.client["id"], "full_name": "Con", "email": "c@x.example",
                               "country": "AU", "engagement_type": "AOR", "rate_hourly": 120, "start_date": "2026-07-01",
                               "classification_answers": {k: False for k in __import__("eor.rules").rules.CLASSIFICATION_FACTORS},
                               "au": {"state": "NSW"}})
        p.engine.tick(date(2026, 7, 1))
        self.assertIn("V-TFN-01", " ".join(p.store.get("workers", bad["id"])["data"]["errors"]))
        self.assertIn("V-CON-02", " ".join(p.store.get("workers", abn["id"])["data"]["errors"]))

    def test_redundancy_settlement(self):
        p = self.p
        w = p.create_worker({"client_id": self.client["id"], "full_name": "Red", "email": "r@x.example",
                             "country": "AU", "salary_annual": 119600, "start_date": "2026-07-01",
                             "au": {"state": "NSW", "tfn": "123456782"}})
        p.engine.tick(date(2026, 7, 1))
        p.offboard(w["id"], {"initiated_by": "employer", "cessation_type": "R", "end_date": "2026-09-15"},
                   today=date(2026, 9, 14))
        p.engine.tick(date(2026, 9, 20))
        sep = p.store.find("pay_runs", period="2026-09")[0]
        slip = p.store.find("payslips", pay_run_id=sep["id"], worker_id=w["id"])[0]
        labels = " ".join(l["label"] for l in slip["data"]["lines"])
        self.assertIn("Lump Sum D", labels)       # notice in lieu inside the tax-free redundancy limit
        self.assertIn("Lump Sum A", labels)       # unused leave taxed at 32%

    def test_quote(self):
        from eor import rules
        q = rules.quote("AU", "EOR", 120000)
        self.assertEqual(q["monthly_net"], Decimal("7586.00"))
        self.assertEqual(q["monthly_total_cost"], Decimal("13042.79"))


if __name__ == "__main__":
    unittest.main()
