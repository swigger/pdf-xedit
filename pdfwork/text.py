from __future__ import annotations

import copy
import math
from collections import defaultdict

import numpy as np
import pikepdf

from .common import PDFWorkError, digest


IDENTITY = [1., 0., 0., 1., 0., 0.]


def multiply(a, b):
    return [a[0]*b[0]+a[2]*b[1], a[1]*b[0]+a[3]*b[1],
            a[0]*b[2]+a[2]*b[3], a[1]*b[2]+a[3]*b[3],
            a[0]*b[4]+a[2]*b[5]+a[4], a[1]*b[4]+a[3]*b[5]+a[5]]


def point(m, x, y):
    return [m[0]*x+m[2]*y+m[4], m[1]*x+m[3]*y+m[5]]


def translate(m, x, y):
    return multiply(m, [1., 0., 0., 1., x, y])


def op(name, *args):
    return pikepdf.ContentStreamInstruction(list(args), pikepdf.Operator(name))


def raw_hash(instructions):
    return digest(pikepdf.unparse_content_stream(instructions))


def initial_state(ctm=None):
    return {"ctm": ctm or IDENTITY[:], "tm": IDENTITY[:], "lm": IDENTITY[:],
            "font": None, "font_obj": None, "fs": 0., "tc": 0., "tw": 0., "h": 1.,
            "leading": 0., "rise": 0., "tr": 0, "paint": {}, "scope": 0, "bt": 0,
            "actualtext": False}


def scan_container(instructions, resources, fonts, initial=None):
    state = copy.copy(initial) if initial else initial_state()
    state["paint"] = dict(state["paint"])
    stack, marks, runs, children = [], [], [], []
    serial = 0
    for index, instruction in enumerate(instructions):
        if isinstance(instruction, pikepdf.ContentStreamInlineImage):
            serial += 1; state["scope"] = serial; continue
        values, operator = instruction.operands, str(instruction.operator)
        if operator == "q":
            stack.append({k: copy.copy(v) for k, v in state.items() if k not in ("tm", "lm")})
            serial += 1; state["scope"] = serial
        elif operator == "Q":
            if stack:
                state.update(stack.pop())
            serial += 1; state["scope"] = serial
        elif operator == "cm":
            state["ctm"] = multiply(state["ctm"], list(map(float, values)))
        elif operator == "BT":
            state["tm"] = IDENTITY[:]; state["lm"] = IDENTITY[:]
            serial += 1; state["bt"] = serial
        elif operator == "Tf":
            state["font"] = str(values[0]); state["fs"] = float(values[1])
            state["font_obj"] = resources.get("/Font", {}).get(state["font"])
        elif operator in ("Tc", "Tw", "Tz", "TL", "Ts", "Tr"):
            key = {"Tc": "tc", "Tw": "tw", "Tz": "h", "TL": "leading", "Ts": "rise", "Tr": "tr"}[operator]
            state[key] = float(values[0]) / (100 if operator == "Tz" else 1)
        elif operator == "Tm":
            state["tm"] = list(map(float, values)); state["lm"] = state["tm"][:]
        elif operator in ("Td", "TD"):
            tx, ty = map(float, values)
            if operator == "TD":
                state["leading"] = -ty
            state["lm"] = translate(state["lm"], tx, ty); state["tm"] = state["lm"][:]
        elif operator == "T*":
            state["lm"] = translate(state["lm"], 0, -state["leading"]); state["tm"] = state["lm"][:]
        elif operator in ("BDC", "BMC"):
            marks.append(state["actualtext"])
            if operator == "BDC":
                prop = values[1]
                if isinstance(prop, pikepdf.Name):
                    prop = resources.get("/Properties", {}).get(str(prop), {})
                state["actualtext"] = state["actualtext"] or "/ActualText" in prop
            serial += 1; state["scope"] = serial
        elif operator == "EMC":
            state["actualtext"] = marks.pop() if marks else False
            serial += 1; state["scope"] = serial
        elif operator == "gs":
            gs = resources.get("/ExtGState", {}).get(str(values[0]), {})
            if "/Font" in gs:
                state["font_obj"] = gs.Font[0]; state["fs"] = float(gs.Font[1]); state["font"] = None
            state["paint"][operator] = str(values)
        elif operator in ("g", "G", "rg", "RG", "k", "K", "cs", "CS", "sc", "SC", "scn", "SCN", "w", "J", "j", "M", "d"):
            state["paint"][operator] = str(values)
        elif operator in ("W", "W*", "f", "f*", "S", "s", "B", "B*", "b", "b*"):
            serial += 1; state["scope"] = serial
        elif operator == "Do":
            target = resources.get("/XObject", {}).get(str(values[0]))
            if target is not None and target.get("/Subtype") == pikepdf.Name.Form:
                child_state = {k: copy.copy(v) for k, v in state.items()}
                child_state["ctm"] = multiply(state["ctm"], list(map(float, target.get("/Matrix", IDENTITY))))
                children.append((index, target, target.get("/Resources", resources), child_state))
            serial += 1; state["scope"] = serial
        if operator not in ("Tj", "TJ", "'", '"'):
            continue
        if operator in ("'", '"'):
            if operator == '"':
                state["tw"], state["tc"] = float(values[0]), float(values[1])
            state["lm"] = translate(state["lm"], 0, -state["leading"]); state["tm"] = state["lm"][:]
        if state["font_obj"] is None:
            raise PDFWorkError("Text instruction has no resolvable font")
        font = fonts.get(state["font_obj"])
        matrix = multiply(state["ctm"], state["tm"])
        items = list(values[0]) if operator == "TJ" else [values[-1]]
        glyphs, advance = [], 0.
        for item in items:
            if isinstance(item, pikepdf.String):
                for code in font.split(bytes(item)):
                    natural = (font.width(code)*state["fs"]/1000 + state["tc"] + (state["tw"] if code == b" " else 0))*state["h"]
                    glyphs.append({"text": font.cmap.get(code, "\ufffd"), "code": code.hex(),
                                   "position": point(matrix, advance, state["rise"]),
                                   "offset": advance, "advance": natural, "width": font.width(code)})
                    advance += natural
            else:
                advance -= float(item)*state["fs"]*state["h"]/1000
        snapshot = {k: copy.copy(v) for k, v in state.items() if k != "font_obj"}
        text = "".join(g["text"] for g in glyphs)
        reason = None
        if font.error:
            reason = font.error
        elif font.vertical or matrix[0] <= 0 or abs(matrix[1]) > 1e-5:
            reason = "Only left-to-right horizontal text supports high-level editing"
        elif state["tr"] >= 4:
            reason = "Text contributes to clipping; retained as raw PDF operators"
        elif state["actualtext"]:
            reason = "ActualText replacement semantics require a coordinated low-level edit"
        elif "\ufffd" in text:
            reason = "Unicode mapping is incomplete"
        elif state["fs"] <= 0 or state["h"] <= 0:
            reason = "Non-positive text scale"
        elif not state["font"]:
            reason = "ExtGState-selected fonts require low-level editing"
        if glyphs:
            runs.append({"op": index, "operator": operator, "font": font.id, "state": snapshot,
                         "matrix": matrix, "glyphs": glyphs, "advance": advance,
                         "text": text, "read_only": reason})
        state["tm"] = translate(state["tm"], advance, 0)
    return runs, children


def style_key(run):
    s = run["state"]
    return (run["font"], s["fs"], s["tc"], s["tw"], s["h"], s["rise"], s["tr"],
            s["scope"], tuple(sorted(s["paint"].items())), tuple(round(x, 7) for x in run["matrix"][:4]))


def group_runs(runs):
    groups = []
    for run in runs:
        if groups:
            prev = groups[-1][-1]
            a, b = prev["glyphs"][-1], run["glyphs"][0]
            sx = prev["matrix"][0]
            gap = b["position"][0] - (a["position"][0] + a["advance"] * sx)
            display_size = abs(prev["state"]["fs"] * prev["matrix"][3])
            same_line = (not run["read_only"] and not prev["read_only"] and style_key(prev) == style_key(run)
                         and abs(a["position"][1] - b["position"][1]) < .03
                         and -.1 <= gap <= max(1., display_size * 1.25))
            if same_line:
                groups[-1].append(run); continue
        groups.append([run])
    return groups


def describe_line(group, identifier, number, container, content_hash):
    first = group[0]
    glyphs = [g for r in group for g in r["glyphs"]]
    matrix, state = first["matrix"], first["state"]
    sx = matrix[0] or 1.
    origin = point(matrix, 0, state["rise"])
    offsets = [(g["position"][0] - origin[0]) / sx for g in glyphs]
    cumulative, natural = 0., []
    for g in glyphs:
        natural.append(cumulative); cumulative += g["advance"]
    indices = np.arange(len(glyphs), dtype=float)
    observed = np.asarray(offsets) - np.asarray(natural)
    # Least-squares tracking correction with the first glyph held at its exact origin.
    tracking = float(np.dot(indices, observed - offsets[0]) / np.dot(indices, indices)) if len(glyphs) > 1 else 0.
    residual = observed - offsets[0] - tracking * indices
    width = (offsets[-1] - offsets[0] + glyphs[-1]["width"] * state["fs"] * state["h"] / 1000) * sx
    size = state["fs"] * math.hypot(matrix[2], matrix[3])
    text = "".join(r["text"] for r in group)
    settings = {"text": text, "mode": "natural", "width": round(max(width, .001), 6),
                "align": "left", "overflow": "error", "min_scale": .6,
                "tracking": "auto", "offset_x": 0., "offset_y": 0.}
    editable = {"id": identifier, **settings, "font": first["font"], "font_size": round(size, 6),
                "origin": [round(x, 6) for x in glyphs[0]["position"]],
                "fit_rms_pt": round(float(np.sqrt(np.mean(residual ** 2))) * abs(sx), 6)}
    if first["read_only"]:
        editable["read_only"] = first["read_only"]
    internal = {"id": identifier, "page": number, "container": container, "hash": content_hash,
                "original": settings, "runs": group, "offsets": offsets,
                "tracking": tracking, "read_only": first["read_only"]}
    return editable, internal


def extract_page(pdf, page, number, fonts):
    editable = {"page": number, "coordinate_system": "PDF user space, origin normally bottom left, before page rotation",
                "media_box": list(map(float, page.MediaBox)), "lines": []}
    internal, notices = [], []
    def visit(instructions, resources, path, state=None, ancestors=()):
        try:
            runs, children = scan_container(instructions, resources, fonts, state)
        except Exception as exc:
            notices.append(f"Page {number}, container {path}: {exc}. Raw objects preserved.")
            return
        for group in group_runs(runs):
            identifier = f"p{number:03d}-l{len(editable['lines']) + 1:04d}"
            human, machine = describe_line(group, identifier, number, path, raw_hash(instructions))
            editable["lines"].append(human); internal.append(machine)
        for index, form, child_resources, child_state in children:
            if form.objgen in ancestors or len(path) >= 24:
                notices.append(f"Recursive/deep Form at page {number}, {path + [index]} preserved without text view")
                continue
            visit(pikepdf.parse_content_stream(form), child_resources, path + [index], child_state, ancestors + (form.objgen,))
    visit(pikepdf.parse_content_stream(page), page.Resources, [])
    return editable, internal, notices


def replacement_tokens(model, text, registry, report):
    # Preserve original subset glyphs even when a validated extension is needed.
    result, i, missing = [], 0, []
    while i < len(text):
        token = next((t for t in model.inverse_tokens if text.startswith(t, i)), None)
        if token:
            result.append((model, token, model.inverse[token])); i += len(token)
        else:
            result.append((None, text[i], None)); missing.append(text[i]); i += 1
    if missing:
        extension = registry.extension(model, "".join(missing), report)
        result = [(m, t, c) if m else (extension, t, extension.encode(t)[0][1]) for m, t, c in result]
    return result


def rewrite_line(pdf, resources, internal, edit, registry, report):
    if internal["read_only"]:
        raise PDFWorkError(f"{internal['id']}: {internal['read_only']}")
    text = edit["text"]
    if not isinstance(text, str) or any(c in text for c in "\r\n\t"):
        raise PDFWorkError(f"{internal['id']}: text must be a single line, without tabs")
    first = internal["runs"][0]
    state, sx = first["state"], first["matrix"][0]
    model = registry.get(resources.Font[state["font"]])
    tokens = replacement_tokens(model, text, registry, report)
    settings = {**internal["original"], **edit}
    width = float(settings["width"])
    if not math.isfinite(width) or width <= 0:
        raise PDFWorkError("Text box width must be finite and positive")
    if settings["align"] not in ("left", "center", "right") or settings["overflow"] not in ("error", "shrink"):
        raise PDFWorkError("align must be left/center/right; overflow must be error/shrink")
    if settings["mode"] not in ("natural", "slots"):
        raise PDFWorkError("mode must be natural or slots")
    if float(settings["offset_y"]) != 0:
        # Ts moves glyphs without altering either text matrix; restore after drawing.
        vertical_scale = first["matrix"][3]
        if abs(vertical_scale) < 1e-12:
            raise PDFWorkError("Cannot apply a vertical offset to a singular text matrix")
    tracking = internal["tracking"] if settings["tracking"] == "auto" else float(settings["tracking"]) / sx
    start = internal["offsets"][0]
    positions, natural_width, scale = [], 0., 1.
    if settings["mode"] == "slots":
        if len(tokens) != len(internal["offsets"]):
            raise PDFWorkError("slots mode requires the same glyph count; use natural mode for longer text")
        positions = list(internal["offsets"])
        if tokens:
            natural_width = positions[-1] - start + tokens[-1][0].width(tokens[-1][2])*state["fs"]*state["h"]/1000
    else:
        for m, t, code in tokens:
            positions.append(start + natural_width)
            natural_width += (m.width(code)*state["fs"]/1000 + state["tc"] + (state["tw"] if t == " " else 0))*state["h"] + tracking
        if tokens:
            natural_width -= state["tc"]*state["h"] + tracking + (state["tw"]*state["h"] if tokens[-1][1] == " " else 0)
    if natural_width * sx > width + .02:
        if settings["overflow"] == "error" or settings["mode"] == "slots":
            raise PDFWorkError(f"{internal['id']}: text needs {natural_width*sx:.3f} pt, box is {width:.3f} pt; widen it or choose overflow=shrink")
        scale = width / (natural_width * sx)
        if not 0 < float(settings["min_scale"]) <= 1:
            raise PDFWorkError("min_scale must be in (0, 1]")
        if scale < float(settings["min_scale"]):
            raise PDFWorkError(f"{internal['id']}: required scale {scale:.3f} is below min_scale")
        positions = [start + (x-start)*scale for x in positions]
    free = width/sx - natural_width*scale
    shift = {"left": 0., "center": free/2, "right": free}[settings["align"]] + float(settings["offset_x"])/sx
    positions = [x + shift for x in positions]
    replacements = {}
    font_dict = pikepdf.Dictionary(resources.get("/Font", {}))
    resources.Font = font_dict
    resource_names = {model.id: state["font"]}
    for m, _, _ in tokens:
        if m.id not in resource_names:
            n = 1
            while f"/PDFWork{n}" in font_dict:
                n += 1
            resource_names[m.id] = f"/PDFWork{n}"; font_dict[resource_names[m.id]] = m.obj
    for n, run in enumerate(internal["runs"]):
        original = run["state"]
        prefix = []
        if run["operator"] == "'":
            prefix.append(op("T*"))
        elif run["operator"] == '"':
            prefix += [op("Tw", original["tw"]), op("Tc", original["tc"]), op("T*")]
        if n:
            replacements[run["op"]] = prefix + [op("TJ", pikepdf.Array([-run["advance"]*1000/(original["fs"]*original["h"])]))]
            continue
        cursor, active_font, active_size = 0., state["font"], state["fs"]
        commands = prefix
        if float(settings["offset_y"]):
            commands.append(op("Ts", state["rise"] + float(settings["offset_y"])/first["matrix"][3]))
        for position, (m, _, code) in zip(positions, tokens):
            name, size = resource_names[m.id], state["fs"]*scale
            if name != active_font or size != active_size:
                commands.append(op("Tf", pikepdf.Name(name), size)); active_font, active_size = name, size
            correction = -(position - cursor)*1000/(active_size*state["h"])
            commands.append(op("TJ", pikepdf.Array([correction, pikepdf.String(code)])))
            cursor = position + (m.width(code)*active_size/1000 + state["tc"] + (state["tw"] if code == b" " else 0))*state["h"]
        # Preserve the original text-matrix advance AND line matrix. No Tm reset.
        commands.append(op("TJ", pikepdf.Array([-(first["advance"]-cursor)*1000/(active_size*state["h"])])))
        if active_font != state["font"] or active_size != state["fs"]:
            commands.append(op("Tf", pikepdf.Name(state["font"]), state["fs"]))
        if float(settings["offset_y"]):
            commands.append(op("Ts", state["rise"]))
        replacements[run["op"]] = commands
    report["changed_lines"].append({"id": internal["id"], "before": internal["original"]["text"],
                                    "after": text, "scale": scale, "mode": settings["mode"],
                                    "width_pt": natural_width*scale*sx, "tracking_pt": tracking*sx})
    return replacements


def compile_page(pdf, page, editable, internal_lines, registry, report):
    original = {line["id"]: line for line in internal_lines}
    edits = {}
    seen = set()
    for edit in editable["lines"]:
        identifier = edit.get("id")
        if identifier not in original or identifier in seen:
            raise PDFWorkError(f"Unknown/duplicate line ID {identifier}")
        seen.add(identifier)
        baseline = original[identifier]["original"]
        if any(edit.get(key, value) != value for key, value in baseline.items()):
            edits[identifier] = edit
    if seen != set(original):
        raise PDFWorkError("Do not remove line entries; set text to an empty string to delete text")
    if not edits:
        return
    grouped = defaultdict(list)
    for identifier, edit in edits.items():
        grouped[tuple(original[identifier]["container"])].append((original[identifier], edit))

    def visit(instructions, resources, path):
        resources = pikepdf.Dictionary(resources)
        replacements = {}
        for internal, edit in grouped.get(path, []):
            if internal["hash"] != raw_hash(instructions):
                raise PDFWorkError(f"Raw content changed beneath {internal['id']}; decompile the changed PDF again before text editing")
            replacements.update(rewrite_line(pdf, resources, internal, edit, registry, report))
        child_indices = sorted({p[len(path)] for p in grouped if len(p) > len(path) and p[:len(path)] == path})
        if child_indices:
            resources.XObject = pikepdf.Dictionary(resources.get("/XObject", {}))
        for index in child_indices:
            instruction = instructions[index]
            if str(instruction.operator) != "Do":
                raise PDFWorkError("Form invocation changed since decompilation")
            form = resources.XObject[str(instruction.operands[0])]
            child_instructions, child_resources = visit(pikepdf.parse_content_stream(form), form.get("/Resources", resources), path+(index,))
            new_form = pdf.make_stream(pikepdf.unparse_content_stream(child_instructions))
            for key, value in form.items():
                if key not in ("/Length", "/Filter", "/DecodeParms", "/Resources"):
                    new_form[key] = value
            new_form.Resources = child_resources
            name = f"/PDFWorkForm{index}"
            while name in resources.XObject:
                name += "x"
            resources.XObject[name] = new_form
            replacements[index] = [op("Do", pikepdf.Name(name))]
        result = []
        for index, instruction in enumerate(instructions):
            result.extend(replacements.get(index, [instruction]))
        return result, resources

    instructions, resources = visit(pikepdf.parse_content_stream(page), page.Resources, ())
    page.Contents = pdf.make_stream(pikepdf.unparse_content_stream(instructions))
    page.Resources = resources
