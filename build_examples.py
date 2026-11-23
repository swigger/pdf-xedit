"""Reproduce the acceptance artifacts. Run in D:\\tmp\\f after setup.ps1."""
from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
import pikepdf
import pymupdf
import reportlab
from fontTools.ttLib import TTFont
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont as RLFont
from reportlab.pdfgen import canvas

from pdfwork.common import configure_stdio, read_json, write_json
from pdfwork.fonts import download, resolve_font
from pdfwork.project import compile_project, decompile
from pdfwork.verify import compare, contact_sheet, render_page


def change(project, original, **settings):
    for path in sorted((project / "pages").glob("*.json")):
        data = read_json(path)
        for line in data["lines"]:
            if line["text"] == original:
                line.update(settings); write_json(path, data); return line
    raise RuntimeError(f"Text not found: {original}")


def local_change(before, after, box, report):
    results = {}
    with pymupdf.open(before) as doc:
        height, count = doc[0].rect.height, len(doc)
    for engine in ("mupdf", "poppler"):
        a, b = np.array(render_page(before, 1, 144, engine)), np.array(render_page(after, 1, 144, engine))
        changed = np.any(a != b, axis=2)
        x0, y0, x1, y1 = box
        outside = changed.copy()
        outside[int((height-y1)*2):int((height-y0)*2), int(x0*2):int(x1*2)] = False
        assert not outside.any(), (engine, "Unexpected pixels changed outside edited box")
        assert changed.any()
        other_pages = []
        for number in range(2, count+1):
            left, right = render_page(before, number, 144, engine), render_page(after, number, 144, engine)
            assert left.tobytes() == right.tobytes()
            other_pages.append(number)
        results[engine] = {"changed_pixels_page1": int(changed.sum()), "outside_box_changed_pixels": int(outside.sum()),
                           "unchanged_pages": other_pages}
    write_json(report, results)
    return results


def main():
    configure_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="examples")
    parser.add_argument("--source", default="a.pdf")
    parser.add_argument("--skip-online", action="store_true")
    args = parser.parse_args()
    root = Path(args.output).resolve(); source = Path(args.source).resolve()
    if root.exists() and any(root.iterdir()):
        raise SystemExit("Use a new, empty --output directory; existing examples are never deleted")
    root.mkdir(parents=True, exist_ok=True)
    reports = root / "reports"; reports.mkdir()
    base = root / "a-project"
    manifest = decompile(source, base)
    roundtrip = root / "01-roundtrip.pdf"; compile_project(base, roundtrip)
    for dpi in (144, 300):
        result = compare(source, roundtrip, reports/f"roundtrip-{dpi}.json", dpi=dpi)
        assert result["identical"]
    print("11-page round trip: both engines, 144 and 300 DPI passed", flush=True)

    digits = root / "digits-project"; shutil.copytree(base, digits)
    change(digits, "2015年10月8日", text="2026年11月8日")
    digit_pdf = root / "02-digits.pdf"; result = compile_project(digits, digit_pdf)
    assert not result["fonts_added"]
    local_change(source, digit_pdf, [480, 70, 650, 125], reports/"digits-region.json")

    chinese = root / "chinese-project"; shutil.copytree(base, chinese)
    target = change(chinese, "项目介绍", text="项目总结")
    resolution = resolve_font(chinese, target["font"], "项目总结")
    write_json(reports/"system-font.json", resolution)
    chinese_pdf = root / "03-chinese.pdf"; compile_project(chinese, chinese_pdf)
    local_change(source, chinese_pdf, [245, 235, 495, 310], reports/"chinese-region.json")

    box = root / "textbox-project"; shutil.copytree(chinese, box)
    change(box, "项目总结", text="项目总结与展望", width=400, align="center", offset_x=-100)
    box_pdf = root / "04-textbox.pdf"; compile_project(box, box_pdf)
    local_change(source, box_pdf, [150, 235, 570, 310], reports/"textbox-region.json")
    print("Sample edits: original glyphs, validated CJK extension, longer centered box passed", flush=True)

    assets = root / "fixture-assets"; assets.mkdir()
    vera = Path(reportlab.__file__).parent / "fonts/Vera.ttf"
    shutil.copyfile(vera.parent/"bitstream-vera-license.txt", assets/"bitstream-vera-license.txt")
    font = TTFont(vera)
    for record in font["name"].names:
        if record.nameID in (1, 4, 6, 16):
            record.string = "PDFWorkWidthFixture".encode(record.getEncoding())
    cmap = font.getBestCmap()
    for c, width in (("1", 650), ("8", 1900)):
        glyph = cmap[ord(c)]; font["hmtx"][glyph] = width, font["hmtx"][glyph][1]
    fontfile = assets / "variable-width.ttf"; font.save(fontfile)
    pdfmetrics.registerFont(RLFont("PDFWorkWidthFixture", str(fontfile)))
    def fixture(path, text):
        c = canvas.Canvas(str(path), pagesize=(500, 200))
        c.setFillColorRGB(.96, .97, .99); c.rect(0, 0, 500, 200, fill=1, stroke=0)
        c.setFillColorRGB(.08, .15, .26); c.setFont("Helvetica", 12)
        c.drawString(40, 160, "Variable digit advances: 1 is narrow; 8 is wide")
        c.setFont("PDFWorkWidthFixture", 32); c.drawString(40, 100, text)
        c.setFont("Helvetica", 11); c.drawString(40, 45, "The following X must move by the accumulated width difference.")
        c.save()
    fixture(root/"05-variable-original.pdf", "111 X")
    fixture(root/"05-variable-expected.pdf", "888 X")
    variable = root / "variable-project"; decompile(root/"05-variable-original.pdf", variable)
    change(variable, "111 X", text="888 X", width=400)
    compile_project(variable, root/"05-variable-edited.pdf")
    variable_report = compare(root/"05-variable-expected.pdf", root/"05-variable-edited.pdf", reports/"variable-golden.json")
    assert variable_report["identical"]

    if not args.skip_online:
        response = download("https://fonts.googleapis.com/css2?"+urlencode({"family": "ABeeZee", "text": "ABCabc123"}))
        url = re.search(r"url\((https://fonts\.gstatic\.com/[^)]+)\)", response.text)[1]
        data = download(url).content
        doc = pymupdf.open(); page = doc.new_page(width=500, height=200)
        page.insert_font(fontname="Demo", fontbuffer=data)
        page.insert_text((40, 100), "ABCabc123", fontname="Demo", fontsize=32)
        original = root/"06-google-original.pdf"; doc.save(original); doc.close()
        project = root/"google-project"; decompile(original, project)
        target = change(project, "ABCabc123", text="ABCXYZ123", width=400)
        resolution = resolve_font(project, target["font"], "XYZ", online=True)
        assert resolution["method"] == "google-fonts"
        write_json(reports/"online-font.json", resolution)
        compile_project(project, root/"06-google-expanded.pdf")
        with pymupdf.open(root/"06-google-expanded.pdf") as doc:
            assert "ABCXYZ123" in doc[0].get_text()
    contact_sheet(roundtrip, root/"preview-all-pages.png")
    for filename in ("02-digits", "03-chinese", "04-textbox", "05-variable-original", "05-variable-edited"):
        render_page(root/(filename+".pdf"), 1).save(root/(filename+".png"))
    write_json(reports/"acceptance.json", {"sample": manifest, "unchanged_roundtrip": "zero differing pixels at 144/300 DPI in MuPDF and Poppler",
        "edited_region_isolation": True, "variable_width_golden": variable_report["identical"],
        "google_live_test": not args.skip_online})
    print("Acceptance artifacts complete: " + str(root), flush=True)


if __name__ == "__main__":
    main()
