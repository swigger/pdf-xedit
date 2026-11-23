from pathlib import Path

import pymupdf
import pikepdf
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen

from pdfwork.common import read_json, write_json
from pdfwork.fonts import bind_font
from pdfwork.project import compile_project, decompile


def test_raw_cff_subset_can_match_opentype_and_expand(tmp_path):
    glyphs = [".notdef", "A", "B", "C", "D"]
    fb = FontBuilder(1000, isTTF=False)
    fb.setupGlyphOrder(glyphs)
    fb.setupCharacterMap({ord(c): c for c in "ABCD"})
    fb.setupHorizontalMetrics({g: (600, 0) for g in glyphs})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": "PDFWorkCFF", "styleName": "Regular", "psName": "PDFWorkCFF", "fullName": "PDFWorkCFF"})
    fb.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    fb.setupPost()
    strings = {}
    for i, name in enumerate(glyphs):
        pen = T2CharStringPen(600, None)
        pen.moveTo((50, 0)); pen.lineTo((400 + i*20, 0)); pen.lineTo((400, 500 + i*30)); pen.lineTo((50, 500)); pen.closePath()
        strings[name] = pen.getCharString()
    fb.setupCFF("PDFWorkCFF", {"FullName": "PDFWorkCFF", "FamilyName": "PDFWorkCFF", "Weight": "Regular"}, strings, {})
    full = tmp_path / "full.otf"; fb.save(full)
    from fontTools import subset
    from fontTools.ttLib import TTFont
    font = TTFont(full); cut = subset.Subsetter(); cut.populate(text="ABC"); cut.subset(font)
    part = tmp_path / "subset.otf"; font.save(part)
    # Embed raw Type1C, not an sfnt OpenType wrapper, to test PDF's common CFF case.
    pdf = pikepdf.Pdf.new(); page = pdf.add_blank_page()
    program = pdf.make_stream(font["CFF "].compile(font)); program.Subtype = pikepdf.Name.Type1C
    descriptor = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.FontDescriptor,
        FontName=pikepdf.Name.PDFWorkCFF, Flags=32, FontBBox=[0, -200, 600, 800],
        ItalicAngle=0, Ascent=800, Descent=-200, CapHeight=700, StemV=80, FontFile3=program))
    resource = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
        BaseFont=pikepdf.Name.PDFWorkCFF, Encoding=pikepdf.Name.WinAnsiEncoding,
        FirstChar=65, LastChar=67, Widths=[600, 600, 600], FontDescriptor=descriptor))
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(Test=resource))
    page.Contents = pdf.make_stream(b"BT /Test 24 Tf 1 0 0 1 40 700 Tm (ABC) Tj ET")
    src = tmp_path / "source.pdf"; pdf.save(src); pdf.close()
    project = tmp_path / "project"; decompile(src, project)
    page = read_json(project / "pages/001.json"); line = page["lines"][0]
    assert read_json(project / "fonts.json")[line["font"]]["program"].endswith(".cff")
    result = bind_font(project, line["font"], full)
    assert result["validation"]["accepted"]
    line.update(text="ABCD", width=200); write_json(project / "pages/001.json", page)
    out = tmp_path / "expanded.pdf"; compile_project(project, out)
    with pymupdf.open(out) as doc:
        assert "ABCD" in doc[0].get_text()
