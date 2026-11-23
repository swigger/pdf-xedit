from __future__ import annotations

import base64
import io
from pathlib import Path

import pikepdf

from .common import PDFWorkError, digest, inside, read_json, write_json


def unpack_objects(pdf, folder):
    """Export the full object graph. No original PDF is needed to build it."""
    folder = Path(folder)
    buffer = io.BytesIO()
    pdf.write_qpdf_json(buffer, decode_level=pikepdf.StreamDecodeLevel.generalized)
    data = __import__("json").loads(buffer.getvalue())
    contents = set()
    for page in pdf.pages:
        for stream in page.get("/Contents", []) if isinstance(page.get("/Contents"), pikepdf.Array) else [page.get("/Contents")]:
            if stream is not None:
                contents.add(stream.objgen)
    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Stream) and obj.get("/Subtype") == pikepdf.Name.Form:
            contents.add(obj.objgen)
    count = 0
    for key, value in data["qpdf"][1].items():
        if "stream" not in value:
            continue
        stream = value["stream"]
        objgen = tuple(map(int, key[4:].split()[:2]))
        ext = "pdfops" if objgen in contents else "bin"
        kind = "streams"
        if stream["dict"].get("/Subtype") == "/Image":
            kind = "images"
            image_filter = stream["dict"].get("/Filter")
            ext = "jpg" if image_filter == "/DCTDecode" else "jp2" if image_filter == "/JPXDecode" else "bin"
        filename = f"{kind}/{objgen[0]}-{objgen[1]}.{ext}"
        path = inside(folder, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(stream.pop("data")))
        stream["datafile"] = filename
        count += 1
    write_json(folder / "objects.json", data)
    return count


def load_objects(folder):
    folder = Path(folder)
    data = read_json(folder / "objects.json")
    for value in data["qpdf"][1].values():
        if "stream" in value:
            stream = value["stream"]
            if "datafile" in stream:
                path = inside(folder, stream.pop("datafile"))
                stream["data"] = base64.b64encode(path.read_bytes()).decode("ascii")
    return pikepdf.Pdf.from_qpdf_json(io.BytesIO(__import__("json").dumps(data).encode("utf-8")))


def decompile(source, folder, password=""):
    from .fonts import FontRegistry
    from .text import extract_page

    source, folder = Path(source), Path(folder)
    if folder.exists() and any(folder.iterdir()):
        raise PDFWorkError(f"Destination must be empty: {folder}")
    folder.mkdir(parents=True, exist_ok=True)
    fonts = FontRegistry(folder)
    layout = []
    warnings = []
    with pikepdf.open(source, password=password) as pdf:
        streams = unpack_objects(pdf, folder)
        for number, page in enumerate(pdf.pages, 1):
            editable, internal, notices = extract_page(pdf, page, number, fonts)
            write_json(folder / f"pages/{number:03d}.json", editable)
            layout.extend(internal)
            warnings.extend(notices)
        fonts.export()
        write_json(folder / "layout.json", layout)
        manifest = {
            "format": "pdfwork-1", "source_name": source.name,
            "source_path": str(source.resolve()),
            "source_sha256": digest(source.read_bytes()), "pdf_version": pdf.pdf_version,
            "pages": len(pdf.pages), "objects": len(pdf.objects), "streams": streams,
            "text_lines": len(layout), "editable_lines": sum(not x.get("read_only") for x in layout),
            "warnings": warnings,
        }
        write_json(folder / "project.json", manifest)
        write_json(folder / "bindings.json", {})
    return manifest


def compile_project(folder, destination):
    from .fonts import FontRegistry
    from .text import compile_page

    folder, destination = Path(folder), Path(destination)
    manifest = read_json(folder / "project.json")
    if manifest.get("format") != "pdfwork-1":
        raise PDFWorkError("Unsupported project format")
    source_path = Path(manifest.get("source_path", folder.parent / manifest["source_name"]))
    if destination.resolve() == source_path.resolve():
        raise PDFWorkError("Choose a new output filename; the original PDF is never overwritten")
    pdf = load_objects(folder)
    fonts = FontRegistry(folder, pdf)
    layout = read_json(folder / "layout.json")
    report = {"changed_lines": [], "fonts_added": [], "warnings": []}
    try:
        for number, page in enumerate(pdf.pages, 1):
            editable = read_json(folder / f"pages/{number:03d}.json")
            compile_page(pdf, page, editable, [x for x in layout if x["page"] == number], fonts, report)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(destination.name + ".building")
        try:
            pdf.save(temp, min_version=manifest["pdf_version"], compress_streams=True,
                     object_stream_mode=pikepdf.ObjectStreamMode.preserve)
            with pikepdf.open(temp) as check:
                problems = check.check_pdf_syntax()
                if problems:
                    raise PDFWorkError("Compiled PDF failed syntax checks: " + "; ".join(problems))
            temp.replace(destination)
        finally:
            if temp.exists():
                temp.unlink()
        write_json(destination.with_suffix(".build.json"), report)
        return report
    finally:
        pdf.close()
