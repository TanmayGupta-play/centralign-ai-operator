"""The agent loop: Goal -> Understand -> Plan -> Execute -> Observe -> Adapt -> Verify -> Complete.

Claude decides WHAT to do next. This code decides what is ALLOWED and what
counts as DONE:
  * every tool result (including errors) is fed back so the model can adapt
  * data-changing clicks go through a human approval gate (in tools.py)
  * 'finish(done)' is rejected unless the system of record was re-read after
    the last write (verification guard)
  * a hard step limit stops runaway loops
  * everything is logged to runs/<timestamp>/ with screenshots
"""
import json
import time
from datetime import date, datetime
from pathlib import Path

from agent.llm import make_brain
from agent.tools import TOOL_SCHEMAS, Toolbox

RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"

SYSTEM_PROMPT = """You are an autonomous operations employee at a company. A colleague gives you a request; your job is to deliver the finished outcome, not advice.

How you work:
1. Understand the intended outcome. Short requests leave steps unstated - work them out yourself.
2. Learn the company context first: read procedures.md in the workspace. Follow its rules.
3. Record a plan with update_plan. Update it whenever reality differs from what you expected.
4. Act with the tools. After every action, look at the result before deciding the next step. When several actions do not depend on each other (reading several files, filling several form fields), request them together in one turn.
5. When something fails, read the error, work out why, and try a reasonable fix or alternative. Do not repeat the exact same failing action more than twice.
6. Ask the human (ask_human) only when information is genuinely missing or ambiguous, or when policy requires approval. Never guess values you cannot find.
7. Verify: after changing anything, re-open the system of record and confirm the change is really there and correct.
8. Call finish with a short summary and concrete evidence. Use status 'needs_human' or 'failed' honestly if you could not complete the task.

The company workspace (files) is separate from web systems, which you reach through the browser tools."""


class Run:
    """State for one task execution: the conversation, the plan, and the audit log."""

    def __init__(self, task: str):
        self.task = task
        self.dir = RUNS_DIR / datetime.now().strftime("%Y%m%d_%H%M%S")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.plan: dict = {}
        self.step = 0
        self.last_write_step = 0   # step of the last successful data-changing action
        self.last_check_step = 0   # step of the last fresh look at a web page

    def log(self, **event):
        event = {"step": self.step, "time": datetime.now().isoformat(timespec="seconds"), **event}
        with open(self.dir / "log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _short(text: str, n: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n] + "..."


def execute_tool(run: Run, tools: Toolbox, name: str, args: dict) -> tuple[str, bool]:
    """Run one tool call. Returns (observation, is_error). Also enforces guards."""
    if name == "update_plan":
        run.plan = args
        steps = "\n".join(f"     {i}. {s}" for i, s in enumerate(args["steps"], 1))
        print(f"  PLAN  goal: {args['goal']}\n{steps}")
        return "Plan recorded.", False

    if name == "finish":
        if args["status"] == "done" and run.last_write_step and run.last_check_step <= run.last_write_step:
            return ("REJECTED: you changed data at step %d but have not re-checked the system of record since. "
                    "Open the relevant page, confirm the change, then call finish again." % run.last_write_step), True
        return "Finished.", False

    if name == "browser_click":
        is_write = tools.is_write_action(args["selector"])
        result = tools.browser_click(args["selector"])
        if is_write and not result.startswith("BLOCKED"):
            run.last_write_step = run.step
        return result, result.startswith("BLOCKED")

    result = getattr(tools, name)(**args)
    if name in ("browser_goto", "browser_read"):
        run.last_check_step = run.step
    return result, False


def run_task(task: str, model: str, effort: str, headless: bool, approve_writes: bool, max_steps: int) -> dict:
    run = Run(task)
    system = f"{SYSTEM_PROMPT}\n\nToday's date: {date.today().isoformat()}"
    brain = make_brain(model, effort=effort, system=system, tools=TOOL_SCHEMAS, task=task)
    tools = Toolbox(run.dir, headless=headless, approve_writes=approve_writes)
    outcome = {"status": "failed", "summary": "Step limit reached before the task was finished.", "evidence": []}
    nudges = 0
    print(f"\nTASK: {task}\nModel: {model}\nRun folder: {run.dir}\n")
    run.log(event="task", task=task, model=model)

    try:
        while run.step < max_steps:
            run.step += 1
            try:
                turn = brain.step()
            except Exception as exc:  # model/API failed even after retries and fallbacks
                outcome = {"status": "failed", "summary": f"Model API error: {_short(exc, 300)}", "evidence": []}
                break
            if turn.refused:
                outcome = {"status": "failed", "summary": "The model declined this request.", "evidence": []}
                break

            # Show the model's reasoning so a human can follow its decisions.
            for thought in turn.thoughts:
                print(f"  THINK {_short(thought, 300)}")
                run.log(event="thought", text=thought[:1500])
            for text in turn.texts:
                print(f"  SAY   {_short(text, 300)}")

            if not turn.calls:
                # The model stopped talking without finishing. Push it back into the loop.
                nudges += 1
                if nudges > 2:
                    outcome = {"status": "failed", "summary": "Agent stopped without calling finish.", "evidence": []}
                    break
                brain.add_user_text("Continue working. When the task is complete and verified, call finish.")
                continue

            results, finished = [], False
            for call in turn.calls:
                print(f"[{run.step:02d}] {call.name}({_short(json.dumps(call.input, ensure_ascii=False), 120)})")
                started = time.time()
                try:
                    observation, is_error = execute_tool(run, tools, call.name, call.input)
                except Exception as exc:  # any tool failure becomes an observation the model can react to
                    observation, is_error = f"ERROR {type(exc).__name__}: {exc}", True
                marker = "  !! " if is_error else "  -> "
                print(marker + _short(observation))
                run.log(event="tool", tool=call.name, input=call.input, error=is_error,
                        observation=observation[:2000], seconds=round(time.time() - started, 2))
                results.append((call, observation, is_error))
                if call.name == "finish" and not is_error:
                    outcome, finished = call.input, True
            brain.add_results(results)
            if finished:
                break
    finally:
        tools.close()

    write_report(run, outcome)
    return outcome


def write_report(run: Run, outcome: dict):
    shots = sorted(p.name for p in run.dir.glob("*.png"))
    report = [f"# Run report\n\n**Task:** {run.task}\n\n**Status:** {outcome['status']}  \n**Steps used:** {run.step}\n",
              f"## Summary\n{outcome['summary']}\n", "## Evidence"]
    report += [f"- {e}" for e in outcome.get("evidence", [])] or ["- (none)"]
    if run.plan:
        report += ["", f"## Final plan\nGoal: {run.plan.get('goal', '')}"] + [f"1. {s}" for s in run.plan.get("steps", [])]
    report += ["", "## Screenshots"] + [f"- {s}" for s in shots] + ["", "Full step-by-step log: `log.jsonl`"]
    (run.dir / "report.md").write_text("\n".join(report), encoding="utf-8")
    run.log(event="finish", **outcome)

    print("\n" + "=" * 70)
    print(f"STATUS: {outcome['status'].upper()}\n\n{outcome['summary']}\n")
    for e in outcome.get("evidence", []):
        print(f"  * {e}")
    print(f"\nReport: {run.dir / 'report.md'}")
