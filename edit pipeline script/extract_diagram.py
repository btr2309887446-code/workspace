"""
extract_diagram.py
从 Q{n}_problem.md 的 DiagramCode 节提取第一个 ```python ... ``` 代码块，
保存到 work/Q{n}.diagram.py。
"""
from __future__ import annotations
import re
import sys
from pathlib import Path


CODE_RE = re.compile(r"```python\s*\n(.*?)```", re.DOTALL)


def main():
    if len(sys.argv) != 3:
        print("usage: extract_diagram.py <in.md> <out.py>")
        sys.exit(1)
    md = Path(sys.argv[1]).read_text(encoding="utf-8")
    m = CODE_RE.search(md)
    if not m:
        print("[extract_diagram] no python code block found, skip.")
        return
    code = m.group(1)
    Path(sys.argv[2]).parent.mkdir(parents=True, exist_ok=True)
    Path(sys.argv[2]).write_text(code, encoding="utf-8")
    print(f"[extract_diagram] wrote {sys.argv[2]}")


if __name__ == "__main__":
    main()
