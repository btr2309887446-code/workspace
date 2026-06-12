"""
convert_to_new_format.py
将旧格式的 Q{n}_problem.json 转换为新格式（对齐最终模板.json）。
旧格式：单个对象，包含 meta, subquestions 等字段
新格式：数组，每个元素代表一个子问题，使用中文key
"""
from __future__ import annotations
import json
import sys
from pathlib import Path


def convert_old_to_new(old_data: dict) -> list[dict]:
    """将旧格式转换为新格式"""
    new_data = []

    meta = old_data.get("meta", {})
    background_info = old_data.get("background_info", "")
    condition_extraction = old_data.get("condition_extraction", {})
    related_knowledge_points = old_data.get("related_knowledge_points", [])
    physical_model = old_data.get("physical_model", "")
    physical_scenario = old_data.get("physical_scenario", "")
    grading_rubric = old_data.get("grading_rubric", [])
    wrong_solution_steps = old_data.get("wrong_solution_steps", [])
    alternative_solutions = old_data.get("alternative_solutions", [])

    for sub in old_data.get("subquestions", []):
        sub_id = sub.get("id", "")
        parts = sub_id.split(".")
        part_letter = parts[0] if len(parts) > 0 else ""

        final_answer = sub.get("final_answer", [])
        n = len(final_answer)

        new_item = {
            "标注基础信息": {
                "来源": meta.get("source", ""),
                "补充": meta.get("supplement", ""),
                "年份": meta.get("year", ""),
                "版块1": meta.get("section_1", ""),
                "版块2": f"Part-{part_letter}" if part_letter else "",
                "版块3": sub_id,
            },
            "题目信息": {
                "背景信息": background_info,
                "上下文": sub.get("context", ""),
                "问题原文": sub.get("original_question", ""),
                "改造后问题": sub.get("reformulated_question", ""),
                "核心思路": sub.get("core_idea", ""),
                "解答过程": sub.get("solution_process", ""),
                "最终答案": final_answer,
                "改造后答案": sub.get("reformulated_answer", [""] * n),
                "答案类型": sub.get("answer_type", [""] * n),
                "改造后答案类型": sub.get("reformulated_answer_type", [""] * n),
                "答案单位": sub.get("answer_unit", [""] * n),
                "改造后答案单位": sub.get("reformulated_answer_unit", [""] * n),
                "关联图片路径": sub.get("related_image_path", [""] * n),
                "模态类型": sub.get("modality_type", [""] * n),
                "难度": sub.get("difficulty", ""),
            },
            "条件提取": {
                "显性条件": condition_extraction.get("explicit_conditions", []),
                "隐性条件": condition_extraction.get("implicit_conditions", []),
            },
            "物理模型": physical_model,
            "关联考点": related_knowledge_points,
            "物理场景": physical_scenario,
            "判分标准": grading_rubric,
            "错误解题步骤": wrong_solution_steps if wrong_solution_steps else ["(暂时不标注)"],
            "多解法标注": alternative_solutions,
        }

        new_data.append(new_item)

    return new_data


def main():
    if len(sys.argv) != 3:
        print("usage: convert_to_new_format.py <old_json> <new_json>")
        sys.exit(1)

    old_path = Path(sys.argv[1])
    new_path = Path(sys.argv[2])

    old_data = json.loads(old_path.read_text(encoding="utf-8"))
    new_data = convert_old_to_new(old_data)

    new_path.write_text(json.dumps(new_data, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"[convert] converted {old_path} to {new_path}")
    print(f"[convert] {len(new_data)} subquestions converted")


if __name__ == "__main__":
    main()
