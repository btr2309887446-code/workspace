"""
split_subquestions.py
读取 work/Q{n}.parsed.json，按 Part 下的 **A.1.** / **A.2.** 等把 Question / Answer
拆成子问题级结构，输出 work/Q{n}.subquestions.json。

v2: 新增 context（完整上文）、per-part 背景信息、grading rubric 提取。
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path


SUBQ_RE = re.compile(
    r"(?:"                                      # alternation wrapper
    # Format 1/2/3: Markdown **A.1** or **A.1.** or **A.1. Title**
    r"\*\*(?P<l>[A-Z])\.(?P<n>\d+)\.?"         # **A.1 or **A.1.
    r"(?:\*\*|[^*]*\*\*)"                       # close bold: immediate ** or ...text**
    r"|"
    # Format 5: LaTeX bold \textbf{A.1} or \textbf{A.1.} or \textbf{A.1 Title} or \textbf{A.1. [pt]}
    r"(?:\\noindent\s+)?"                       # optional \noindent prefix
    r"\\textbf\{(?P<bl>[A-Z])\.(?P<bn>\d+)\.?" # \textbf{A.1 or \textbf{A.1.
    r"[^\}]*\}"                                 # rest of \textbf content until }
    r"|"
    # Format 6: LaTeX item \item[A.1.] or \item[A.1]
    r"\\item\[(?P<il>[A-Z])\.(?P<in>\d+)\.?\]"  # \item[A.1.] or \item[A.1]
    r"|"
    # Format 4: plain A.1. at line start (no bold)
    r"^(?P<pl>[A-C])\.(?P<pn>\d+)\.?\b"        # plain A.1. at line start
    r")"
    r"\s*"                                       # separating whitespace
    r"(?P<text>.*?)"                             # question text (non-greedy)
    r"(?=\n\s*(?:[A-Z]\.\d"                      # until next sub-q marker
    r"|\*\*[A-Z]\.\d"                             # Markdown bold
    r"|\\noindent\s*\\textbf\{[A-Z]\.\d"         # LaTeX bold with noindent
    r"|\\textbf\{[A-Z]\.\d"                      # LaTeX bold
    r"|\\item\[[A-Z]\.\d"                         # LaTeX item
    r")|\Z)",
    re.DOTALL | re.MULTILINE
)
STD_SOL_RE = re.compile(
    r"\*\*\[([A-Z])\.(\d+)'s Standard Solution\]\*\*\s*(.*?)(?=\n\*\*\[[A-Z]\.\d+'s Standard Solution\]\*\*|\Z)",
    re.DOTALL,
)
FINAL_RESULT_RE = re.compile(r"\*\*\[Final Result\]\*\*\s*:\s*(.*?)\s*(?=\n\n|\n---|\n\*\*|$)", re.DOTALL)
IMG_LINE_RE = re.compile(r"^!\[\]\([^)]*\)\s*$", re.MULTILINE)
USEFUL_INFO_RE = re.compile(
    r"\*\*Useful Information[^:]*:\*\*\s*(.*?)(?=\n\n---|\n###|\n\*\*[A-Z]\.|\Z)", re.DOTALL | re.IGNORECASE
)
TABLE_ROW_RE = re.compile(r"^\|.+\|$")
PART_H4_RE = re.compile(r"^####\s+(Part [A-C]:.+)$", re.MULTILINE)


def clean_useful_info(text: str) -> str:
    """提取并清理 Useful Information 块：
    - 去除 **Useful Information:** 标题头
    - 去除每行开头的 *   列表标记"""
    blocks: list[str] = []
    for m in USEFUL_INFO_RE.finditer(text):
        content = m.group(1).strip()
        content = re.sub(r"^\*\s*", "", content, flags=re.MULTILINE)
        content = content.strip()
        if content:
            blocks.append(content)
    return "\n\n".join(blocks) if blocks else ""


def _part_key(title: str) -> str:
    m = re.search(r"Part\s+([A-C])", title, re.IGNORECASE)
    return m.group(1).upper() if m else ""


def extract_background_info_per_part(question_section: dict) -> dict[str, str]:
    """返回 {part_letter: cleaned_background_info}。
    Part A/B 用全局 _intro + 非 Part 节的 Useful Information。
    Part C 用 Part C 专属的 Useful Information for Part C。"""
    result: dict[str, str] = {}
    # 收集全局文本：_intro + 所有非 Part 节（如 Introduction）
    global_text = question_section.get("_intro", "")
    for title, text in question_section.get("parts", {}).items():
        key = _part_key(title)
        if not key:
            global_text += "\n" + text
    global_info = clean_useful_info(global_text)
    for title, text in question_section.get("parts", {}).items():
        key = _part_key(title)
        if not key:
            continue
        part_info = clean_useful_info(text)
        result[key] = part_info if part_info else global_info
    return result


def extract_subquestions(question_section: dict) -> dict[str, dict]:
    """从 Question 节的各 Part 提取子问题原文"""
    subs: dict[str, dict] = {}
    for part_title, part_text in question_section.get("parts", {}).items():
        part_letter = _part_key(part_title)
        if not part_letter:
            continue
        for m in SUBQ_RE.finditer(part_text):
            letter = m.group('l') or m.group('bl') or m.group('il') or m.group('pl')
            idx = m.group('n') or m.group('bn') or m.group('in') or m.group('pn')
            sub_id = f"{letter}.{idx}"
            subs[sub_id] = {
                "id": sub_id,
                "part_title": part_title,
                "part_letter": part_letter,
                "original_question": m.group('text').strip(),
                # record start position in part_text for context extraction
                "_start_in_part": m.start(),
            }
    return subs


def extract_context_text(full_part_text: str, sub_start_pos: int) -> str:
    """提取子问题之前的 part 内文本（Part 导语部分）。"""
    return full_part_text[:sub_start_pos].strip()


def extract_solutions(answer_section: dict) -> dict[str, dict]:
    """从 Answer 节各 Part 抽取每个子问题的 Standard Solution"""
    sols: dict[str, dict] = {}
    for part_title, part_text in answer_section.get("parts", {}).items():
        if not part_title.lower().startswith("part "):
            continue
        clean_text = IMG_LINE_RE.sub("", part_text)
        for m in STD_SOL_RE.finditer(clean_text):
            part_letter, idx, body = m.group(1), m.group(2), m.group(3).strip()
            sub_id = f"{part_letter}.{idx}"
            final = ""
            fm = FINAL_RESULT_RE.search(body)
            if fm:
                final = fm.group(1).strip()
                final = re.sub(r"\n-{3,}.*$", "", final, flags=re.MULTILINE)
                final = re.sub(r"\n{2,}", " ", final)
                final = final.strip()
                if final.startswith("$") and final.endswith("$"):
                    final = final[1:-1].strip()
            sols[sub_id] = {
                "id": sub_id,
                "solution_process": body,
                "final_answer_raw": final,
            }
    return sols


def _strip_md_fmt(text: str) -> str:
    """去除 markdown 加粗标记，保留 LaTeX。"""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = text.replace("<br>", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_rubric_table(table_text: str) -> dict[str, list[str]]:
    """解析 GradingRubric 表格，返回 {sub_id: [rubric_item, ...]}。

    表格格式：| sub-part | item | marks | notes |
    - sub-part 列为空表示延续上一条
    - 输出格式：Award {marks} pt if the answer correctly {item}. Otherwise, award 0 pt.
    """
    result: dict[str, list[str]] = {}
    current_sub: str | None = None
    skip_subtotal = re.compile(r"part [a-c] total|subtotal", re.IGNORECASE)
    lines = table_text.splitlines()

    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        # skip header/separator rows (contain :--- table alignment marks)
        if ":---" in stripped:
            continue

        # Use safe split that handles | inside LaTeX math
        raw_cells = _split_table_row(stripped)

        if len(raw_cells) < 4:
            continue

        # columns: 1=sub-part, 2=item, 3=marks, 4=notes (0-indexed: 1,2,3,4)
        sub_part_cell = raw_cells[1] if len(raw_cells) > 1 else ""
        item_cell = raw_cells[2] if len(raw_cells) > 2 else ""
        marks_cell = raw_cells[3] if len(raw_cells) > 3 else ""

        # Extract sub-part identifier
        sub_match = re.search(r"\*\*([A-C]\.\d+)\*\*", sub_part_cell)
        if sub_match:
            candidate = sub_match.group(1)
            if skip_subtotal.search(item_cell) or skip_subtotal.search(sub_part_cell):
                current_sub = None
                continue
            current_sub = candidate
            if current_sub not in result:
                result[current_sub] = []

        if current_sub is None:
            continue

        # Skip rows that are subtotals within a part (like "Part A Total" in item)
        if skip_subtotal.search(item_cell):
            continue

        # Extract marks
        marks_match = re.search(r"([\d.]+)", marks_cell)
        if not marks_match:
            continue
        marks_str = marks_match.group(1)
        try:
            marks_val = float(marks_str)
        except ValueError:
            continue
        # Skip non-step-score values (subtotal marks like 3.0, 4.0)
        if marks_val > 2.0:
            continue
        if marks_val < 0.05:
            continue

        # Clean item text
        item_clean = _strip_md_fmt(item_cell)
        if not item_clean:
            continue

        rubric_line = f"Award {marks_str} pt if the answer correctly {item_clean[0].lower() + item_clean[1:]}. Otherwise, award 0 pt."
        result[current_sub].append(rubric_line)

    return result


def _split_table_row(line: str) -> list[str]:
    """安全切分表格行，避免 LaTeX 中的 `|` 干扰。"""
    cells: list[str] = []
    current: list[str] = []
    in_math = False
    math_dollar_count = 0
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == '$':
            # detect $$ or $
            if i + 1 < len(line) and line[i + 1] == '$':
                math_dollar_count += 1
                in_math = (math_dollar_count % 2 == 1)
                current.append('$$')
                i += 2
                continue
            else:
                in_math = not in_math
                current.append('$')
                i += 1
                continue
        if ch == '|' and not in_math:
            cells.append(''.join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1
    if current:
        cells.append(''.join(current).strip())
    return cells


def extract_rubric(grading_section: dict) -> dict[str, list[str]]:
    """从 GradingRubric 节提取每个子问题的评分标准。"""
    all_text = grading_section.get("_intro", "")
    for part_text in grading_section.get("parts", {}).values():
        all_text += "\n" + part_text

    # find table blocks between #### Part X: headers
    part_blocks = PART_H4_RE.split(all_text)
    full_table_text = ""
    i = 2  # re.split with capture groups: [text, capture, text, capture, ...]
    while i < len(part_blocks):
        full_table_text += part_blocks[i] + "\n"
        i += 2

    if not full_table_text.strip():
        full_table_text = all_text

    return _parse_rubric_table(full_table_text)


def build(parsed: dict, meta: dict) -> dict:
    question = parsed.get("Question", {"parts": {}})
    answer = parsed.get("Answer", {"parts": {}})
    grading_rubric = parsed.get("GradingRubric", {})

    subs = extract_subquestions(question)
    sols = extract_solutions(answer)
    bg_map = extract_background_info_per_part(question)
    rubric_map = extract_rubric(grading_rubric)

    source = meta.get("source", "ipho")
    year = meta.get("year", "2025")
    section_1 = meta.get("section_1", "Q1")
    problem_number = "".join(c for c in section_1 if c.isdigit()) or "1"

    # image placeholders: Figure 1 before Part A, Figure 2 before B.1
    img1 = f"image/{source}_{year}_{problem_number}_1.png"
    img2 = f"image/{source}_{year}_{problem_number}_2.png"

    question_intro = question.get("_intro", "")
    # 收集所有非 Part 节内容（如 Introduction）
    non_part_text = ""
    for title, text in question.get("parts", {}).items():
        if not _part_key(title):
            non_part_text += "\n" + text
    question_intro_full = (question_intro + non_part_text).strip()

    merged = []
    for sub_id in sorted(subs.keys()):
        entry = subs[sub_id]
        part_letter = entry.get("part_letter", "")
        part_title = entry.get("part_title", "")
        sol = sols.get(sub_id, {})
        entry["solution_process"] = sol.get("solution_process", "")
        entry["final_answer_raw"] = sol.get("final_answer_raw", "")

        # build context
        context_parts: list[str] = []
        if int(sub_id[2:]) == 1:  # first subquestion in part
            if sub_id.startswith("A"):
                context_parts.append(question_intro_full)
                context_parts.append(f"---\n\n![]({img1})")
            elif sub_id.startswith("B"):
                context_parts.append(f"---\n\n![]({img2})")
            else:
                context_parts.append("---")
            context_parts.append(f"### {part_title}")
            part_text = question.get("parts", {}).get(part_title, "")
            header = extract_context_text(part_text, entry.get("_start_in_part", 0))
            if header:
                context_parts.append(header)
            entry["context"] = "\n\n".join(context_parts)
        else:
            entry["context"] = ""

        # per-part background info
        entry["background_info"] = bg_map.get(part_letter, "")

        # per-subquestion rubric
        entry["rubric_items"] = rubric_map.get(sub_id, [])

        merged.append(entry)

    # grading text (for LLM use)
    grading_text = grading_rubric.get("_intro", "")
    for part_text in grading_rubric.get("parts", {}).values():
        if grading_text:
            grading_text += "\n\n" + part_text
        else:
            grading_text = part_text

    return {
        "subquestions": merged,
        "grading_rubric_text": grading_text,
    }


def main():
    if len(sys.argv) < 3:
        print("usage: split_subquestions.py <parsed.json> <out.json> [meta.json]")
        sys.exit(1)
    parsed = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    meta = {}
    if len(sys.argv) >= 4:
        meta = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
    out = build(parsed, meta)
    Path(sys.argv[2]).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[split_subquestions] wrote {sys.argv[2]} ({len(out['subquestions'])} subquestions)")


if __name__ == "__main__":
    main()
