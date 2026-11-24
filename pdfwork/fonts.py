from __future__ import annotations

import io
import json
import math
import os
import re
import sys
import tarfile
import zipfile
from pathlib import Path
from urllib.parse import quote, urlencode

import numpy as np
import pikepdf
import pymupdf
import requests
from fontTools import agl, subset
from fontTools.pens.basePen import BasePen
from fontTools.ttLib import TTFont, TTCollection, newTable
from fontTools.varLib.instancer import instantiateVariableFont

from .common import PDFWorkError, digest, inside, read_json, ref, write_json


def font_name(name):
    return re.sub(r"^[A-Z]{6}\+", "", str(name).lstrip("/"))


def normalized_name(name):
    return re.sub(r"[\s,_\-]+", "", font_name(name)).casefold()


def parse_cmap(raw):
    """Read bfchar/bfrange and cidchar/cidrange, including array destinations."""
    text = re.sub(rb"%[^\r\n]*", b"", raw).decode("latin1")
    tokens = re.findall(r"<[0-9A-Fa-f\s]+>|\[|\]|[^\s<>\[\]]+", text)
    unicode_map, cid_map, spaces = {}, {}, []

    def binary(token):
        return bytes.fromhex(token[1:-1])

    def string(token):
        return binary(token).decode("utf-16-be", errors="strict")

    for i, token in enumerate(tokens):
        if token not in ("beginbfchar", "beginbfrange", "begincidchar", "begincidrange", "begincodespacerange"):
            continue
        n, j = int(tokens[i - 1]), i + 1
        for _ in range(n):
            start = binary(tokens[j]); j += 1
            if token == "beginbfchar":
                unicode_map[start] = string(tokens[j]); j += 1
            elif token == "begincidchar":
                cid_map[start] = int(tokens[j]); j += 1
            else:
                end = binary(tokens[j]); j += 1
                if token == "begincodespacerange":
                    spaces.append((start, end)); continue
                first, last = int.from_bytes(start, "big"), int.from_bytes(end, "big")
                if last - first > 65535:
                    raise PDFWorkError("CMap range too large for editable text")
                target = tokens[j]; j += 1
                if target == "[":
                    targets = []
                    while tokens[j] != "]":
                        targets.append(tokens[j]); j += 1
                    j += 1
                    if len(targets) != last - first + 1:
                        raise PDFWorkError("Malformed CMap range array")
                for k, value in enumerate(range(first, last + 1)):
                    code = value.to_bytes(len(start), "big")
                    if token == "begincidrange":
                        cid_map[code] = int(target) + k
                    elif target == "[":
                        unicode_map[code] = string(targets[k])
                    else:
                        b = binary(target)
                        unicode_map[code] = (int.from_bytes(b, "big") + k).to_bytes(len(b), "big").decode("utf-16-be")
    return unicode_map, cid_map, spaces


def font_program(font):
    base = font.DescendantFonts[0] if "/DescendantFonts" in font else font
    descriptor = base.get("/FontDescriptor", {})
    for key, ext in (("/FontFile2", "ttf"), ("/FontFile3", "otf"), ("/FontFile", "pfb")):
        if key in descriptor:
            stream = descriptor[key]
            if str(stream.get("/Subtype", "")) in ("/Type1C", "/CIDFontType0C"):
                ext = "cff"
            return stream.read_bytes(), ext
    return b"", "none"


def read_embedded_program(raw, ext):
    if ext in ("ttf", "otf"):
        return TTFont(io.BytesIO(raw))
    if ext == "cff":
        from fontTools.cffLib import CFFFontSet
        from fontTools.pens.basePen import NullPen
        from fontTools.ttLib.tables._c_m_a_p import CmapSubtable
        font = TTFont()
        cff = CFFFontSet(); cff.decompile(io.BytesIO(raw), font)
        table = newTable("CFF "); table.cff = cff; font["CFF "] = table
        top = cff[0]
        font.setGlyphOrder(top.charset)
        head = newTable("head"); head.unitsPerEm = round(1/abs(top.FontMatrix[0])); font["head"] = head
        metrics = {}
        for name in top.charset:
            charstring = top.CharStrings[name]; charstring.draw(NullPen())
            metrics[name] = (charstring.width, 0)
        hmtx = newTable("hmtx"); hmtx.metrics = metrics; font["hmtx"] = hmtx
        cmap = newTable("cmap"); cmap.tableVersion = 0
        sub = CmapSubtable.newSubtable(4); sub.platformID = 3; sub.platEncID = 1; sub.language = 0
        sub.cmap = {ord(agl.toUnicode(name)): name for name in top.charset if len(agl.toUnicode(name)) == 1}
        cmap.tables = [sub]; font["cmap"] = cmap
        return font
    return None


class FontModel:
    def __init__(self, font, identifier):
        self.obj, self.id = font, identifier
        self.name = font_name(font.get("/BaseFont", "Unknown"))
        self.subtype = str(font.get("/Subtype", ""))
        self.composite = self.subtype == "/Type0"
        self.base = font.DescendantFonts[0] if self.composite else font
        self.raw, self.ext = font_program(font)
        self.tt = None
        if self.raw:
            try:
                self.tt = read_embedded_program(self.raw, self.ext)
            except Exception:
                pass
        self.glyph_order = self.tt.getGlyphOrder() if self.tt else []
        self.cmap, self.cidmap, self.codespaces = {}, {}, []
        self.error = None
        try:
            if "/ToUnicode" in font:
                self.cmap, _, self.codespaces = parse_cmap(font.ToUnicode.read_bytes())
        except Exception as exc:
            self.error = f"Invalid ToUnicode: {exc}"
        enc = font.get("/Encoding", "/StandardEncoding")
        self.vertical = str(enc).endswith("-V")
        if self.composite:
            if isinstance(enc, pikepdf.Stream):
                _, self.cidmap, spaces = parse_cmap(enc.read_bytes())
                self.codespaces = spaces or self.codespaces
                self.vertical = bool(re.search(rb"/WMode\s+1", enc.read_bytes()))
                if b"usecmap" in enc.read_bytes():
                    self.error = "Inherited custom CMaps require explicit expansion before editing"
            elif str(enc) not in ("/Identity-H", "/Identity-V"):
                self.error = f"Named CMap {enc} is preserved but not editable"
            if not self.codespaces:
                self.codespaces = [(b"\0\0", b"\xff\xff")]
        else:
            self.codespaces = [(b"\0", b"\xff")]
            if not self.cmap:
                self.cmap = self.simple_map(enc)
        self.widths = {}
        if self.composite:
            self.default_width = float(self.base.get("/DW", 1000))
            w, i = list(self.base.get("/W", [])), 0
            while i < len(w):
                start, next_value = int(w[i]), w[i + 1]; i += 2
                if isinstance(next_value, pikepdf.Array):
                    for off, width in enumerate(next_value):
                        self.widths[start + off] = float(width)
                else:
                    width = float(w[i]); i += 1
                    for cid in range(start, int(next_value) + 1):
                        self.widths[cid] = width
        else:
            self.default_width = float(self.base.get("/FontDescriptor", {}).get("/MissingWidth", 0))
            first = int(font.get("/FirstChar", 0))
            for i, width in enumerate(font.get("/Widths", [])):
                self.widths[first + i] = float(width)
            if not self.widths:
                try:
                    base14 = pymupdf.Font(fontname=self.name)
                    self.widths = {int.from_bytes(c, "big"): base14.text_length(t) * 1000 for c, t in self.cmap.items()}
                except Exception:
                    pass
        self.gid_map = None
        mapping = self.base.get("/CIDToGIDMap")
        if isinstance(mapping, pikepdf.Stream):
            self.gid_map = mapping.read_bytes()
        self.inverse = {}
        # Some generators map their visible space glyph to U+0009. The editable
        # representation is a single line: use an ordinary space, retaining the
        # same source code/glyph for encoding and leaving the PDF CMap untouched.
        self.cmap = {code: text.replace("\t", " ") for code, text in self.cmap.items()}
        for code, text in self.cmap.items():
            if not text or "\ufffd" in text:
                continue
            gid = self.gid(code)
            if self.tt and (gid is None or gid <= 0 or gid >= len(self.glyph_order)):
                continue
            self.inverse.setdefault(text, code)
        self.inverse_tokens = sorted(self.inverse, key=len, reverse=True)
        self.calibration = self.calibrate_advances()

    def calibrate_advances(self):
        """Measure the renderer's effective advances, including fractional-width rounding.

        Some PDF renderers quantize simple-font Widths to 1/1000 em. Reading the
        floating point Widths verbatim otherwise accumulates visible slot drift.
        Two occurrences of each code isolate its advance, independent of ink bbox.
        """
        if self.vertical or not self.inverse or not self.obj.is_indirect:
            return "unavailable"
        codes = list(dict.fromkeys(self.inverse.values()))
        pdf = pikepdf.Pdf.new()
        try:
            page = pdf.add_blank_page(page_size=(5000, 2000))
            page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F=pdf.copy_foreign(self.obj)))
            commands = []
            for code in codes:
                commands.append(b"BT /F 1000 Tf 1 0 0 1 0 1000 Tm <" + (code+code).hex().encode() + b"> Tj ET")
            page.Contents = pdf.make_stream(b"\n".join(commands))
            buf = io.BytesIO(); pdf.save(buf)
            with pymupdf.open(stream=buf.getvalue(), filetype="pdf") as doc:
                chars = [c for span in doc[0].get_texttrace() for c in span["chars"] if c[1] != -1]
            if len(chars) != 2*len(codes):
                return "unavailable: trace/code count mismatch"
            for i, code in enumerate(codes):
                self.widths[self.cid(code)] = chars[2*i+1][2][0] - chars[2*i][2][0]
            return "MuPDF measured, 1000 pt"
        except Exception as exc:
            return "unavailable: " + str(exc)
        finally:
            pdf.close()

    def simple_map(self, enc):
        from fontTools.encodings.StandardEncoding import StandardEncoding
        from fontTools.encodings.MacRoman import MacRoman
        base = str(enc.get("/BaseEncoding", "/StandardEncoding")) if isinstance(enc, pikepdf.Dictionary) else str(enc)
        mapping = {}
        if base == "/WinAnsiEncoding":
            for i in range(256):
                try:
                    mapping[bytes([i])] = bytes([i]).decode("cp1252")
                except UnicodeError:
                    pass
        else:
            encoding = MacRoman if base == "/MacRomanEncoding" else StandardEncoding
            mapping = {bytes([i]): agl.toUnicode(n) for i, n in enumerate(encoding) if n != ".notdef"}
        if isinstance(enc, pikepdf.Dictionary):
            index = 0
            for value in enc.get("/Differences", []):
                if isinstance(value, int):
                    index = value
                else:
                    mapping[bytes([index])] = agl.toUnicode(str(value)[1:]); index += 1
        return mapping

    def split(self, data):
        result, i = [], 0
        sizes = sorted({len(a) for a, _ in self.codespaces})
        while i < len(data):
            found = False
            for size in sizes:
                code = data[i:i + size]
                if len(code) == size and any(len(a) == size and a <= code <= b for a, b in self.codespaces):
                    result.append(code); i += size; found = True; break
            if not found:
                raise PDFWorkError(f"{self.name}: invalid character code at byte {i}")
        return result

    def cid(self, code):
        return self.cidmap.get(code, int.from_bytes(code, "big"))

    def gid(self, code):
        cid = self.cid(code)
        if self.composite:
            if self.gid_map is not None:
                return int.from_bytes(self.gid_map[2 * cid:2 * cid + 2], "big") if 2 * cid + 2 <= len(self.gid_map) else 0
            if self.tt and "CFF " in self.tt:
                try:
                    return self.glyph_order.index(f"cid{cid:05d}")
                except ValueError:
                    return cid
            return cid
        if not self.tt:
            return None
        text = self.cmap.get(code, "")
        cmap = self.tt.getBestCmap() or {}
        name = cmap.get(ord(text)) if len(text) == 1 else None
        enc = self.obj.get("/Encoding")
        if isinstance(enc, pikepdf.Dictionary):
            index = 0
            for value in enc.get("/Differences", []):
                if isinstance(value, int):
                    index = value
                else:
                    if index == cid and str(value)[1:] in self.glyph_order:
                        name = str(value)[1:]; break
                    index += 1
        if not name:
            for table in self.tt["cmap"].tables:
                name = table.cmap.get(cid) or table.cmap.get(cid + 0xF000)
                if name:
                    break
        return self.tt.getGlyphID(name) if name else 0

    def width(self, code):
        return self.widths.get(self.cid(code), self.default_width)

    def encode(self, text):
        result, i = [], 0
        while i < len(text):
            token = next((t for t in self.inverse_tokens if text.startswith(t, i)), None)
            if token is None:
                raise PDFWorkError(f"{self.name}: missing glyph {text[i]!r} (U+{ord(text[i]):04X}); run fonts resolve/bind first")
            result.append((token, self.inverse[token])); i += len(token)
        return result

    def metadata(self):
        return {"id": self.id, "name": self.name, "subtype": self.subtype,
                "advance_calibration": self.calibration,
                "embedded": bool(self.raw), "vertical": self.vertical, "error": self.error,
                "characters": "".join(sorted(self.inverse)),
                "codes": {c.hex(): {"text": t, "width": self.width(c), "gid": self.gid(c)} for c, t in self.cmap.items()},
                "program": f"fonts/{self.id}.{self.ext}" if self.raw else None,
                "program_sha256": digest(self.raw) if self.raw else None}


class FontRegistry:
    def __init__(self, folder, pdf=None):
        self.folder, self.pdf = Path(folder), pdf
        self.models, self.extensions = {}, {}
        self.bindings = read_json(self.folder / "bindings.json") if (self.folder / "bindings.json").exists() else {}

    def get(self, obj):
        identifier = f"f{obj.objgen[0]}_{obj.objgen[1]}" if obj.is_indirect else "fd" + digest(obj.unparse())[:12]
        if identifier not in self.models:
            self.models[identifier] = FontModel(obj, identifier)
        return self.models[identifier]

    def export(self):
        entries = {}
        objects = read_json(self.folder / "objects.json")
        programs = {}
        for key, model in self.models.items():
            entries[key] = model.metadata()
            if model.raw:
                descriptor = model.base.get("/FontDescriptor", {})
                stream = next(descriptor[k] for k in ("/FontFile2", "/FontFile3", "/FontFile") if k in descriptor)
                stream_id = "obj:" + ref(stream)
                if stream_id not in programs:
                    name = entries[key]["program"]
                    path = inside(self.folder, name)
                    path.parent.mkdir(exist_ok=True); path.write_bytes(model.raw)
                    previous = inside(self.folder, objects["qpdf"][1][stream_id]["stream"]["datafile"])
                    objects["qpdf"][1][stream_id]["stream"]["datafile"] = name
                    programs[stream_id] = name
                    if previous != path:
                        previous.unlink()
                entries[key]["program"] = programs[stream_id]
        write_json(self.folder / "fonts.json", entries)
        write_json(self.folder / "objects.json", objects)

    def extension(self, model, text, report):
        if model.id not in self.bindings:
            missing = sorted(set(c for c in text if c not in model.inverse))
            raise PDFWorkError(f"{model.id} ({model.name}): missing glyphs {''.join(missing)!r}; run fonts resolve --text ... or fonts bind")
        binding = self.bindings[model.id]
        path = inside(self.folder, binding["file"])
        if digest(path.read_bytes()) != binding["sha256"]:
            raise PDFWorkError(f"Bound font was changed: {path}; bind it again")
        # Always revalidate the actual source subset, never trust an editable manifest.
        candidate = TTFont(path)
        validation = verify_candidate(model, candidate)
        if not validation["accepted"]:
            raise PDFWorkError(f"Font validation failed for {model.id}: {validation['reason']}")
        needed = "".join(sorted(set(text)))
        key = (model.id, needed)
        if key in self.extensions:
            return self.extensions[key]
        cmap = candidate.getBestCmap() or {}
        missing = [c for c in needed if ord(c) not in cmap]
        if missing:
            raise PDFWorkError(f"Validated font does not contain {''.join(missing)!r}; resolve again with --text")
        options = subset.Options()
        options.layout_features = []
        cutter = subset.Subsetter(options=options)
        cutter.populate(unicodes=[ord(c) for c in needed])
        cutter.subset(candidate)
        candidate.flavor = None
        data = io.BytesIO(); candidate.save(data)
        # MuPDF supplies the correct CID encoding for TrueType and OpenType/CFF.
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_font(fontname="Expanded", fontbuffer=data.getvalue())
        page.insert_text((10, 40), needed, fontname="Expanded", fontsize=10)
        with pikepdf.open(io.BytesIO(doc.tobytes())) as foreign:
            font = self.pdf.copy_foreign(foreign.pages[0].Resources.Font.Expanded)
        doc.close()
        ext = FontModel(font, "extension_" + model.id)
        ext.encode(needed)
        self.extensions[key] = ext
        report["fonts_added"].append({"source": model.id, "characters": needed, "validation": validation})
        return ext


class OutlinePen(BasePen):
    """Flatten outlines in em coordinates, including composite components."""
    def __init__(self, glyph_set, units):
        super().__init__(glyph_set)
        self.units, self.points, self.start, self.last = units, [], None, None
        self.contour_starts = []

    def _add(self, p):
        self.points.append((p[0] / self.units, p[1] / self.units)); self.last = p

    def _moveTo(self, p):
        self.contour_starts.append(len(self.points))
        self.start = p; self._add(p)

    def _lineTo(self, p):
        a = self.last
        steps = max(1, math.ceil(math.dist(a, p) / self.units * 150))
        for i in range(1, steps + 1):
            t = i / steps; self._add((a[0] + (p[0] - a[0]) * t, a[1] + (p[1] - a[1]) * t))

    def _qCurveToOne(self, control, end):
        a = self.last
        steps = max(8, math.ceil((math.dist(a, control) + math.dist(control, end)) / self.units * 150))
        for i in range(1, steps + 1):
            t = i / steps; u = 1 - t
            self._add((u*u*a[0] + 2*u*t*control[0] + t*t*end[0], u*u*a[1] + 2*u*t*control[1] + t*t*end[1]))

    def _curveToOne(self, b, c, d):
        a = self.last
        steps = max(12, math.ceil((math.dist(a, b) + math.dist(b, c) + math.dist(c, d)) / self.units * 150))
        for i in range(1, steps + 1):
            t = i / steps; u = 1-t
            self._add((u**3*a[0]+3*u*u*t*b[0]+3*u*t*t*c[0]+t**3*d[0], u**3*a[1]+3*u*u*t*b[1]+3*u*t*t*c[1]+t**3*d[1]))

    def _closePath(self):
        if self.last != self.start:
            self._lineTo(self.start)

    def _endPath(self):
        pass


def outline(font, glyph, with_area=False):
    glyph_set = font.getGlyphSet()
    pen = OutlinePen(glyph_set, font["head"].unitsPerEm)
    glyph_set[glyph].draw(pen)
    points = np.asarray(pen.points, dtype=float).reshape((-1, 2))
    area = 0.
    contours = []
    limits = pen.contour_starts + [len(points)]
    for start, end in zip(limits, limits[1:]):
        contour = points[start:end]
        # Full fonts can retain single-point contours that PDF subsets omit
        # (e.g. DejaVu Sans 'u'). They draw no segment and must not contribute
        # to boundary distances or the count of comparable outline glyphs.
        # Use exact coincidence, not an area/size threshold: tiny marks and
        # self-intersecting contours with zero signed area must still be checked.
        if len(contour) < 2 or np.all(contour == contour[0]):
            continue
        contours.append(contour)
        if len(contour) > 2:
            nxt = np.roll(contour, -1, axis=0)
            area += float(np.sum(contour[:, 0]*nxt[:, 1] - nxt[:, 0]*contour[:, 1]) / 2)
    points = np.concatenate(contours) if contours else np.empty((0, 2), dtype=float)
    return (points, abs(area)) if with_area else points


def shape_error(a, b):
    if not len(a) or not len(b):
        return (0., 0.) if len(a) == len(b) else (1., 1.)
    if a.shape == b.shape and np.array_equal(a, b):
        return 0., 0.
    distances = []
    for source, target in ((a, b), (b, a)):
        for i in range(0, len(source), 128):
            distances.extend(np.sqrt(((source[i:i+128, None, :] - target[None, :, :]) ** 2).sum(axis=2).min(axis=1)))
    d = np.asarray(distances)
    return float(np.sqrt(np.mean(d*d))), float(np.max(d))


def verify_candidate(model, candidate, tolerance=0.004):
    result = {"accepted": False, "reason": "", "checked": 0, "outline_glyphs": 0,
              "max_rms_em": 0., "max_distance_em": 0., "max_width_em": 0., "max_area_em2": 0.,
              "tolerance_em": tolerance, "failures": []}
    if model.tt is None:
        result["reason"] = "Embedded font is not a readable TrueType/OpenType font"; return result
    cmap = candidate.getBestCmap() or {}
    units = candidate["head"].unitsPerEm
    for code, text in model.cmap.items():
        gid = model.gid(code)
        if len(text) != 1 or text.isspace() or gid is None or gid <= 0 or gid >= len(model.glyph_order):
            continue
        source_glyph = model.glyph_order[gid]
        target_glyph = cmap.get(ord(text))
        if not target_glyph:
            result["failures"].append({"text": text, "reason": "missing in candidate"}); continue
        (a, area_a), (b, area_b) = outline(model.tt, source_glyph, True), outline(candidate, target_glyph, True)
        rms, maximum = shape_error(a, b)
        area_error = abs(area_a-area_b)
        source_width = model.tt["hmtx"][source_glyph][0] / model.tt["head"].unitsPerEm
        width = abs(source_width - candidate["hmtx"][target_glyph][0] / units)
        result["checked"] += 1
        result["outline_glyphs"] += bool(len(a))
        result["max_rms_em"] = max(result["max_rms_em"], rms)
        result["max_distance_em"] = max(result["max_distance_em"], maximum)
        result["max_width_em"] = max(result["max_width_em"], width)
        result["max_area_em2"] = max(result["max_area_em2"], area_error)
        if rms > tolerance or maximum > tolerance*4 or width > tolerance or area_error > tolerance:
            result["failures"].append({"text": text, "rms_em": rms, "max_em": maximum, "width_em": width, "area_em2": area_error})
    result["accepted"] = not result["failures"] and result["outline_glyphs"] >= 3
    result["reason"] = "All comparable embedded outlines and advances match" if result["accepted"] else (
        "Glyph outlines/metrics differ or candidate is incomplete" if result["failures"] else "Fewer than three comparable outline glyphs; insufficient evidence")
    return result


def load_project_model(folder, font_id):
    from .project import load_objects
    pdf = load_objects(folder)
    if not re.fullmatch(r"f\d+_\d+", font_id):
        pdf.close(); raise PDFWorkError("Font binding requires an indirect font object")
    objgen = tuple(map(int, font_id[1:].split("_")))
    return pdf, FontModel(pdf.get_object(objgen), font_id)


def read_font(path, index=0):
    return TTFont(path, fontNumber=index)


def bind_font(folder, font_id, path, index=0, *, provenance=None):
    folder = Path(folder)
    pdf, model = load_project_model(folder, font_id)
    try:
        with read_font(path, index) as candidate:
            check = verify_candidate(model, candidate)
            if not check["accepted"]:
                raise PDFWorkError(json.dumps(check, ensure_ascii=False))
            candidate.flavor = None
            stream = io.BytesIO(); candidate.save(stream)
        data = stream.getvalue()
        name = f"fonts/resolved/{font_id}-{digest(data)[:16]}.otf"
        target = inside(folder, name); target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
        bindings = read_json(folder / "bindings.json")
        bindings[font_id] = {"file": name, "sha256": digest(data), "source": str(Path(path).resolve()), "font_index": index, "validation": check}
        bindings[font_id].update(provenance or {})
        write_json(folder / "bindings.json", bindings)
        return bindings[font_id]
    finally:
        pdf.close()


def system_font_files(extra=()):
    dirs = [Path(x) for x in extra]
    if sys.platform == "win32":
        dirs += [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
                 Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Windows/Fonts"]
    elif sys.platform == "darwin":
        dirs += [Path("/Library/Fonts"), Path("/System/Library/Fonts"), Path.home()/"Library/Fonts"]
    else:
        dirs += [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home()/".local/share/fonts"]
    paths = set()
    for directory in dirs:
        if directory.exists():
            paths.update(p for p in directory.rglob("*") if p.suffix.lower() in (".ttf", ".otf", ".ttc", ".otc"))
    return sorted(paths)


def names(font):
    result = set()
    for record in font["name"].names:
        if record.nameID in (1, 4, 6, 16, 17):
            try:
                result.add(normalized_name(record.toUnicode()))
            except Exception:
                pass
    return result


def google_family(model):
    """Prefer the embedded typographic/family name over a PostScript style name."""
    if model.tt and "name" in model.tt:
        table = model.tt["name"]
        for identifier in (16, 1):
            candidates = [r for r in table.names if r.nameID == identifier]
            candidates.sort(key=lambda r: (r.langID not in (0, 0x409), r.platformID != 3))
            for record in candidates:
                try:
                    value = font_name(record.toUnicode())
                    if value:
                        return value
                except Exception:
                    pass
    return re.sub(r"[- ](Regular|Roman|Bold|Italic|BoldItalic)$", "", model.name)


def download(url):
    try:
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=45)
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        raise PDFWorkError(f"Font download failed: {exc}") from exc


# Official binary releases. Keep versions explicit: upstream changes must still
# pass the embedded-outline checks, and adding a family should only need an entry.
UPSTREAM_FONT_PACKAGES = (
    {
        "source": "dejavu", "families": ("dejavu",), "archive": "zip",
        "sha256": "7576310b219e04159d35ff61dd4a4ec4cdba4f35c00e002a136f00e96a908b0a",
        "urls": (
            "https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip",
            "https://downloads.sourceforge.net/project/dejavu/dejavu/2.37/dejavu-fonts-ttf-2.37.zip",
        ),
    },
    {
        "source": "liberation", "families": ("liberationsans", "liberationserif", "liberationmono"),
        "archive": "tar",
        "urls": ("https://github.com/liberationfonts/liberation-fonts/files/7261482/liberation-fonts-ttf-2.1.5.tar.gz",),
    },
    {
        "source": "gnu-freefont", "families": ("freesans", "freeserif", "freemono"), "archive": "zip",
        "urls": (
            "https://ftp.gnu.org/gnu/freefont/freefont-ttf-20120503.zip",
            "https://ftpmirror.gnu.org/freefont/freefont-ttf-20120503.zip",
        ),
    },
)


def font_style(model):
    if model.tt and "OS/2" in model.tt:
        table = model.tt["OS/2"]
        return int(table.usWeightClass), int(bool(table.fsSelection & (1 | 512)))
    name = normalized_name(model.name)
    return (700 if "bold" in name else 400), int("italic" in name or "oblique" in name)


def google_font_resources(model, family, text):
    weight, italic = font_style(model)
    chars = "".join(sorted(set(text + "".join(t for t in model.inverse if len(t) == 1 and t.isprintable()))))
    selector = family if ":" in family else f"{family}:ital,wght@{italic},{weight}"
    url = "https://fonts.googleapis.com/css2?" + urlencode({"family": selector, "text": chars})
    response = download(url)
    urls = list(dict.fromkeys(re.findall(r"url\(['\"]?(https://fonts\.gstatic\.com/[^)'\"\s]+)['\"]?\)", response.text)))
    if not urls:
        raise PDFWorkError("Google Fonts returned no font resources")
    for url in urls:
        yield {"url": url}


def github_font_resources(model, family, text):
    """The official Google Fonts repository also serves full TTF/OTF fonts."""
    slug = re.sub(r"[^a-z0-9]", "", family.split(":", 1)[0].casefold())
    if not slug:
        raise PDFWorkError("No usable family name for the Google Fonts repository")
    found = False
    for license_dir in ("ofl", "apache", "ufl"):
        url = f"https://api.github.com/repos/google/fonts/contents/{license_dir}/{slug}"
        try:
            entries = download(url).json()
        except PDFWorkError as exc:
            cause = exc.__cause__
            if isinstance(cause, requests.HTTPError) and cause.response is not None and cause.response.status_code == 404:
                continue
            raise
        if not isinstance(entries, list):
            raise PDFWorkError("Invalid Google Fonts repository directory response")
        # Prefer the exact PostScript filename, but validate every candidate:
        # filenames and family names alone never establish a match.
        entries.sort(key=lambda item: normalized_name(Path(item["name"]).stem) != normalized_name(model.name))
        for item in entries:
            if item.get("type") == "file" and Path(item["name"]).suffix.lower() in (".ttf", ".otf"):
                found = True
                path = f"{license_dir}/{slug}/{quote(item['name'], safe='')}"
                yield {"url": f"https://raw.githubusercontent.com/google/fonts/main/{path}"}
    if not found:
        raise PDFWorkError(f"Family {family!r} has no TTF/OTF files in the Google Fonts repository")


def online_font_sources(model, family, text):
    family_key = normalized_name(family.split(":", 1)[0])
    for package in UPSTREAM_FONT_PACKAGES:
        if family_key.startswith(package["families"]):
            resources = [{"url": url, "archive": package["archive"], "sha256": package.get("sha256")}
                         for url in package["urls"]]
            yield package["source"], resources
    yield "google-fonts", google_font_resources(model, family, text)
    yield "google-fonts-github", github_font_resources(model, family, text)


def font_payloads(data, resource, model):
    """Read archive members in memory; never extract downloaded paths to disk."""
    if resource.get("sha256") and digest(data) != resource["sha256"]:
        raise PDFWorkError("Font archive SHA-256 does not match the official release")
    kind = resource.get("archive")
    if not kind:
        yield None, data
        return
    wanted = normalized_name(model.name)
    def priority(name):
        return normalized_name(Path(name).stem) != wanted, name
    if kind == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = [m for m in archive.infolist() if not m.is_dir() and Path(m.filename).suffix.lower() in (".ttf", ".otf")]
            for member in sorted(members, key=lambda m: priority(m.filename)):
                if member.file_size > 32 * 1024 * 1024:
                    raise PDFWorkError("Font archive member exceeds 32 MiB")
                yield member.filename, archive.read(member)
    elif kind == "tar":
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            members = [m for m in archive.getmembers() if m.isfile() and Path(m.name).suffix.lower() in (".ttf", ".otf")]
            for member in sorted(members, key=lambda m: priority(m.name)):
                if member.size > 32 * 1024 * 1024:
                    raise PDFWorkError("Font archive member exceeds 32 MiB")
                with archive.extractfile(member) as stream:
                    yield member.name, stream.read()
    else:
        raise PDFWorkError(f"Unsupported font archive format: {kind}")


def instantiate_candidate(candidate, model):
    if "fvar" not in candidate:
        return {}
    weight, italic = font_style(model)
    values = {"wght": weight, "ital": italic}
    if model.tt:
        if "post" in model.tt:
            values["slnt"] = float(model.tt["post"].italicAngle)
        if "OS/2" in model.tt:
            widths = {1: 50, 2: 62.5, 3: 75, 4: 87.5, 5: 100, 6: 112.5, 7: 125, 8: 150, 9: 200}
            values["wdth"] = widths.get(model.tt["OS/2"].usWidthClass, 100)
    axes = {axis.axisTag: max(axis.minValue, min(axis.maxValue, values.get(axis.axisTag, axis.defaultValue)))
            for axis in candidate["fvar"].axes}
    instantiateVariableFont(candidate, axes, inplace=True)
    return axes


def resolve_online_font(folder, font_id, model, family, text, attempts):
    cache = folder / "fonts/downloads"
    wanted = normalized_name(model.name)
    family_key = normalized_name(family.split(":", 1)[0])
    for source, resources in online_font_sources(model, family, text):
        try:
            for resource in resources:
                provenance = {"download_source": source, "download_url": resource["url"]}
                try:
                    data = download(resource["url"]).content
                    matched = False
                    for member, payload in font_payloads(data, resource, model):
                        detail = {**provenance, **({"archive_member": member} if member else {})}
                        try:
                            with TTFont(io.BytesIO(payload)) as candidate:
                                if member and not {wanted, family_key}.intersection(names(candidate)):
                                    continue
                                matched = True
                                axes = instantiate_candidate(candidate, model)
                                if axes:
                                    detail["variation_axes"] = axes
                                cmap = candidate.getBestCmap() or {}
                                missing = sorted({c for c in text if ord(c) not in cmap})
                                if missing:
                                    raise PDFWorkError(f"Downloaded font does not contain requested characters: {''.join(missing)!r}")
                                candidate.flavor = None
                                stream = io.BytesIO(); candidate.save(stream)
                            font_data = stream.getvalue()
                            cache.mkdir(parents=True, exist_ok=True)
                            path = cache / (digest(font_data) + ".otf"); path.write_bytes(font_data)
                            result = bind_font(folder, font_id, path, provenance=detail)
                            return {"resolved": True, "method": source, **result}
                        except Exception as exc:
                            attempts.append({"source": source, "url": resource["url"],
                                             **({"archive_member": member} if member else {}), "error": str(exc)[:2000]})
                    if not matched:
                        raise PDFWorkError("Font package contains no matching family or PostScript name")
                except Exception as exc:
                    attempts.append({"source": source, "url": resource["url"], "error": str(exc)[:2000]})
        except Exception as exc:
            attempts.append({"source": source, "error": str(exc)[:2000]})
    return None


def resolve_font(folder, font_id, text="", online=False, family=None, extra=()):
    folder = Path(folder)
    pdf, model = load_project_model(folder, font_id)
    attempts = []
    wanted = normalized_name(model.name)
    try:
        for path in system_font_files(extra):
            count = 1
            if path.suffix.lower() in (".ttc", ".otc"):
                try:
                    collection = TTCollection(path, lazy=True); count = len(collection.fonts); collection.close()
                except Exception:
                    continue
            for index in range(count):
                try:
                    font = read_font(path, index)
                    if wanted not in names(font):
                        font.close(); continue
                    if any(ord(c) not in (font.getBestCmap() or {}) for c in text):
                        attempts.append({"path": str(path), "error": "Requested characters missing"}); font.close(); continue
                    font.close()
                    result = bind_font(folder, font_id, path, index)
                    return {"resolved": True, "method": "system", **result}
                except Exception as exc:
                    attempts.append({"path": str(path), "error": str(exc)[:2000]})
        if online:
            family = family or google_family(model)
            result = resolve_online_font(folder, font_id, model, family, text, attempts)
            if result:
                return result
        raise PDFWorkError(json.dumps({"resolved": False, "font": model.name, "attempts": attempts,
                                      "hint": "Provide an exact font with fonts bind, or use --online --family NAME"}, ensure_ascii=False))
    finally:
        pdf.close()
