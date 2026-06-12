"""
pipeline_new.py
MVP 工作流统一入口（对齐最终模板.json 格式）。
用法：
    python scripts/pipeline_new.py --file Q1
    python scripts/pipeline_new.py --file Q1 --stage annotate   # 只跑标注不提交

LLM 集成：
    设置环境变量 LLM_API_KEY 后，fill_annotation_new 会自动调用 LLM 填充标注字段。
    可选环境变量：LLM_BASE_URL, LLM_MODEL, LLM_MAX_TOKENS, LLM_TEMPERATURE

目录约定（项目根 = 本脚本上两级目录）：
    data/Q1/Q1_problem.md
    data/Q1/Q1_problem.json   <- 输出（新格式，对齐最终模板）
    data/Q1/image/            <- 图片输出
    data/Q1/work/             <- 中间产物
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # .../test/mvp
SCRIPTS = ROOT / "scripts"

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def run(step: str, *args: str) -> None:
    cmd = [sys.executable, str(SCRIPTS / step), *args]
    try:
        print(f"\n>>> {step} " + " ".join(args))
    except UnicodeEncodeError:
        sys.stdout.buffer.write(f"\n>>> {step} ".encode("utf-8", errors="replace"))
    res = subprocess.run(cmd, env=_child_env())
    if res.returncode != 0:
        raise SystemExit(f"step failed: {step}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="e.g. Q1")
    ap.add_argument("--source", default="model-teacher", help="数据来源 (ipho/cpho/model-teacher/模拟题)")
    ap.add_argument("--year", default="2026", help="题目年份")
    ap.add_argument("--supplement", default="", help="补充信息（竞赛真题/来源说明等）")
    ap.add_argument("--git", action="store_true", help="在流水线首尾自动执行 init_commit + final_commit")
    ap.add_argument("--stage", choices=["all", "original", "annotate", "final"], default="all",
                    help="all=跑全部; original=只做原版 git 提交; annotate=只跑标注不提交; final=只做标注版 git 提交")
    ap.add_argument("--no-llm", action="store_true", help="跳过 LLM 调用，仅生成骨架")
    args = ap.parse_args()

    if args.no_llm:
        os.environ.pop("LLM_API_KEY", None)

    qid = args.file
    qdir = ROOT / "data" / qid
    md = qdir / f"{qid}_problem.md"
    out_json = qdir / f"{qid}_problem.json"
    work = qdir / "work"
    image_dir = qdir / "image"
    work.mkdir(parents=True, exist_ok=True)
    image_dir.mkdir(parents=True, exist_ok=True)

    if not md.exists():
        raise SystemExit(f"input not found: {md}")

    # --- 阶段 1：原版 git 提交（规范要求的修改留痕第一轮）---
    if args.stage in ("all", "original") and (args.git or args.stage == "original"):
        run("init_commit.py", "--file", qid)
        if args.stage == "original":
            return 0

    parsed = work / f"{qid}.parsed.json"
    subq = work / f"{qid}.subquestions.json"
    diagram_py = work / f"{qid}.diagram.py"
    review_todo = work / f"{qid}.review.todo.md"
    meta_file = work / f"{qid}.meta.json"

    # 生成 meta
    problem_number = "".join(c for c in qid if c.isdigit()) or "1"
    meta = {
        "source": args.source,
        "supplement": args.supplement,
        "year": args.year,
        "section_1": qid,
        "section_2": "",
        "section_3": "",
    }
    meta_file.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    # 1. parse_md - 解析 md 文件层级结构
    run("parse_md.py", str(md), str(parsed))
    # 2. split_subquestions - 按 Part 拆分子问题 + context/background/rubric
    run("split_subquestions.py", str(parsed), str(subq), str(meta_file))
    # 3. fill_annotation_new - 生成新格式 JSON 骨架 + LLM 填充
    run("fill_annotation_new.py", str(subq), str(out_json), str(review_todo), str(meta_file))
    # 4. extract_diagram - 提取图表代码
    run("extract_diagram.py", str(md), str(diagram_py))
    # 5. render_diagram - 按规范命名渲染图片: image/{source}_{year}_{n}_1.png
    rel_img = f"image/{args.source}_{args.year}_{problem_number}_1.png"
    abs_img = qdir / rel_img
    if diagram_py.exists():
        run("render_diagram.py", str(diagram_py), str(abs_img), str(qdir))
        if abs_img.exists():
            run("sync_image_refs_new.py", str(md), str(out_json), rel_img)
        else:
            print("[pipeline_new] image not rendered, skip sync")
    else:
        print("[pipeline_new] no diagram script, skip render/sync")

    # 6. validate（使用新的验证脚本）
    print("\n>>> validate_annotation_new (non-blocking in MVP)")
    vcmd = [sys.executable, str(SCRIPTS / "validate_annotation_new.py"), str(out_json), str(md), str(qdir)]
    res = subprocess.run(vcmd, env=_child_env())

    print(f"\n[pipeline_new] done. outputs:\n  {out_json}\n  {md}\n  {abs_img if abs_img.exists() else '(no image)'}\n  {review_todo}")

    if os.environ.get("LLM_API_KEY"):
        print("[pipeline_new] LLM was used for annotation filling.")
    else:
        print("[pipeline_new] LLM not configured. Set LLM_API_KEY to enable automatic annotation filling.")

    # --- 阶段 2：标注版 git 提交（规范要求的修改留痕第二轮）---
    if args.stage in ("all", "final") and (args.git or args.stage == "final"):
        run("final_commit.py", "--file", qid)

    return 0


if __name__ == "__main__":
    main()
