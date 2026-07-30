#!/usr/bin/env python3
"""
Render LaTeX equations to transparent PNG images for embedding in PPTX.

Usage:
    # Auto-detect best engine (tries pdflatex first, falls back to matplotlib)
    python scripts/render_latex.py "E = mc^2" -o figures/eq_energy.png

    # Force matplotlib (no LaTeX installation required)
    python scripts/render_latex.py "\\frac{\\partial L}{\\partial \\theta}" -o eq.png --engine matplotlib

    # Force pdflatex (higher quality, needs texlive/miktex)
    python scripts/render_latex.py "\\sum_{i=1}^{N} x_i" -o eq.png --engine pdflatex

    # Adjust size and resolution
    python scripts/render_latex.py "\\alpha + \\beta" -o eq.png --fontsize 32 --dpi 400
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def render_with_matplotlib(latex: str, output: str, fontsize: int = 24, dpi: int = 300):
    """Render using matplotlib mathtext. No LaTeX installation required."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(0.01, 0.01))
    ax.axis("off")
    ax.text(
        0.5, 0.5, f"${latex}$",
        fontsize=fontsize, ha="center", va="center",
        transform=ax.transAxes,
    )
    fig.savefig(
        output, dpi=dpi, bbox_inches="tight",
        pad_inches=0.15, transparent=True,
    )
    plt.close(fig)
    print(f"Rendered (matplotlib): {output}")


def render_with_pdflatex(latex: str, output: str, dpi: int = 300):
    """Render using pdflatex + pdftoppm. Requires texlive/miktex + poppler."""
    tmpdir = tempfile.mkdtemp()
    try:
        tex_path = Path(tmpdir) / "eq.tex"
        tex_content = (
            "\\documentclass[border=2pt]{standalone}\n"
            "\\usepackage{amsmath,amssymb,amsfonts}\n"
            "\\begin{document}\n"
            f"$\\displaystyle {latex}$\n"
            "\\end{document}\n"
        )
        tex_path.write_text(tex_content)

        subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "eq.tex"],
            cwd=tmpdir, capture_output=True, check=True,
        )

        pdf_path = Path(tmpdir) / "eq.pdf"
        subprocess.run(
            ["pdftoppm", "-png", "-r", str(dpi), "-singlefile",
             str(pdf_path), str(Path(tmpdir) / "eq")],
            capture_output=True, check=True,
        )

        shutil.copy(Path(tmpdir) / "eq.png", output)
        print(f"Rendered (pdflatex): {output}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(
        description="Render LaTeX equation to transparent PNG",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("latex", help="LaTeX math string (without $ delimiters)")
    parser.add_argument("-o", "--output", required=True, help="Output PNG path")
    parser.add_argument("--dpi", type=int, default=300, help="Resolution (default: 300)")
    parser.add_argument(
        "--fontsize", type=int, default=24,
        help="Font size for matplotlib engine (default: 24, ignored by pdflatex)",
    )
    parser.add_argument(
        "--engine", choices=["auto", "matplotlib", "pdflatex"], default="auto",
        help="Rendering engine (default: auto — tries pdflatex, falls back to matplotlib)",
    )
    args = parser.parse_args()

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)

    if args.engine == "pdflatex":
        render_with_pdflatex(args.latex, args.output, args.dpi)
    elif args.engine == "matplotlib":
        render_with_matplotlib(args.latex, args.output, args.fontsize, args.dpi)
    else:  # auto
        try:
            subprocess.run(
                ["pdflatex", "--version"], capture_output=True, check=True,
            )
            render_with_pdflatex(args.latex, args.output, args.dpi)
        except (subprocess.CalledProcessError, FileNotFoundError):
            print("pdflatex not available, falling back to matplotlib", file=sys.stderr)
            render_with_matplotlib(args.latex, args.output, args.fontsize, args.dpi)


if __name__ == "__main__":
    main()
