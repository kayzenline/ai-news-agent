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
    m = re.search(r"draft to (\S+)", messages[0]["content"])
    out = m.group(1) if m else "draft.json"
    if "read_file" not in done:
        return _call("read_file", path="profile.json")
    if "list_dir" not in done:
        return _call("list_dir", path="briefs")
    profile = json.loads(next(m["content"] for m in tool_msgs if m["content"].strip().startswith("{")))
    topics = profile.get("topics") or ["AI"]
    if "web_search" not in done:
        q = " ".join((profile.get("keywords") or topics)[:3])
        return _call("web_search", query=q, sources=["hackernews"], days=2)
    results = [l for l in tool_msgs[-1]["content"].splitlines() if re.match(r"^- (ev_\d+ )?\[", l)] if done[-1] == "web_search" else None
    if results is not None:
        if results:
            url = results[0].split(" | ")[1]
            return _call("search_content", keyword=url, dir="briefs")
    if "write_file" not in done:
        # 从本次运行自己的消息历史取搜索结果（无模块级共享状态，多用户并发互不串扰）
        ws = tool_msgs[done.index("web_search")]["content"] if "web_search" in done else ""
        items = [l for l in ws.splitlines() if re.match(r"^- ev_\d+ \[", l)]
        if tool_msgs[-1]["content"] != "no matches" and items:
            items = items[1:]  # 第一条在历史简报里出现过 -> 去重
        topic = topics[0]
        draft = {"schema_version": 1, "items": [], "note": "演示模型：没有新的候选或全部已报道。"}
        for l in items[:8]:
            eid = l.split()[1]
            title = l.split("] ", 1)[1].split(" | ")[0].strip()
            draft["items"].append({"title": title, "topic": topic,
                                   "summary": f"（演示模型，未精读原文）仅根据标题收录：{title}",
                                   "why_it_matters": f"与你订阅的「{topic}」相关。",
                                   "evidence_ids": [eid], "event_id": None, "change_type": "new", "delta": None})
        return _call("write_file", path=out, content=json.dumps(draft, ensure_ascii=False, indent=2))
    last = messages[-1]
    if last["role"] == "user" and "failed validation" in last["content"]:
        # 演示模型不会修订：退化成 0 条（no_news），避免发布未通过校验的条目
        return _call("write_file", path=out, content=json.dumps(
            {"schema_version": 1, "items": [], "note": "演示模型：草稿未通过校验，本次不发布条目。"}, ensure_ascii=False))
    return LLMResponse(f"已生成 {out}", [], "end")
