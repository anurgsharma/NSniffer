import os
from dataclasses import dataclass, field
from decimal import Decimal


def _env(name, default):
    return os.environ.get(f"EOR_{name}", default)


@dataclass
class Config:
    db_path: str = field(default_factory=lambda: _env("DB", "eor.db"))
    host: str = field(default_factory=lambda: _env("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("PORT", "8080")))
    api_key: str = field(default_factory=lambda: _env("API_KEY", ""))
    tick_seconds: int = field(default_factory=lambda: int(_env("TICK_SECONDS", "30")))
    ops_email: str = field(default_factory=lambda: _env("OPS_EMAIL", "ops@lancesofteor.com"))
    # Commercial defaults for new clients
    eor_fee_monthly: Decimal = field(default_factory=lambda: Decimal(_env("EOR_FEE_MONTHLY", "599")))
    aor_fee_pct: Decimal = field(default_factory=lambda: Decimal(_env("AOR_FEE_PCT", "0.05")))
    # Automation thresholds: anything above these is routed to a human approval task
    payroll_cutoff_day: int = field(default_factory=lambda: int(_env("PAYROLL_CUTOFF_DAY", "20")))
    payrun_approval_threshold: Decimal = field(
        default_factory=lambda: Decimal(_env("PAYRUN_APPROVAL_THRESHOLD", "250000")))
    timesheet_auto_approve_hours: Decimal = field(
        default_factory=lambda: Decimal(_env("TIMESHEET_AUTO_APPROVE_HOURS", "200")))
    expense_auto_approve: Decimal = field(default_factory=lambda: Decimal(_env("EXPENSE_AUTO_APPROVE", "1000")))

    def adapter(self, name):
        return _env(f"ADAPTER_{name.upper()}", "mock")
