"""
final_commit.py
规范要求的"修改留痕"第二轮：在完成自动标注 + 人工定稿后，把标注产物提交到 git。

会 git add：
  - data/Q{n}/Q{n}_problem.md        （可能因图片占位符同步而变更）
  - data/Q{n}/Q{n}_problem.json
  - data/Q{n}/image/**               （生成的图片）

用法：
    python scripts/final_commit.py --file Q1
"""
from __future__ import annotations
import argparse
from pathlib import Path

from git_utils import ensure_repo, run_git, last_commit_subject

ROOT = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="e.g. Q1")
    args = ap.parse_args()
    qid = args.file

    ensure_repo(ROOT)

    json_rel = f"data/{qid}/{qid}_problem.json"
    md_rel = f"data/{qid}/{qid}_problem.md"
    img_rel = f"data/{qid}/image"

    for rel in (md_rel, json_rel, img_rel):
        if (ROOT / rel).exists():
            run_git(["add", rel], cwd=ROOT)

    # 检查是否真的有 staged 变更
    status = run_git(["diff", "--cached", "--name-only"], cwd=ROOT)
    if not status.stdout.strip():
        print("[final_commit] no staged changes, skip commit.")
        return

    subject = f"annotate: {qid} annotated per spec v0416"
    res = run_git(["commit", "-m", subject], cwd=ROOT, check=False)
    if res.returncode != 0:
        raise RuntimeError(res.stdout + res.stderr)
    print(f"[final_commit] committed: {subject}")
    print("[final_commit] last:", last_commit_subject(ROOT))


if __name__ == "__main__":
    main()
