"""离线演示用的“假模型”：根据对话中最近的工具结果做决策（模拟真实模型行为），无需网络与密钥。"""
import json
import re

from .base import LLMResponse, ToolCall

_n = 0


def _call(name, **args):
    global _n
    _n += 1
    return LLMResponse("", [ToolCall(f"call_{_n}", name, json.dumps(args, ensure_ascii=False))], "tool_use")


def demo_script(messages):
    tool_msgs = [m for m in messages if m["role"] == "tool"]
    done = [c["name"] for m in messages if m["role"] == "assistant" for c in m.get("tool_calls") or []]
    today = re.search(r"Today is (\d{4}-\d{2}-\d{2})", messages[0]["content"]).group(1)
    if "read_file" not in done:
        return _call("read_file", path="profile.json")
    if "list_dir" not in done:
        return _call("list_dir", path="briefs")
    profile = json.loads(next(m["content"] for m in tool_msgs if m["content"].strip().startswith("{")))
    topics = profile.get("topics") or ["AI"]
    if "web_search" not in done:
        q = " ".join((profile.get("keywords") or topics)[:3])
        return _call("web_search", query=q, sources=["hackernews"], days=2)
    results = [l for l in tool_msgs[-1]["content"].splitlines() if l.startswith("- [")] if done[-1] == "web_search" else None
    if results is not None:
        global _cache
        _cache = results
        if results:
            url = results[0].split(" | ")[1]
            return _call("search_content", keyword=url, dir="briefs")
    if "write_file" not in done:
        items = _cache if "_cache" in globals() else []
        if tool_msgs[-1]["content"] != "no matches" and items:
            items = items[1:]  # 第一条在历史简报里出现过 -> 去重
        body = "\n".join(f"- **{l.split(' | ')[0][4:].split('] ',1)[-1]}** — {l.split(' | ')[1]}" for l in items[:8]) or "- 今日未检索到新内容（来源不可用或已全部去重）"
        md = f"# {profile.get('name','')} 的 AI 新闻简报 · {today}\n\n## {' / '.join(topics)}\n\n{body}\n"
        return _call("write_file", path=f"briefs/{today}.md", content=md)
    return LLMResponse(f"已生成 briefs/{today}.md", [], "end")
