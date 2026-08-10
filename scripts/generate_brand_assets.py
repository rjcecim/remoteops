"""Utilitários da marca PSExecGUI.

Os masters visuais em assets/ são a identidade premium (glassmorphism).
Este script apenas re-exporta tamanhos / ICO a partir de app_icon.png e
app_mark.png — não redesenha a marca do zero.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"


def to_ico(src: Image.Image, dest: Path) -> None:
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    master = src.resize((256, 256), Image.Resampling.LANCZOS)
    master.save(dest, format="ICO", sizes=sizes)


def main() -> None:
    icon_path = ASSETS / "app_icon.png"
    mark_path = ASSETS / "app_mark.png"
    if not icon_path.is_file():
        raise SystemExit(f"Master ausente: {icon_path}")

    icon = Image.open(icon_path).convert("RGBA")
    # Garante 512px no master PNG
    if icon.size != (512, 512):
        icon = icon.resize((512, 512), Image.Resampling.LANCZOS)
        icon.save(icon_path, "PNG", optimize=True)

    if mark_path.is_file():
        mark = Image.open(mark_path).convert("RGBA")
        if mark.size != (512, 512):
            mark = mark.resize((512, 512), Image.Resampling.LANCZOS)
            mark.save(mark_path, "PNG", optimize=True)

    to_ico(icon, ASSETS / "icon.ico")
    print("ICO atualizado a partir de app_icon.png")
    for path in sorted(ASSETS.glob("app_*.png")) + [ASSETS / "icon.ico"]:
        if path.exists():
            print(f"  {path.name:16s} {path.stat().st_size:8d} bytes")


if __name__ == "__main__":
    main()
