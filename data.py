"""Tiny JSON-backed store shared by both cogs.

Holds invoices, sales (payments received) and expenses, plus the small amount
of query logic the statements need (period filtering, totals).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config
from config import DATA_FILE

PERIODS: Dict[str, str] = {
    "today": "Today",
    "week": "Last 7 days",
    "month": "Last 30 days",
    "all": "All time",
}


# --------------------------------------------------------------------------
# store plumbing
# --------------------------------------------------------------------------
def _empty() -> Dict[str, Any]:
    return {"invoices": [], "payments": [], "expenses": [], "next_invoice": 1001}


def _load() -> Dict[str, Any]:
    if not Path(DATA_FILE).exists():
        return _empty()
    try:
        raw = json.loads(Path(DATA_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(raw, dict):
        return _empty()
    data = _empty()
    data.update(raw)
    return data


def _save(data: Dict[str, Any]) -> None:
    path = Path(DATA_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False), "utf-8")
    os.replace(tmp, path)


def _local_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=config.UTC_OFFSET_HOURS)))


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return _local_now().date().isoformat()


def period_start(period: str) -> str:
    """ISO date boundary for a period key ('', meaning no cutoff, for 'all')."""
    today = _local_now()
    if period == "today":
        return today.date().isoformat()
    if period == "week":
        return (today - timedelta(days=7)).date().isoformat()
    if period == "month":
        return (today - timedelta(days=30)).date().isoformat()
    return ""


def money(value: float) -> str:
    """Rupees with Indian digit grouping, e.g. ₹1,25,000.00"""
    sign = "-" if value < 0 else ""
    whole, frac = f"{abs(value):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}₹{whole}.{frac}"


def pretty_date(iso: str) -> str:
    """2026-10-27 -> 27 Oct 2026 (falls back to the raw text)."""
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%d %b %Y")
    except ValueError:
        return iso or "—"


def _entry_date(row: Dict[str, Any]) -> str:
    """Local date of a payment (older rows only have a UTC created_at)."""
    return str(row.get("date") or str(row.get("created_at", ""))[:10])


# --------------------------------------------------------------------------
# invoices
# --------------------------------------------------------------------------
def invoices() -> List[Dict[str, Any]]:
    return _load()["invoices"]


def invoice(number: str) -> Optional[Dict[str, Any]]:
    number = number.strip().upper()
    for inv in invoices():
        if str(inv.get("number", "")).upper() == number:
            return inv
    return None


def create_invoice(
    customer: str,
    contact: str,
    amount: float,
    due: str,
    service: str,
    created_by: str,
    company: str = "",
) -> Dict[str, Any]:
    data = _load()
    number = f"INV-{data['next_invoice']}"
    data["next_invoice"] += 1
    record = {
        "number": number,
        "customer": customer.strip(),
        "contact": contact.strip(),
        "amount": round(amount, 2),
        "due": due.strip(),
        "service": service.strip(),
        "status": "unpaid",
        "created_at": _stamp(),
        "created_by": created_by,
        "paid_at": None,
        "paid_method": None,
        "company": (company or "").strip().lower(),
    }
    data["invoices"].append(record)
    _save(data)
    return record


def mark_paid(
    number: str,
    method: str,
    transaction_id: str,
    server_name: str,
    server_uuid: str,
    remarks: str,
    paid_by: str,
    company: str = "",
) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Mark an invoice paid and record the matching sale.

    Returns ``(invoice, sale)`` or ``None`` if it is missing / already paid.
    """
    data = _load()
    number = number.strip().upper()
    target = None
    for inv in data["invoices"]:
        if str(inv.get("number", "")).upper() == number:
            target = inv
            break
    if target is None or target.get("status") == "paid":
        return None

    method = method.strip() or "unspecified"
    chosen = (company or target.get("company") or "").strip().lower()
    target["status"] = "paid"
    target["paid_at"] = _stamp()
    target["paid_method"] = method
    target["company"] = chosen

    sale = {
        "date": _today(),
        "client_name": target["customer"],
        "client_contact": target.get("contact", ""),
        "server_name": server_name.strip(),
        "server_uuid": server_uuid.strip(),
        "service": target.get("service") or target.get("description", ""),
        "amount": target["amount"],
        "method": method,
        "transaction_id": transaction_id.strip(),
        "remarks": remarks.strip(),
        "invoice": target["number"],
        "company": chosen,
        "created_at": _stamp(),
        "recorded_by": paid_by,
    }
    data["payments"].append(sale)
    _save(data)
    return target, sale


# --------------------------------------------------------------------------
# sales (payments received)
# --------------------------------------------------------------------------
def _in_company(row: Dict[str, Any], company: Optional[str]) -> bool:
    """Company filter: unset means every company (old records have no company)."""
    return not company or str(row.get("company", "")) == company


def payments(start: str = "", company: Optional[str] = None) -> List[Dict[str, Any]]:
    rows = [p for p in _load()["payments"] if _entry_date(p) >= start and _in_company(p, company)]
    return sorted(rows, key=lambda p: (_entry_date(p), p.get("created_at", "")), reverse=True)


def total_sales(start: str = "", company: Optional[str] = None) -> float:
    return round(sum(p["amount"] for p in payments(start, company)), 2)


def add_payment(
    client_name: str,
    client_contact: str,
    server_name: str,
    server_uuid: str,
    service: str,
    amount: float,
    method: str,
    transaction_id: str,
    remarks: str,
    date: str,
    recorded_by: str,
    company: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    data = _load()
    record = {
        "date": date.strip() or _today(),
        "client_name": client_name.strip(),
        "client_contact": client_contact.strip(),
        "server_name": server_name.strip(),
        "server_uuid": server_uuid.strip(),
        "service": service.strip(),
        "amount": round(amount, 2),
        "method": method.strip() or "unspecified",
        "transaction_id": transaction_id.strip(),
        "remarks": remarks.strip(),
        "company": (company or "").strip().lower(),
        "created_at": _stamp(),
        "recorded_by": recorded_by,
    }
    if extra:
        record.update(extra)
    data["payments"].append(record)
    _save(data)
    return record


# --------------------------------------------------------------------------
# expenses
# --------------------------------------------------------------------------
def expenses(start: str = "", company: Optional[str] = None) -> List[Dict[str, Any]]:
    rows = [e for e in _load()["expenses"]
            if str(e.get("date", "")) >= start and _in_company(e, company)]
    return sorted(rows, key=lambda e: (e.get("date", ""), e.get("created_at", "")), reverse=True)


def total_expenses(start: str = "", company: Optional[str] = None) -> float:
    return round(sum(e["amount"] for e in expenses(start, company)), 2)


def add_expense(
    description: str,
    category: str,
    amount: float,
    method: str,
    paid_to: str,
    transaction_id: str,
    remarks: str,
    date: str,
    recorded_by: str,
    company: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    data = _load()
    record = {
        "date": date.strip() or _today(),
        "description": description.strip(),
        "category": category.strip() or "General",
        "amount": round(amount, 2),
        "method": method.strip() or "unspecified",
        "paid_to": paid_to.strip(),
        "transaction_id": transaction_id.strip(),
        "remarks": remarks.strip(),
        "company": (company or "").strip().lower(),
        "created_at": _stamp(),
        "recorded_by": recorded_by,
    }
    if extra:
        record.update(extra)
    data["expenses"].append(record)
    _save(data)
    return record


def has_ref(ref: str) -> bool:
    """True if a sale/expense carrying this external reference was already recorded."""
    data = _load()
    return any(r.get("ref") == ref for r in data["payments"] + data["expenses"])
