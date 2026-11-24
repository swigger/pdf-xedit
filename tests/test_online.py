import io
import re
import tarfile
import zipfile
from urllib.parse import urlencode

import pymupdf
import pytest
import requests
from fontTools.ttLib import TTFont
from fontTools import subset
from fontTools.varLib.instancer import instantiateVariableFont

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


@pytest.mark.online
@pytest.mark.parametrize("source,member,url,weight", [
    ("dejavu", "DejaVuSansMono.ttf", "https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip", None),
    ("liberation", "LiberationSerif-Bold.ttf", "https://github.com/liberationfonts/liberation-fonts/files/7261482/liberation-fonts-ttf-2.1.5.tar.gz", None),
    ("gnu-freefont", "FreeMono.ttf", "https://ftp.gnu.org/gnu/freefont/freefont-ttf-20120503.zip", None),
    ("google-fonts-github", None, "https://raw.githubusercontent.com/google/fonts/main/ofl/abeezee/ABeeZee-Regular.ttf", None),
    ("google-fonts-github", None, "https://raw.githubusercontent.com/google/fonts/main/ofl/roboto/Roboto%5Bwdth,wght%5D.ttf", 700),
])
def test_live_additional_sources_download_validate_and_expand(tmp_path, monkeypatch, source, member, url, weight):
    from pdfwork import fonts
    response = requests.get(url, timeout=45); response.raise_for_status()
    data = response.content
    if member and url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            data = archive.read(next(n for n in archive.namelist() if n.endswith("/" + member)))
    elif member:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            entry = next(m for m in archive.getmembers() if m.name.endswith("/" + member))
            with archive.extractfile(entry) as stream:
                data = stream.read()
    with TTFont(io.BytesIO(data)) as font:
        if weight:
            instantiateVariableFont(font, {"wght": weight, "wdth": 100}, inplace=True)
        # Subset before embedding so PDF CMaps cannot refer to stripped glyphs.
        subsetter = subset.Subsetter(); subsetter.populate(text="ABC abc 123")
        subsetter.subset(font)
        stream = io.BytesIO(); font.save(stream); data = stream.getvalue()
    src = tmp_path / "original.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_font(fontname="OnlineFixture", fontbuffer=data)
        page.insert_text((40, 100), "ABC abc 123", fontname="OnlineFixture", fontsize=24)
        doc.save(src)
    project = tmp_path / "project"; decompile(src, project)
    monkeypatch.setattr(fonts, "system_font_files", lambda extra: [])
    if source == "google-fonts-github":
        real_get = requests.get
        def without_google_api(url, **kwargs):
            if url.startswith("https://fonts.googleapis.com/"):
                raise requests.exceptions.SSLError("Simulated Google API outage")
            return real_get(url, **kwargs)
        monkeypatch.setattr(requests, "get", without_google_api)
    page = read_json(project / "pages/001.json"); font_id = page["lines"][0]["font"]
    result = resolve_font(project, font_id, "é", online=True)
    assert result["method"] == source and result["validation"]["accepted"]
    if weight:
        assert result["variation_axes"]["wght"] == weight
    page["lines"][0].update(text="ABC é", width=300)
    write_json(project / "pages/001.json", page)
    out = tmp_path / "expanded.pdf"; report = compile_project(project, out)
    assert report["fonts_added"]
    with pymupdf.open(out) as doc:
        assert "ABC é" in doc[0].get_text()
