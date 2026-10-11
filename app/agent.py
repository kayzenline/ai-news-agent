"""手写 Agent 循环：模型决定调用哪个工具、调用几次、何时停止。"""
import json
import time
from dataclasses import dataclass, field
from datetime import date

from . import config
from .llm.base import LLMProvider
from .tools.registry import ToolRegistry

SYSTEM_PROMPT = """You are a daily AI-news briefing agent working inside a user's private workspace.
Workspace layout: profile.json (user name, topics, keywords, language), briefs/ (previous briefs, YYYY-MM-DD.md).
Goal: write today's brief to {output} (this run's output file; do not write it into briefs/, the system archives it there after success), then reply with a one-line summary and STOP calling tools.
Guidelines (you decide the order and number of tool calls):
- Read profile.json first to learn the user's topics/keywords/language.
- Use web_search with focused queries derived from the topics; fetch_url only for the few stories you need details on.
- Avoid repeats: check previous briefs (search_content for a distinctive title word or URL in briefs/) and drop already-covered stories.
- Brief format: markdown, title, 5-10 items grouped by topic, each item: bold title, 1-2 sentence summary, source link. Write in the user's language.
- If a tool returns ERROR, adapt (fix arguments, try another source) instead of repeating the same call.
- Be economical: typically 4-10 tool calls total. Today is {today}."""


@dataclass
class AgentResult:
    final_text: str
    trace: list[dict] = field(default_factory=list)
    steps: int = 0
    stopped_reason: str = ""
    usage: dict = field(default_factory=lambda: {"input": 0, "output": 0})


class Agent:
    def __init__(self, llm: LLMProvider, tools: ToolRegistry, max_steps: int | None = None):
        self.llm, self.tools = llm, tools
        self.max_steps = max_steps or config.MAX_STEPS

    def run(self, task: str, today: str | None = None, output: str | None = None) -> AgentResult:
        today = today or date.today().isoformat()
        output = output or f"briefs/{today}.md"
        prompt = SYSTEM_PROMPT.replace("{today}", today).replace("{output}", output)
        messages = [{"role": "system", "content": prompt},
                    {"role": "user", "content": task}]
        res = AgentResult(final_text="")
        seen_calls: dict[str, int] = {}
        for step in range(1, self.max_steps + 1):
            res.steps = step
            t0 = time.time()
            try:
                resp = self.llm.chat(messages, self.tools.schemas())
            except Exception as e:
                res.trace.append({"step": step, "type": "llm_error", "error": f"{type(e).__name__}: {e}"})
                res.stopped_reason = "llm_error"
                return res
            res.usage["input"] += resp.usage.get("input", 0)
            res.usage["output"] += resp.usage.get("output", 0)
            res.trace.append({"step": step, "type": "llm", "text": resp.text, "stop_reason": resp.stop_reason,
                              "tool_calls": [c.__dict__ for c in resp.tool_calls], "ms": int((time.time() - t0) * 1000)})
            if not resp.tool_calls:
                # 模型不再请求工具 => 它认为任务完成
                res.final_text = resp.text
                res.stopped_reason = "length" if resp.stop_reason == "length" else "model_finished"
                return res
            messages.append({"role": "assistant", "content": resp.text,
                             "tool_calls": [c.__dict__ for c in resp.tool_calls]})
            for c in resp.tool_calls:
                key = f"{c.name}:{c.arguments}"
                seen_calls[key] = seen_calls.get(key, 0) + 1
                t1 = time.time()
                if seen_calls[key] > 2:
                    ok, out = False, "ERROR: identical call repeated 3 times; change approach or finish."
                else:
                    ok, out = self.tools.call(c.name, c.arguments)
                res.trace.append({"step": step, "type": "tool", "name": c.name, "arguments": c.arguments,
                                  "ok": ok, "output": out[:2000], "ms": int((time.time() - t1) * 1000)})
                messages.append({"role": "tool", "tool_call_id": c.id, "content": out})
            if step == self.max_steps - 1:
                messages.append({"role": "user", "content": "Step budget almost exhausted: write the brief now with what you have and finish."})
        res.stopped_reason = "max_steps"
        res.final_text = "stopped: max steps reached"
        return res


def dump_trace(trace: list[dict]) -> str:
    return json.dumps(trace, ensure_ascii=False, indent=2)
