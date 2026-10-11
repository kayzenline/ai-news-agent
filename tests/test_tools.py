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
