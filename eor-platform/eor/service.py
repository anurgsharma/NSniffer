"""Use-case layer shared by the HTTP API and the CLI. Validates input, writes records,
and leaves all downstream work to the Automator."""
from datetime import date, timedelta

from . import rules
from .adapters import build_adapters
from .config import Config
from .db import Store
from .engine import Automator
from .rules import D


class ValidationError(ValueError):
    pass


def _require(body, *names):
    missing = [n for n in names if body.get(n) in (None, "")]
    if missing:
        raise ValidationError(f"missing required field(s): {', '.join(missing)}")


def _date(value, name):
    try:
        return rules.parse_date(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{name} must be an ISO date (YYYY-MM-DD)")


class Platform:
    def __init__(self, config=None, store=None):
        self.cfg = config or Config()
        self.store = store or Store(self.cfg.db_path)
        self.adapters = build_adapters(self.store, self.cfg)
        self.engine = Automator(self.store, self.adapters, self.cfg)

    # ---------------------------------------------------------------- clients

    def create_client(self, body):
        _require(body, "name", "contact_email")
        c = self.store.insert(
            "clients", name=body["name"], country=(body.get("country") or "").upper() or None,
            contact_email=body["contact_email"], billing_currency=body.get("billing_currency", "USD").upper(),
            eor_fee_monthly=str(body.get("eor_fee_monthly", self.cfg.eor_fee_monthly)),
            aor_fee_pct=str(body.get("aor_fee_pct", self.cfg.aor_fee_pct)),
            payment_terms_days=int(body.get("payment_terms_days", 7)), status="new")
        self.store.audit("clients", c["id"], "created")
        return c

    # ---------------------------------------------------------------- workers

    def create_worker(self, body):
        _require(body, "client_id", "full_name", "email", "country", "start_date")
        if not self.store.get("clients", body["client_id"]):
            raise ValidationError("unknown client_id")
        etype = body.get("engagement_type", "EOR").upper()
        if etype not in ("EOR", "AOR"):
            raise ValidationError("engagement_type must be EOR or AOR")
        _date(body["start_date"], "start_date")
        if body.get("end_date"):
            _date(body["end_date"], "end_date")
        r = rules.country(body["country"])
        w = self.store.insert(
            "workers", client_id=body["client_id"], full_name=body["full_name"], email=body["email"],
            country=body["country"].upper(), engagement_type=etype, job_title=body.get("job_title"),
            start_date=body["start_date"], end_date=body.get("end_date"),
            salary_annual=str(body["salary_annual"]) if body.get("salary_annual") else None,
            rate_hourly=str(body["rate_hourly"]) if body.get("rate_hourly") else None,
            currency=r["currency"] if r else None, status="requested",
            data={"classification_answers": body.get("classification_answers") or {},
                  "leave_taken_days": body.get("leave_taken_days", 0)})
        self.store.audit("workers", w["id"], "created")
        return w

    def update_worker(self, worker_id, body):
        w = self._get("workers", worker_id)
        allowed = {"full_name", "email", "job_title", "salary_annual", "rate_hourly", "start_date", "end_date",
                   "country", "engagement_type"}
        fields = {k: (str(v) if v is not None else None) for k, v in body.items() if k in allowed}
        data = {k: body[k] for k in ("classification_answers", "leave_taken_days") if k in body}
        if w["status"] == "needs_attention":
            fields["status"] = "requested"
            if "classification" in w["data"]:
                data["classification"] = None
        w = self.store.update("workers", worker_id, data=data or None, **fields)
        self.store.audit("workers", worker_id, "updated", fields=sorted(fields))
        return w

    def offboard(self, worker_id, body, today=None):
        today = today or date.today()
        w = self._get("workers", worker_id)
        if w["status"] in ("terminated", "rejected", "withdrawn", "offboarding"):
            raise ValidationError(f"worker is already {w['status']}")
        if w["status"] not in ("active", "ready"):
            self.store.update("workers", worker_id, status="withdrawn", data={"withdraw_reason": body.get("reason")})
            self.store.audit("workers", worker_id, "status_changed", **{"from": w["status"], "to": "withdrawn"})
            return self.store.get("workers", worker_id)
        r = rules.country(w["country"])
        end = _date(body["end_date"], "end_date") if body.get("end_date") else today + timedelta(days=r["notice_days"])
        if end < rules.parse_date(w["start_date"]):
            end = rules.parse_date(w["start_date"])
        self.store.update("workers", worker_id, status="offboarding", end_date=end.isoformat(),
                          data={"initiated_by": body.get("initiated_by", "employer"),
                                "offboard_reason": body.get("reason"),
                                "notice_given_days": max((end - today).days, 0)})
        self.store.audit("workers", worker_id, "status_changed",
                         **{"from": w["status"], "to": "offboarding", "note": body.get("reason", "")})
        return self.store.get("workers", worker_id)

    # ---------------------------------------------------------------- timesheets

    def submit_timesheet(self, body):
        _require(body, "worker_id", "period")
        self._get("workers", body["worker_id"])
        try:
            rules.period_bounds(body["period"])
        except ValueError:
            raise ValidationError("period must be YYYY-MM")
        return self.store.insert("timesheets", worker_id=body["worker_id"], period=body["period"],
                                 hours=str(D(body.get("hours", 0))), expenses=str(D(body.get("expenses", 0))),
                                 status="submitted", data={"notes": body.get("notes")})

    # ---------------------------------------------------------------- tasks & webhooks

    def resolve_task(self, task_id, body):
        t = self._get("tasks", task_id)
        decision = body.get("decision")
        if decision not in ("approve", "reject"):
            raise ValidationError("decision must be 'approve' or 'reject'")
        if t["status"] != "open":
            raise ValidationError(f"task already {t['status']}")
        t = self.store.update("tasks", task_id, status={"approve": "approved", "reject": "rejected"}[decision],
                              data={"note": body.get("note"), "resolved_by": body.get("resolved_by", "ops")})
        self.store.audit("tasks", task_id, "task_resolved", decision=decision, note=body.get("note"))
        return t

    def webhook(self, kind, body):
        """Entry point for providers that push status instead of being polled."""
        if kind == "esign":
            docs = self.store.find("documents", external_ref=body.get("ref"))
            if not docs:
                raise ValidationError("unknown envelope ref")
            return self.store.update("documents", docs[0]["id"], status=body["status"])
        if kind == "payments":
            invs = self.store.find("invoices", external_ref=body.get("ref"))
            if not invs:
                raise ValidationError("unknown collection ref")
            return self.store.update("invoices", invs[0]["id"], status=body["status"])
        raise ValidationError(f"unknown webhook {kind}")

    # ---------------------------------------------------------------- read models

    def payrun_detail(self, run_id):
        run = self._get("pay_runs", run_id)
        run["payslips"] = self.store.find("payslips", pay_run_id=run_id)
        return run

    def dashboard(self):
        return {
            "clients": self.store.count_by("clients", "status"),
            "workers": self.store.count_by("workers", "status"),
            "pay_runs": self.store.count_by("pay_runs", "status"),
            "invoices": self.store.count_by("invoices", "status"),
            "open_tasks": len(self.store.find("tasks", status="open")),
            "queued_messages": len(self.store.find("outbox", status="queued")),
        }

    def _get(self, table, id):
        row = self.store.get(table, id)
        if not row:
            raise LookupError(f"{table[:-1]} {id} not found")
        return row
