"""五个基础工具：list_dir / read_file / search_content / write_file / bash。"""
import glob
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
        real = f.resolve()
        if real != sb.root and sb.root not in real.parents:
            continue  # 遍历到指向工作区外的软链：跳过
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
    sb.check_writable(f)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {sb.rel(f)}"


# bash：白名单命令 + 每个命令的选项白名单 + 禁止元字符 + 不走 shell + cwd 固定在工作区 + 超时
# 只保留只读命令（外加工作区内 mkdir）；sort/uniq 有写文件能力（sort -o、uniq IN OUT），已移除。
# spec: flags=允许的无参短选项字母; value=带参短选项 -> 参数类型("num"/"text");
#       operands="path"(全部按路径做沙箱检查) / "text"(不是路径) / "date"(只能是 +FORMAT)
BASH_SPECS = {
    "ls":    {"flags": "lahR1trSFd", "value": {}, "operands": "path"},
    "cat":   {"flags": "nbsAE", "value": {}, "operands": "path"},
    "head":  {"flags": "qv", "value": {"n": "num", "c": "num"}, "operands": "path"},
    "tail":  {"flags": "qv", "value": {"n": "num", "c": "num"}, "operands": "path"},
    "wc":    {"flags": "lwcm", "value": {}, "operands": "path"},
    "grep":  {"flags": "inrvcHhlLwxFEoqs", "value": {"e": "text", "m": "num", "A": "num", "B": "num", "C": "num"},
              "operands": "grep"},
    "find":  {"operands": "find"},
    "date":  {"flags": "uR", "value": {}, "operands": "date"},
    "echo":  {"flags": "neE", "value": {}, "operands": "text"},
    "mkdir": {"flags": "pv", "value": {}, "operands": "path"},
}
ALLOWED_CMDS = set(BASH_SPECS)
FIND_TESTS = {"-name": "text", "-iname": "text", "-path": "text", "-ipath": "text", "-type": "text",
              "-maxdepth": "num", "-mindepth": "num", "-mtime": "text", "-mmin": "text", "-size": "text",
              "-newer": "path"}
FIND_FLAGS = {"-print", "-empty", "-not", "!", "-o", "-a", "-or", "-and", "(", ")"}
FORBIDDEN_TOKENS = [";", "&&", "||", "|", "`", "$(", ">", "<", "\n"]
BASH_TIMEOUT = 10


def _check_path(sb: Sandbox, a: str):
    if a.startswith("~"):
        raise SandboxError("~ not allowed")
    if a.startswith("/"):
        raise SandboxError(f"absolute paths not allowed in bash: {a}")
    sb.resolve(a)  # 每个操作数都解析（含 basename 软链），确保真实目标在工作区内


def _check_value(sb: Sandbox, kind: str, v: str, opt: str):
    if kind == "num" and not v.lstrip("+-").isdigit():
        raise SandboxError(f"option {opt} expects a number, got {v!r}")
    if kind == "path":
        _check_path(sb, v)


def _validate_find(sb: Sandbox, args: list[str]):
    i, seen_expr = 0, False
    while i < len(args):
        a = args[i]
        if a in FIND_TESTS:
            if i + 1 >= len(args):
                raise SandboxError(f"find {a} needs a value")
            _check_value(sb, FIND_TESTS[a], args[i + 1], a)
            i, seen_expr = i + 2, True
            continue
        if a in FIND_FLAGS:
            i, seen_expr = i + 1, True
            continue
        if a.startswith("-"):
            raise SandboxError(f"find option not allowed: {a}; allowed: {sorted(FIND_TESTS) + sorted(FIND_FLAGS)}")
        if seen_expr:
            raise SandboxError(f"unexpected find argument: {a}")
        _check_path(sb, a)  # 起始目录
        i += 1


def _validate(sb: Sandbox, argv: list[str]):
    cmd, args = argv[0], argv[1:]
    spec = BASH_SPECS[cmd]
    if spec["operands"] == "find":
        return _validate_find(sb, args)
    operands, i, end_opts = [], 0, False
    need_pattern = spec["operands"] == "grep"
    while i < len(args):
        a = args[i]
        i += 1
        if end_opts or not a.startswith("-") or a == "-":
            operands.append(a)
            continue
        if a == "--":
            end_opts = True
            continue
        if a.startswith("--"):
            raise SandboxError(f"long options are not allowed in bash: {a.split('=')[0]}")
        if cmd in ("head", "tail") and a[1:].isdigit():  # head -5
            continue
        j = 1
        while j < len(a):
            ch = a[j]
            if ch in spec["value"]:
                v = a[j + 1:]
                if not v:
                    if i >= len(args):
                        raise SandboxError(f"option -{ch} needs a value")
                    v, i = args[i], i + 1
                _check_value(sb, spec["value"][ch], v, f"-{ch}")
                if cmd == "grep" and ch == "e":
                    need_pattern = False
                break
            if ch not in spec["flags"]:
                raise SandboxError(f"option -{ch} not allowed for {cmd}; allowed flags: {spec['flags']} "
                                   f"value options: {sorted(spec['value'])}")
            j += 1
    kind = spec["operands"]
    if kind == "grep":
        if need_pattern and operands:
            operands = operands[1:]  # 第一个操作数是 PATTERN
        kind = "path"
    for a in operands:
        if kind == "path":
            _check_path(sb, a)
            if cmd == "mkdir":
                sb.check_writable(sb.resolve(a))
        elif kind == "date" and not a.startswith("+"):
            raise SandboxError("date only accepts +FORMAT")


GLOB_CMDS = {"ls", "cat", "head", "tail", "wc", "grep"}
GLOB_CHARS = set("*?[")
MAX_GLOB_MATCHES = 200


def expand_globs(sb: Sandbox, argv: list[str]) -> list[str]:
    """不经 shell，所以自己做通配符展开：只展开文件操作数，只在工作区内匹配，结果按字典序。
    grep 的 PATTERN、选项值、find/echo 的参数不展开。"""
    cmd = argv[0]
    if cmd not in GLOB_CMDS:
        return argv
    out, need_pattern = [cmd], cmd == "grep" and not any(a == "-e" or (a.startswith("-e") and len(a) > 2) for a in argv[1:])
    value_opts = set(BASH_SPECS[cmd]["value"])
    skip_next = end_opts = False
    for a in argv[1:]:
        if skip_next:
            out.append(a); skip_next = False; continue
        if not end_opts and a == "--":
            end_opts = True; out.append(a); continue
        if not end_opts and a.startswith("-") and a != "-":
            if a[-1] in value_opts and len(a) == 2:
                skip_next = True
            out.append(a); continue
        if need_pattern:
            need_pattern = False; out.append(a); continue
        if not GLOB_CHARS & set(a):
            out.append(a); continue
        _check_path(sb, a.split("*")[0].split("?")[0].split("[")[0] or ".")  # 拒绝绝对路径、~、..逃逸的前缀
        matches = sorted(glob.glob(a, root_dir=sb.root))
        if not matches:
            raise SandboxError(f"no files match {a!r}")
        if len(matches) > MAX_GLOB_MATCHES:
            raise SandboxError(f"{a!r} matches {len(matches)} files (max {MAX_GLOB_MATCHES}); narrow the pattern")
        out.extend(matches)
    return out


def bash(sb: Sandbox, command: str) -> str:
    for t in FORBIDDEN_TOKENS:
        if t in command:
            raise SandboxError(f"forbidden token {t!r}; pipes/redirection/chaining are disabled")
    argv = shlex.split(command)
    if not argv:
        raise ValueError("empty command")
    if argv[0] not in ALLOWED_CMDS:
        raise SandboxError(f"command not allowed: {argv[0]}; allowed: {sorted(ALLOWED_CMDS)}")
    argv = expand_globs(sb, argv)
    _validate(sb, argv)  # 展开后的每个路径仍逐个做沙箱检查
    try:
        r = subprocess.run(argv, cwd=sb.root, capture_output=True, text=True,
                           timeout=BASH_TIMEOUT, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"command timed out after {BASH_TIMEOUT}s")
    out = (r.stdout + (("\n[stderr]\n" + r.stderr) if r.stderr else ""))
    return f"[exit {r.returncode}]\n{out}"
