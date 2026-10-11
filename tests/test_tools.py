import os
import pytest
from app.tools.sandbox import Sandbox, SandboxError
from app.tools import fs_tools as T
from app.tools.registry import ToolRegistry


@pytest.fixture
def sb(tmp_path):
    s = Sandbox(tmp_path / "ws")
    (s.root / "briefs").mkdir()
    (s.root / "briefs" / "2026-10-10.md").write_text("- **OpenAI ships X** https://ex.com/a\n")
    return s


def test_basic_roundtrip(sb):
    assert "wrote" in T.write_file(sb, "notes/a.txt", "hello")
    assert T.read_file(sb, "notes/a.txt") == "hello"
    assert "[D] notes" in T.list_dir(sb, ".")
    assert "2026-10-10.md:1" in T.search_content(sb, "https://ex.com/a", "briefs")
    assert T.search_content(sb, "nothing-here", "briefs") == "no matches"


@pytest.mark.parametrize("p", ["../outside.txt", "a/../../x", "/etc/passwd", "briefs/../../.."])
def test_path_escape_blocked(sb, p):
    with pytest.raises((SandboxError, FileNotFoundError)):
        T.read_file(sb, p)
    if p != "/etc/passwd":
        with pytest.raises(SandboxError):
            T.write_file(sb, p, "x")


def test_absolute_path_is_rebased(sb):
    T.write_file(sb, "/inside.txt", "y")
    assert (sb.root / "inside.txt").exists()


def test_symlink_escape_blocked(sb, tmp_path):
    (tmp_path / "secret.txt").write_text("s")
    os.symlink(tmp_path / "secret.txt", sb.root / "link.txt")
    with pytest.raises(SandboxError):
        T.read_file(sb, "link.txt")


@pytest.mark.parametrize("cmd", ["rm -rf /", "curl http://x", "python -c 1", "ls; rm x", "cat a | sh",
                                 "echo hi > f", "cat /etc/passwd", "cat ../../etc/passwd", "ls ~",
                                 "find . -exec rm {} ;", "echo $(id)"])
def test_bash_blocked(sb, cmd):
    with pytest.raises(SandboxError):
        T.bash(sb, cmd)


def test_bash_allowed_runs_in_workspace(sb):
    out = T.bash(sb, "ls briefs")
    assert "[exit 0]" in out and "2026-10-10.md" in out


def test_registry_feeds_errors_back(sb):
    reg = ToolRegistry(sb)
    ok, out = reg.call("read_file", '{"path": "../x"}')
    assert not ok and out.startswith("ERROR")
    ok, out = reg.call("nope", "{}")
    assert not ok and "unknown tool" in out
    ok, out = reg.call("read_file", "{bad json")
    assert not ok and "invalid JSON" in out
    ok, out = reg.call("read_file", '{"wrong": 1}')
    assert not ok and "bad arguments" in out


# --- review fix #1: options could smuggle output paths / write actions ---
@pytest.mark.parametrize("cmd", [
    "sort -o../outside.txt input.txt", "sort -o ../outside.txt input.txt",
    "uniq input.txt ../outside.txt", "grep -f../outside.txt x input.txt",
    "grep --file=../outside.txt input.txt", "find . -fprint ../outside.txt",
    "find . -delete", "find . -exec cat {} +", "ls --color=always", "cat -Z input.txt",
    "head -n../x input.txt", "date -s 2020-01-01", "date -f input.txt",
    "find ../ -name x", "find . -newer ../outside.txt",
])
def test_bash_option_smuggling_blocked(sb, cmd):
    (sb.root / "input.txt").write_text("b\na\n")
    with pytest.raises(SandboxError):
        T.bash(sb, cmd)
    assert not (sb.root.parent / "outside.txt").exists()


@pytest.mark.parametrize("cmd", ["head -n 1 input.txt", "head -5 input.txt", "grep -in a input.txt",
                                 "grep -e b input.txt", "find . -name '*.txt' -type f", "wc -l input.txt",
                                 "date +%F", "ls -la briefs", "tail -n1 input.txt", "mkdir -p notes/x"])
def test_bash_safe_options_allowed(sb, cmd):
    (sb.root / "input.txt").write_text("b\na\n")
    assert T.bash(sb, cmd).startswith("[exit 0]")


# --- review fix #6: basename symlinks / traversed symlinks escaping the workspace ---
def test_symlink_basename_and_search_blocked(sb):
    secret = sb.root.parent / "secret.txt"
    secret.write_text("TOPSECRET\n")
    (sb.root / "link.txt").symlink_to(secret)
    with pytest.raises(SandboxError):
        T.bash(sb, "cat link.txt")
    assert "TOPSECRET" not in T.search_content(sb, "TOPSECRET", ".")
    with pytest.raises(SandboxError):
        T.bash(sb, "grep -R TOPSECRET .")
