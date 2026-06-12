"""
parse_md.py
解析 Q{n}_problem.md 的层级结构，输出 work/Q{n}.parsed.json。
规则：
  - 顶层节以 `# ` 开头，但仅识别白名单中的 section 名，避免把题目标题等误判为 section。
    白名单：Question / Answer / DiagramCode / QuestionReview / AnswerValidation / GradingRubric
  - 二级节以 `### ` 或 `**Part X: ...**` 开头（Part A / Part B / ...）
  - 其余 `# Xxx` 行按普通文本处理，保留在当前 section 的正文中
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path


H1 = re.compile(r"^#\s+(.+?)\s*$")
H3 = re.compile(r"^###\s+(.+?)\s*$")
BOLD_PART = re.compile(r"^\*\*Part\s+[A-C].*\*\*\s*$")
LATEX_PART = re.compile(r"^\\(?:textbf\{Part|section\*\{Part)\s+[A-C]")

TOP_SECTION_WHITELIST = {
    "Question",
    "Answer",
    "DiagramCode",
    "QuestionReview",
    "AnswerValidation",
    "GradingRubric",
}


def _match_top_section(line: str) -> str | None:
    m = H1.match(line)
    if not m:
        return None
    name = m.group(1).strip()
    # 只有白名单 section 才作为顶层节切分
    first_token = name.split()[0] if name else ""
    if name in TOP_SECTION_WHITELIST or first_token in TOP_SECTION_WHITELIST:
        return name
    return None


def parse_md(md_text: str) -> dict:
    sections: dict[str, dict] = {}
    current_h1: str | None = None
    current_h3: str | None = None
    buf: list[str] = []

    def flush():
        if current_h1 is None:
            return
        text = "\n".join(buf).strip()
        if current_h3:
            sections.setdefault(current_h1, {"_intro": "", "parts": {}})
            sections[current_h1]["parts"][current_h3] = text
        else:
            sections.setdefault(current_h1, {"_intro": "", "parts": {}})
            if not sections[current_h1]["_intro"]:
                sections[current_h1]["_intro"] = text
            else:
                sections[current_h1]["_intro"] += "\n" + text

    for line in md_text.splitlines():
        top = _match_top_section(line)
        m3 = H3.match(line)
        bp = BOLD_PART.match(line)
        lp = LATEX_PART.match(line)
        if top is not None:
            flush()
            buf = []
            current_h1 = top
            current_h3 = None
            continue
        if m3:
            flush()
            buf = []
            current_h3 = m3.group(1).strip().strip("* ")
            continue
        if bp:
            flush()
            buf = []
            current_h3 = bp.group(0).strip().strip("* ").rstrip("*").strip()
            continue
        if lp:
            flush()
            buf = []
            # Extract part title from \textbf{Part A: ...} or \section*{Part A: ...}
            raw = lp.group(0)
            current_h3 = re.sub(r'^\\(?:textbf\{|section\*\{)', 'Part ', raw).rstrip('}')
            current_h3 = re.sub(r'\}\s*$', '', current_h3)
            continue
        buf.append(line)
    flush()
    return sections


def main():
    if len(sys.argv) != 3:
        print("[parse_md] usage: parse_md.py <in.md> <out.json>")
        sys.exit(1)
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    dst.parent.mkdir(parents=True, exist_ok=True)
    parsed = parse_md(src.read_text(encoding="utf-8"))
    dst.write_text(json.dumps(parsed, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[parse_md] wrote {dst}")


if __name__ == "__main__":
    main()
