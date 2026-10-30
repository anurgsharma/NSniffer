"""Integration boundary. Each external system (KYC, e-signature, background checks,
payroll/tax filing, payments, FX, notifications) has an interface here and a mock
implementation that behaves like a real provider. Swap in real providers by adding a
class and selecting it with EOR_ADAPTER_<NAME>=<impl> (see build_adapters).

Mock behaviour can be steered through email addresses so every path can be exercised:
  +kycfail   KYC check fails            +decline  e-signature is declined
  +flag      background check flagged   +latepay  invoice is never auto-paid
"""
import uuid
from decimal import Decimal


def _ref(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class KYCProvider:
    def verify(self, client): raise NotImplementedError


class ESignProvider:
    def send(self, document, signers): raise NotImplementedError
    def status(self, ref): raise NotImplementedError


class BackgroundCheckProvider:
    def start(self, worker, checks): raise NotImplementedError
    def status(self, ref): raise NotImplementedError


class PayrollProvider:
    def provision(self, worker, benefits): raise NotImplementedError
    def file_taxes(self, pay_run, payslips): raise NotImplementedError


class PaymentsProvider:
    def create_payee(self, worker): raise NotImplementedError
    def collect(self, invoice, client): raise NotImplementedError
    def status(self, ref): raise NotImplementedError
    def payout(self, payslip, worker): raise NotImplementedError


class FXProvider:
    def convert(self, amount, src, dst): raise NotImplementedError


class Notifier:
    def send(self, recipient, subject, body): raise NotImplementedError


# ---------------------------------------------------------------- mocks

class MockKYC(KYCProvider):
    def verify(self, client):
        ok = "+kycfail" not in client["contact_email"]
        return {"status": "passed" if ok else "failed", "ref": _ref("kyc"),
                "reason": None if ok else "Beneficial owner could not be verified"}


class MockESign(ESignProvider):
    """Signature arrives on the first status poll after sending (async-like)."""
    def __init__(self):
        self.envelopes = {}

    def send(self, document, signers):
        ref = _ref("env")
        self.envelopes[ref] = "declined" if any("+decline" in s for s in signers) else "signed"
        return ref

    def status(self, ref):
        return self.envelopes.get(ref, "signed")


class MockBackgroundCheck(BackgroundCheckProvider):
    def __init__(self):
        self.checks = {}

    def start(self, worker, checks):
        ref = _ref("bgc")
        self.checks[ref] = "consider" if "+flag" in worker["email"] else "clear"
        return ref

    def status(self, ref):
        return self.checks.get(ref, "clear")


class MockPayroll(PayrollProvider):
    def provision(self, worker, benefits):
        return {"ref": _ref("emp"), "benefits_enrolled": list(benefits)}

    def file_taxes(self, pay_run, payslips):
        return {"ref": _ref("filing"), "filings": len(payslips)}


class MockPayments(PaymentsProvider):
    def __init__(self):
        self.collections = {}

    def create_payee(self, worker):
        return _ref("payee")

    def collect(self, invoice, client):
        ref = _ref("col")
        self.collections[ref] = "pending" if "+latepay" in client["contact_email"] else "paid"
        return ref

    def status(self, ref):
        return self.collections.get(ref, "paid")

    def payout(self, payslip, worker):
        return _ref("po")


class MockFX(FXProvider):
    TO_USD = {"USD": "1", "GBP": "1.27", "CAD": "0.73", "INR": "0.012", "EUR": "1.08",
              "PHP": "0.0175", "MXN": "0.055"}

    def convert(self, amount, src, dst):
        return Decimal(str(amount)) * Decimal(self.TO_USD[src]) / Decimal(self.TO_USD[dst])


class OutboxNotifier(Notifier):
    """Writes every message to the outbox table; a real mail/SMS relay drains it."""
    def __init__(self, store):
        self.store = store

    def send(self, recipient, subject, body):
        self.store.insert("outbox", recipient=recipient, subject=subject, status="queued", data={"body": body})


class Adapters:
    def __init__(self, **kw):
        self.__dict__.update(kw)


REGISTRY = {
    "kyc": {"mock": MockKYC}, "esign": {"mock": MockESign}, "background": {"mock": MockBackgroundCheck},
    "payroll": {"mock": MockPayroll}, "payments": {"mock": MockPayments}, "fx": {"mock": MockFX},
}


def build_adapters(store, config):
    chosen = {name: impls[config.adapter(name)]() for name, impls in REGISTRY.items()}
    return Adapters(notify=OutboxNotifier(store), **chosen)
