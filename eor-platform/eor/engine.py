"""The automated production line.

Every client, worker, timesheet and pay run is a state machine. `Automator.tick()` runs
each record's handler repeatedly until nothing moves, so a record flows through as many
stages as it can in one tick and stops only when it waits on the outside world (a
signature, a start date, a payment) or on a human approval task. Human gates are only
raised for exceptions (failed KYC, high misclassification risk, flagged background
check, large pay runs, unusual timesheets); the happy path needs no human.
"""
from datetime import date, timedelta
from decimal import Decimal

from . import documents, rules
from .rules import D, money

CLIENT_FLOW = ["new", "kyc_review", "kyc_passed", "msa_sent", "active"]
WORKER_FLOW = ["requested", "classification", "contracting", "contract_sent", "screening",
               "provisioning", "ready", "active", "offboarding", "terminated"]
PAYRUN_FLOW = ["draft", "calculated", "approval", "approved", "invoiced", "funded", "disbursed", "closed"]
WORKER_DONE = {"terminated", "rejected", "withdrawn"}
PAYABLE = {"ready", "active", "offboarding", "terminated"}


class Automator:
    def __init__(self, store, adapters, config):
        self.store, self.ad, self.cfg = store, adapters, config

    # ------------------------------------------------------------ plumbing

    def tick(self, today=None):
        today = today or date.today()
        total = 0
        with self.store.lock:
            for _ in range(50):
                moved = (self._each("clients", self.client_step, today)
                         + self._each("workers", self.worker_step, today)
                         + self._each("timesheets", self.timesheet_step, today)
                         + self.schedule_payruns(today)
                         + self._each("pay_runs", self.payrun_step, today))
                total += moved
                if not moved:
                    break
        return {"today": today.isoformat(), "transitions": total}

    def _each(self, table, fn, today):
        done = {"clients": ["active", "rejected"], "workers": list(WORKER_DONE),
                "timesheets": ["approved", "rejected"], "pay_runs": ["closed", "rejected"]}[table]
        rows = [r for r in self.store.find(table) if r["status"] not in done]
        return sum(1 for r in rows if fn(r, today))

    def move(self, table, row, status, note="", data=None, **fields):
        self.store.update(table, row["id"], data=data, status=status, **fields)
        self.store.audit(table, row["id"], "status_changed", **{"from": row["status"], "to": status, "note": note})
        return True

    def gate(self, owner_type, owner_id, kind, title, payload=None):
        """Human approval checkpoint. Returns 'pending', 'approved' or 'rejected'."""
        found = self.store.find("tasks", order="created_at DESC", limit=1,
                                owner_type=owner_type, owner_id=owner_id, kind=kind)
        if not found:
            t = self.store.insert("tasks", owner_type=owner_type, owner_id=owner_id, kind=kind, title=title,
                                  status="open", data=payload or {})
            self.store.audit("tasks", t["id"], "task_opened", kind=kind, owner=owner_id)
            self.ad.notify.send(self.cfg.ops_email, f"Approval needed: {title}", f"Task {t['id']} ({kind})")
            return "pending"
        return {"open": "pending"}.get(found[0]["status"], found[0]["status"])

    def esign_status(self, doc):
        """Prefer a status pushed by webhook; otherwise poll the provider."""
        if doc["status"] == "sent":
            st = self.ad.esign.status(doc["external_ref"])
            if st != "sent":
                doc = self.store.update("documents", doc["id"], status=st)
        return doc["status"]

    def send_for_signature(self, owner_type, owner_id, kind, body, signers):
        doc = self.store.insert("documents", owner_type=owner_type, owner_id=owner_id, kind=kind,
                                status="draft", data={"body": body, "signers": signers})
        ref = self.ad.esign.send(doc, signers)
        return self.store.update("documents", doc["id"], status="sent", external_ref=ref)

    # ------------------------------------------------------------ clients

    def client_step(self, c, today):
        s = c["status"]
        if s == "new":
            res = self.ad.kyc.verify(c)
            if res["status"] == "passed":
                return self.move("clients", c, "kyc_passed", "KYC passed", data={"kyc_ref": res["ref"]})
            return self.move("clients", c, "kyc_review", res.get("reason") or "KYC failed",
                             data={"kyc_ref": res["ref"], "kyc_reason": res.get("reason")})
        if s == "kyc_review":
            g = self.gate("clients", c["id"], "kyc_review", f"KYC review: {c['name']}",
                          {"reason": c["data"].get("kyc_reason")})
            if g == "approved":
                return self.move("clients", c, "kyc_passed", "KYC manually approved")
            if g == "rejected":
                return self.move("clients", c, "rejected", "KYC rejected")
            return False
        if s == "kyc_passed":
            body = (f"MASTER SERVICES AGREEMENT\n\nLanceSoft EOR and {c['name']}.\n"
                    f"EOR fee: {c['billing_currency']} {c['eor_fee_monthly']} per employee per month.\n"
                    f"AOR fee: {D(c['aor_fee_pct']) * 100}% of contractor payments.\n"
                    f"Payment terms: {c['payment_terms_days']} days from invoice.")
            doc = self.send_for_signature("clients", c["id"], "msa", body, [c["contact_email"]])
            return self.move("clients", c, "msa_sent", "MSA sent for signature", data={"msa_doc_id": doc["id"]})
        if s == "msa_sent":
            st = self.esign_status(self.store.get("documents", c["data"]["msa_doc_id"]))
            if st == "signed":
                self.ad.notify.send(c["contact_email"], "Welcome to LanceSoft EOR",
                                    "Your account is active. You can now add workers.")
                return self.move("clients", c, "active", "MSA signed")
            if st == "declined":
                return self.move("clients", c, "rejected", "MSA declined")
        return False

    # ------------------------------------------------------------ workers

    def validate_worker(self, w, r):
        errs = []
        if not r:
            return [f"Country {w['country']} is not supported"]
        if w["engagement_type"] == "AOR" and not r["aor_allowed"]:
            errs.append(f"Contractor engagements are not supported in {r['name']}")
        if w["engagement_type"] == "EOR":
            if not w.get("salary_annual"):
                errs.append("salary_annual is required for EOR employees")
            elif D(w["salary_annual"]) < D(r["min_wage_annual"]):
                errs.append(f"Salary below statutory minimum ({r['currency']} {r['min_wage_annual']}/yr)")
        elif not (w.get("rate_hourly") or w.get("salary_annual")):
            errs.append("rate_hourly or salary_annual is required for contractors")
        if "@" not in (w.get("email") or ""):
            errs.append("A valid email is required")
        return errs

    def worker_step(self, w, today):
        s = w["status"]
        r = rules.country(w["country"])
        client = self.store.get("clients", w["client_id"])

        if s == "requested":
            if client["status"] == "rejected":
                return self.move("workers", w, "rejected", "Client account rejected")
            if client["status"] != "active":
                return False
            errs = self.validate_worker(w, r)
            if errs:
                self.ad.notify.send(client["contact_email"], f"Action needed for {w['full_name']}", "\n".join(errs))
                return self.move("workers", w, "needs_attention", "Validation failed", data={"errors": errs})
            nxt = "classification" if w["engagement_type"] == "AOR" else "contracting"
            return self.move("workers", w, nxt, "Validated against country rules", data={"errors": []},
                             currency=r["currency"])

        if s == "classification":
            result = w["data"].get("classification")
            if not result:
                result = rules.classify(w["data"].get("classification_answers"), r)
                self.store.update("workers", w["id"], data={"classification": result})
            if result["level"] != "high":
                return self.move("workers", w, "contracting", f"Classification risk {result['level']}")
            g = self.gate("workers", w["id"], "classification_review",
                          f"High misclassification risk: {w['full_name']} ({w['country']})",
                          {"classification": result,
                           "approve": "keep as contractor (AOR)", "reject": "convert to employee (EOR)"})
            if g == "approved":
                return self.move("workers", w, "contracting", "Contractor status approved by reviewer",
                                 data={"classification_override": True})
            if g == "rejected":
                salary = w.get("salary_annual") or str(money(D(w.get("rate_hourly")) * 2080))
                return self.move("workers", w, "requested", "Converted from AOR to EOR",
                                 engagement_type="EOR", salary_annual=salary, rate_hourly=None,
                                 data={"converted_from": "AOR"})
            return False

        if s == "contracting":
            body = documents.contract(w, client, r)
            kind = "employment_agreement" if w["engagement_type"] == "EOR" else "contractor_agreement"
            doc = self.send_for_signature("workers", w["id"], kind, body, [w["email"]])
            return self.move("workers", w, "contract_sent", "Contract sent for signature",
                             data={"contract_doc_id": doc["id"]})

        if s == "contract_sent":
            st = self.esign_status(self.store.get("documents", w["data"]["contract_doc_id"]))
            if st == "signed":
                checks = ["identity", "criminal"] + (["right_to_work"] if w["engagement_type"] == "EOR" else [])
                ref = self.ad.background.start(w, checks)
                return self.move("workers", w, "screening", "Contract signed; screening started",
                                 data={"background_ref": ref, "checks": checks})
            if st == "declined":
                self.ad.notify.send(client["contact_email"], f"{w['full_name']} declined the contract", "")
                return self.move("workers", w, "withdrawn", "Contract declined")
            return False

        if s == "screening":
            st = self.ad.background.status(w["data"]["background_ref"])
            if st == "clear":
                return self.move("workers", w, "provisioning", "Screening clear")
            if st == "consider":
                g = self.gate("workers", w["id"], "background_review", f"Background check flagged: {w['full_name']}")
                if g == "approved":
                    return self.move("workers", w, "provisioning", "Screening result accepted")
                if g == "rejected":
                    return self.move("workers", w, "withdrawn", "Screening result not accepted")
            return False

        if s == "provisioning":
            if w["engagement_type"] == "EOR":
                res = self.ad.payroll.provision(w, r["benefits"])
                data = {"payroll_ref": res["ref"], "benefits_enrolled": res["benefits_enrolled"]}
            else:
                data = {"payee_ref": self.ad.payments.create_payee(w)}
            self.ad.notify.send(w["email"], f"Welcome aboard, {w['full_name']}",
                                f"Your start date is {w['start_date']}. Payslips arrive by email each month.")
            return self.move("workers", w, "ready", "Payroll/payee set up; welcome sent", data=data)

        if s == "ready":
            if rules.parse_date(w["start_date"]) <= today:
                self.ad.notify.send(client["contact_email"], f"{w['full_name']} has started", "")
                return self.move("workers", w, "active", "Start date reached")
            return False

        if s == "active":
            # Fixed-term contracts roll into offboarding automatically once the notice window opens.
            if w.get("end_date"):
                end = rules.parse_date(w["end_date"])
                if today >= end - timedelta(days=r["notice_days"]):
                    return self.move("workers", w, "offboarding", "Fixed term ending",
                                     data={"initiated_by": "contract_end",
                                           "notice_given_days": max((end - today).days, 0)})
            return False

        if s == "offboarding":
            if "settlement" not in w["data"]:
                items = rules.settlement(w, r, w["data"].get("notice_given_days", 0), w["data"].get("initiated_by"))
                self.store.update("workers", w["id"], data={"settlement": [[l, str(a)] for l, a in items]})
                self.ad.notify.send(client["contact_email"], f"Offboarding scheduled: {w['full_name']}",
                                    f"Last day {w['end_date']}. Revoke system access and collect equipment.")
                self.ad.notify.send(w["email"], "Your offboarding details",
                                    f"Last day {w['end_date']}. Final pay includes: "
                                    + (", ".join(l for l, _ in items) or "regular pay"))
                self.store.audit("workers", w["id"], "settlement_computed", items=[[l, str(a)] for l, a in items])
                return True
            if today > rules.parse_date(w["end_date"]):
                return self.move("workers", w, "terminated", "Last day passed")
        return False

    # ------------------------------------------------------------ timesheets

    def timesheet_step(self, t, today):
        if t["status"] == "submitted":
            if D(t["hours"]) <= self.cfg.timesheet_auto_approve_hours and D(t["expenses"]) <= self.cfg.expense_auto_approve:
                return self.move("timesheets", t, "approved", "Auto-approved within thresholds")
            return self.move("timesheets", t, "review", "Exceeds auto-approval thresholds")
        if t["status"] == "review":
            g = self.gate("timesheets", t["id"], "timesheet_review",
                          f"Timesheet {t['period']}: {t['hours']} h, expenses {t['expenses']}")
            if g in ("approved", "rejected"):
                return self.move("timesheets", t, g, "Reviewed")
        return False

    # ------------------------------------------------------------ payroll

    def eligible_workers(self, client_id, period):
        start, end = rules.period_bounds(period)
        out = []
        for w in self.store.find("workers", client_id=client_id, status=list(PAYABLE)):
            if rules.employed_days(w, start, end) > 0:
                out.append(w)
        return out

    def schedule_payruns(self, today):
        if today.day < self.cfg.payroll_cutoff_day:
            return 0
        period = today.strftime("%Y-%m")
        n = 0
        for c in self.store.find("clients", status="active"):
            if not self.store.find("pay_runs", client_id=c["id"], period=period) and self.eligible_workers(c["id"], period):
                run = self.store.insert("pay_runs", client_id=c["id"], period=period, status="draft")
                self.store.audit("pay_runs", run["id"], "created", period=period)
                n += 1
        return n

    def payrun_step(self, run, today):
        s = run["status"]
        client = self.store.get("clients", run["client_id"])
        fx = self.ad.fx

        if s == "draft":
            start, end = rules.period_bounds(run["period"])
            billing = Decimal(0)
            for w in self.eligible_workers(client["id"], run["period"]):
                r = rules.country(w["country"])
                sheets = self.store.find("timesheets", worker_id=w["id"], period=run["period"],
                                         status="approved", pay_run_id=None)
                extras = []
                if w.get("end_date") and start <= rules.parse_date(w["end_date"]) <= end:
                    extras = [(l, D(a)) for l, a in w["data"].get("settlement", [])]
                slip = rules.calc_payslip(w, r, run["period"], sheets, extras)
                cost_billing = money(fx.convert(slip["total_cost"], slip["currency"], client["billing_currency"]))
                fee = (money(fx.convert(client["eor_fee_monthly"], "USD", client["billing_currency"]))
                       if w["engagement_type"] == "EOR" else money(cost_billing * D(client["aor_fee_pct"])))
                billing += cost_billing + fee
                self.store.insert("payslips", pay_run_id=run["id"], worker_id=w["id"], currency=slip["currency"],
                                  gross=slip["gross"], net=slip["net"], employer_cost=slip["employer_cost"],
                                  total_cost=slip["total_cost"], status="calculated",
                                  data={"lines": [{**l, "amount": str(l["amount"])} for l in slip["lines"]],
                                        "days": slip["days"], "engagement_type": w["engagement_type"],
                                        "worker_name": w["full_name"],
                                        "billing_cost": str(cost_billing), "billing_fee": str(fee)})
                for t in sheets:
                    self.store.update("timesheets", t["id"], pay_run_id=run["id"])
            return self.move("pay_runs", run, "calculated", "Payslips calculated",
                             data={"billing_total": str(money(billing)), "billing_currency": client["billing_currency"]})

        if s == "calculated":
            if D(run["data"]["billing_total"]) > self.cfg.payrun_approval_threshold:
                return self.move("pay_runs", run, "approval", "Above approval threshold")
            return self.move("pay_runs", run, "approved", "Auto-approved within threshold")

        if s == "approval":
            g = self.gate("pay_runs", run["id"], "payrun_approval",
                          f"Approve {run['period']} pay run for {client['name']}: "
                          f"{run['data']['billing_currency']} {run['data']['billing_total']}")
            if g in ("approved", "rejected"):
                return self.move("pay_runs", run, g, "Reviewed")
            return False

        if s == "approved":
            slips = self.store.find("payslips", pay_run_id=run["id"])
            lines = [{"worker": sl["data"]["worker_name"], "type": sl["data"]["engagement_type"],
                      "cost": sl["data"]["billing_cost"], "fee": sl["data"]["billing_fee"]} for sl in slips]
            inv = self.store.insert("invoices", client_id=client["id"], pay_run_id=run["id"], period=run["period"],
                                    currency=client["billing_currency"], amount=run["data"]["billing_total"],
                                    due_date=(today + timedelta(days=client["payment_terms_days"])).isoformat(),
                                    status="issued", data={"lines": lines})
            ref = self.ad.payments.collect(inv, client)
            self.store.update("invoices", inv["id"], external_ref=ref)
            self.ad.notify.send(client["contact_email"], f"Invoice {inv['id']} for {run['period']}",
                                f"Amount due {inv['currency']} {inv['amount']} by {inv['due_date']}.")
            return self.move("pay_runs", run, "invoiced", "Invoice issued and collection started",
                             data={"invoice_id": inv["id"]})

        if s == "invoiced":
            inv = self.store.get("invoices", run["data"]["invoice_id"])
            if inv["status"] != "paid" and self.ad.payments.status(inv["external_ref"]) == "paid":
                inv = self.store.update("invoices", inv["id"], status="paid")
            if inv["status"] == "paid":
                return self.move("pay_runs", run, "funded", "Client funds received")
            if today > rules.parse_date(inv["due_date"]) and not inv["data"].get("dunning_sent"):
                self.store.update("invoices", inv["id"], status="overdue", data={"dunning_sent": today.isoformat()})
                self.ad.notify.send(client["contact_email"], f"Overdue: invoice {inv['id']}",
                                    "Payroll is on hold until this invoice is paid.")
                self.store.audit("invoices", inv["id"], "dunning_sent")
            return False

        if s == "funded":
            for sl in self.store.find("payslips", pay_run_id=run["id"]):
                w = self.store.get("workers", sl["worker_id"])
                ref = self.ad.payments.payout(sl, w)
                self.store.update("payslips", sl["id"], status="paid", data={"payout_ref": ref})
                self.ad.notify.send(w["email"], f"Your payslip for {run['period']}",
                                    f"Net pay {sl['currency']} {sl['net']} has been sent.")
            return self.move("pay_runs", run, "disbursed", "Workers paid")

        if s == "disbursed":
            eor = [sl for sl in self.store.find("payslips", pay_run_id=run["id"])
                   if sl["data"]["engagement_type"] == "EOR"]
            data = {}
            if eor:
                res = self.ad.payroll.file_taxes(run, eor)
                data = {"tax_filing_ref": res["ref"]}
            return self.move("pay_runs", run, "closed", "Statutory filings submitted" if eor else "Closed", data=data)
        return False
