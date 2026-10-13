"""工作区沙箱：所有文件类工具的路径都必须落在用户工作区内。"""

from pathlib import Path


class SandboxError(Exception):
    pass


class Sandbox:
    def __init__(self, root: Path, writable: list[str] | None = None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        # None = 整个工作区可写（单测/工具层默认）；运行时由 Service 只开放本轮 staging 目录
        self.writable = None if writable is None else [(self.root / w).resolve() for w in writable]

    def check_writable(self, full: Path) -> None:
        """写权限边界：画像、已发布历史和其它运行的文件对模型只读。"""
        if self.writable is None:
            return
        if not any(full == w or w in full.parents for w in self.writable):
            allowed = ", ".join(self.rel(w) for w in self.writable)
            raise SandboxError(f"read-only path: {self.rel(full)}; this run may only write under: {allowed}")

    def resolve(self, path: str) -> Path:
        """把模型给的相对路径解析成绝对路径；resolve() 会展开 .. 和符号链接，
        再检查是否仍在 root 下，防止 ../../etc/passwd 或软链逃逸。"""
        if path is None:
            path = "."
        p = Path(path)
        if p.is_absolute():
            # 绝对路径一律视为相对工作区
            p = Path(*p.parts[1:]) if len(p.parts) > 1 else Path(".")
        full = (self.root / p).resolve()
        if full != self.root and self.root not in full.parents:
            raise SandboxError(f"path escapes workspace: {path}")
        return full

    def rel(self, p: Path) -> str:
        return str(p.relative_to(self.root)) or "."
