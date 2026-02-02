#!/usr/bin/env python3
"""
将「地震学学术周报.md」转为格式美化的 PDF。

字体：与 Cursor 编辑器一致，系统无衬线（Mac 上为苹方 + SF Pro），在 report.css 中设置；
使用 weasyprint 或浏览器打印时自动生效；使用 xelatex 时需在命令行传入对应变量。

用法:
  python md_to_pdf.py

若已安装 weasyprint，会直接生成 地震学学术周报.pdf；
否则会生成 HTML，并提示用浏览器打开后「打印 → 另存为 PDF」。
"""
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
MD = BASE / "地震学学术周报.md"
HTML = BASE / "地震学学术周报.html"
PDF = BASE / "地震学学术周报.pdf"
CSS = BASE / "report.css"


def run(cmd, check=True):
    return subprocess.run(cmd, cwd=BASE, check=check, capture_output=True, text=True)


def main():
    if not MD.exists():
        print(f"未找到: {MD}")
        sys.exit(1)

    # 1. 用 pandoc 生成 HTML（若尚未生成，或 MD/CSS 有更新）
    if not HTML.exists() or MD.stat().st_mtime > HTML.stat().st_mtime or (CSS.exists() and CSS.stat().st_mtime > HTML.stat().st_mtime):
        run([
            "pandoc", str(MD),
            "-f", "markdown", "-t", "html5",
            "--standalone", "--metadata", "title=「地震起止过程与机理」学术周报",
            f"--css={CSS.name}", "-o", str(HTML),
        ])
        print(f"已生成: {HTML}")

    # 2. 尝试用 weasyprint 转 PDF
    try:
        import weasyprint
        weasyprint.HTML(filename=str(HTML)).write_pdf(str(PDF))
        print(f"已生成: {PDF}")
        return
    except ImportError:
        pass

    # 3. 尝试 pandoc + weasyprint 命令行
    r = subprocess.run(
        [
            "pandoc", str(MD),
            "-f", "markdown", "-t", "html5",
            "--standalone", "--metadata", "title=「地震起止过程与机理」学术周报",
            f"--css={CSS.name}", "-o", str(PDF),
            "--pdf-engine=weasyprint",
        ],
        cwd=BASE, capture_output=True, text=True,
    )
    if r.returncode == 0:
        print(f"已生成: {PDF}")
        return

    # 4. 无法生成 PDF 时，提示用户
    print("当前环境无法直接生成 PDF（未检测到 weasyprint）。")
    print()
    print("请任选其一：")
    print("  1. 用浏览器打开「地震学学术周报.html」，按 Cmd+P（Mac）或 Ctrl+P（Win），")
    print("     选择「另存为 PDF」；建议在打印选项中勾选「背景图形」、边距选「默认」。")
    print("  2. 安装 weasyprint 后重新运行本脚本：")
    print("     pip install weasyprint")
    print("  3. 若已安装 MacTeX，可用 pandoc 直接生成 PDF（与编辑器一致：苹方）：")
    print("     pandoc 地震学学术周报.md -o 地震学学术周报.pdf --pdf-engine=xelatex \\")
    print("       -V mainfont='PingFang SC' -V CJKmainfont='PingFang SC'")


if __name__ == "__main__":
    main()
