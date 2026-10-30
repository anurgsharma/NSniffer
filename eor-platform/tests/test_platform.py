import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from decimal import Decimal

from eor import rules
from eor.__main__ import run_demo
from eor.api import serve
from eor.config import Config
from eor.db import Store
from eor.service import Platform, ValidationError


def platform(**cfg):
    c = Config()
    c.db_path = ":memory:"
    for k, v in cfg.items():
        setattr(c, k, v)
    return Platform(c, Store(":memory:"))


def active_client(p, email="ops@client.example"):
    c = p.create_client({"name": "Client", "contact_email": email})
    p.engine.tick(date(2026, 1, 1))
    return p.store.get("clients", c["id"])


class RulesTest(unittest.TestCase):
    def test_progressive_tax(self):
        brackets = [[0, 0.1], [100, 0.2]]
        self.assertEqual(rules.progressive_tax(Decimal(50), brackets), Decimal("5.0"))
        self.assertEqual(rules.progressive_tax(Decimal(150), brackets), Decimal("20.0"))

    def test_contribution_floor_and_cap(self):
        item = {"rate": 0.1, "floor": 100, "cap": 300}
        self.assertEqual(rules.contribution(Decimal(50), item), 0)
        self.assertEqual(rules.contribution(Decimal(1000), item), Decimal("20.0"))

    def test_us_quote_is_consistent(self):
        q = rules.quote("US", "EOR", 120000)
        self.assertEqual(q["monthly_gross"], Decimal("10000.00"))
        self.assertEqual(q["monthly_total_cost"], q["monthly_gross"] + q["monthly_employer_cost"])
        self.assertLess(q["monthly_net"], q["monthly_gross"])

    def test_contractor_has_no_withholding(self):
        q = rules.quote("MX", "AOR", 120000)
        self.assertEqual(q["monthly_net"], q["monthly_gross"])
        self.assertEqual(q["monthly_employer_cost"], 0)

    def test_proration(self):
        w = {"engagement_type": "EOR", "salary_annual": "120000", "start_date": "2026-01-17", "end_date": None}
        slip = rules.calc_payslip(w, rules.country("US"), "2026-01")
        self.assertEqual(slip["days"], 15)
        self.assertEqual(slip["gross"], Decimal("4838.71"))

    def test_classification_levels(self):
        all_no = {k: False for k in rules.CLASSIFICATION_FACTORS}
        self.assertEqual(rules.classify(all_no, rules.country("US"))["level"], "low")
        all_yes = {k: True for k in rules.CLASSIFICATION_FACTORS}
        self.assertEqual(rules.classify(all_yes, rules.country("US"))["level"], "high")
        self.assertGreater(rules.classify(all_no, rules.country("GB"))["score"], 0)

    def test_settlement_pay_in_lieu(self):
        w = {"engagement_type": "EOR", "salary_annual": "36500", "start_date": "2026-01-01",
             "end_date": "2026-12-31", "data": {"leave_taken_days": 28}}
        items = dict(rules.settlement(w, rules.country("GB"), notice_given_days=2, initiated_by="employer"))
        self.assertEqual(items["Pay in lieu of notice (5 days)"], Decimal("500.00"))


class LifecycleTest(unittest.TestCase):
    def test_client_onboarding(self):
        p = platform()
        self.assertEqual(active_client(p)["status"], "active")
        bad = p.create_client({"name": "Bad", "contact_email": "x+kycfail@bad.example"})
        p.engine.tick(date(2026, 1, 1))
        self.assertEqual(p.store.get("clients", bad["id"])["status"], "kyc_review")
        task = p.store.find("tasks", owner_id=bad["id"])[0]
        p.resolve_task(task["id"], {"decision": "reject"})
        p.engine.tick(date(2026, 1, 1))
        self.assertEqual(p.store.get("clients", bad["id"])["status"], "rejected")

    def test_eor_hire_to_paid(self):
        p = platform()
        c = active_client(p)
        w = p.create_worker({"client_id": c["id"], "full_name": "A", "email": "a@x.example", "country": "DE",
                             "salary_annual": 60000, "start_date": "2026-01-10"})
        p.engine.tick(date(2026, 1, 2))
        self.assertEqual(p.store.get("workers", w["id"])["status"], "ready")
        p.engine.tick(date(2026, 1, 20))
        self.assertEqual(p.store.get("workers", w["id"])["status"], "active")
        run = p.store.find("pay_runs", client_id=c["id"])[0]
        self.assertEqual(run["status"], "closed")
        self.assertIn("tax_filing_ref", run["data"])
        inv = p.store.find("invoices")[0]
        self.assertEqual(inv["status"], "paid")
        slip = p.store.find("payslips")[0]
        self.assertEqual(slip["status"], "paid")
        self.assertEqual(Decimal(inv["amount"]),
                         Decimal(slip["data"]["billing_cost"]) + Decimal(slip["data"]["billing_fee"]))

    def test_validation_and_fix(self):
        p = platform()
        c = active_client(p)
        w = p.create_worker({"client_id": c["id"], "full_name": "Low", "email": "l@x.example", "country": "GB",
                             "salary_annual": 1000, "start_date": "2026-01-10"})
        p.engine.tick(date(2026, 1, 2))
        w = p.store.get("workers", w["id"])
        self.assertEqual(w["status"], "needs_attention")
        self.assertTrue(w["data"]["errors"])
        p.update_worker(w["id"], {"salary_annual": 40000})
        p.engine.tick(date(2026, 1, 2))
        self.assertEqual(p.store.get("workers", w["id"])["status"], "ready")

    def test_high_risk_contractor_converted_to_eor(self):
        p = platform()
        c = active_client(p)
        w = p.create_worker({"client_id": c["id"], "full_name": "C", "email": "c@x.example", "country": "CA",
                             "engagement_type": "AOR", "rate_hourly": 50, "start_date": "2026-01-10",
                             "classification_answers": {k: True for k in rules.CLASSIFICATION_FACTORS}})
        p.engine.tick(date(2026, 1, 2))
        self.assertEqual(p.store.get("workers", w["id"])["status"], "classification")
        task = p.store.find("tasks", kind="classification_review")[0]
        p.resolve_task(task["id"], {"decision": "reject"})
        p.engine.tick(date(2026, 1, 2))
        w = p.store.get("workers", w["id"])
        self.assertEqual((w["engagement_type"], w["status"], w["salary_annual"]), ("EOR", "ready", "104000.00"))

    def test_contractor_paid_from_timesheets(self):
        p = platform()
        c = active_client(p)
        w = p.create_worker({"client_id": c["id"], "full_name": "T", "email": "t@x.example", "country": "MX",
                             "engagement_type": "AOR", "rate_hourly": 100, "start_date": "2026-01-01",
                             "classification_answers": {k: False for k in rules.CLASSIFICATION_FACTORS}})
        p.engine.tick(date(2026, 1, 1))
        p.submit_timesheet({"worker_id": w["id"], "period": "2026-01", "hours": 10, "expenses": 5})
        big = p.submit_timesheet({"worker_id": w["id"], "period": "2026-01", "hours": 500})
        p.engine.tick(date(2026, 1, 20))
        slip = p.store.find("payslips")[0]
        self.assertEqual(slip["gross"], "1000.00")
        self.assertEqual(slip["net"], "1005.00")
        self.assertEqual(p.store.get("timesheets", big["id"])["status"], "review")

    def test_offboarding_and_final_pay(self):
        p = platform()
        c = active_client(p)
        w = p.create_worker({"client_id": c["id"], "full_name": "O", "email": "o@x.example", "country": "GB",
                             "salary_annual": 52000, "start_date": "2026-01-01"})
        p.engine.tick(date(2026, 1, 1))
        p.offboard(w["id"], {"initiated_by": "employer", "end_date": "2026-02-10"}, today=date(2026, 2, 9))
        p.engine.tick(date(2026, 2, 9))
        self.assertTrue(p.store.get("workers", w["id"])["data"]["settlement"])
        p.engine.tick(date(2026, 2, 11))
        self.assertEqual(p.store.get("workers", w["id"])["status"], "terminated")
        p.engine.tick(date(2026, 2, 20))
        feb = p.store.find("pay_runs", period="2026-02")[0]
        labels = [l["label"] for s in p.store.find("payslips", pay_run_id=feb["id"]) for l in s["data"]["lines"]]
        self.assertTrue(any(l.startswith("Pay in lieu") for l in labels))

    def test_large_payrun_needs_approval_and_late_payment_dunning(self):
        p = platform(payrun_approval_threshold=Decimal("1"))
        c = active_client(p, "pay+latepay@client.example")
        p.create_worker({"client_id": c["id"], "full_name": "B", "email": "b@x.example", "country": "US",
                         "salary_annual": 90000, "start_date": "2026-01-01"})
        p.engine.tick(date(2026, 1, 20))
        run = p.store.find("pay_runs")[0]
        self.assertEqual(run["status"], "approval")
        p.resolve_task(p.store.find("tasks", kind="payrun_approval")[0]["id"], {"decision": "approve"})
        p.engine.tick(date(2026, 1, 20))
        self.assertEqual(p.store.get("pay_runs", run["id"])["status"], "invoiced")
        p.engine.tick(date(2026, 2, 1))
        self.assertEqual(p.store.find("invoices")[0]["status"], "overdue")
        p.webhook("payments", {"ref": p.store.find("invoices")[0]["external_ref"], "status": "paid"})
        p.engine.tick(date(2026, 2, 1))
        self.assertEqual(p.store.get("pay_runs", run["id"])["status"], "closed")

    def test_demo_runs_clean(self):
        p = platform()
        run_demo(p, out=lambda *_: None)
        self.assertFalse(p.store.find("tasks", status="open"))
        self.assertTrue(all(r["status"] == "closed" for r in p.store.find("pay_runs")))

    def test_input_validation(self):
        p = platform()
        with self.assertRaises(ValidationError):
            p.create_client({"name": "x"})
        with self.assertRaises(ValidationError):
            p.create_worker({"client_id": "nope", "full_name": "a", "email": "a@b", "country": "US",
                             "start_date": "2026-01-01"})


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = platform(port=0, api_key="secret")
        cls.httpd, cls.stop = serve(cls.p, scheduler=False)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def call(self, path, body=None, key="secret"):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "X-API-Key": key})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_auth(self):
        self.assertEqual(self.call("/api/dashboard", key="wrong")[0], 401)
        self.assertEqual(self.call("/api/health", key="")[0], 200)

    def test_flow_over_http(self):
        s, c = self.call("/api/clients", {"name": "Api Co", "contact_email": "a@api.example"})
        self.assertEqual(s, 200)
        s, w = self.call("/api/workers", {"client_id": c["id"], "full_name": "W", "email": "w@x.example",
                                          "country": "IN", "salary_annual": 1500000, "start_date": "2026-01-05"})
        self.assertEqual(s, 200)
        self.call("/api/automation/tick", {"today": "2026-01-25"})
        self.assertEqual(self.call(f"/api/workers/{w['id']}")[1]["status"], "active")
        runs = self.call(f"/api/payruns?client_id={c['id']}")[1]
        self.assertEqual(runs[0]["status"], "closed")
        self.assertTrue(self.call(f"/api/payruns/{runs[0]['id']}")[1]["payslips"])

    def test_errors(self):
        self.assertEqual(self.call("/api/clients", {"name": "x"})[0], 400)
        self.assertEqual(self.call("/api/workers/wrk_missing")[0], 404)
        self.assertEqual(self.call("/api/quote", {"country": "US", "salary_annual": 100000})[0], 200)


if __name__ == "__main__":
    unittest.main()
