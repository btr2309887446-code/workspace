"""
render_diagram.py
执行 work/Q{n}.diagram.py 并按规范命名生成图片。

- 主要目标文件：<out_img>（一般是 image/{source}_{year}_{n}_1.png）
- 若脚本硬编码 savefig('Figure_1.png'/...)，我们会把生成的 PNG 按出现顺序重命名为
  image/{prefix}_{i}.png 放回 image/ 目录，i 从 1 起。
- 流程保证：如果至少产生了一张 png，就把第一张拷贝到 <out_img>，让 sync_image_refs 能工作。

Windows GBK 兼容：所有子进程 I/O 使用 UTF-8。
"""
from __future__ import annotations
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def _safe_print(msg: str) -> None:
    try:
        print(msg)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))


def _list_pngs(d: Path) -> set[Path]:
    if not d.exists():
        return set()
    return {p.resolve() for p in d.rglob("*.png")}


def main():
    if len(sys.argv) != 4:
        print("usage: render_diagram.py <script.py> <out_image_path> <cwd>")
        sys.exit(1)
    script = Path(sys.argv[1]).resolve()
    out_img = Path(sys.argv[2]).resolve()
    cwd = Path(sys.argv[3]).resolve()
    out_img.parent.mkdir(parents=True, exist_ok=True)

    before = _list_pngs(cwd)

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["MPLBACKEND"] = "Agg"

    cmd = [sys.executable, str(script), str(out_img)]
    _safe_print("[render_diagram] running: " + " ".join(cmd))
    try:
        res = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            timeout=180, env=env, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        _safe_print("[render_diagram] TIMEOUT, aborting.")
        sys.exit(1)

    if res.stdout:
        _safe_print(res.stdout)
    if res.returncode != 0:
        _safe_print("[render_diagram] STDERR:\n" + (res.stderr or ""))
        # 不直接退出——有些脚本可能部分成功。但若完全没 png 产出，下面会自然报错。

    # 先看目标位置是否已直接命中（A 型脚本）
    after = _list_pngs(cwd)
    new_pngs = sorted(after - before, key=lambda p: str(p))

    # 推导命名前缀，例如 image/ipho_2025_1_1.png -> ipho_2025_1
    stem = out_img.stem  # ipho_2025_1_1
    m = re.match(r"^(.+)_(\d+)$", stem)
    prefix = m.group(1) if m else stem  # ipho_2025_1

    picked_src: Path | None = None
    if out_img.exists():
        _safe_print(f"[render_diagram] ok: {out_img}")
    elif new_pngs:
        picked_src = new_pngs[0]
        shutil.copyfile(picked_src, out_img)
        _safe_print(f"[render_diagram] fallback: copied {picked_src.name} -> {out_img}")
    else:
        _safe_print(
            f"[render_diagram] WARNING: no PNG produced. STDERR:\n{res.stderr or '(empty)'}"
        )
        return

    # 若脚本产生了多张图（例如 Figure_1.png / Figure_2.png），按顺序额外落地
    # image/{prefix}_2.png、image/{prefix}_3.png ...
    used = {out_img.resolve()}
    if picked_src is not None:
        used.add(picked_src.resolve())
    extra = [p for p in new_pngs if p.resolve() not in used]
    for i, p in enumerate(extra, start=2):
        target = out_img.parent / f"{prefix}_{i}.png"
        try:
            shutil.copyfile(p, target)
            _safe_print(f"[render_diagram] extra image: {p.name} -> {target}")
        except Exception as exc:  # pragma: no cover
            _safe_print(f"[render_diagram] failed to copy extra image {p}: {exc}")


if __name__ == "__main__":
    main()
