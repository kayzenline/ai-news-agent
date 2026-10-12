"""本轮证据库：工具层给每条 web_search 结果 / fetch_url 页面分配 evidence_id，模型只能引用这些 ID。

URL、标题、日期、摘录由工具结果产生并由 Service 保存，模型不能在草稿里自己填链接。
"""
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = re.compile(r"^(utm_[a-z]+|fbclid|gclid|mc_cid|mc_eid|ref_src)$", re.I)


def canonical_url(url: str) -> str:
    """去 fragment、主机小写、去默认端口和明确的跟踪参数；保留路径大小写、协议和其它 query。"""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = (p.hostname or "").lower()
    if p.port and not ((p.scheme == "http" and p.port == 80) or (p.scheme == "https" and p.port == 443)):
        host = f"{host}:{p.port}"
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)])
    return urlunsplit((p.scheme.lower(), host, p.path or "/", query, ""))


@dataclass
class Evidence:
    evidence_id: str
    tool: str
    source: str
    title: str
    url: str
    published_at: str      # 来源给出的日期；未知为 "unknown"（HN 是提交时间，不等于原文发布时间）
    retrieved_at: str      # 抓取时间（UTC）
    excerpt: str
    content_hash: str


class EvidenceStore:
    def __init__(self):
        self.items: dict[str, Evidence] = {}

    def add(self, tool: str, source: str, title: str, url: str, published_at: str = "", excerpt: str = "") -> str:
        eid = f"ev_{len(self.items) + 1:03d}"
        excerpt = (excerpt or "")[:500]
        self.items[eid] = Evidence(eid, tool, source, (title or "").strip()[:300], url.strip(),
                                   published_at.strip() or "unknown",
                                   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), excerpt,
                                   hashlib.sha256(f"{url}\n{title}\n{excerpt}".encode()).hexdigest()[:16])
        return eid

    def get(self, eid: str) -> Evidence | None:
        return self.items.get(eid)

    def to_json(self) -> str:
        return json.dumps([asdict(e) for e in self.items.values()], ensure_ascii=False, indent=2)

    def annotate_search(self, out: str) -> str:
        """把 web_search 的每行 `- [src] title | url | date | summary` 改成带 evidence_id 的行。"""
        lines = []
        for line in out.splitlines():
            m = re.match(r"^- \[([^\]]+)\] (.*)$", line)
            if not m:
                lines.append(line)
                continue
            parts = [x.strip() for x in m.group(2).split(" | ")]
            if len(parts) < 2 or not parts[1].startswith(("http://", "https://")):
                lines.append(line)
                continue
            title, url = parts[0], parts[1]
            date = parts[2] if len(parts) > 2 else ""
            summary = " | ".join(parts[3:]) if len(parts) > 3 else ""
            eid = self.add("web_search", m.group(1), title, url, date, summary or title)
            lines.append(f"- {eid} [{m.group(1)}] " + m.group(2))
        return "\n".join(lines)

    def annotate_fetch(self, url: str, text: str) -> str:
        eid = self.add("fetch_url", urlsplit(url).hostname or "web", "", url, "", text)
        return f"evidence_id: {eid} (url: {url})\n{text}"
