"""五个基础工具：list_dir / read_file / search_content / write_file / bash。"""
import shlex
import subprocess

from .sandbox import Sandbox, SandboxError

MAX_READ = 20_000

def list_dir(sb: Sandbox, path: str = ".") -> str:
    d = sb.resolve(path)
    if not d.exists():
        raise FileNotFoundError(f"no such directory: {path}")
    if not d.is_dir():
        raise NotADirectoryError(path)
    items = []
    for c in sorted(d.iterdir()):
        items.append(f"{'[D]' if c.is_dir() else '[F]'} {sb.rel(c)}")
    return "\n".join(items) or "(empty)"


def read_file(sb: Sandbox, path: str) -> str:
    f = sb.resolve(path)
    if not f.is_file():
        raise FileNotFoundError(f"no such file: {path}")
    text = f.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_READ:
        text = text[:MAX_READ] + "\n...[truncated]"
    return text


def search_content(sb: Sandbox, keyword: str, dir: str = ".") -> str:
    """大小写不敏感的子串搜索，返回 文件:行号: 内容，用于去重历史简报。"""
    d = sb.resolve(dir)
    if not d.is_dir():
        raise NotADirectoryError(dir)
    kw = keyword.lower()
    hits = []
    for f in sorted(d.rglob("*")):
        if not f.is_file() or f.stat().st_size > 2_000_000:
            continue
        try:
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if kw in line.lower():
                    hits.append(f"{sb.rel(f)}:{i}: {line.strip()[:200]}")
                    if len(hits) >= 50:
                        return "\n".join(hits) + "\n...[more hits truncated]"
        except OSError:
            continue
    return "\n".join(hits) or "no matches"


def write_file(sb: Sandbox, path: str, content: str) -> str:
    f = sb.resolve(path)
    if f == sb.root:
        raise SandboxError("cannot write to workspace root")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {sb.rel(f)}"


# bash：白名单命令 + 禁止元字符 + 不走 shell + cwd 固定在工作区 + 超时
ALLOWED_CMDS = {"ls", "cat", "head", "tail", "wc", "grep", "find", "date", "echo", "sort", "uniq", "mkdir"}
FORBIDDEN_TOKENS = [";", "&&", "||", "|", "`", "$(", ">", "<", "\n"]
BASH_TIMEOUT = 10


def bash(sb: Sandbox, command: str) -> str:
    for t in FORBIDDEN_TOKENS:
        if t in command:
            raise SandboxError(f"forbidden token {t!r}; pipes/redirection/chaining are disabled")
    argv = shlex.split(command)
    if not argv:
        raise ValueError("empty command")
    if argv[0] not in ALLOWED_CMDS:
        raise SandboxError(f"command not allowed: {argv[0]}; allowed: {sorted(ALLOWED_CMDS)}")
    if argv[0] == "find" and any(a in ("-exec", "-execdir", "-delete", "-ok") for a in argv):
        raise SandboxError("find actions are disabled")
    # 所有看起来像路径的参数都必须在沙箱内
    for a in argv[1:]:
        if a.startswith("-"):
            continue
        if a.startswith("~"):
            raise SandboxError("~ not allowed")
        if "/" in a or a.startswith(".."):
            if a.startswith("/"):
                raise SandboxError(f"absolute paths not allowed in bash: {a}")
            sb.resolve(a)
    try:
        r = subprocess.run(argv, cwd=sb.root, capture_output=True, text=True,
                           timeout=BASH_TIMEOUT, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"command timed out after {BASH_TIMEOUT}s")
    out = (r.stdout + (("\n[stderr]\n" + r.stderr) if r.stderr else ""))
    return f"[exit {r.returncode}]\n{out}"
