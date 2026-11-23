from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image, ImageChops, ImageDraw

from .common import PDFWorkError, write_json


def render_page(path, number, dpi=144, renderer="mupdf"):
    if renderer == "mupdf":
        with pymupdf.open(path) as document:
            pix = document[number - 1].get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    executable = shutil.which("pdftoppm")
    if not executable:
        raise PDFWorkError("Poppler pdftoppm is not installed or is not on PATH")
    with tempfile.TemporaryDirectory(prefix="pdfwork-render-") as directory:
        prefix = Path(directory) / "page"
        result = subprocess.run([executable, "-f", str(number), "-l", str(number), "-r", str(dpi),
                                 "-png", "-singlefile", str(Path(path).resolve()), str(prefix)],
                                capture_output=True, timeout=180)
        if result.returncode:
            raise PDFWorkError(result.stderr.decode("utf-8", errors="replace"))
        with Image.open(prefix.with_suffix(".png")) as image:
            return image.convert("RGB").copy()


def compare(first, second, report_path, dpi=144, renderer="both", diff_dir=None):
    report = {"first": str(Path(first).resolve()), "second": str(Path(second).resolve()), "dpi": dpi,
              "identical": True, "engines": {}}
    with pymupdf.open(first) as a, pymupdf.open(second) as b:
        if len(a) != len(b):
            report.update(identical=False, error="Page counts differ", first_pages=len(a), second_pages=len(b))
            write_json(report_path, report); return report
        count = len(a)
    engines = ["mupdf", "poppler"] if renderer == "both" else [renderer]
    for engine in engines:
        pages = []
        for number in range(1, count + 1):
            a, b = render_page(first, number, dpi, engine), render_page(second, number, dpi, engine)
            if a.size != b.size:
                item = {"page": number, "identical": False, "error": "Rendered dimensions differ", "first_size": a.size, "second_size": b.size}
            else:
                difference = ImageChops.difference(a, b)
                arr = np.asarray(difference)
                mask = arr.max(axis=2) != 0
                changed = int(np.count_nonzero(mask))
                item = {"page": number, "size": a.size, "identical": changed == 0,
                        "changed_pixels": changed, "total_pixels": a.width*a.height,
                        "max_channel_delta": int(arr.max()),
                        "rms_channel_delta": float(np.sqrt(np.mean(arr.astype(np.float32)**2)))}
                if changed and diff_dir:
                    path = Path(diff_dir); path.mkdir(parents=True, exist_ok=True)
                    difference.save(path / f"{engine}-{number:03d}-diff.png")
            pages.append(item)
            report["identical"] &= item["identical"]
        report["engines"][engine] = pages
    write_json(report_path, report)
    return report


def contact_sheet(path, output, dpi=72, columns=3):
    with pymupdf.open(path) as doc:
        count = len(doc)
    images = [render_page(path, n, dpi) for n in range(1, count + 1)]
    cellw, cellh = 480, 390
    canvas = Image.new("RGB", (cellw*columns, cellh*math.ceil(count/columns)), "#e6e9ee")
    draw = ImageDraw.Draw(canvas)
    for i, image in enumerate(images):
        image.thumbnail((cellw - 24, cellh - 44))
        x, y = (i % columns)*cellw + 12, (i // columns)*cellh + 28
        canvas.paste(image, (x, y))
        draw.text((x, y - 20), f"Page {i+1}", fill="#182330")
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output)

