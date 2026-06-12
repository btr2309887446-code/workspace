"""
init_commit.py
规范要求的"修改留痕"第一轮：在任何自动/人工修改前，把原始输入提交到 git。

用法：
    python scripts/init_commit.py --file Q1
只会添加并提交 data/Q{n}/Q{n}_problem.md（原版题面）。
若该文件在最近一次提交里已经以"upload: original"的主题被提交过，则跳过。
"""
from __future__ import annotations
import argparse
from pathlib import Path

from git_utils import ensure_repo, run_git, last_commit_subject, file_tracked

ROOT = Path(__file__).resolve().parents[1]  # test/mvp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="e.g. Q1")
    args = ap.parse_args()
    qid = args.file
    rel_md = f"data/{qid}/{qid}_problem.md"
    abs_md = ROOT / rel_md
    if not abs_md.exists():
        raise SystemExit(f"original md not found: {abs_md}")

    created = ensure_repo(ROOT)
    if created:
        print(f"[init_commit] initialized git repo at {ROOT}")

    # 若已经有过 "upload: original Q{n}" 的提交，就幂等跳过
    # 改为扫描 git log 主题，避免仅看最后一条
    log_res = run_git(["log", "--pretty=%s"], cwd=ROOT, check=False)
    subjects = log_res.stdout.splitlines() if log_res.returncode == 0 else []
    target_subject = f"upload: original {qid} before annotation"
    if target_subject in subjects:
        print(f"[init_commit] already committed before: '{target_subject}', skip.")
        return

    # 先把 .gitignore 纳入初始提交（若存在且未追踪）
    if (ROOT / ".gitignore").exists() and not file_tracked(ROOT, ".gitignore"):
        run_git(["add", ".gitignore"], cwd=ROOT)

    run_git(["add", rel_md], cwd=ROOT)
    res = run_git(["commit", "-m", target_subject], cwd=ROOT, check=False)
    if res.returncode != 0:
        # 可能"nothing to commit"，属于正常情况
        if "nothing to commit" in (res.stdout + res.stderr).lower():
            print("[init_commit] nothing to commit (clean tree), skip.")
            return
        raise RuntimeError(res.stdout + res.stderr)
    print(f"[init_commit] committed: {target_subject}")
    print(last_commit_subject(ROOT))


if __name__ == "__main__":
    main()
