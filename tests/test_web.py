import importlib
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
    return service, uid, sent


def _writer(text):
    import json, re
    from app.llm.base import LLMResponse, ToolCall
    def model(msgs):
        if msgs[-1]["role"] == "tool":
            return LLMResponse("done", [], "end")
        out = re.search(r"write today's brief to (\S+)", msgs[0]["content"]).group(1)
        return LLMResponse("", [ToolCall("w", "write_file", json.dumps({"path": out, "content": text}))], "tool_use")
    return model


def test_stale_same_day_file_does_not_mark_run_ok(tmp_path, monkeypatch):
    """review fix #3: second run that writes nothing must fail and not re-send email."""
    from app.llm.providers import FakeProvider
    from app.llm.base import LLMResponse
    service, uid, sent = _setup(tmp_path, monkeypatch)
    b1 = service.run_for_user(uid, llm=FakeProvider(_writer("# First")), today="2026-10-11")
    b2 = service.run_for_user(uid, llm=FakeProvider([LLMResponse("nothing", [], "end")]), today="2026-10-11")
    assert b1.status == "ok" and b2.status == "failed"
    assert len(sent) == 1


def test_same_day_runs_keep_their_own_body(tmp_path, monkeypatch):
    """review fix #4: history entries must not share a mutable file."""
    from pathlib import Path
    from app.llm.providers import FakeProvider
    service, uid, _ = _setup(tmp_path, monkeypatch)
    b1 = service.run_for_user(uid, llm=FakeProvider(_writer("First successful brief")), today="2026-10-11")
    b2 = service.run_for_user(uid, llm=FakeProvider(_writer("Replacement brief")), today="2026-10-11")
    assert b1.path != b2.path
    assert Path(b1.path).read_text() == "First successful brief"
    assert Path(b2.path).read_text() == "Replacement brief"
    dedup = Path(b1.path).parents[2] / "briefs" / "2026-10-11.md"
    assert dedup.read_text() == "Replacement brief"
