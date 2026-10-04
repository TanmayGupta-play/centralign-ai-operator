"""Mock internal finance portal - the "company system" the agent operates.

It is a normal web app that knows nothing about the agent. The agent uses it
the way an employee would: through the browser.

Built-in realism so we can see the agent cope with things going wrong:
  * The first invoice submission after startup fails with a 503 (flaky system).
  * Server-side validation rejects badly formatted amounts / dates.
  * Duplicate invoice numbers are rejected.

Run:  python portal/app.py            (http://127.0.0.1:5000)
      python portal/app.py --reset    (wipe entered invoices back to the seed, then start)
"""
import json
import re
import sys
from datetime import date
from pathlib import Path

from flask import Flask, redirect, render_template_string, request, url_for

DB_PATH = Path(__file__).parent / "db.json"
VENDORS = ["Acme Supplies Pvt Ltd", "Globex Corporation", "Initech Solutions", "Umbrella Logistics"]
SEED = [{"vendor": "Acme Supplies Pvt Ltd", "invoice_number": "INV-1042", "amount": "48500.00",
         "due_date": "2026-09-11", "entered_on": "2026-08-13", "notes": ""}]

app = Flask(__name__)
state = {"submissions": 0}  # used to simulate one transient outage


def load_db():
    if not DB_PATH.exists():
        save_db(SEED)
    return json.loads(DB_PATH.read_text(encoding="utf-8"))


def save_db(rows):
    DB_PATH.write_text(json.dumps(rows, indent=2), encoding="utf-8")


PAGE = """<!doctype html>
<html><head><title>{{ title }} - FinancePortal</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;background:#f4f6f8;color:#1d2733}
 header{background:#1f3b57;color:#fff;padding:14px 28px;display:flex;gap:24px;align-items:center}
 header a{color:#cfe3f5;text-decoration:none} main{padding:24px 28px;max-width:900px}
 table{border-collapse:collapse;width:100%;background:#fff} td,th{border:1px solid #d5dce3;padding:8px;text-align:left}
 form{background:#fff;padding:20px;border:1px solid #d5dce3;display:grid;gap:12px;max-width:420px}
 label{display:grid;gap:4px;font-size:14px} input,select{padding:7px;font-size:14px}
 button{padding:9px;background:#1f3b57;color:#fff;border:0;cursor:pointer}
 .ok{background:#e3f6e8;border:1px solid #8bc79b;padding:10px;margin-bottom:14px}
 .err{background:#fde8e8;border:1px solid #e09a9a;padding:10px;margin-bottom:14px}
</style></head><body>
<header><b>FinancePortal</b><a href="/invoices">Invoices</a><a href="/invoices/new">New invoice</a></header>
<main>{{ body|safe }}</main></body></html>"""


def page(title, body, status=200):
    return render_template_string(PAGE, title=title, body=body), status


@app.get("/")
def home():
    return redirect(url_for("list_invoices"))


@app.get("/invoices")
def list_invoices():
    rows = load_db()
    created = request.args.get("created")
    body = f'<div class="ok">Invoice {created} saved successfully.</div>' if created else ""
    body += "<h2>Payables register</h2><table><tr><th>Vendor</th><th>Invoice #</th><th>Amount (INR)</th><th>Due date</th><th>Entered on</th><th>Notes</th></tr>"
    for r in rows:
        body += (f"<tr><td>{r['vendor']}</td><td>{r['invoice_number']}</td><td>{r['amount']}</td>"
                 f"<td>{r['due_date']}</td><td>{r['entered_on']}</td><td>{r['notes']}</td></tr>")
    return page("Invoices", body + "</table>")


def form_html(values=None, error=None):
    v = values or {}
    opts = "".join(f'<option {"selected" if v.get("vendor") == x else ""}>{x}</option>' for x in VENDORS)
    err = f'<div class="err">{error}</div>' if error else ""
    return f"""{err}<h2>New payable invoice</h2>
<form method="post" action="/invoices/new">
 <label>Vendor <select id="vendor" name="vendor"><option value="">-- select vendor --</option>{opts}</select></label>
 <label>Invoice number <input id="invoice_number" name="invoice_number" value="{v.get('invoice_number', '')}"></label>
 <label>Amount (INR) <input id="amount" name="amount" placeholder="e.g. 12500.00" value="{v.get('amount', '')}"></label>
 <label>Due date <input id="due_date" name="due_date" placeholder="YYYY-MM-DD" value="{v.get('due_date', '')}"></label>
 <label>Notes <input id="notes" name="notes" value="{v.get('notes', '')}"></label>
 <button id="save" type="submit">Save invoice</button>
</form>"""


@app.get("/invoices/new")
def new_invoice():
    return page("New invoice", form_html())


@app.post("/invoices/new")
def create_invoice():
    f = {k: request.form.get(k, "").strip() for k in ("vendor", "invoice_number", "amount", "due_date", "notes")}

    # Simulated transient outage: the very first submission is lost.
    state["submissions"] += 1
    if state["submissions"] == 1 and "--no-flaky" not in sys.argv:
        return page("Error", '<div class="err"><b>503 Service Temporarily Unavailable.</b> '
                    'The ledger service did not respond. Your invoice was NOT saved. Please try again.</div>'
                    '<a href="/invoices/new">Back to new invoice</a>', 503)

    error = None
    if f["vendor"] not in VENDORS:
        error = "Please select a valid vendor."
    elif not f["invoice_number"]:
        error = "Invoice number is required."
    elif not re.fullmatch(r"\d+(\.\d{1,2})?", f["amount"]):
        error = "Amount must be a plain number without commas or currency symbols, e.g. 12500.00"
    elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}", f["due_date"]):
        error = "Due date must be in YYYY-MM-DD format."
    elif any(r["invoice_number"] == f["invoice_number"] for r in load_db()):
        error = f"Invoice {f['invoice_number']} already exists in the register."
    if error:
        return page("New invoice", form_html(f, error), 400)

    rows = load_db()
    rows.append({**f, "amount": f"{float(f['amount']):.2f}", "entered_on": date.today().isoformat()})
    save_db(rows)
    return redirect(url_for("list_invoices", created=f["invoice_number"]))


if __name__ == "__main__":
    if "--reset" in sys.argv:
        save_db(SEED)
        print("Database reset to seed data.")
    app.run(port=5000, debug=False)
