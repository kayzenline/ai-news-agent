"""Provider 无关的统一消息格式。

内部对话历史统一用 OpenAI 风格的 dict：
  {"role":"system"|"user"|"assistant"|"tool", ...}
  assistant 可带 tool_calls=[{"id","name","arguments"(json str)}]
  tool 消息带 tool_call_id
各 adapter 负责与具体 API 互转，Agent 循环只认 LLMResponse。
"""
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON 字符串


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end"  # 归一化后: "tool_use" | "end" | "length"
    usage: dict = field(default_factory=lambda: {"input": 0, "output": 0})


class LLMProvider:
    name = "base"

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMResponse:  # pragma: no cover
        raise NotImplementedError
