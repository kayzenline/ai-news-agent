import importlib
import json
from fastapi.testclient import TestClient


def test_pages_and_run(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LLM_PROVIDER", "fake")
    from app import service, main
    from app.tools import news_tools
    monkeypatch.setattr(news_tools, "web_search", lambda query, **k: "- [hn] A | https://ex.com/a | ")
    with TestClient(main.app) as c:
        assert c.get("/").status_code == 200
        r = c.post("/users", data={"name": "Edison", "email": "e@x.com"})
        assert r.status_code == 200 and "订阅偏好" in r.text
        c.post("/users/1/prefs", data={"name": "Edison", "topics": "大模型,Agent", "keywords": "LLM", "send_time": "02:00"})
        b = service.run_for_user(1)
        assert b.status == "ok"
        page = c.get(f"/briefs/{b.id}")
        assert "ex.com/a" in page.text and "write_file" in page.text


def test_nickname_xss_is_sanitized(tmp_path, monkeypatch):
    """review fix #2: nickname reaches the brief heading; raw HTML must not survive rendering."""
    from app import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LLM_PROVIDER", "fake")
    from app import service, main
    from app.tools import news_tools
    monkeypatch.setattr(news_tools, "web_search",
                        lambda query, **k: "- [hn] A | https://ex.com/a | ")
    evil = "<img src=x onerror=alert(1)>"
    with TestClient(main.app) as c:
        c.post("/users", data={"name": evil, "email": "e@x.com"})
        uid = max(u.id for u in service.SessionLocal().query(service.User).all())
        b = service.run_for_user(uid)
        page = c.get(f"/briefs/{b.id}")
        assert page.status_code == 200
        card = page.text.split('<div class="card">', 1)[1].split("</div>", 1)[0]
        assert "<img" not in card and "onerror" not in card
        assert "ex.com/a" in card


def test_render_markdown_allowlist():
    from app.main import render_markdown
    out = render_markdown('# hi <script>alert(1)</script>\n\n[x](javascript:alert(1)) [ok](https://ex.com)')
    assert "<script" not in out and "javascript:" not in out and 'href="https://ex.com"' in out


def _setup(tmp_path, monkeypatch):
    from app import config, service, db
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    db.init_db()
    with db.SessionLocal() as s:
        u = db.User(name="Edison", email="e@x.com", email_enabled=1)
        s.add(u); s.commit(); uid = u.id
    sent = []
    monkeypatch.setattr(service, "send_email", lambda *a: sent.append(a))
    from app.tools import news_tools
    monkeypatch.setattr(news_tools, "web_search",
                        lambda query, **k: "- [hn] A | https://ex.com/a | \n- [hn] B | https://ex.com/b | ")
    return service, uid, sent


def _writer(text):
    import json, re
    from app.llm.base import LLMResponse, ToolCall
    def model(msgs):
        n = sum(1 for m in msgs if m["role"] == "tool")
        if n == 0:
            return LLMResponse("", [ToolCall("s", "web_search", json.dumps({"query": "ai"}))], "tool_use")
        if n == 2:
            return LLMResponse("done", [], "end")
        out = re.search(r"write today's brief to (\S+)", msgs[0]["content"]).group(1)
        return LLMResponse("", [ToolCall("w", "write_file", json.dumps({"path": out, "content": text}))], "tool_use")
    return model


def test_stale_same_day_file_does_not_mark_run_ok(tmp_path, monkeypatch):
    """review fix #3: second run that writes nothing must fail and not re-send email."""
    from app.llm.providers import FakeProvider
    from app.llm.base import LLMResponse
    service, uid, sent = _setup(tmp_path, monkeypatch)
    b1 = service.run_for_user(uid, llm=FakeProvider(_writer("# First\n- **A** https://ex.com/a")), today="2026-10-11")
    b2 = service.run_for_user(uid, llm=FakeProvider([LLMResponse("nothing", [], "end")]), today="2026-10-11")
    assert b1.status == "ok" and b2.status == "failed"
    assert len(sent) == 1


def test_same_day_runs_keep_their_own_body(tmp_path, monkeypatch):
    """review fix #4 + design review P1-2: history is append-only, A -> B -> A still finds A."""
    from pathlib import Path
    from app.llm.providers import FakeProvider
    from app.tools import fs_tools
    service, uid, _ = _setup(tmp_path, monkeypatch)
    b1 = service.run_for_user(uid, llm=FakeProvider(_writer("- Story A https://ex.com/a")), today="2026-10-11")
    b2 = service.run_for_user(uid, llm=FakeProvider(_writer("- Story B https://ex.com/b")), today="2026-10-11")
    assert b1.path != b2.path
    assert Path(b1.path).read_text() == "- Story A https://ex.com/a"
    assert Path(b2.path).read_text() == "- Story B https://ex.com/b"
    sb = service.workspace(uid)
    assert "ex.com/a" in fs_tools.search_content(sb, "https://ex.com/a", "briefs")
    assert "ex.com/b" in fs_tools.search_content(sb, "https://ex.com/b", "briefs")


def test_model_cannot_rewrite_history_or_profile(tmp_path, monkeypatch):
    """design review P1-2: the agent may only write its own staging dir."""
    import json
    from pathlib import Path
    from app.llm.base import LLMResponse, ToolCall
    from app.llm.providers import FakeProvider
    service, uid, _ = _setup(tmp_path, monkeypatch)
    b1 = service.run_for_user(uid, llm=FakeProvider(_writer("Story A")), today="2026-10-11")
    hist = next((tmp_path / "workspaces" / f"user_{uid}" / "briefs").rglob("*.md"))
    rel_old = str(Path(b1.path).relative_to(tmp_path / "workspaces" / f"user_{uid}"))
    seen = []
    def evil(msgs):
        if msgs[-1]["role"] == "tool":
            seen.append(msgs[-1]["content"])
        if len(seen) == 4:
            return LLMResponse("done", [], "end")
        targets = ["profile.json", str(hist.relative_to(hist.parents[2])), rel_old]
        if len(seen) < 3:
            return LLMResponse("", [ToolCall(f"e{len(seen)}", "write_file",
                               json.dumps({"path": targets[len(seen)], "content": "pwned"}))], "tool_use")
        return LLMResponse("", [ToolCall("m", "bash", json.dumps({"command": "mkdir -p briefs/x"}))], "tool_use")
    service.run_for_user(uid, llm=FakeProvider(evil), today="2026-10-12")
    assert all(o.startswith("ERROR") and "read-only" in o for o in seen)
    assert Path(b1.path).read_text() == "Story A" and hist.read_text() == "Story A"
    assert "pwned" not in (hist.parents[2] / "profile.json").read_text()


def test_invalid_send_time_rejected_and_startup_survives(tmp_path, monkeypatch):
    """review fix #5: 25:00 must be rejected before commit; a bad stored value must not crash startup."""
    from app import config, db, main, scheduler
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    with TestClient(main.app) as c:
        c.post("/users", data={"name": "A", "email": "a@x.com"})
        r = c.post("/users/1/prefs", data={"name": "A", "send_time": "25:00"})
        assert r.status_code == 422
        with db.SessionLocal() as s:
            assert s.get(db.User, 1).send_time != "25:00"
            s.get(db.User, 1).send_time = "99:99"  # 模拟历史坏数据
            s.commit()
    scheduler.start()  # 不应抛异常


def test_fabricated_link_is_fed_back_then_rejected(tmp_path, monkeypatch):
    """design review P1-1: links must come from this run's tool evidence; errors go back to the model."""
    from app.llm.providers import FakeProvider
    service, uid, sent = _setup(tmp_path, monkeypatch)
    b = service.run_for_user(uid, llm=FakeProvider(_always_writer("- **X** https://made-up.example/x")), today="2026-10-11")
    assert b.status == "failed" and b.stopped_reason == "validation_failed"
    trace = json.loads(b.trace_json)
    assert sum(1 for t in trace if t["type"] == "validation") == 3  # 1 次 + 2 次修订
    assert sent == []


def _always_writer(text):
    import re
    from app.llm.base import LLMResponse, ToolCall
    def model(msgs):
        if msgs[-1]["role"] == "tool":
            return LLMResponse("done", [], "end")
        out = re.search(r"write today's brief to (\S+)", msgs[0]["content"]).group(1)
        return LLMResponse("", [ToolCall("w", "write_file", json.dumps({"path": out, "content": text}))], "tool_use")
    return model


def test_zero_items_is_no_news_and_not_emailed(tmp_path, monkeypatch):
    from app.llm.providers import FakeProvider
    service, uid, sent = _setup(tmp_path, monkeypatch)
    b = service.run_for_user(uid, llm=FakeProvider(_writer("# Brief\n今天没有新的相关内容。")), today="2026-10-11")
    assert b.status == "no_news" and sent == []


def test_validate_brief_rules():
    from app.agent import validate_brief
    ev = {f"https://ex.com/{i}" for i in range(12)}
    assert validate_brief("\n".join(f"- s https://ex.com/{i}" for i in range(10)), ev) == []
    assert "too many items" in validate_brief("\n".join(f"- s https://ex.com/{i}" for i in range(11)), ev)[0]
    assert "not found" in validate_brief("- s https://evil.com/x", ev)[0]
