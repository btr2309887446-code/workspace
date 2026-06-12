"""
validate_annotation_new.py
轻量规范校验（对齐最终模板格式）：
  - JSON 必须是数组格式
  - 每个元素必须包含 9 大节（中文key）
  - answer_type / 改造后答案类型 / modality_type / difficulty 枚举校验
  - 改造后问题不得包含常见单位符号（告警）
  - md 中不得出现 ![非空](...) 写法
  - JSON value 中禁止连续中文字符（规范：value必须为英文）
  - 声明的图片路径必须存在
  - 数组长度约束（final_answer, answer_type, answer_unit等）
  - 关联考点格式校验（数组的数组，每组3个元素）
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


TOP_SECTIONS = [
    "标注基础信息", "题目信息", "条件提取", "物理模型",
    "关联考点", "物理场景", "判分标准", "错误解题步骤", "多解法标注"
]
ANSWER_TYPES = {"expression", "numerical", "choice", "equation", "open-ended", "inequality", ""}
MODALITY_TYPES = {"text-only", "text+illustration figure", "text+variable figure", "text+data figure", ""}
DIFFICULTIES = {"easy", "medium", "hard", ""}
UNIT_PATTERNS = [r"\bT\b", r"\beV\b", r"\brad/s\b", r"\bN\b", r"\bJ\b", r"\bkg\b", r"\bm/s\b"]
BAD_MD_IMG = re.compile(r"!\[[^\]]+\]\([^)]+\)")
GOOD_MD_IMG = re.compile(r"!\[\]\(([^)]+)\)")
CJK = re.compile(r"[\u4e00-\u9fff]")


def load_ipho_knowledge_points() -> set[tuple[str, str, str]]:
    taxonomy_path = ROOT / "IPHO考纲_分类标签.json"
    if not taxonomy_path.exists():
        return set()
    taxonomy = json.loads(taxonomy_path.read_text(encoding="utf-8"))
    points: set[tuple[str, str, str]] = set()
    for category in taxonomy.get("categories", []):
        level1 = category.get("level1", "")
        for subcategory in category.get("subcategories", []):
            level2 = subcategory.get("level2", "")
            for topic in subcategory.get("topics", []):
                points.add((level1, level2, topic))
    return points


def validate(json_path: Path, md_path: Path, qdir: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    ipho_points = load_ipho_knowledge_points()

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        errors.append(f"Invalid JSON: {e}")
        return errors, warnings

    md_text = md_path.read_text(encoding="utf-8")

    if not isinstance(data, list):
        errors.append(f"JSON must be an array, got {type(data).__name__}")
        return errors, warnings

    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            errors.append(f"Item {idx}: must be an object, got {type(item).__name__}")
            continue

        sub_id = item.get("标注基础信息", {}).get("版块3", f"item_{idx}")

        for key in TOP_SECTIONS:
            if key not in item:
                errors.append(f"Item {sub_id}: missing top-level section '{key}'")

        qi = item.get("题目信息", {})
        n = len(qi.get("最终答案", []))

        for k in ("改造后答案", "答案类型", "改造后答案类型", "答案单位", "改造后答案单位", "关联图片路径", "模态类型"):
            if len(qi.get(k, [])) != n:
                errors.append(f"Item {sub_id}: {k} length {len(qi.get(k, []))} != 最终答案 length {n}")

        for t in qi.get("答案类型", []):
            if t not in ANSWER_TYPES:
                errors.append(f"Item {sub_id}: invalid answer_type '{t}'")
        for t in qi.get("改造后答案类型", []):
            if t not in ANSWER_TYPES:
                errors.append(f"Item {sub_id}: invalid 改造后答案类型 '{t}'")
        for t in qi.get("模态类型", []):
            if t not in MODALITY_TYPES:
                errors.append(f"Item {sub_id}: invalid modality_type '{t}'")
        if qi.get("难度") not in DIFFICULTIES:
            errors.append(f"Item {sub_id}: invalid difficulty '{qi.get('难度')}'")

        rq = qi.get("改造后问题", "")
        for pat in UNIT_PATTERNS:
            if re.search(pat, rq):
                warnings.append(f"Item {sub_id}: 改造后问题 may contain unit matching /{pat}/")

        if not rq:
            warnings.append(f"Item {sub_id}: 改造后问题 is empty (manual fill required)")
        if not qi.get("难度"):
            warnings.append(f"Item {sub_id}: 难度 is empty (manual fill required)")
        if not qi.get("核心思路"):
            warnings.append(f"Item {sub_id}: 核心思路 is empty (manual fill required)")

        for ip in qi.get("关联图片路径", []):
            if ip and not (qdir / ip).exists():
                errors.append(f"Item {sub_id}: 关联图片路径 '{ip}' not found under {qdir}")

        ce = item.get("条件提取", {})
        if not isinstance(ce.get("显性条件"), list):
            errors.append(f"Item {sub_id}: 显性条件 must be an array")
        if not isinstance(ce.get("隐性条件"), list):
            errors.append(f"Item {sub_id}: 隐性条件 must be an array")
        else:
            for ic_idx, ic in enumerate(ce["隐性条件"]):
                if not isinstance(ic, dict):
                    errors.append(f"Item {sub_id}: 隐性条件[{ic_idx}] must be an object")
                else:
                    if "条件原文" not in ic:
                        errors.append(f"Item {sub_id}: 隐性条件[{ic_idx}] missing '条件原文'")
                    if "隐藏条件" not in ic:
                        errors.append(f"Item {sub_id}: 隐性条件[{ic_idx}] missing '隐藏条件'")

        rkp = item.get("关联考点", [])
        if not isinstance(rkp, list):
            errors.append(f"Item {sub_id}: 关联考点 must be an array")
        else:
            for kp_idx, kp in enumerate(rkp):
                if not isinstance(kp, list) or len(kp) != 3:
                    errors.append(f"Item {sub_id}: 关联考点[{kp_idx}] must be an array of 3 strings, got {kp}")
                else:
                    for level_idx, level in enumerate(kp):
                        if not isinstance(level, str):
                            errors.append(f"Item {sub_id}: 关联考点[{kp_idx}][{level_idx}] must be a string")
                    if ipho_points and tuple(kp) not in ipho_points:
                        errors.append(f"Item {sub_id}: 关联考点[{kp_idx}] is not an exact IPhO Section 2 theoretical topic: {kp}")

        rubric = item.get("判分标准", [])
        if not isinstance(rubric, list):
            errors.append(f"Item {sub_id}: 判分标准 must be an array")
        else:
            for ri, r in enumerate(rubric):
                if not isinstance(r, str):
                    errors.append(f"Item {sub_id}: 判分标准[{ri}] must be a string")

    for m in BAD_MD_IMG.finditer(md_text):
        if not m.group(0).startswith("![]("):
            errors.append(f"md image format violates spec (brackets must be empty): {m.group(0)}")

    json_text = json_path.read_text(encoding="utf-8")
    lines = json_text.split('\n')
    for line_num, line in enumerate(lines, 1):
        is_key_line = False
        for key in TOP_SECTIONS:
            if f'"{key}":' in line:
                is_key_line = True
                break
        if is_key_line:
            continue
        if CJK.search(line):
            if '":"' in line or '": "' in line:
                warnings.append(f"Line {line_num}: value may contain CJK characters (values should be in English)")

    return errors, warnings


def main():
    if len(sys.argv) != 4:
        print("usage: validate_annotation_new.py <json> <md> <qdir>")
        sys.exit(1)
    errors, warnings = validate(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
    for w in warnings:
        print(f"[warn] {w}")
    for e in errors:
        print(f"[error] {e}")
    print(f"\n[validate] {len(errors)} error(s), {len(warnings)} warning(s)")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
