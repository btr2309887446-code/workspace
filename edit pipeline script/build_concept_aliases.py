"""
build_concept_aliases.py
扫描 IPHO考纲_分类标签.json 的 168 个 Level-3 叶子节点，按规则自动派生多重别名，
输出 scripts/_concept_aliases_auto.py（被 fill_annotation_new.py 在启动时合并）。

派生规则（纯规则，无 LLM）：
1. 原文（去首尾空白、统一引号）
2. 去括号注释： "Phase transitions (boiling, ...)" → "Phase transitions"
3. 去冒号副句： "Sound waves: speed as a function of ..." → "Sound waves"
4. 去常见前导插入语： "Concept of heat conductivity" → "heat conductivity"
5. 形态归一： 去 "'s" / 标点 / 多空格
6. 同义词扩展（按白名单替换）：
     "law" ↔ "principle" 之类高频映射
7. 输出键 = 全小写、去停用词的形式

设计目标：让 168 个 Level-3 中至少 95% 至少有 1 条别名能被关键词匹配到。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAXONOMY_PATH = ROOT / "IPHO考纲_分类标签.json"
OUTPUT_PATH = ROOT / "scripts" / "_concept_aliases_auto.py"


_PREFIXES_TO_STRIP = (
    "concept of ",
    "concepts of ",
    "approximation of ",
    "construction of ",
    "finding ",
    "using these laws for ",
    "addition of ",
    "boundary conditions for ",
    "recognition of the cases when ",
)


_SYNONYM_RULES: list[tuple[str, str]] = [
    ("polarisation", "polarization"),
    ("polarisers", "polarizers"),
    ("vapour", "vapor"),
    ("centre", "center"),
    ("colour", "color"),
    ("modelling", "modeling"),
]


_STOP_WORDS_FOR_KEY = {
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or",
    "is", "are", "was", "were", "be", "as", "by", "with", "from", "its",
    "it", "no", "not", "can", "may", "has", "have", "that", "this", "these",
    "those", "also", "only", "both", "other", "some", "into", "over", "under",
    "between", "through", "due", "via", "per", "used", "using", "does",
    "need", "needed", "known", "such",
}


def _normalize_to_key(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s\-]", " ", text)
    text = text.replace("-", " ")
    words = [w for w in text.split() if w and w not in _STOP_WORDS_FOR_KEY]
    return " ".join(words)


def _strip_parens(text: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*", " ", text).strip()


def _strip_after_colon(text: str) -> str:
    if ":" in text:
        return text.split(":", 1)[0].strip()
    return text


def _strip_prefixes(text: str) -> str:
    low = text.lower()
    for pre in _PREFIXES_TO_STRIP:
        if low.startswith(pre):
            return text[len(pre):]
    return text


def _apply_synonyms(text: str) -> list[str]:
    variants = [text]
    for src, dst in _SYNONYM_RULES:
        new_variants = []
        for v in variants:
            if src in v.lower():
                new_variants.append(re.sub(src, dst, v, flags=re.IGNORECASE))
            if dst in v.lower():
                new_variants.append(re.sub(dst, src, v, flags=re.IGNORECASE))
        variants.extend(new_variants)
    seen = set()
    out = []
    for v in variants:
        k = v.lower().strip()
        if k and k not in seen:
            seen.add(k)
            out.append(v)
    return out


def derive_aliases(l3_text: str) -> list[str]:
    """从单个 Level-3 文本派生多种别名形式（保留原始大小写）"""
    raw_forms: list[str] = [l3_text]

    no_paren = _strip_parens(l3_text)
    if no_paren and no_paren != l3_text:
        raw_forms.append(no_paren)

    no_colon = _strip_after_colon(no_paren)
    if no_colon and no_colon not in raw_forms:
        raw_forms.append(no_colon)

    for f in list(raw_forms):
        s = _strip_prefixes(f)
        if s and s not in raw_forms:
            raw_forms.append(s)

    # apply synonym swaps
    expanded: list[str] = []
    for f in raw_forms:
        expanded.extend(_apply_synonyms(f))

    # de-dup, preserving order
    seen = set()
    out: list[str] = []
    for f in expanded:
        if not f.strip():
            continue
        if f.lower() in seen:
            continue
        seen.add(f.lower())
        out.append(f.strip())
    return out


def build_alias_table() -> dict[str, tuple[str, str, str]]:
    """扫考纲，输出 {alias_key_lowercase_normalized: (L1, L2, L3)} 映射"""
    taxonomy = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
    table: dict[str, tuple[str, str, str]] = {}
    skipped: list[str] = []

    for cat in taxonomy.get("categories", []):
        l1 = cat.get("level1", "")
        for sub in cat.get("subcategories", []):
            l2 = sub.get("level2", "")
            for l3 in sub.get("topics", []):
                canonical = (l1, l2, l3)
                aliases = derive_aliases(l3)
                for alias in aliases:
                    key = _normalize_to_key(alias)
                    if not key or len(key.split()) < 1:
                        continue
                    # 单字关键词太通用，跳过 (e.g. "Work" / "Heat" / "Entropy" 仍保留单词)
                    if len(key) <= 2:
                        continue
                    if key in table:
                        # 第一个胜出（保留更具体的；考纲遍历顺序自然给优先级）
                        continue
                    table[key] = canonical
            if not sub.get("topics"):
                skipped.append(f"{l1} > {l2}")

    return table


def render_python_module(table: dict[str, tuple[str, str, str]]) -> str:
    lines = [
        '"""Auto-generated concept aliases from IPhO syllabus.',
        '   DO NOT EDIT BY HAND — regenerate via scripts/build_concept_aliases.py',
        '"""',
        "from __future__ import annotations",
        "",
        "CONCEPT_ALIASES_AUTO: dict[str, tuple[str, str, str]] = {",
    ]
    for key in sorted(table.keys()):
        l1, l2, l3 = table[key]
        # escape any quote
        ek = key.replace('"', '\\"')
        el1 = l1.replace('"', '\\"')
        el2 = l2.replace('"', '\\"')
        el3 = l3.replace('"', '\\"')
        lines.append(f'    "{ek}": ("{el1}", "{el2}", "{el3}"),')
    lines.append("}")
    lines.append("")
    lines.append(f"# Total entries: {len(table)}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    table = build_alias_table()
    code = render_python_module(table)
    OUTPUT_PATH.write_text(code, encoding="utf-8")
    print(f"[build_concept_aliases] wrote {len(table)} aliases -> {OUTPUT_PATH}")

    # Coverage diagnostic
    taxonomy = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
    all_l3: set[tuple[str, str, str]] = set()
    for cat in taxonomy.get("categories", []):
        for sub in cat.get("subcategories", []):
            for l3 in sub.get("topics", []):
                all_l3.add((cat["level1"], sub["level2"], l3))
    covered = set(table.values())
    missing = all_l3 - covered
    print(f"[build_concept_aliases] L3 leaves covered: {len(covered)} / {len(all_l3)}")
    if missing:
        print("[build_concept_aliases] MISSING (need manual aliases):")
        for m in sorted(missing):
            print(f"   - {m[0]} > {m[1]} > {m[2]}")


if __name__ == "__main__":
    main()
