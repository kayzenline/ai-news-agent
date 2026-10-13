"""工具注册表：JSON Schema（给模型看）+ 分发执行（错误转成字符串回传给模型）。"""
import json
from typing import Callable

from .. import config
from . import fs_tools, news_tools
from .sandbox import Sandbox


def _fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}

S = {"type": "string"}
TOOL_SCHEMAS = [
    _fn("list_dir", "List files in a directory of the user workspace (relative path).", {"path": S}, ["path"]),
    _fn("read_file", "Read a UTF-8 text file in the user workspace.", {"path": S}, ["path"]),
    _fn("search_content", "Case-insensitive keyword search across files under dir. Use it to check whether a story/url already appeared in previous briefs.",
        {"keyword": S, "dir": S}, ["keyword", "dir"]),
    _fn("write_file", "Create or overwrite a text file in the user workspace.", {"path": S, "content": S}, ["path", "content"]),
    _fn("bash", "Run ONE restricted read-only shell command inside the workspace (allowed: ls cat head tail wc grep find date echo mkdir, each with a small option allowlist; no pipes/redirects/long options). Globs (* ? [...]) in file operands of ls/cat/head/tail/wc/grep are expanded inside the workspace, e.g. `ls briefs/2026-10-12/*.md`. 10s timeout.",
        {"command": S}, ["command"]),
    _fn("web_search", "Search recent AI news. Sources: hackernews, rss (TechCrunch/VentureBeat/MIT TR/HF blog), arxiv, tavily (if configured). Returns one line per result: `- ev_NNN [source] title | url | date`; cite results by their evidence id.",
        {"query": S, "sources": {"type": "array", "items": {"type": "string", "enum": ["hackernews", "rss", "arxiv", "tavily"]}},
         "days": {"type": "integer", "description": "look-back window in days (applies to all sources), default 2; items with unknown publish date are kept and marked date=unknown"}}, ["query"]),
    _fn("fetch_url", "Fetch a public web page and return its plain text (truncated), prefixed with the evidence_id assigned to this page.", {"url": S}, ["url"]),
]


def _without_tavily(schema: dict) -> dict:
    """未配置 TAVILY_API_KEY 时不向模型宣传 tavily。"""
    import copy
    s = copy.deepcopy(schema)
    f = s["function"]
    f["description"] = f["description"].replace(", tavily (if configured)", "")
    f["parameters"]["properties"]["sources"]["items"]["enum"] = ["hackernews", "rss", "arxiv"]
    return s


class ToolRegistry:
    def __init__(self, sandbox: Sandbox, extra: dict[str, Callable] | None = None, evidence=None):
        self.sb = sandbox
        self.evidence = evidence  # EvidenceStore：给 web_search/fetch_url 结果分配本轮 evidence_id
        self.impl: dict[str, Callable[..., str]] = {
            "list_dir": lambda path=".": fs_tools.list_dir(self.sb, path),
            "read_file": lambda path: fs_tools.read_file(self.sb, path),
            "search_content": lambda keyword, dir=".": fs_tools.search_content(self.sb, keyword, dir),
            "write_file": lambda path, content: fs_tools.write_file(self.sb, path, content),
            "bash": lambda command: fs_tools.bash(self.sb, command),
            "web_search": news_tools.web_search,
            "fetch_url": news_tools.fetch_url,
        }
        if extra:
            self.impl.update(extra)

    def schemas(self):
        return [_without_tavily(s) if s["function"]["name"] == "web_search" and not config.TAVILY_API_KEY else s
                for s in TOOL_SCHEMAS if s["function"]["name"] in self.impl]

    def call(self, name: str, raw_args: str) -> tuple[bool, str]:
        """返回 (ok, output)。任何异常都不会抛出循环，而是作为文本反馈给模型让它自我纠正。"""
        if name not in self.impl:
            return False, f"ERROR: unknown tool {name}. Available: {list(self.impl)}"
        try:
            args = json.loads(raw_args or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as e:
            return False, f"ERROR: invalid JSON arguments: {e}"
        try:
            raw_out = self.impl[name](**args)
            out = str(raw_out)
        except TypeError as e:
            return False, f"ERROR: bad arguments for {name}: {e}"
        except Exception as e:
            return False, f"ERROR: {type(e).__name__}: {e}"
        if name == "web_search":
            from .news_tools import SearchResult
            if isinstance(raw_out, SearchResult):
                all_failed = raw_out.all_failed
                requested = raw_out.sources_requested
                succeeded = raw_out.sources_succeeded
                failed = raw_out.sources_failed
                results_count = raw_out.results_count
            else:
                lines = [l.strip() for l in out.splitlines() if l.strip()]
                results_count = sum(1 for l in lines if l.startswith("- ") and " | " in l)
                has_errors = any(l.startswith("errors:") for l in lines) or out.startswith("errors:")
                all_failed = (results_count == 0 and has_errors)
                requested = args.get("sources") or ["hackernews", "rss", "arxiv"]
                succeeded = [s for s in requested] if (results_count > 0 or not has_errors) else []
                failed = {"all": out} if all_failed else {}

            if self.evidence is not None:
                self.evidence.record_search(
                    str(args.get("query", "")), requested=requested, succeeded=succeeded,
                    failed=failed, items_count=results_count, all_failed=all_failed
                )
            if all_failed:
                return False, f"ERROR: all search sources failed ({out})"

        if self.evidence is not None:
            if name == "web_search":
                out = self.evidence.annotate_search(out)
            elif name == "fetch_url":
                out = self.evidence.annotate_fetch(str(args.get("url", "")), out)
        if len(out) > config.TOOL_OUTPUT_LIMIT:
            out = out[: config.TOOL_OUTPUT_LIMIT] + f"\n...[truncated {len(out) - config.TOOL_OUTPUT_LIMIT} chars]"
        return True, out
