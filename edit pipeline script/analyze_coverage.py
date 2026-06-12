r"""
analyze_coverage.py
扫一批已标注题目的 *_problem.json，统计：
1. 每题的 related_knowledge_points 是否非空
2. 全集覆盖了多少 IPhO Level-3 叶子（覆盖率）
3. 多少 Level-3 至少被命中 2 次（>=2x 覆盖率，对应"80% >=2x"目标）
4. 哪些题目仍然空 / 哪些 L3 还未触达

用法：
    python scripts/analyze_coverage.py
    python scripts/analyze_coverage.py --ids 3 37 ... 910
    python scripts/analyze_coverage.py --threshold 0.8 --min-hits 2

数据约定：每个题目目录形如 data/Q0001/Q0001_problem.json
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
TAXONOMY_PATH = ROOT / "IPHO考纲_分类标签.json"

# 用户指定的 92 题列表（含重复——重复体现采样权重）
DEFAULT_PROBLEM_IDS: list[str] = [
    "299", "222", "106", "678", "787", "294", "579", "263", "284", "37",
    "297", "229", "267", "240", "313", "377", "369", "387", "788", "457",
    "659", "746", "765", "875", "901", "143", "413", "463", "836", "85",
    "910", "374", "187", "267", "37", "465", "72", "800", "861", "193",
    "356", "101", "104", "235", "271", "284", "3", "304", "335", "338",
    "369", "463", "575", "63", "742", "76", "910", "145", "163", "177",
    "287", "374", "375", "672", "732", "788", "101", "104", "111", "194",
    "262", "3", "304", "313", "338", "360", "366", "413", "457", "575",
    "63", "654", "742", "746", "800", "85", "163", "177", "287", "375",
    "672", "732",
]


def load_taxonomy() -> set[tuple[str, str, str]]:
    data = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
    leaves: set[tuple[str, str, str]] = set()
    for cat in data.get("categories", []):
        for sub in cat.get("subcategories", []):
            for l3 in sub.get("topics", []):
                leaves.add((cat["level1"], sub["level2"], l3))
    return leaves


def find_problem_file(qid: str) -> Path | None:
    pad = qid.zfill(4)
    candidate = DATA_DIR / f"Q{pad}" / f"Q{pad}_problem.json"
    if candidate.exists():
        return candidate
    # fallback：直接遍历
    for d in DATA_DIR.glob("Q*"):
        if d.name.endswith(pad) or d.name.endswith(qid):
            for f in d.glob("*_problem.json"):
                return f
    return None


def collect_kps(problem_path: Path) -> tuple[list[tuple[str, str, str]], int, int]:
    """
    Returns (all_kp_triples, total_subq, empty_subq)
    """
    data = json.loads(problem_path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        subqs = data
    elif isinstance(data, dict):
        subqs = (
            data.get("sub_questions")
            or data.get("subquestions")
            or data.get("子题目")
            or []
        )
    else:
        subqs = []
    kps: list[tuple[str, str, str]] = []
    empty = 0
    for s in subqs:
        rel = s.get("related_knowledge_points") or s.get("关联考点") or []
        if not rel:
            empty += 1
            continue
        for triple in rel:
            if isinstance(triple, list) and len(triple) >= 3:
                kps.append((str(triple[0]), str(triple[1]), str(triple[2])))
    return kps, len(subqs), empty


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", nargs="*", default=DEFAULT_PROBLEM_IDS,
                        help="题号列表（默认用内置 92 题）")
    parser.add_argument("--threshold", type=float, default=0.8,
                        help="目标覆盖率阈值 (默认 0.8)")
    parser.add_argument("--min-hits", type=int, default=2,
                        help="≥N 次命中算达标 (默认 2)")
    parser.add_argument("--output", default="coverage_report.json",
                        help="输出报告路径")
    args = parser.parse_args()

    leaves = load_taxonomy()
    print(f"[coverage] IPhO Level-3 leaves total: {len(leaves)}")

    counter: Counter[tuple[str, str, str]] = Counter()
    missing_files: list[str] = []
    per_problem: list[dict] = []

    for qid in args.ids:
        path = find_problem_file(qid)
        if path is None:
            missing_files.append(qid)
            continue
        kps, total, empty = collect_kps(path)
        for k in kps:
            counter[k] += 1
        per_problem.append({
            "id": qid,
            "subq_total": total,
            "subq_empty": empty,
            "kp_count": len(kps),
        })

    covered = set(counter.keys())
    twice_covered = {k for k, c in counter.items() if c >= args.min_hits}
    cov_ratio = len(covered) / len(leaves) if leaves else 0.0
    twice_ratio = len(twice_covered) / len(leaves) if leaves else 0.0

    print(f"[coverage] processed problems: {len(per_problem)}")
    print(f"[coverage] missing problem files: {len(missing_files)}  -> {missing_files[:10]}{'...' if len(missing_files) > 10 else ''}")
    print(f"[coverage] L3 leaves covered (>=1 hit): {len(covered)} / {len(leaves)}  ({cov_ratio:.1%})")
    print(f"[coverage] L3 leaves covered (>={args.min_hits} hits): {len(twice_covered)} / {len(leaves)}  ({twice_ratio:.1%})")
    target_ok = twice_ratio >= args.threshold
    mark = "[PASS]" if target_ok else "[FAIL]"
    print(f"[coverage] target {args.threshold:.0%} @ >={args.min_hits}x: {mark}")

    # Top under-represented leaves (covered but only once)
    once_only = [k for k, c in counter.items() if c == 1]
    never = [k for k in leaves if k not in covered]

    report = {
        "summary": {
            "leaves_total": len(leaves),
            "covered_at_least_1": len(covered),
            "covered_at_least_n": len(twice_covered),
            "min_hits": args.min_hits,
            "threshold": args.threshold,
            "target_pass": target_ok,
            "problems_processed": len(per_problem),
            "problems_missing": missing_files,
        },
        "per_problem": per_problem,
        "leaf_hit_counts": [
            {"l1": k[0], "l2": k[1], "l3": k[2], "hits": c}
            for k, c in counter.most_common()
        ],
        "leaves_only_once": [
            {"l1": k[0], "l2": k[1], "l3": k[2]} for k in once_only
        ],
        "leaves_never": [
            {"l1": k[0], "l2": k[1], "l3": k[2]} for k in never
        ],
    }
    out_path = ROOT / args.output
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[coverage] report -> {out_path}")


if __name__ == "__main__":
    main()
