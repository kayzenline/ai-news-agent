from app.evidence import EvidenceStore, canonical_url
from app.tools.registry import ToolRegistry
from app.tools.sandbox import Sandbox


def test_search_results_get_sequential_ids(tmp_path):
    store = EvidenceStore()
    reg = ToolRegistry(Sandbox(tmp_path), evidence=store,
                       extra={"web_search": lambda query, **k: "- [hn] A | https://ex.com/a | 2026-10-11\n- [rss] B | https://ex.com/b | unknown | sum\nerrors: arxiv: down",
                              "fetch_url": lambda url, **k: "page text"})
    ok, out = reg.call("web_search", '{"query":"x"}')
    assert ok and out.splitlines()[0].startswith("- ev_001 [hn] A") and out.splitlines()[1].startswith("- ev_002 [rss]")
    assert "errors: arxiv" in out
    assert store.get("ev_001").url == "https://ex.com/a" and store.get("ev_001").published_at == "2026-10-11"
    assert store.get("ev_002").excerpt == "sum"
    ok, out = reg.call("fetch_url", '{"url":"https://ex.com/a"}')
    assert out.startswith("evidence_id: ev_003") and store.get("ev_003").tool == "fetch_url"


def test_failed_tool_call_assigns_no_evidence(tmp_path):
    store = EvidenceStore()
    def boom(**k): raise RuntimeError("x")
    reg = ToolRegistry(Sandbox(tmp_path), evidence=store, extra={"fetch_url": boom})
    ok, _ = reg.call("fetch_url", '{"url":"https://ex.com"}')
    assert not ok and store.items == {}


def test_canonical_url():
    assert canonical_url("HTTPS://Ex.COM:443/Path?utm_source=x&id=7#frag") == "https://ex.com/Path?id=7"
    assert canonical_url("http://ex.com:8080/a") == "http://ex.com:8080/a"
