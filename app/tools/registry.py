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
    _fn("bash", "Run ONE restricted read-only shell command inside the workspace (allowed: ls cat head tail wc grep find date echo sort uniq mkdir; no pipes/redirects). 10s timeout.",
        {"command": S}, ["command"]),
    _fn("web_search", "Search recent AI news. Sources: hackernews, rss (TechCrunch/VentureBeat/MIT TR/HF blog), arxiv, tavily (if configured). Returns title|url|date lines.",
        {"query": S, "sources": {"type": "array", "items": {"type": "string", "enum": ["hackernews", "rss", "arxiv", "tavily"]}},
         "days": {"type": "integer", "description": "look-back window in days, default 2"}}, ["query"]),
    _fn("fetch_url", "Fetch a web page and return its plain text (truncated) to read details of a story.", {"url": S}, ["url"]),
]


class ToolRegistry:
    def __init__(self, sandbox: Sandbox, extra: dict[str, Callable] | None = None):
        self.sb = sandbox
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
        return [s for s in TOOL_SCHEMAS if s["function"]["name"] in self.impl]

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
            out = str(self.impl[name](**args))
        except TypeError as e:
            return False, f"ERROR: bad arguments for {name}: {e}"
        except Exception as e:
            return False, f"ERROR: {type(e).__name__}: {e}"
        if len(out) > config.TOOL_OUTPUT_LIMIT:
            out = out[: config.TOOL_OUTPUT_LIMIT] + f"\n...[truncated {len(out) - config.TOOL_OUTPUT_LIMIT} chars]"
        return True, out
