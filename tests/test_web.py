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
