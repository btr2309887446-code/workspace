"""
git_utils.py
Git 相关公共工具，供 init_commit.py / final_commit.py / pipeline.py 复用。
所有命令都把 MVP 仓库根（ROOT）作为 cwd，不影响用户项目根目录。
"""
from __future__ import annotations
import subprocess
from pathlib import Path


GITIGNORE_CONTENT = """# MVP 中间产物
work/
refs/
__pycache__/
*.pyc
.DS_Store

# 标注项目不追踪虚拟环境
.venv/
venv/
"""


def run_git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """执行 git 子命令并返回 CompletedProcess。"""
    cmd = ["git", *args]
    res = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8")
    if check and res.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (code {res.returncode}):\n"
            f"STDOUT: {res.stdout}\nSTDERR: {res.stderr}"
        )
    return res


def ensure_repo(root: Path) -> bool:
    """确保 root 下是一个 git 仓库。返回 True 表示本次新初始化。"""
    if (root / ".git").exists():
        return False
    root.mkdir(parents=True, exist_ok=True)
    run_git(["init", "-b", "main"], cwd=root)
    # 本地默认 user 配置（避免 commit 时报错），只在未配置时才写
    who = run_git(["config", "user.email"], cwd=root, check=False)
    if who.returncode != 0 or not who.stdout.strip():
        run_git(["config", "user.email", "mvp@localhost"], cwd=root)
        run_git(["config", "user.name", "MVP Annotator"], cwd=root)
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(GITIGNORE_CONTENT, encoding="utf-8")
    return True


def has_staged_changes(root: Path) -> bool:
    res = run_git(["diff", "--cached", "--name-only"], cwd=root)
    return bool(res.stdout.strip())


def file_tracked(root: Path, rel_path: str) -> bool:
    res = run_git(["ls-files", "--error-unmatch", rel_path], cwd=root, check=False)
    return res.returncode == 0


def last_commit_subject(root: Path) -> str:
    res = run_git(["log", "-1", "--pretty=%s"], cwd=root, check=False)
    return res.stdout.strip() if res.returncode == 0 else ""
