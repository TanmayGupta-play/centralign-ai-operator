# Autonomous AI Operator — prototype

You give it a short request in plain English. It works out the steps, reads company files, operates a real web app in a real browser, recovers when things go wrong, asks a human only when it must, **verifies** the result in the system of record, and returns a summary with evidence.

```
python run.py "Find the latest invoice from Acme, enter it into our finance portal, and tell me once it's done"
```

The request doesn't say which file is "latest", that payment reminders aren't invoices, what format the portal wants, or how to work out a due date given as "Net 30". The agent has to figure all of that out.

---

## Setup

Requires Python 3.10+ and **either** a free Google Gemini key (aistudio.google.com/apikey) **or** an Anthropic API key.

```powershell
pip install -r requirements.txt
python -m playwright install chromium
$env:GEMINI_API_KEY="..."          # free tier, uses gemini-3.8-flash
# or
$env:ANTHROPIC_API_KEY="sk-ant-..." # uses claude-opus-5-5
```
The model is picked from whichever key is set; override with `--model`.

## Run

Terminal 1, the company's internal system:
```bash
python portal/app.py --reset             # http://127.0.0.1:5000 , wipes data back to the seed
```

Terminal 2, the agent:
```bash
python run.py "Find the latest invoice from Acme, enter it into our finance portal, and tell me once it's done"
```

Options: `--headless` (hide the browser), `--auto-approve` (skip the submit-approval prompt), `--effort low|medium|high|xhigh|max`, `--max-steps N`.

Each run writes `runs/<timestamp>/` containing `report.md` (summary + evidence), `log.jsonl` (every tool call, input, observation, error and timing), a screenshot after every browser observation, and `browser.webm`, a video of everything the agent did in the browser.

### Demo scenarios (same code, different requests)

| Request | What it shows |
|---|---|
| *"Find the latest invoice from Acme, enter it into our finance portal, and tell me once it's done"* | Ignores the newer *payment reminder*; works out the due date from "Net 30" (2026-10-14); first submit hits a **503** → re-plans and retries; verifies in the register |
| *Same request again* | Finds INV-1077 already in the register → does **not** create a duplicate (idempotent) |
| *"Enter the Globex invoice into the portal"* | Over ₹1,00,000 → **asks for approval** per company policy; converts `20/10/2026` and `1,24,000.00` |
| *"Record Initech's invoice"* | Amount missing from the document → **asks the human** instead of guessing |
| *"Which Acme invoices are overdue as of today?"* | A read-only task, no code changes — shows the agent isn't hard-wired to data entry |

---

## Architecture

```
            ┌──────────────────────────── agent/loop.py ────────────────────────────┐
 request ──▶│  Claude (reasoning)  ──tool call──▶  Guards  ──▶  Toolbox (agent/tools.py)│
            │        ▲                                            │                  │
            │        └──────────── observation / error ◀──────────┘                  │
            │  State: conversation · plan · last-write / last-check steps · run log │
            └───────────────────────────────────────────────────────────────────────┘
                    │ files (sandboxed)                 │ real Chromium (Playwright)
                    ▼                                   ▼
          company/  procedures.md, inbox/       portal/app.py  (mock FinancePortal)
```

| File | Role |
|---|---|
| `agent/loop.py` | The loop: asks the model for the next action, executes tool calls, feeds results back, enforces guards, writes logs and report |
| `agent/llm.py` | Model layer: one small interface (`step`, `add_results`) with Gemini and Claude implementations; retries, backoff and model fallback |
| `agent/tools.py` | Generic tools: `update_plan`, `list_files`, `read_file`, `browser_goto/read/fill/click`, `ask_human`, `finish` |
| `company/procedures.md` | Company knowledge (where things live, formats, approval policy, definition of done) |
| `company/inbox/` | Vendor emails: invoices, a reminder (distractor), an over-limit invoice, one with a missing amount |
| `portal/app.py` | Independent Flask app: the system of record. Knows nothing about the agent |
| `run.py` | CLI entry point |

### How one loop iteration works
1. Send the conversation so far (task + every previous action and observation) to the model, along with the tool definitions.
2. The model reasons and returns one or more tool calls (independent ones are batched into a single turn).
3. Each call goes through the guards and then runs. **Every result, including exceptions**, becomes a `tool_result` the model sees next turn. That's how it observes and adapts.
4. Repeat until Claude calls `finish`, or the step limit is hit.

Mapping to the loop in the brief:

| Stage | Where it happens |
|---|---|
| Understand | The model reads the request, then `procedures.md` (company context) |
| Plan | `update_plan` — an explicit, logged plan, revised when reality changes |
| Execute | File and browser tools act on a real browser and a real web app |
| Observe | Every action returns the resulting page text, form fields and errors, plus a screenshot |
| Adapt | Errors are returned to the model, never swallowed; the prompt requires diagnosing them before retrying |
| Verify | **Enforced in code**: `finish(done)` is rejected unless a page was re-read *after* the last data-changing click |
| Complete | `finish` returns a status (`done` / `failed` / `needs_human`), a summary and evidence → `report.md` |

---

## Key design decisions

1. **The model decides; code enforces.** The model chooses the next action, which gives flexibility on unseen tasks. Hard rules (sandboxed file access, human approval before form submissions, verification before "done", step limit) live in code, because a prompt can be ignored and code can't.
2. **Generic tools, company knowledge as data.** No tool mentions invoices. The workflow knowledge is in `procedures.md`. A new workflow means a new document, not new code. That's the *generalization* criterion.
3. **Hand-written loop instead of an agent framework.** It's about 150 lines, every step is visible, and it's easy to debug and extend live. Frameworks hide exactly the parts being evaluated (failure handling, verification, approvals).
4. **Text page observations instead of screenshots-as-input.** `browser_read` returns visible text plus every interactive element with a ready-to-use CSS selector. That's cheaper, faster and more precise than pixel-based computer use for web apps. Screenshots are still saved as human evidence.
5. **Two levels of human-in-the-loop.**
   - *Mechanical*: any click that submits a form changes data in another system, so it needs approval. This is detected from the DOM, not from the model's say-so.
   - *Judgment*: company policy ("above ₹1,00,000 needs approval", "don't guess missing fields") is followed by the model through `ask_human`.
6. **Verification against the system of record, not the agent's memory.** "I clicked save" doesn't mean the data was saved (see the 503). Done means seen in the register with the right values.
7. **Realistic failure built into the environment.** The portal really fails on the first submission, really validates formats and really rejects duplicates, so the recovery shown in the demo is real, not scripted.
8. **Full audit trail.** `log.jsonl` + screenshots + `report.md` per run, so any decision can be traced afterwards.
9. **Reasoning visible.** Thinking summaries are printed (`THINK`) so a human can see *why* the agent did something.
10. **Model-agnostic, and resilient to the model itself failing.** The loop talks to a small `Brain` interface, so providers can be swapped. Model outages are handled like any other failure: `503 overloaded` → back off and retry; `429 quota exhausted` → move to the next model in a fallback chain. Mid-run model switches keep the conversation intact. This came out of real testing: the free tier returned both errors during development.
11. **Fewer model calls.** The prompt asks for independent actions (reading several files, filling a whole form) to be batched into one turn. That cut a full run from 22 model calls to 13.

## Models, APIs and libraries used
- **Google Gemini** (`gemini-3.8-flash`, falling back through other Flash models) through the official `google-genai` SDK: function calling with thought summaries. This is what the demo uses (free tier).
- **Claude Opus 5.5** (`claude-opus-5-5`) through the official `anthropic` SDK, as the alternative provider: tool use, adaptive thinking with summaries, effort `high`, and server-side refusal fallback (`fallbacks: "default"`).
- **Playwright** (Chromium) for browser control. **Flask** for the mock portal.
- No agent framework, vector DB or other external services.

## Assumptions
- The company system is a web app reachable by URL (true for most internal tools). Desktop apps are out of scope for the prototype.
- Company procedures exist as a document the agent can read.
- Inbox emails are exported as text files (a mail or document connector is a drop-in replacement for `list_files` / `read_file`).
- Everything runs locally against mock data. No real credentials or third-party systems are used.

## Known limitations
- **Memory is per-run.** Context lives in the conversation; nothing is learned across runs.
- Browser observation is DOM/text based. Canvas-heavy UIs or native desktop apps would need screenshot-based computer use.
- No login/auth flows, file attachments or PDFs in the demo environment.
- Long tasks grow the context, with no compaction or summarisation yet.
- Approval and clarification are synchronous terminal prompts. A real deployment needs async channels (Slack/email) and the ability to pause and resume.
- Single-threaded, one task at a time, no task queue or scheduling.
- The Gemini free tier allows about 20 requests per model per day, so only a few full runs fit in a day. The fallback chain stretches this, but it's a demo constraint, not a design one.

## What I would build next
1. **Persistent company memory**: store outcomes, human corrections and learned facts ("Acme's due date is always Net 30") and retrieve them for future tasks.
2. **Connector abstraction**: tools declared with read/write risk levels and per-company permissions, so approval policy is configuration instead of DOM heuristics.
3. **Checkpoint and resume**: persist run state so a task waiting on human approval can pause for hours and continue later. Run it on a background task queue with retries.
4. **Hybrid observation**: fall back to screenshot-based computer use when the DOM isn't usable, and extend to desktop apps.
5. **Evaluation harness**: a suite of tasks with automatic checks on the portal state, to measure success rate, steps, cost and human interventions per change.
6. **Independent verifier**: a separate, cheaper model call that checks the evidence against the original request before reporting "done".
