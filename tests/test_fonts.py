import io
from array import array
from pathlib import Path

import pymupdf
import pytest
from fontTools.ttLib import TTFont

from pdfwork.common import PDFWorkError, read_json, write_json
from pdfwork.fonts import bind_font, load_project_model, outline, resolve_font, verify_candidate
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


def append_contour(font, name, points):
    glyph = font["glyf"][name]
    assert not glyph.isComposite()
    glyph.coordinates.extend(points)
    glyph.flags.extend(array("B", [1] * len(points)))
    glyph.endPtsOfContours.append(len(glyph.coordinates) - 1)
    glyph.numberOfContours += 1
    glyph.recalcBounds(font["glyf"])


@pytest.mark.parametrize("side", ["embedded", "candidate"])
@pytest.mark.parametrize("repetitions", [1, 3])
def test_point_contours_do_not_change_font_match(make_pdf, tmp_path, vera, side, repetitions):
    project = tmp_path / "project"; decompile(make_pdf("ABC uü"), project)
    pdf, model = load_project_model(project, get_font_id(project))
    try:
        with TTFont(vera) as candidate:
            before = verify_candidate(model, candidate)
            assert before["accepted"]
            if side == "candidate":
                font, name = candidate, candidate.getBestCmap()[ord("u")]
            else:
                code = next(code for code, text in model.cmap.items() if text == "u")
                font, name = model.tt, model.glyph_order[model.gid(code)]
            # A point well outside the visible outline reproduces the false
            # rejection; candidate composites such as ü inherit this contour.
            units = font["head"].unitsPerEm
            append_contour(font, name, [(3 * units, 3 * units)] * repetitions)
            assert verify_candidate(model, candidate) == before
    finally:
        pdf.close()


@pytest.mark.parametrize("shape", ["tiny_square", "zero_signed_area"])
def test_visible_contours_are_not_discarded_by_area(make_pdf, tmp_path, vera, shape):
    project = tmp_path / "project"; decompile(make_pdf("ABC uü"), project)
    pdf, model = load_project_model(project, get_font_id(project))
    try:
        with TTFont(vera) as candidate:
            name = candidate.getBestCmap()[ord("u")]
            units = candidate["head"].unitsPerEm
            x, y, size = 3 * units, 3 * units, max(1, units // 100)
            points = [(x, y), (x + size, y), (x + size, y + size), (x, y + size)]
            if shape == "zero_signed_area":
                points[1], points[2] = points[2], points[1]
            _, before_area = outline(candidate, name, True)
            append_contour(candidate, name, points)
            _, after_area = outline(candidate, name, True)
            assert abs(after_area - before_area) < 0.004
            result = verify_candidate(model, candidate)
            assert not result["accepted"]
            assert result["max_distance_em"] > 0.016
            assert any(item["text"] == "u" for item in result["failures"])
    finally:
        pdf.close()


def test_point_only_glyphs_do_not_supply_outline_evidence(make_pdf, tmp_path, vera):
    from fontTools.ttLib.tables._g_l_y_f import Glyph, GlyphCoordinates
    project = tmp_path / "project"; decompile(make_pdf("ABC"), project)
    pdf, model = load_project_model(project, get_font_id(project))
    try:
        model.cmap = {code: text for code, text in model.cmap.items() if text in "ABC"}
        with TTFont(vera) as candidate:
            for code, text in model.cmap.items():
                for font, name in ((model.tt, model.glyph_order[model.gid(code)]),
                                   (candidate, candidate.getBestCmap()[ord(text)])):
                    glyph = Glyph(); glyph.numberOfContours = 1
                    glyph.coordinates = GlyphCoordinates([(500, 500)])
                    glyph.flags = array("B", [1]); glyph.endPtsOfContours = [0]
                    font["glyf"][name] = glyph; glyph.recalcBounds(font["glyf"])
            result = verify_candidate(model, candidate)
            assert not result["accepted"] and result["outline_glyphs"] == 0
            assert "insufficient evidence" in result["reason"]
    finally:
        pdf.close()
