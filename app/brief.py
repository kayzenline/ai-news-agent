"""简报内容契约：校验模型写的 draft.json，并由 Service 渲染 Markdown（模型不直接写正文）。

draft.json 示例（字段含义见 docs/DESIGN.md §4）：
{"schema_version": 1, "note": "可选，0 条时说明原因",
 "items": [{"title": "...", "topic": "...", "summary": "...", "why_it_matters": "...",
            "evidence_ids": ["ev_001"], "event_id": null, "change_type": "new", "delta": null}]}
"""

import json
import re

from .evidence import EvidenceStore, canonical_url

MAX_ITEMS = 10
MAX_EVIDENCE_PER_ITEM = 5
TEXT_FIELDS = {"title": 200, "topic": 60, "summary": 600, "why_it_matters": 300}
OPTIONAL_FIELDS = {"event_id", "change_type", "delta"}
CHANGE_TYPES = {"new", "update", "uncertain"}  # duplicate 不发布，应直接删掉
TOP_FIELDS = {"schema_version", "items", "note"}
URL_RE = re.compile(r"https?://\S+")
HISTORY_URL_RE = re.compile(r"https?://[^\s)\]>\"'<|]+")


def history_urls(md_texts) -> set[str]:
    """已发布历史中出现过的规范 URL（由 Service 从 briefs/ 读取，不信任模型）。"""
    out = set()
    for t in md_texts:
        out.update(canonical_url(u.rstrip(".,;")) for u in HISTORY_URL_RE.findall(t))
    return out


def validate_draft(raw: str, store: EvidenceStore, published: set[str] | None = None) -> tuple[dict | None, list[str]]:
    """返回 (draft, errors)。errors 为空才可发布；错误文本会原样回传给模型修订。"""
    published = published or set()
    try:
        d = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, [f"draft.json is not valid JSON: {e}"]
    if not isinstance(d, dict):
        return None, ["draft.json must be a JSON object"]
    errors = []
    extra = set(d) - TOP_FIELDS
    if extra:
        errors.append(f"unknown top-level fields: {sorted(extra)}")
    if d.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if "note" in d and not isinstance(d["note"], (str, type(None))):
        errors.append("note must be a string")
    items = d.get("items")
    if not isinstance(items, list):
        return None, errors + ["items must be a list (it may be empty when nothing new is worth sending)"]
    if len(items) > MAX_ITEMS:
        errors.append(f"too many items: {len(items)} > {MAX_ITEMS}; keep only the most relevant")
    seen_urls: dict[str, int] = {}
    for i, it in enumerate(items, 1):
        p = f"items[{i}]"
        if not isinstance(it, dict):
            errors.append(f"{p} must be an object")
            continue
        extra = set(it) - set(TEXT_FIELDS) - OPTIONAL_FIELDS - {"evidence_ids"}
        if extra:
            errors.append(f"{p}: unknown fields {sorted(extra)}")
        for f, limit in TEXT_FIELDS.items():
            v = it.get(f)
            if not isinstance(v, str) or not v.strip():
                errors.append(f"{p}.{f} must be a non-empty string")
            elif len(v) > limit:
                errors.append(f"{p}.{f} too long ({len(v)} > {limit})")
            elif URL_RE.search(v):
                errors.append(f"{p}.{f} must not contain URLs; cite sources only via evidence_ids")
        ct = it.get("change_type") or "new"
        if ct == "duplicate":
            errors.append(f"{p}: duplicate items must be removed, not published")
        elif ct not in CHANGE_TYPES:
            errors.append(f"{p}.change_type must be one of {sorted(CHANGE_TYPES)}")
        if ct == "update" and not (isinstance(it.get("delta"), str) and it["delta"].strip()):
            errors.append(f"{p}: change_type=update needs a non-empty delta describing what is new")
        for f in ("event_id", "delta"):
            if it.get(f) is not None and not isinstance(it.get(f), str):
                errors.append(f"{p}.{f} must be a string or null")
        ids = it.get("evidence_ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(x, str) for x in ids):
            errors.append(f"{p}.evidence_ids must be a non-empty list of evidence ids from this run's tool results")
            continue
        if len(ids) > MAX_EVIDENCE_PER_ITEM:
            errors.append(f"{p}: at most {MAX_EVIDENCE_PER_ITEM} evidence ids")
        unknown = [x for x in ids if store.get(x) is None]
        if unknown:
            errors.append(f"{p}: unknown evidence ids {unknown}; use ids returned by web_search/fetch_url in this run")
            continue
        urls = {canonical_url(store.get(x).url) for x in ids}
        for u in urls:
            if u in seen_urls:
                errors.append(f"{p}: same source as items[{seen_urls[u]}] ({u}); merge them")
            seen_urls.setdefault(u, i)
        if ct != "update" and urls and urls <= published:
            errors.append(
                f"{p}: already published in a previous brief; drop it or mark change_type=update with a delta"
            )
    return (d if not errors else None), errors


L10N = {
    "zh": {
        "title": "{name} 的 AI 新闻简报 · {date}",
        "why": "推荐理由",
        "src": "来源",
        "unknown": "日期未知",
        "update": "更新",
        "empty": "今日没有新的相关内容。",
    },
    "en": {
        "title": "{name}'s AI news brief · {date}",
        "why": "Why it matters",
        "src": "Sources",
        "unknown": "date unknown",
        "update": "Update",
        "empty": "Nothing new and relevant today.",
    },
}


def _md_escape(s: str) -> str:
    s = re.sub(r"([\\`*_\[\]#|])", r"\\\1", s.strip())
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_markdown(
    draft: dict,
    store: EvidenceStore,
    name: str,
    date: str,
    language: str = "zh",
    stopped_reason: str | None = None,
    steps: int | None = None,
) -> str:
    """Service 侧统一渲染：模型文本一律转义，链接只来自证据库。"""
    t = L10N.get(language, L10N["zh"])
    out = [f"# {_md_escape(t['title'].format(name=name, date=date))}", ""]
    if stopped_reason == "max_steps":
        step_str = f"（{steps} 步）" if steps else ""
        if language == "zh":
            warn = f"> ⚠️ **运行提示**：Agent 在达到最大步数上限{step_str}时自动截断，本简报为阶段性草稿。如需更深度的调研，可提高步数上限（在 `.env` 中调大 `AGENT_MAX_STEPS`）重新生成。"
        else:
            warn = f"> ⚠️ **Notice**: Agent stopped upon reaching the step limit{step_str}; this brief is generated from the intermediate draft. To enable deeper exploration, increase `AGENT_MAX_STEPS` in `.env` and rerun."
        out.extend([warn, ""])
    items = draft.get("items") or []
    if not items:
        out.append(t["empty"])
        if draft.get("note"):
            out.append("")
            out.append(_md_escape(draft["note"]))
        return "\n".join(out) + "\n"
    topics: dict[str, list] = {}
    for it in items:
        topics.setdefault(it["topic"].strip(), []).append(it)
    for topic, group in topics.items():
        out += [f"## {_md_escape(topic)}", ""]
        for it in group:
            tag = f"（{t['update']}）" if it.get("change_type") == "update" else ""
            out.append(f"- **{_md_escape(it['title'])}**{tag} — {_md_escape(it['summary'])}")
            if it.get("change_type") == "update":
                out.append(f"  - {t['update']}: {_md_escape(it['delta'])}")
            out.append(f"  - {t['why']}: {_md_escape(it['why_it_matters'])}")
            srcs = []
            for eid in it["evidence_ids"]:
                e = store.get(eid)
                label = _md_escape(e.title or e.source)
                when = e.published_at if e.published_at != "unknown" else t["unknown"]
                srcs.append(f"[{label}]({e.url}) ({_md_escape(e.source)}, {when})")
            out.append(f"  - {t['src']}: " + "; ".join(srcs))
        out.append("")
    return "\n".join(out).rstrip() + "\n"
