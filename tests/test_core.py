import json
import shutil
from pathlib import Path

import numpy as np
import pikepdf
import pymupdf
import pytest

from pdfwork.common import PDFWorkError, read_json, write_json
from pdfwork.fonts import parse_cmap
from pdfwork.project import compile_project, decompile, load_objects
from pdfwork.verify import render_page


def edits(project):
    return read_json(project / "pages/001.json")


def change(project, text, **settings):
    page = edits(project)
    page["lines"][0].update(text=text, **settings)
    write_json(project / "pages/001.json", page)


def get_chars(path):
    with pymupdf.open(path) as pdf:
        return [c for span in pdf[0].get_texttrace() for c in span["chars"]]


def test_standalone_roundtrip_without_source(make_pdf, tmp_path):
    src = make_pdf()
    before = np.asarray(render_page(src, 1))
    project = tmp_path / "project"
    manifest = decompile(src, project)
    assert manifest["editable_lines"] == 1
    src.unlink()
    moved = tmp_path / "moved"; project.rename(moved)
    out = tmp_path / "roundtrip.pdf"
    compile_project(moved, out)
    assert np.array_equal(before, np.asarray(render_page(out, 1)))
    assert not list(moved.rglob("*.pdf"))


def test_original_is_protected_even_for_nested_project(make_pdf, tmp_path):
    source = make_pdf()
    project = tmp_path / "nested/projects/project"
    decompile(source, project)
    with pytest.raises(PDFWorkError, match="original PDF"):
        compile_project(project, source)


def test_digit_and_letter_reuse(make_pdf, tmp_path):
    src = make_pdf("ABC 1234567890")
    project = tmp_path / "project"; decompile(src, project)
    change(project, "CBA 9876543210")
    out = tmp_path / "edited.pdf"; report = compile_project(project, out)
    assert report["fonts_added"] == []
    with pymupdf.open(out) as pdf:
        assert "CBA 9876543210" in pdf[0].get_text()
    before, after = np.array(render_page(src, 1)), np.array(render_page(out, 1))
    assert not np.array_equal(before, after)
    # Outside the text's full bounding region, the page must be byte-for-byte equal.
    before[140:220, 70:900] = 0; after[140:220, 70:900] = 0
    assert np.array_equal(before, after)


def test_missing_glyph_rejected_atomically(make_pdf, tmp_path):
    src = make_pdf(); project = tmp_path / "project"; decompile(src, project)
    change(project, "ABC 汉")
    out = tmp_path / "edited.pdf"
    out.write_bytes(b"existing user output")
    with pytest.raises(PDFWorkError, match="missing glyph"):
        compile_project(project, out)
    assert out.read_bytes() == b"existing user output"


def test_variable_width_digits_reflow(make_pdf, tmp_path):
    src = make_pdf("111 X 88", variable=True)
    project = tmp_path / "project"; decompile(src, project)
    change(project, "888 X 88", width=300)
    out = tmp_path / "edited.pdf"; compile_project(project, out)
    before, after = get_chars(src), get_chars(out)
    x_before = next(c[2][0] for c in before if c[0] == ord("X"))
    x_after = next(c[2][0] for c in after if c[0] == ord("X"))
    ones = [c for c in before if c[0] == ord("1")]
    eights = [c for c in before if c[0] == ord("8")]
    one_advance = ones[1][2][0] - ones[0][2][0]
    eight_advance = eights[1][2][0] - eights[0][2][0]
    expected = 3*(eight_advance - one_advance)
    assert x_after - x_before == pytest.approx(expected, abs=.001)
    positions = [c[2][0] for c in after if c[0] == ord("8")]
    assert positions[1]-positions[0] == pytest.approx(eight_advance, abs=.001)


def test_delete_text_without_displacing_following_line(make_pdf, tmp_path):
    src = make_pdf("ABC", tail=True)
    project = tmp_path / "project"; decompile(src, project)
    change(project, "")
    out = tmp_path / "deleted.pdf"; compile_project(project, out)
    with pymupdf.open(out) as doc:
        text = doc[0].get_text()
        assert "ABC" not in text and "Unchanged following line" in text
    a, b = np.asarray(render_page(src, 1)), np.asarray(render_page(out, 1))
    assert np.array_equal(a[225:], b[225:])


def test_offset_is_local_to_selected_line(make_pdf, tmp_path):
    src = make_pdf("ABC", tail=True)
    project = tmp_path / "project"; decompile(src, project)
    change(project, "ABC", offset_x=12, offset_y=8)
    out = tmp_path / "offset.pdf"; compile_project(project, out)
    a, b = get_chars(src), get_chars(out)
    assert b[0][2][0]-a[0][2][0] == pytest.approx(12, abs=.001)
    assert b[0][2][1]-a[0][2][1] == pytest.approx(-8, abs=.001)
    assert np.allclose([c[2] for c in a[3:]], [c[2] for c in b[3:]], atol=.001)


def test_slots_keep_origins(make_pdf, tmp_path):
    src = make_pdf("888 X", variable=True)
    project = tmp_path / "project"; decompile(src, project)
    change(project, "111 X", mode="slots")
    out = tmp_path / "edited.pdf"; compile_project(project, out)
    assert np.allclose([c[2] for c in get_chars(src)], [c[2] for c in get_chars(out)], atol=.001)


def test_unequal_length_and_following_line_preserved(make_pdf, tmp_path):
    src = make_pdf("ABC", tail=True)
    project = tmp_path / "project"; decompile(src, project)
    change(project, "ABC ABC ABC", width=400)
    out = tmp_path / "edited.pdf"; compile_project(project, out)
    with pymupdf.open(out) as doc:
        assert "ABC ABC ABC" in doc[0].get_text()
    before, after = np.asarray(render_page(src, 1)), np.asarray(render_page(out, 1))
    assert np.array_equal(before[225:], after[225:])


def test_shrink_and_overflow(make_pdf, tmp_path):
    src = make_pdf("ABC")
    project = tmp_path / "project"; decompile(src, project)
    change(project, "ABCABC")
    with pytest.raises(PDFWorkError, match="text needs"):
        compile_project(project, tmp_path / "fail.pdf")
    change(project, "ABCABC", overflow="shrink", min_scale=.4)
    report = compile_project(project, tmp_path / "shrink.pdf")
    assert report["changed_lines"][0]["scale"] == pytest.approx(.5, abs=.01)


def test_raw_assets_are_authoritative(make_pdf, tmp_path):
    src = make_pdf(); project = tmp_path / "project"; decompile(src, project)
    objects = read_json(project / "objects.json")
    for record in objects["qpdf"][1].values():
        if record.get("value", {}).get("/Type") == "/Page":
            record["value"]["/MediaBox"] = [0, 0, 600, 300]
    write_json(project / "objects.json", objects)
    out = tmp_path / "wide.pdf"; compile_project(project, out)
    with pymupdf.open(out) as doc:
        assert doc[0].rect.width == 600


def test_raw_text_conflict_is_rejected(make_pdf, tmp_path):
    src = make_pdf(); project = tmp_path / "project"; decompile(src, project)
    content = next((project / "streams").glob("*.pdfops"))
    content.write_bytes(content.read_bytes() + b"\nq Q\n")
    change(project, "ABC")
    with pytest.raises(PDFWorkError, match="Raw content changed"):
        compile_project(project, tmp_path / "fail.pdf")


def test_shared_form_only_selected_instance_changes(make_pdf, tmp_path):
    src = make_pdf("ABC")
    with pikepdf.open(src) as pdf:
        page = pdf.pages[0]
        form = pdf.make_stream(pikepdf.unparse_content_stream(pikepdf.parse_content_stream(page)))
        form.Type = pikepdf.Name.XObject; form.Subtype = pikepdf.Name.Form
        form.BBox = page.MediaBox; form.Resources = page.Resources
        page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Shared=form))
        page.MediaBox = [0, 0, 500, 600]
        page.Contents = pdf.make_stream(b"q /Shared Do Q q 1 0 0 1 0 300 cm /Shared Do Q")
        shared = tmp_path / "shared.pdf"; pdf.save(shared)
    project = tmp_path / "project"; result = decompile(shared, project)
    assert result["editable_lines"] == 2
    change(project, "CBA")
    out = tmp_path / "edited.pdf"; compile_project(project, out)
    before, after = np.asarray(render_page(shared, 1)), np.asarray(render_page(out, 1))
    assert np.array_equal(before[:600], after[:600])
    assert not np.array_equal(before[600:], after[600:])
    with pymupdf.open(out) as doc:
        assert "ABC" in doc[0].get_text() and "CBA" in doc[0].get_text()


def test_separate_text_objects_on_one_baseline_form_one_box(make_pdf, tmp_path):
    src = make_pdf("ABC")
    with pikepdf.open(src) as pdf:
        page = pdf.pages[0]
        font = next(str(k) for k, f in page.Resources.Font.items() if f.get("/Subtype") == pikepdf.Name.TrueType)
        content = "\n".join(f"BT {font} 24 Tf 1 0 0 1 {x} 200 Tm ({c}) Tj ET" for c, x in zip("ABC", [40, 58, 76]))
        page.Contents = pdf.make_stream(content.encode())
        split = tmp_path / "split.pdf"; pdf.save(split)
    project = tmp_path / "project"; result = decompile(split, project)
    assert result["text_lines"] == 1
    assert edits(project)["lines"][0]["text"] == "ABC"
    change(project, "CBA", width=100)
    output = tmp_path / "joined.pdf"; compile_project(project, output)
    with pymupdf.open(output) as doc:
        assert "CBA" in doc[0].get_text()


def test_cmap_ranges_arrays_and_surrogates():
    data = b"1 begincodespacerange <0000> <ffff> endcodespacerange 1 beginbfchar <0001> <d83dde00> endbfchar 2 beginbfrange <0002> <0003> <0041> <0004> <0005> [<4e00> <4e8c>] endbfrange"
    cmap, _, _ = parse_cmap(data)
    assert list(cmap.values()) == ["😀", "A", "B", "一", "二"]

