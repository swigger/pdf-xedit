import codecs
import json
import os
from pathlib import Path
import subprocess
import sys

import pymupdf
import pytest

from pdfwork.common import PDFWorkError, read_json, write_json
from pdfwork.editing import batchedit, list_lines
from pdfwork.project import decompile


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "中文工程"
    for page in range(1, 4):
        line = {"id": f"p{page:03d}-l0001", "text": f"第{page}页：原文 Ω 😀",
                "mode": "natural", "width": 200., "align": "left", "overflow": "error", "min_scale": .6,
                "tracking": "auto", "offset_x": 0., "offset_y": 0., "font": "f1_0",
                "font_size": 24., "origin": [40., 200.], "fit_rms_pt": 0.}
        if page == 3:
            line["read_only"] = "Unresolved text encoding"
        write_json(root / f"pages/{page:03d}.json", {"page": page, "media_box": [0, 0, 500, 300], "lines": [line]})
    return root


def snapshot(project):
    return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (project / "pages").glob("*.json")}


def cli(*args):
    return subprocess.run([sys.executable, "-m", "pdfwork", *map(str, args)], cwd=ROOT,
                          capture_output=True, timeout=30)


def test_list_edit_import_by_id_across_pages_then_idempotent(project, tmp_path):
    input_file, report_file = tmp_path / "列表.json", tmp_path / "报告.json"
    result = cli("list", project, "-o", input_file)
    assert result.returncode == 0, result.stderr
    rows = read_json(input_file)
    before = snapshot(project)
    rows[0]["text"] = "第一项修改后：中文 Ω 😀"
    rows[1].update(width=300, align="center", offset_x=-10)
    rows.reverse()
    write_json(input_file, rows)
    result = cli("batchedit", project, input_file, "-o", report_file)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b""
    report = read_json(report_file)
    assert (report["submitted"], report["changed"], report["unchanged"], report["pages_changed"]) == (3, 2, 1, 2)
    page1, page2, page3 = [project / f"pages/{i:03d}.json" for i in (1, 2, 3)]
    assert read_json(page1)["lines"][0]["text"] == rows[2]["text"]
    assert read_json(page2)["lines"][0]["width"] == 300
    assert read_json(page1)["lines"][0]["font"] == "f1_0"
    assert snapshot(project)[page3] == before[page3]
    assert not page1.read_bytes().startswith(codecs.BOM_UTF8)
    after = snapshot(project)
    second = batchedit(project, input_file)
    assert second["changed"] == 0 and second["unchanged"] == 3
    assert snapshot(project) == after


def test_filtered_export_missing_rows_and_missing_fields_are_untouched(project, tmp_path):
    data = list_lines(project, page_number=2)
    assert len(data) == 1
    before = snapshot(project)
    patch = tmp_path / "partial.json"
    write_json(patch, [{"id": data[0]["id"], "text": ""}])
    report = batchedit(project, patch)
    assert report["changed"] == 1
    assert list_lines(project, 2)[0]["text"] == ""
    assert list_lines(project, 2)[0]["width"] == 200
    for page in (1, 3):
        path = project / f"pages/{page:03d}.json"
        assert snapshot(project)[path] == before[path]


def test_dry_run_and_empty_input_never_write(project, tmp_path):
    path = tmp_path / "patch.json"
    write_json(path, [{"id": "p001-l0001", "text": "待修改"}])
    before = snapshot(project)
    result = cli("batchedit", project, path, "--dry-run")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.decode("utf-8"))
    assert report["dry_run"] and report["changed"] == 1
    assert report["changes"][0]["fields"]["text"]["after"] == "待修改"
    assert snapshot(project) == before
    write_json(path, [])
    assert batchedit(project, path)["changed"] == 0
    assert snapshot(project) == before


@pytest.mark.parametrize("invalid,reason", [
    ({"id": "unknown", "text": "改"}, "Unknown line ID"),
    ({"id": "p001-l0001", "text": "重复"}, "Duplicate input ID"),
    ({"id": "p002-l0001", "page": 1}, "page is read-only"),
    ({"id": "p002-l0001", "font_size": 30}, "read-only metadata"),
    ({"id": "p003-l0001", "text": "修改只读项"}, "read-only text"),
    ({"id": "p002-l0001", "colour": "red"}, "unknown field"),
    ({"id": "p002-l0001", "width": 0}, "must be positive"),
    ({"id": "p002-l0001", "width": "200"}, "finite JSON number"),
    ({"id": "p002-l0001", "tracking": float("nan")}, "finite JSON number"),
    ({"id": "p002-l0001", "offset_x": True}, "finite JSON number"),
    ({"id": "p002-l0001", "min_scale": 2}, "at most 1"),
    ({"id": "p002-l0001", "align": "middle"}, "must be one of"),
    ({"id": "p002-l0001", "text": "第一行\n第二行"}, "single-line text"),
    ({"text": "没有ID"}, "non-empty string id"),
])
def test_invalid_later_item_rejects_entire_batch(project, tmp_path, invalid, reason):
    path = tmp_path / "patch.json"
    write_json(path, [{"id": "p001-l0001", "text": "这一项有效"}, invalid])
    before = snapshot(project)
    with pytest.raises(PDFWorkError, match=reason):
        batchedit(project, path)
    assert snapshot(project) == before
    assert not list((project / "pages").glob(".*.batch-*"))


def test_input_schema_bom_and_noop_preserve_exact_page_bytes(project, tmp_path):
    path = tmp_path / "patch.json"
    write_json(path, {"lines": list_lines(project)})
    before = snapshot(project)
    with pytest.raises(PDFWorkError, match="JSON array"):
        batchedit(project, path)
    write_json(path, list_lines(project))
    path.write_bytes(codecs.BOM_UTF8 + path.read_bytes())
    assert batchedit(project, path)["changed"] == 0
    assert snapshot(project) == before


def test_late_write_failure_rolls_back_earlier_pages(project, tmp_path, monkeypatch):
    path = tmp_path / "patch.json"
    write_json(path, [{"id": "p001-l0001", "text": "一"}, {"id": "p002-l0001", "text": "二"}])
    before = snapshot(project)
    real_replace = os.replace
    calls = 0

    def fail_second(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("Simulated file lock")
        return real_replace(source, destination)

    monkeypatch.setattr("pdfwork.editing.os.replace", fail_second)
    with pytest.raises(PDFWorkError, match="rolled back"):
        batchedit(project, path)
    assert calls == 3
    assert {p: p.read_bytes() for p in before} == {p: data for p, (data, _) in before.items()}
    assert not list((project / "pages").glob(".*.batch-*"))


def test_summary_cannot_overwrite_batch_input_or_project_pages(project, tmp_path):
    path = tmp_path / "patch.json"
    write_json(path, [{"id": "p001-l0001", "text": "改"}])
    original = path.read_bytes()
    before = snapshot(project)
    for target in (path, project / "pages/001.json"):
        result = cli("batchedit", project, path, "-o", target)
        assert result.returncode == 2
        assert snapshot(project) == before
        assert path.read_bytes() == original


def test_imported_changes_reach_the_compiled_pdf(make_pdf, tmp_path):
    source = make_pdf("ABC 123")
    project = tmp_path / "project"
    decompile(source, project)
    path = tmp_path / "list.json"
    assert cli("list", project, "-o", path).returncode == 0
    rows = read_json(path)
    rows[0]["text"] = "CBA 321"
    write_json(path, rows)
    result = cli("batchedit", project, path)
    assert result.returncode == 0, result.stderr
    output = tmp_path / "edited.pdf"
    result = cli("compile", project, output)
    assert result.returncode == 0, result.stderr
    with pymupdf.open(output) as document:
        assert "CBA 321" in document[0].get_text()
