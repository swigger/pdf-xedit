import io
import json
import tarfile
import zipfile

import pymupdf
import pytest
import requests
from fontTools.ttLib import TTFont
from fontTools.designspaceLib import DesignSpaceDocument
from fontTools.varLib import build
from fontTools.varLib.instancer import instantiateVariableFont

from pdfwork import fonts
from pdfwork.common import PDFWorkError, digest, read_json, write_json
from pdfwork.project import compile_project, decompile


def response(url, content=b"", status=200):
    result = requests.Response()
    result.status_code, result.url, result._content = status, url, content
    return result


@pytest.fixture
def project(make_pdf, tmp_path, monkeypatch):
    folder = tmp_path / "project"
    decompile(make_pdf("ABC abc 123"), folder)
    monkeypatch.setattr(fonts, "system_font_files", lambda extra: [])
    return folder, read_json(folder / "pages/001.json")["lines"][0]["font"]


def archive_bytes(kind, members):
    buffer = io.BytesIO()
    if kind == "zip":
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)
    else:
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, data in members.items():
                member = tarfile.TarInfo(name); member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
            # A downloaded symlink must not become a candidate or a disk write.
            member = tarfile.TarInfo("linked.ttf"); member.type = tarfile.SYMTYPE
            member.linkname = "../../escape.ttf"; archive.addfile(member)
    return buffer.getvalue()


def wrong_same_name(vera, bold):
    with TTFont(bold) as wrong, TTFont(vera) as correct:
        wrong["name"] = correct["name"]
        stream = io.BytesIO(); wrong.save(stream)
        return stream.getvalue()


@pytest.mark.parametrize("family,source", [
    ("DejaVu Sans", "dejavu"), ("DejaVu Serif", "dejavu"),
    ("DejaVu Sans Mono", "dejavu"), ("Liberation Serif", "liberation"),
    ("FreeMono", "gnu-freefont"),
])
def test_official_packages_validate_and_expand(project, monkeypatch, vera, bold, family, source):
    folder, font_id = project
    package = next(p for p in fonts.UPSTREAM_FONT_PACKAGES if p["source"] == source)
    payload = archive_bytes(package["archive"], {
        "bundle/A-wrong.ttf": wrong_same_name(vera, bold),
        "bundle/Z-correct.ttf": vera.read_bytes(),
        "../../escape.txt": b"must never be extracted",
    })
    monkeypatch.setattr(fonts, "UPSTREAM_FONT_PACKAGES", ({**package, "sha256": digest(payload)},))
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        assert kwargs.get("verify", True) is True
        if len(package["urls"]) > 1 and url == package["urls"][0]:
            raise requests.exceptions.SSLError("primary source unavailable")
        assert url == package["urls"][-1]
        return response(url, payload)
    monkeypatch.setattr(fonts.requests, "get", get)
    result = fonts.resolve_font(folder, font_id, "Ω", online=True, family=family)
    assert result["resolved"] and result["method"] == source
    assert result["validation"]["accepted"]
    assert result["archive_member"] == "bundle/Z-correct.ttf"
    binding = read_json(folder / "bindings.json")[font_id]
    assert binding["download_url"] == package["urls"][-1]
    assert binding["download_source"] == source
    assert not (folder.parent / "escape.txt").exists()
    assert not (folder / "bundle").exists()
    data = read_json(folder / "pages/001.json")
    data["lines"][0].update(text="ABC Ω", width=300)
    write_json(folder / "pages/001.json", data)
    output = folder.parent / "expanded.pdf"
    compile_project(folder, output)
    with pymupdf.open(output) as doc:
        assert "ABC Ω" in doc[0].get_text()


@pytest.mark.parametrize("license_dir", ["ofl", "apache", "ufl"])
def test_google_connection_failure_falls_back_to_github(project, monkeypatch, vera, bold, license_dir):
    folder, font_id = project
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        if url.startswith("https://fonts.googleapis.com/"):
            raise requests.exceptions.SSLError("TLS EOF")
        if "api.github.com" in url:
            if f"/{license_dir}/alias" not in url:
                return response(url, status=404)
            return response(url, json.dumps([
                {"type": "file", "name": "wrong.ttf"},
                {"type": "file", "name": "correct.ttf"},
                {"type": "file", "name": "OFL.txt"},
            ]).encode())
        if url.endswith("wrong.ttf"):
            return response(url, wrong_same_name(vera, bold))
        assert url == f"https://raw.githubusercontent.com/google/fonts/main/{license_dir}/alias/correct.ttf"
        return response(url, vera.read_bytes())
    monkeypatch.setattr(fonts.requests, "get", get)
    result = fonts.resolve_font(folder, font_id, "Ω", online=True, family="Alias:ital,wght@0,400")
    assert result["method"] == "google-fonts-github"
    assert result["validation"]["accepted"]
    assert any(url.endswith("wrong.ttf") for url in calls)
    assert result["download_url"].endswith("correct.ttf")


def test_failed_google_binary_does_not_abort_other_urls(project, monkeypatch, vera):
    folder, font_id = project
    def get(url, **kwargs):
        if "fonts.googleapis.com" in url:
            return response(url, b"src: url('https://fonts.gstatic.com/bad.woff2'); src: url(https://fonts.gstatic.com/good.woff2)")
        if url.endswith("bad.woff2"):
            return response(url, status=503)
        assert url == "https://fonts.gstatic.com/good.woff2"
        return response(url, vera.read_bytes())
    monkeypatch.setattr(fonts.requests, "get", get)
    result = fonts.resolve_font(folder, font_id, "Ω", online=True)
    assert result["method"] == "google-fonts"
    assert read_json(folder / "bindings.json")[font_id]["download_url"].endswith("good.woff2")


def test_all_failures_report_sources_and_preserve_binding(project, monkeypatch, vera):
    folder, font_id = project
    fonts.bind_font(folder, font_id, vera)
    original = (folder / "bindings.json").read_bytes()
    package = fonts.UPSTREAM_FONT_PACKAGES[0]
    payload = archive_bytes("zip", {"DejaVuSans.ttf": vera.read_bytes()})
    monkeypatch.setattr(fonts, "UPSTREAM_FONT_PACKAGES", ({**package, "sha256": digest(payload)},))
    def get(url, **kwargs):
        if url in package["urls"]:
            return response(url, payload)
        if "fonts.googleapis.com" in url:
            return response(url, status=400)
        return response(url, status=403)
    monkeypatch.setattr(fonts.requests, "get", get)
    with pytest.raises(PDFWorkError) as exc:
        fonts.resolve_font(folder, font_id, "\U0010ffff", online=True, family="DejaVu Sans")
    result = json.loads(str(exc.value))
    assert not result["resolved"]
    assert {a["source"] for a in result["attempts"]} == {"dejavu", "google-fonts", "google-fonts-github"}
    assert any("requested characters" in a["error"] for a in result["attempts"])
    assert (folder / "bindings.json").read_bytes() == original


def test_archive_checksum_failure_cannot_bind(project, monkeypatch, vera):
    folder, font_id = project
    payload = archive_bytes("zip", {"DejaVuSans.ttf": vera.read_bytes()})
    def get(url, **kwargs):
        if url in fonts.UPSTREAM_FONT_PACKAGES[0]["urls"]:
            return response(url, payload)
        return response(url, status=404)
    monkeypatch.setattr(fonts.requests, "get", get)
    with pytest.raises(PDFWorkError) as exc:
        fonts.resolve_font(folder, font_id, online=True, family="DejaVu Sans")
    assert "SHA-256" in str(exc.value)
    assert read_json(folder / "bindings.json") == {}


def test_offline_resolution_never_downloads(project, monkeypatch):
    def get(*args, **kwargs):
        pytest.fail("Offline resolution attempted a network request")
    monkeypatch.setattr(fonts.requests, "get", get)
    with pytest.raises(PDFWorkError, match='"resolved": false'):
        fonts.resolve_font(*project)


def test_github_variable_font_uses_embedded_weight(tmp_path, monkeypatch, vera):
    designspace = DesignSpaceDocument()
    designspace.addAxisDescriptor(name="Weight", tag="wght", minimum=400, default=400, maximum=700)
    for weight in (400, 700):
        path = tmp_path / f"master-{weight}.ttf"
        with TTFont(vera) as font:
            for record in font["name"].names:
                if record.nameID in (1, 4, 6):
                    record.string = "PDFWorkVariableSource".encode(record.getEncoding())
            font["OS/2"].usWeightClass = weight
            if weight == 700:
                for name in font.getGlyphOrder():
                    glyph = font["glyf"][name]
                    if hasattr(glyph, "coordinates"):
                        glyph.coordinates.scale((1.2, 1))
                    width, side_bearing = font["hmtx"][name]
                    font["hmtx"][name] = (round(width * 1.2), round(side_bearing * 1.2))
            font.save(path)
        designspace.addSourceDescriptor(path=str(path), name=f"master-{weight}", location={"Weight": weight})
    # Vera's old OS/2 table lacks the optional metrics used by MVAR; this
    # fixture varies outlines and advances, not global metrics.
    variable, _, _ = build(designspace, exclude=["MVAR"])
    stream = io.BytesIO(); variable.save(stream); payload = stream.getvalue()
    static = instantiateVariableFont(variable, {"wght": 700})
    static_path = tmp_path / "static.ttf"; static.save(static_path)
    src = tmp_path / "source.pdf"
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont as ReportLabFont
    from reportlab.pdfgen import canvas
    pdfmetrics.registerFont(ReportLabFont("VariableSource", str(static_path)))
    doc = canvas.Canvas(str(src)); doc.setFont("VariableSource", 24)
    doc.drawString(40, 100, "ABC abc 123"); doc.save()
    variable.close(); static.close()
    folder = tmp_path / "variable-project"; decompile(src, folder)
    font_id = read_json(folder / "pages/001.json")["lines"][0]["font"]
    monkeypatch.setattr(fonts, "system_font_files", lambda extra: [])
    def get(url, **kwargs):
        if "fonts.googleapis.com" in url:
            raise requests.exceptions.ConnectionError("Google API unavailable")
        if "api.github.com" in url:
            return response(url, json.dumps([{"type": "file", "name": "Test[wght].ttf"}]).encode())
        assert url.endswith("Test%5Bwght%5D.ttf")
        return response(url, payload)
    monkeypatch.setattr(fonts.requests, "get", get)
    result = fonts.resolve_font(folder, font_id, "Ω", online=True)
    assert result["method"] == "google-fonts-github"
    assert result["variation_axes"] == {"wght": 700}
    assert result["validation"]["accepted"]
    with TTFont(folder / result["file"]) as bound:
        assert "fvar" not in bound
        assert bound["OS/2"].usWeightClass == 700
