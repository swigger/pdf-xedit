"""Recover single lines from independently positioned print-driver glyphs."""
import numpy as np
import pikepdf
import pymupdf
import pytest

from pdfwork.common import read_json, write_json
from pdfwork.project import compile_project, decompile
from pdfwork.verify import render_page


def source_pdf(tmp_path, commands, tab_space=False):
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(500, 300))
    font = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
        BaseFont=pikepdf.Name.Helvetica, Encoding=pikepdf.Name.WinAnsiEncoding))
    if tab_space:
        # A visible blank mapped to a control character must still be editable.
        mapping = "\n".join(f"<{n:02x}> <{9 if n == 32 else n:04x}>" for n in range(32, 127))
        font.ToUnicode = pdf.make_stream((
            "/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
            "1 begincodespacerange <00> <ff> endcodespacerange\n"
            f"95 beginbfchar\n{mapping}\nendbfchar\nendcmap end end").encode())
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F=font))
    page.Contents = pdf.make_stream(commands.encode())
    path = tmp_path / "source.pdf"
    pdf.save(path)
    pdf.close()
    return path


def show(text, x, y=200, wrapper=True):
    command = f"BT /F 10 Tf 1 0 0 1 {x:.6f} {y:.6f} Tm <{text.encode().hex()}> Tj ET"
    return f"q {command} Q" if wrapper else command


def positioned_line(text, y=200):
    font = pymupdf.Font("helv")
    # Stable glyph-dependent rounding, including negative gaps below -0.1 pt.
    advance = {c: round(font.text_length(c, fontsize=10) / .8) * .8 for c in set(text)}
    commands, x = [], 40.
    for c in text:
        commands.append(show(c, x, y))
        x += advance[c]
    return "\n".join(commands), advance


def trace(path):
    with pymupdf.open(path) as doc:
        return [c for span in doc[0].get_texttrace() for c in span["chars"]]


def test_grid_positioned_glyphs_recover_one_editable_line(tmp_path):
    text = "ABRACADABRA ABRACADABRA"
    content, advances = positioned_line(text)
    src = source_pdf(tmp_path, content, tab_space=True)
    project = tmp_path / "project"
    result = decompile(src, project)
    page = read_json(project / "pages/001.json")
    assert result["text_lines"] == 1
    line = page["lines"][0]
    assert line["text"] == text and "\t" not in line["text"]
    assert line["fit_max_pt"] < .001
    assert read_json(project / "layout.json")[0]["advance_corrections"]
    out = tmp_path / "roundtrip.pdf"
    compile_project(project, out)
    assert np.array_equal(render_page(src, 1), render_page(out, 1))

    # Geometry-only edits retain every original horizontal origin.
    line.update(offset_x=3, offset_y=2)
    write_json(project / "pages/001.json", page)
    moved = tmp_path / "moved.pdf"
    compile_project(project, moved)
    before = np.array([c[2] for c in trace(src)])
    after = np.array([c[2] for c in trace(moved)])
    assert np.allclose(after - before, [3, -2], atol=.001)

    # Changed length/order reflows using the recovered character advances.
    new_text = "DABRA ABRACADABRA ABRA"
    line.update(text=new_text, offset_x=0, offset_y=0, width=400)
    write_json(project / "pages/001.json", page)
    edited = tmp_path / "edited.pdf"
    compile_project(project, edited)
    expected = 40 + np.r_[0, np.cumsum([advances[c] for c in new_text[:-1]])]
    assert np.allclose([c[2][0] for c in trace(edited)], expected, atol=.001)

    # Explicit tracking overrides automatic, glyph-dependent spacing.
    line.update(tracking=0)
    write_json(project / "pages/001.json", page)
    compile_project(project, edited)
    font = pymupdf.Font("helv")
    expected = 40 + np.r_[0, np.cumsum([font.text_length(c, fontsize=10) for c in new_text[:-1]])]
    assert np.allclose([c[2][0] for c in trace(edited)], expected, atol=.001)


def test_multiline_and_columns_stay_separate(tmp_path):
    commands = [show("AB", 40), show("CD", 220), show("AB", 40, 185), show("CD", 220, 185)]
    src = source_pdf(tmp_path, "\n".join(commands))
    project = tmp_path / "project"
    assert decompile(src, project)["text_lines"] == 4
    assert [x["text"] for x in read_json(project / "pages/001.json")["lines"]] == ["AB", "CD", "AB", "CD"]


def test_subpixel_baseline_jitter_cannot_chain_into_next_row(tmp_path):
    src = source_pdf(tmp_path, "\n".join([
        show("A", 40, 200), show("B", 46.4, 200.2), show("C", 52.8, 200.4)]))
    project = tmp_path / "project"
    decompile(src, project)
    lines = read_json(project / "pages/001.json")["lines"]
    assert [x["text"] for x in lines] == ["AB", "C"]
    assert lines[0]["fit_max_pt"] == pytest.approx(.2)


@pytest.mark.parametrize("boundary", [
    "q 1 1 1 rg 0 0 100 300 re f Q",  # intervening paint survives Q
    "q 0 0 100 300 re W n",  # entering a clip
    "0 0 1 rg",  # a different color
])
def test_drawing_clip_and_style_boundaries_are_retained(tmp_path, boundary):
    commands = show("A", 40) + "\n" + boundary + "\n" + show("B", 46.4)
    if boundary.endswith("W n"):
        commands += "\nQ"
    src = source_pdf(tmp_path, commands)
    project = tmp_path / "project"
    assert decompile(src, project)["text_lines"] == 2


def test_leaving_clip_is_a_boundary_and_duplicate_ink_is_not_merged(tmp_path):
    src = source_pdf(tmp_path, "q 0 0 100 300 re W n\n" + show("A", 40)
                     + "\nQ\n" + show("B", 46.4) + "\n" + show("B", 46.4))
    project = tmp_path / "project"
    assert decompile(src, project)["text_lines"] == 3


def test_geometry_edit_preserves_irregular_original_spacing(tmp_path):
    src = source_pdf(tmp_path, "\n".join(show(c, x) for c, x in zip("AAAA", [40, 46.4, 53.8, 60.2])))
    project = tmp_path / "project"
    assert decompile(src, project)["text_lines"] == 1
    page = read_json(project / "pages/001.json")
    page["lines"][0]["offset_x"] = 4
    write_json(project / "pages/001.json", page)
    out = tmp_path / "shifted.pdf"
    compile_project(project, out)
    assert np.allclose([c[2][0] for c in trace(out)], [44, 50.4, 57.8, 64.2], atol=.001)
