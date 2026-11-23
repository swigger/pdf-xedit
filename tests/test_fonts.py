import io
from pathlib import Path

import pymupdf
import pytest
from fontTools.ttLib import TTFont

from pdfwork.common import PDFWorkError, read_json, write_json
from pdfwork.fonts import bind_font, load_project_model, resolve_font, verify_candidate
from pdfwork.project import compile_project, decompile


def get_font_id(project):
    return read_json(project / "pages/001.json")["lines"][0]["font"]


def test_correct_font_and_wrong_same_name(make_pdf, tmp_path, vera, bold):
    src = make_pdf("ABC abc 123")
    project = tmp_path / "project"; decompile(src, project)
    pdf, model = load_project_model(project, get_font_id(project))
    try:
        assert verify_candidate(model, TTFont(vera))["accepted"]
        wrong = TTFont(bold); correct = TTFont(vera)
        wrong["name"] = correct["name"]
        result = verify_candidate(model, wrong)
        assert not result["accepted"] and result["failures"]
    finally:
        pdf.close()


def test_small_outline_variation_allowed(make_pdf, tmp_path, vera):
    src = make_pdf("ABC abc 123")
    project = tmp_path / "project"; decompile(src, project)
    pdf, model = load_project_model(project, get_font_id(project))
    try:
        candidate = TTFont(vera)
        for glyph in candidate["glyf"].glyphs.values():
            glyph.expand(candidate["glyf"])
            if hasattr(glyph, "coordinates"):
                glyph.coordinates.translate((1, 0))
        result = verify_candidate(model, candidate)
        assert result["accepted"] and result["max_rms_em"] > 0
    finally:
        pdf.close()


def test_expansion_and_bound_font_tampering(make_pdf, tmp_path, vera):
    src = make_pdf("ABC abc 123")
    project = tmp_path / "project"; decompile(src, project)
    font_id = get_font_id(project)
    binding = bind_font(project, font_id, vera)
    page = read_json(project / "pages/001.json")
    page["lines"][0].update(text="ABC Ω", width=300)
    write_json(project / "pages/001.json", page)
    out = tmp_path / "expanded.pdf"
    report = compile_project(project, out)
    assert report["fonts_added"][0]["characters"] == "Ω"
    with pymupdf.open(out) as doc:
        assert "ABC Ω" in doc[0].get_text()
    path = project / binding["file"]
    path.write_bytes(path.read_bytes() + b"tamper")
    with pytest.raises(PDFWorkError, match="Bound font was changed"):
        compile_project(project, tmp_path / "bad.pdf")


def test_system_search_validates_name_match(make_pdf, tmp_path, vera):
    src = make_pdf("ABC abc 123")
    project = tmp_path / "project"; decompile(src, project)
    result = resolve_font(project, get_font_id(project), "Ω", extra=[vera.parent])
    assert result["resolved"] and result["validation"]["accepted"]


def test_same_boundaries_but_filled_counter_is_rejected(make_pdf, tmp_path, vera):
    src = make_pdf("ABC O")
    project = tmp_path / "project"; decompile(src, project)
    pdf, model = load_project_model(project, get_font_id(project))
    try:
        candidate = TTFont(vera)
        glyph = candidate["glyf"][candidate.getBestCmap()[ord("O")]]
        end = glyph.endPtsOfContours[0] + 1
        glyph.coordinates[:end] = list(reversed(glyph.coordinates[:end]))
        glyph.flags[:end] = glyph.flags[:end][::-1]
        result = verify_candidate(model, candidate)
        assert not result["accepted"]
        assert result["max_area_em2"] > result["tolerance_em"]
    finally:
        pdf.close()

