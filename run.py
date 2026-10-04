"""Give the AI operator a task in plain English.

  python run.py "Find the latest invoice from Acme, enter it into the finance portal and tell me when done"

Options:
  --headless        hide the browser window
  --auto-approve    don't ask before submitting forms (company policy approvals still apply)
  --model NAME      claude-opus-5-5 or gemini-3.8-flash (default: whichever API key is set)
  --effort LEVEL    low | medium | high | xhigh | max  (Claude only, default: high)
  --max-steps N     hard limit on loop iterations (default: 40)
"""
import argparse
import os
import sys

from agent.loop import run_task

sys.stdout.reconfigure(encoding="utf-8")

parser = argparse.ArgumentParser(description="Autonomous AI operator")
parser.add_argument("task", nargs="?", help="What you want done")
default_model = "claude-opus-5-5" if os.environ.get("ANTHROPIC_API_KEY") else "gemini-3.8-flash"
parser.add_argument("--model", default=default_model, help="claude-* or gemini-* (default depends on which API key is set)")
parser.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"], help="Claude only")
parser.add_argument("--headless", action="store_true")
parser.add_argument("--auto-approve", action="store_true")
parser.add_argument("--max-steps", type=int, default=40)
args = parser.parse_args()

task = args.task or input("What should I do? ")
outcome = run_task(task, model=args.model, effort=args.effort, headless=args.headless,
                   approve_writes=not args.auto_approve, max_steps=args.max_steps)
sys.exit(0 if outcome["status"] == "done" else 1)
