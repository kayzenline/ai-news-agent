"""具体 adapter：OpenAI 兼容（OpenAI/Grok/DeepSeek/Kimi）、Anthropic Claude、离线 Fake。"""
import json

from .. import config
from .base import LLMProvider, LLMResponse, ToolCall

_OPENAI_STOP = {"tool_calls": "tool_use", "stop": "end", "length": "length"}
_CLAUDE_STOP = {"tool_use": "tool_use", "end_turn": "end", "stop_sequence": "end", "max_tokens": "length"}


class OpenAICompatProvider(LLMProvider):
    def __init__(self, api_key: str, base_url: str, model: str, name: str = "openai"):
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=60, max_retries=2)
        self.model, self.name = model, name

    @staticmethod
    def to_api_messages(messages):
        out = []
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                out.append({"role": "assistant", "content": m.get("content") or None,
                            "tool_calls": [{"id": c["id"], "type": "function",
                                            "function": {"name": c["name"], "arguments": c["arguments"]}} for c in m["tool_calls"]]})
            elif m["role"] == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
            else:
                out.append({"role": m["role"], "content": m["content"]})
        return out

    def chat(self, messages, tools):
        r = self.client.chat.completions.create(model=self.model, messages=self.to_api_messages(messages),
                                                tools=tools or None, temperature=0.3, max_tokens=4096)
        ch = r.choices[0]
        calls = [ToolCall(c.id, c.function.name, c.function.arguments or "{}") for c in (ch.message.tool_calls or [])]
        stop = "tool_use" if calls else _OPENAI_STOP.get(ch.finish_reason, "end")
        u = r.usage
        return LLMResponse(ch.message.content or "", calls, stop,
                           {"input": getattr(u, "prompt_tokens", 0), "output": getattr(u, "completion_tokens", 0)})


class ClaudeProvider(LLMProvider):
    name = "claude"

    def __init__(self, api_key: str, model: str):
        import anthropic
        self.client = anthropic.Anthropic(api_key=api_key, timeout=60, max_retries=2)
        self.model = model

    @staticmethod
    def to_api_tools(tools):
        return [{"name": t["function"]["name"], "description": t["function"]["description"],
                 "input_schema": t["function"]["parameters"]} for t in tools]

    @staticmethod
    def to_api_messages(messages):
        """system 单独抽出；assistant tool_calls -> tool_use 块；连续的 tool 结果合并进一条 user 的 tool_result 块。"""
        system, out = "", []
        for m in messages:
            if m["role"] == "system":
                system += m["content"]
            elif m["role"] == "assistant":
                blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                for c in m.get("tool_calls") or []:
                    try:
                        inp = json.loads(c["arguments"] or "{}")
                    except json.JSONDecodeError:
                        inp = {}
                    blocks.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": inp})
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(empty)"}]})
            elif m["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"],
                         "is_error": m["content"].startswith("ERROR")}
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                        and out[-1]["content"] and out[-1]["content"][0].get("type") == "tool_result":
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
            else:
                out.append({"role": "user", "content": m["content"]})
        return system, out

    def chat(self, messages, tools):
        system, msgs = self.to_api_messages(messages)
        r = self.client.messages.create(model=self.model, system=system, messages=msgs,
                                        tools=self.to_api_tools(tools), max_tokens=4096, temperature=0.3)
        text = "".join(b.text for b in r.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, json.dumps(b.input, ensure_ascii=False)) for b in r.content if b.type == "tool_use"]
        return LLMResponse(text, calls, _CLAUDE_STOP.get(r.stop_reason, "end"),
                           {"input": r.usage.input_tokens, "output": r.usage.output_tokens})


class FakeProvider(LLMProvider):
    """离线脚本化模型：按顺序返回预设响应；也可传 callable(messages)->LLMResponse 实现“看情况决策”。"""
    name = "fake"

    def __init__(self, script):
        self.script, self.calls = script, 0

    def chat(self, messages, tools):
        self.calls += 1
        if callable(self.script):
            return self.script(messages)
        if not self.script:
            return LLMResponse("done", [], "end")
        return self.script.pop(0)


def get_provider() -> LLMProvider:
    p = config.LLM_PROVIDER
    if p == "grok":
        return OpenAICompatProvider(config.XAI_API_KEY, config.XAI_BASE_URL, config.LLM_MODEL or "grok-4", "grok")
    if p == "claude":
        return ClaudeProvider(config.ANTHROPIC_API_KEY, config.LLM_MODEL or "claude-sonnet-4-5")
    if p == "openai":
        return OpenAICompatProvider(config.LLM_API_KEY, config.LLM_BASE_URL, config.LLM_MODEL or "gpt-4o-mini", "openai")
    if p == "fake":
        from .demo import demo_script
        return FakeProvider(demo_script)
    raise ValueError(f"unknown LLM_PROVIDER={p}")
