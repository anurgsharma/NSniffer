"""CLI: python -m eor {serve|tick|quote|demo}"""
import argparse
import json
from datetime import date, timedelta

from . import rules
from .config import Config
from .db import Store
from .service import Platform


def cmd_serve(args):
    from .api import serve
    cfg = Config()
    if args.port:
        cfg.port = args.port
    if args.host:
        cfg.host = args.host
    p = Platform(cfg)
    httpd, stop = serve(p)
    print(f"LanceSoft EOR/AOR platform on http://{cfg.host}:{cfg.port}  (automation every {cfg.tick_seconds}s)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()


def cmd_tick(args):
    today = rules.parse_date(args.today) if args.today else None
    print(json.dumps(Platform().engine.tick(today)))


def cmd_quote(args):
    print(json.dumps(rules.quote(args.country, args.type.upper(), args.salary), default=str, indent=2))


def run_demo(platform, start=date(2026, 1, 5), days=70, auto_resolve=True, out=print):
    """Seed a realistic book of business and run the line day by day."""
    p = platform
    acme = p.create_client({"name": "Acme Robotics", "contact_email": "finance@acme.example", "country": "US"})
    shady = p.create_client({"name": "Shady Shell Ltd", "contact_email": "ceo+kycfail@shady.example"})
    for w in [
        {"full_name": "Priya Nair", "email": "priya@example.com", "country": "IN", "salary_annual": 2400000,
         "job_title": "Backend Engineer"},
        {"full_name": "Oliver Smith", "email": "oliver@example.com", "country": "GB", "salary_annual": 65000,
         "job_title": "Product Manager", "leave_taken_days": 2},
        {"full_name": "Lena Becker", "email": "lena@example.com", "country": "DE", "salary_annual": 72000,
         "job_title": "Data Scientist"},
        {"full_name": "Juan Reyes", "email": "juan@example.com", "country": "PH", "salary_annual": 900000,
         "job_title": "Support Lead"},
        {"full_name": "Sofia Garcia", "email": "sofia@example.com", "country": "MX", "engagement_type": "AOR",
         "rate_hourly": 450, "job_title": "UX Designer",
         "classification_answers": {"client_controls_schedule": False, "works_exclusively_for_client": False,
                                    "integrated_into_core_business": False, "client_provides_equipment": False,
                                    "paid_regular_salary_like": False, "engagement_over_12_months": False,
                                    "cannot_subcontract": False, "receives_employee_benefits": False}},
        {"full_name": "Mark Lee", "email": "mark@example.com", "country": "CA", "engagement_type": "AOR",
         "rate_hourly": 95, "job_title": "DevOps (full-time, exclusive)",
         "classification_answers": {"client_controls_schedule": True, "works_exclusively_for_client": True,
                                    "integrated_into_core_business": True, "paid_regular_salary_like": True}},
        {"full_name": "Dan Flag", "email": "dan+flag@example.com", "country": "US", "salary_annual": 90000},
    ]:
        p.create_worker({"client_id": acme["id"], "start_date": (start + timedelta(days=10)).isoformat(), **w})
    p.create_worker({"client_id": shady["id"], "full_name": "Eve", "email": "eve@example.com", "country": "US",
                     "salary_annual": 50000, "start_date": start.isoformat()})

    offboarded = False
    for i in range(days):
        today = start + timedelta(days=i)
        p.engine.tick(today)
        sofia = p.store.find("workers", full_name="Sofia Garcia")[0]
        if sofia["status"] == "active" and today.day == 15:
            p.submit_timesheet({"worker_id": sofia["id"], "period": today.strftime("%Y-%m"), "hours": 120,
                                "expenses": 80})
        oliver = p.store.find("workers", full_name="Oliver Smith")[0]
        if not offboarded and oliver["status"] == "active" and i >= 35:
            p.offboard(oliver["id"], {"reason": "Role eliminated", "initiated_by": "employer",
                                      "end_date": (today + timedelta(days=3)).isoformat()}, today)
            offboarded = True
        if auto_resolve:
            for t in p.store.find("tasks", status="open"):
                # Demo policy: reject high-risk contractors (auto-convert to EOR) and bad KYC; approve the rest.
                decision = "reject" if t["kind"] in ("classification_review", "kyc_review") else "approve"
                p.resolve_task(t["id"], {"decision": decision, "note": "demo reviewer", "resolved_by": "demo"})
                out(f"{today}  task {t['kind']:<22} -> {decision:<8}({t['title']})")
        p.engine.tick(today)

    out("\nWorkers")
    for w in p.store.find("workers"):
        out(f"  {w['full_name']:<14} {w['country']} {w['engagement_type']}  {w['status']}")
    out("\nPay runs")
    for r in p.store.find("pay_runs", order="period"):
        out(f"  {r['period']}  {r['status']:<9} {r['data'].get('billing_currency')} {r['data'].get('billing_total')}")
        for sl in p.store.find("payslips", pay_run_id=r["id"]):
            out(f"      {sl['data']['worker_name']:<14} gross {sl['currency']} {sl['gross']:>12}  "
                f"net {sl['net']:>12}  employer {sl['employer_cost']:>11}")
    out(f"\nDashboard: {json.dumps(p.dashboard())}")
    out(f"Notifications queued: {len(p.store.find('outbox'))}, audit events: {len(p.store.find('events'))}")


def cmd_demo(args):
    cfg = Config()
    cfg.db_path = args.db
    run_demo(Platform(cfg, Store(args.db)))


def main():
    ap = argparse.ArgumentParser(prog="eor", description="LanceSoft EOR/AOR automation platform")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run API, console and scheduler")
    s.add_argument("--port", type=int)
    s.add_argument("--host")
    s.set_defaults(fn=cmd_serve)
    t = sub.add_parser("tick", help="run one automation pass")
    t.add_argument("--today")
    t.set_defaults(fn=cmd_tick)
    q = sub.add_parser("quote", help="monthly cost estimate")
    q.add_argument("country")
    q.add_argument("salary", type=float)
    q.add_argument("--type", default="EOR")
    q.set_defaults(fn=cmd_quote)
    d = sub.add_parser("demo", help="simulate ~10 weeks of operations")
    d.add_argument("--db", default=":memory:")
    d.set_defaults(fn=cmd_demo)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
