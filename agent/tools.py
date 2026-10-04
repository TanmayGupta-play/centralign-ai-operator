"""The agent's hands and eyes.

Every tool is generic (files, browser, human). Nothing here knows about
invoices - the task-specific knowledge lives in company/procedures.md and in
the model's reasoning. That is what lets the same code handle other tasks.

Each tool returns a plain-text observation. Exceptions are caught by the
agent loop and returned to the model as errors, so it can adapt.
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

WORKSPACE = Path(__file__).resolve().parent.parent / "company"

# Tool definitions sent to Claude. Descriptions are written for the model.
TOOL_SCHEMAS = [
    {
        "name": "update_plan",
        "description": "Record your current step-by-step plan. Call it once you understand the goal, and again whenever something unexpected happens and the plan changes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string", "description": "The user's intended outcome in one sentence."},
                "steps": {"type": "array", "items": {"type": "string"}, "description": "Remaining steps, in order."},
            },
            "required": ["goal", "steps"],
        },
    },
    {
        "name": "list_files",
        "description": "List files in a folder of the company workspace. Use '.' for the root.",
        "input_schema": {
            "type": "object",
            "properties": {"folder": {"type": "string"}},
            "required": ["folder"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a text file from the company workspace (path relative to workspace root).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "browser_goto",
        "description": "Open a URL in the browser. Returns the page text and its interactive elements.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "browser_read",
        "description": "Re-read the current browser page: visible text plus form fields, buttons and links with CSS selectors you can use.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_fill",
        "description": "Type a value into an input, or choose an option (by visible label) in a <select>.",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector from browser_read, e.g. #amount"},
                "value": {"type": "string"},
            },
            "required": ["selector", "value"],
        },
    },
    {
        "name": "browser_click",
        "description": "Click an element. Clicking a form submit button changes data and may require human approval.",
        "input_schema": {
            "type": "object",
            "properties": {"selector": {"type": "string"}},
            "required": ["selector"],
        },
    },
    {
        "name": "ask_human",
        "description": "Ask the human a question and wait for the answer. Use when information is missing or ambiguous, or when company policy requires approval. Do not use for things you can find out yourself.",
        "input_schema": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
    {
        "name": "finish",
        "description": "End the task. Only call with status 'done' after you have independently re-checked the system of record and confirmed the outcome.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["done", "failed", "needs_human"]},
                "summary": {"type": "string", "description": "What was done, for the user."},
                "evidence": {"type": "array", "items": {"type": "string"},
                             "description": "Concrete observations proving the outcome (what you saw, where)."},
            },
            "required": ["status", "summary", "evidence"],
        },
    },
]

# JavaScript run inside the page to produce a compact, model-readable view of it.
_DESCRIBE_PAGE_JS = """() => {
  const sel = el => el.id ? '#' + el.id : (el.name ? `${el.tagName.toLowerCase()}[name="${el.name}"]` : null);
  const out = [];
  document.querySelectorAll('input, select, textarea, button, a').forEach(el => {
    const s = sel(el) || (el.tagName === 'A' ? `a[href="${el.getAttribute('href')}"]` : null);
    if (!s) return;
    if (el.tagName === 'SELECT') {
      const opts = [...el.options].map(o => o.text).join(' | ');
      out.push(`select ${s} value="${el.options[el.selectedIndex]?.text || ''}" options: ${opts}`);
    } else if (el.tagName === 'A') {
      out.push(`link ${s} "${el.innerText.trim()}"`);
    } else if (el.tagName === 'BUTTON') {
      out.push(`button ${s} "${el.innerText.trim()}" type=${el.type}`);
    } else {
      out.push(`${el.tagName.toLowerCase()} ${s} value="${el.value}" placeholder="${el.placeholder || ''}"`);
    }
  });
  return out.join('\\n');
}"""


class Toolbox:
    """Holds the live browser and executes tool calls."""

    def __init__(self, run_dir: Path, headless: bool, approve_writes: bool):
        self.run_dir = run_dir
        self.approve_writes = approve_writes
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=headless, slow_mo=400)
        # Every run records a video of the browser as evidence (saved as browser.webm).
        self._context = self._browser.new_context(viewport={"width": 1280, "height": 720},
                                                  record_video_dir=str(run_dir),
                                                  record_video_size={"width": 1280, "height": 720})
        self.page = self._context.new_page()
        self.page.set_default_timeout(5000)
        self.shot_count = 0

    def close(self):
        video = self.page.video
        self._context.close()  # the video file is written when the context closes
        if video:
            Path(video.path()).rename(self.run_dir / "browser.webm")
        self._browser.close()
        self._pw.stop()

    # ---------- files ----------
    def _safe_path(self, rel: str) -> Path:
        p = (WORKSPACE / rel).resolve()
        if WORKSPACE not in p.parents and p != WORKSPACE:
            raise PermissionError(f"'{rel}' is outside the company workspace.")
        return p

    def list_files(self, folder: str) -> str:
        p = self._safe_path(folder)
        return "\n".join(sorted(f"{x.name}{'/' if x.is_dir() else ''}" for x in p.iterdir())) or "(empty)"

    def read_file(self, path: str) -> str:
        return self._safe_path(path).read_text(encoding="utf-8")

    # ---------- browser ----------
    def _screenshot(self) -> str:
        self.shot_count += 1
        path = self.run_dir / f"screen_{self.shot_count:02d}.png"
        self.page.screenshot(path=str(path), full_page=True)
        return path.name

    def browser_read(self) -> str:
        text = self.page.inner_text("body")[:4000]
        elements = self.page.evaluate(_DESCRIBE_PAGE_JS)
        return (f"URL: {self.page.url}\nTITLE: {self.page.title()}\n\n--- PAGE TEXT ---\n{text}"
                f"\n\n--- INTERACTIVE ELEMENTS ---\n{elements}\n\n[screenshot: {self._screenshot()}]")

    def browser_goto(self, url: str) -> str:
        response = self.page.goto(url)
        status = response.status if response else "unknown"
        return f"HTTP {status}\n" + self.browser_read()

    def browser_fill(self, selector: str, value: str) -> str:
        el = self.page.locator(selector)
        if el.evaluate("e => e.tagName") == "SELECT":
            el.select_option(label=value)
        else:
            el.fill(value)
        actual = el.evaluate("e => e.tagName === 'SELECT' ? e.options[e.selectedIndex].text : e.value")
        return f"Set {selector}. Field now contains: \"{actual}\""

    def is_write_action(self, selector: str) -> bool:
        """A click that submits a form changes data in another system."""
        return self.page.locator(selector).evaluate(
            "e => e.type === 'submit' || (e.tagName === 'BUTTON' && !!e.closest('form'))")

    def form_snapshot(self, selector: str) -> str:
        return self.page.locator(selector).evaluate(
            "e => { const f = e.closest('form'); if (!f) return '';"
            " return [...new FormData(f)].map(([k, v]) => `  ${k}: ${v}`).join('\\n'); }")

    def browser_click(self, selector: str) -> str:
        if self.approve_writes and self.is_write_action(selector):
            print(f"\n  [APPROVAL REQUIRED] The agent wants to submit this form on {self.page.url}:")
            print(self.form_snapshot(selector))
            if input("  Approve? (y/n): ").strip().lower() != "y":
                return "BLOCKED: the human declined this submission. Nothing was submitted."
        self.page.click(selector)
        self.page.wait_for_load_state("load")
        return "Clicked. Page after click:\n" + self.browser_read()

    # ---------- human ----------
    @staticmethod
    def ask_human(question: str) -> str:
        print(f"\n  [AGENT ASKS] {question}")
        answer = input("  Your answer: ").strip()
        return answer or "(no answer given)"
