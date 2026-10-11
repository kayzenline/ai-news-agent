import json
from app.agent import Agent
from app.llm.base import LLMResponse, ToolCall
from app.llm.providers import FakeProvider, ClaudeProvider, OpenAICompatProvider
from app.tools.registry import ToolRegistry
from app.tools.sandbox import Sandbox


def tc(i, name, **a):
    return LLMResponse("", [ToolCall(f"c{i}", name, json.dumps(a))], "tool_use", {"input": 10, "output": 5})


def make(tmp_path, script, max_steps=10):
    sb = Sandbox(tmp_path)
    (sb.root / "briefs").mkdir()
    (sb.root / "briefs" / "2026-10-10.md").write_text("- old story https://ex.com/old\n")
    (sb.root / "profile.json").write_text('{"name":"Edison","topics":["LLM"]}')
    fake_search = lambda query, **k: "- [hn] Old | https://ex.com/old\n- [hn] New | https://ex.com/new"
    reg = ToolRegistry(sb, extra={"web_search": fake_search})
    return sb, Agent(FakeProvider(script), reg, max_steps=max_steps)


def test_scripted_run_writes_brief_and_traces(tmp_path):
    script = [tc(1, "read_file", path="profile.json"), tc(2, "web_search", query="LLM"),
              tc(3, "search_content", keyword="https://ex.com/old", dir="briefs"),
              tc(4, "write_file", path="briefs/2026-10-11.md", content="# brief\n- New https://ex.com/new"),
              LLMResponse("done", [], "end")]
    sb, agent = make(tmp_path, script)
    r = agent.run("go", today="2026-10-11")
    assert r.stopped_reason == "model_finished" and r.steps == 5
    assert (sb.root / "briefs/2026-10-11.md").read_text().startswith("# brief")
    names = [t["name"] for t in r.trace if t["type"] == "tool"]
    assert names == ["read_file", "web_search", "search_content", "write_file"]
    assert r.usage == {"input": 40, "output": 20}


def test_tool_error_is_fed_back(tmp_path):
    seen = []
    def model(msgs):
        seen.append(msgs[-1])
        if len(seen) == 1:
            return tc(1, "read_file", path="../../etc/passwd")
        return LLMResponse("ok", [], "end")
    _, agent = make(tmp_path, model)
    r = agent.run("go")
    assert seen[1]["role"] == "tool" and seen[1]["content"].startswith("ERROR")
    assert r.stopped_reason == "model_finished"


def test_max_steps_guard(tmp_path):
    _, agent = make(tmp_path, lambda m: tc(len(m), "list_dir", path="."), max_steps=4)
    r = agent.run("go")
    assert r.stopped_reason == "max_steps" and r.steps == 4


def test_repeated_identical_call_blocked(tmp_path):
    _, agent = make(tmp_path, lambda m: tc(len(m), "list_dir", path="."), max_steps=4)
    r = agent.run("go")
    outs = [t["output"] for t in r.trace if t["type"] == "tool"]
    assert "repeated 3 times" in outs[2]


def test_llm_exception_stops_cleanly(tmp_path):
    def boom(m): raise RuntimeError("503")
    _, agent = make(tmp_path, boom)
    r = agent.run("go")
    assert r.stopped_reason == "llm_error"


def test_demo_fake_model_end_to_end(tmp_path):
    from app.llm.demo import demo_script
    _, agent = make(tmp_path, demo_script)
    r = agent.run("go", today="2026-10-11")
    assert r.stopped_reason == "model_finished"
    brief = (tmp_path / "briefs/2026-10-11.md").read_text()
    assert "https://ex.com/new" in brief and "https://ex.com/old" not in brief  # 去重生效


HIST = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "name": "list_dir", "arguments": '{"path":"."}'},
                                                            {"id": "b", "name": "read_file", "arguments": '{"path":"x"}'}]},
        {"role": "tool", "tool_call_id": "a", "content": "[F] x"},
        {"role": "tool", "tool_call_id": "b", "content": "ERROR: nope"}]


def test_claude_message_conversion():
    system, msgs = ClaudeProvider.to_api_messages(HIST)
    assert system == "sys" and [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert [b["type"] for b in msgs[1]["content"]] == ["tool_use", "tool_use"]
    assert len(msgs[2]["content"]) == 2 and msgs[2]["content"][1]["is_error"]


def test_openai_message_conversion():
    msgs = OpenAICompatProvider.to_api_messages(HIST)
    assert msgs[2]["tool_calls"][0]["function"]["name"] == "list_dir"
    assert msgs[3] == {"role": "tool", "tool_call_id": "a", "content": "[F] x"}
