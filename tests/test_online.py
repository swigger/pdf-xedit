import re
from urllib.parse import urlencode

import pymupdf
import pytest
import requests

from pdfwork.common import read_json, write_json
from pdfwork.fonts import resolve_font
from pdfwork.project import compile_project, decompile


@pytest.mark.online
def test_live_google_font_download_validate_and_expand(tmp_path):
    css = requests.get("https://fonts.googleapis.com/css2?" + urlencode({"family": "ABeeZee", "text": "ABCabc123"}), timeout=30)
    css.raise_for_status()
    url = re.search(r"url\((https://fonts\.gstatic\.com/[^)]+)\)", css.text)[1]
    response = requests.get(url, timeout=30); response.raise_for_status()
    font = response.content
    source = tmp_path / "google-original.pdf"
    doc = pymupdf.open(); page = doc.new_page()
    page.insert_font(fontname="TestFont", fontbuffer=font)
    page.insert_text((50, 100), "ABCabc123", fontname="TestFont", fontsize=28)
    doc.save(source); doc.close()
    project = tmp_path / "project"; decompile(source, project)
    data = read_json(project / "pages/001.json")
    font_id = data["lines"][0]["font"]
    result = resolve_font(project, font_id, "XYZ", online=True)
    assert result["method"] == "google-fonts" and result["validation"]["accepted"]
    data["lines"][0].update(text="ABCXYZ123", width=400)
    write_json(project / "pages/001.json", data)
    output = tmp_path / "google-expanded.pdf"
    report = compile_project(project, output)
    assert report["fonts_added"]
    with pymupdf.open(output) as doc:
        assert "ABCXYZ123" in doc[0].get_text()
