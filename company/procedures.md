# Finance Team - Operating Procedures

This is the company knowledge the AI operator reads before acting.
It is plain text on purpose: a different company would give a different file,
and the agent code would not change.

## Where things live
- Vendor emails and invoices arrive in the `inbox/` folder. File names start with the date received.
- The payables system of record is **FinancePortal** at http://127.0.0.1:5000
  - Register of entered invoices: http://127.0.0.1:5000/invoices
  - Enter a new invoice: http://127.0.0.1:5000/invoices/new

## Entering a vendor invoice
1. Identify the correct document. Payment reminders, quotes and statements are NOT invoices.
2. Check the register first. Never enter an invoice number that is already in the register.
3. Use the **total payable amount including GST**, in INR, as a plain number with two decimals (e.g. `62350.00`).
4. Dates are entered as `YYYY-MM-DD`.
5. Select the vendor from the portal's vendor list; do not invent new vendors.
6. In Notes, write the source file name the data came from.

## Approval policy
- Invoices with a total **above INR 1,00,000** require explicit approval from a human before they are entered.
- If a required field (amount, due date, invoice number) is missing or ambiguous in the document, do not guess. Ask a human.

## Definition of done
An invoice counts as entered only when it is visible in the register with the correct amount and due date.
