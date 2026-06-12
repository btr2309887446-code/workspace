"""
sync_image_refs_new.py
把生成的图片路径按规范格式 ![](image/xxx.png) 写入：
  1. Q{n}_problem.md 的 DiagramCode 节前（若尚未插入同名引用）；
  2. Q{n}_problem.json 的第一个子问题的关联图片路径和模态类型。

新格式：JSON是数组格式，完全对齐最终模板.json。
"""
from __future__ import annotations
import json
import sys
from pathlib import Path


def inject_md(md_path: Path, rel_img: str) -> None:
    text = md_path.read_text(encoding="utf-8")
    placeholder = f"![]({rel_img})"
    if placeholder in text:
        return
    marker = "# DiagramCode"
    if marker in text:
        text = text.replace(marker, f"{placeholder}\n\n{marker}", 1)
    else:
        text = text.rstrip() + f"\n\n{placeholder}\n"
    md_path.write_text(text, encoding="utf-8")


def inject_json(json_path: Path, rel_img: str) -> None:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        return

    first = data[0]
    qi = first.get("题目信息", {})
    paths = qi.get("关联图片路径", [])
    if not paths:
        qi["关联图片路径"] = [rel_img]
    else:
        paths[0] = rel_img

    # 更新模态类型：有图片时调用 infer_modality 精确判断
    question_text = qi.get("问题原文", "")
    try:
        from fill_annotation_new import infer_modality
        modality = infer_modality(question_text, has_image=True, image_paths=[rel_img])
    except ImportError:
        modality = "text+illustration figure"
    
    qi["模态类型"] = [modality]

    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def main():
    if len(sys.argv) != 4:
        print("usage: sync_image_refs_new.py <md_path> <json_path> <relative_image_path>")
        sys.exit(1)
    inject_md(Path(sys.argv[1]), sys.argv[3])
    inject_json(Path(sys.argv[2]), sys.argv[3])
    print(f"[sync_image_refs_new] injected {sys.argv[3]} into md & json")


if __name__ == "__main__":
    main()
