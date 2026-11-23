from __future__ import annotations

import argparse
import json
import sys
from functools import partial
from pathlib import Path

from .common import PDFWorkError, configure_stdio, read_json, write_json


def emit(value, output=None):
    if output is not None:
        write_json(output, value)
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))


def add_json_output(command):
    command.add_argument(
        "-o", "--output", dest="json_output", metavar="FILE",
        help="Write the JSON result directly to FILE as UTF-8 without BOM (bypasses shell redirection). "
             "For compile/render this is the JSON summary; the PDF/image remains the positional output.",
    )


def parser():
    p = argparse.ArgumentParser(prog="pdfwork", description="Decompile PDF objects and editable text; compile with original font fidelity.")
    sub = p.add_subparsers(dest="command", required=True)
    dec = sub.add_parser("decompile", help="Export a standalone PDF project (destination must be empty)")
    add_json_output(dec)
    dec.add_argument("pdf"); dec.add_argument("project"); dec.add_argument("--password", default="")
    comp = sub.add_parser("compile", help="Compile all project assets plus text edits")
    add_json_output(comp)
    comp.add_argument("project"); comp.add_argument("output")
    ls = sub.add_parser("list", help="List editable single-line text boxes")
    add_json_output(ls)
    ls.add_argument("project"); ls.add_argument("--page", type=int); ls.add_argument("--contains", default="")
    edit = sub.add_parser("edit", help="Update one text box by stable ID")
    add_json_output(edit)
    edit.add_argument("project"); edit.add_argument("id"); edit.add_argument("--text")
    edit.add_argument("--mode", choices=["natural", "slots"])
    edit.add_argument("--width", type=float); edit.add_argument("--align", choices=["left", "center", "right"])
    edit.add_argument("--overflow", choices=["error", "shrink"]); edit.add_argument("--min-scale", type=float)
    edit.add_argument("--tracking"); edit.add_argument("--offset-x", type=float); edit.add_argument("--offset-y", type=float)
    batch = sub.add_parser("batchedit", help="Apply changed editable fields from a list JSON export by line ID")
    add_json_output(batch)
    batch.add_argument("project"); batch.add_argument("input", help="UTF-8 JSON array exported by list")
    batch.add_argument("--dry-run", action="store_true", help="Validate and show changes without modifying the project")
    fonts = sub.add_parser("fonts", help="Inspect, validate, or resolve fonts")
    fs = fonts.add_subparsers(dest="font_command", required=True)
    fl = fs.add_parser("list"); fl.add_argument("project")
    add_json_output(fl)
    for command in ("bind", "check"):
        f = fs.add_parser(command); f.add_argument("project"); f.add_argument("font_id"); f.add_argument("file"); f.add_argument("--index", type=int, default=0)
        add_json_output(f)
    resolve = fs.add_parser("resolve"); resolve.add_argument("project"); resolve.add_argument("font_id")
    add_json_output(resolve)
    resolve.add_argument("--text", default=""); resolve.add_argument("--online", action="store_true")
    resolve.add_argument("--family"); resolve.add_argument("--font-dir", action="append", default=[])
    cmp = sub.add_parser("compare", help="Render every page and compare exact RGB pixels")
    add_json_output(cmp)
    cmp.add_argument("first"); cmp.add_argument("second"); cmp.add_argument("--report", required=True)
    cmp.add_argument("--dpi", type=int, default=144); cmp.add_argument("--renderer", choices=["both", "mupdf", "poppler"], default="both")
    cmp.add_argument("--diff-dir")
    render = sub.add_parser("render", help="Make a contact sheet or a full page PNG")
    add_json_output(render)
    render.add_argument("pdf"); render.add_argument("output"); render.add_argument("--page", type=int)
    render.add_argument("--dpi", type=int, default=144); render.add_argument("--renderer", choices=["mupdf", "poppler"], default="mupdf")
    return p


def main(argv=None):
    configure_stdio()
    args = parser().parse_args(argv)
    emit_result = partial(emit, output=args.json_output)
    try:
        if args.json_output:
            destination = Path(args.json_output).resolve()
            for field in ("pdf", "output", "first", "second", "report", "file", "input"):
                artifact = getattr(args, field, None)
                if artifact and Path(artifact).resolve() == destination:
                    raise PDFWorkError(
                        "-o/--output is the JSON result and must use a different path "
                        f"from an input or PDF/image/font/report: {artifact}"
                    )
            if args.command == "batchedit":
                project = Path(args.project).resolve()
                if destination.is_relative_to(project / "pages") or destination in {
                    project / name for name in ("project.json", "layout.json", "objects.json", "fonts.json", "bindings.json")
                }:
                    raise PDFWorkError("The batchedit JSON report must not overwrite project data")
        if args.command == "decompile":
            from .project import decompile
            emit_result(decompile(args.pdf, args.project, args.password))
        elif args.command == "compile":
            from .project import compile_project
            emit_result(compile_project(args.project, args.output))
        elif args.command == "list":
            from .editing import list_lines
            emit_result(list_lines(args.project, args.page, args.contains))
        elif args.command == "batchedit":
            from .editing import batchedit
            emit_result(batchedit(args.project, args.input, args.dry_run))
        elif args.command == "edit":
            from .editing import EDITABLE_FIELDS
            for path in sorted((Path(args.project)/"pages").glob("*.json")):
                page = read_json(path)
                for line in page["lines"]:
                    if line["id"] != args.id:
                        continue
                    for key in EDITABLE_FIELDS:
                        value = getattr(args, key)
                        if value is not None:
                            line[key] = float(value) if key == "tracking" and value != "auto" else value
                    write_json(path, page); emit_result(line); return 0
            raise PDFWorkError(f"Line not found: {args.id}")
        elif args.command == "fonts":
            from .fonts import bind_font, load_project_model, read_font, resolve_font, verify_candidate
            if args.font_command == "list":
                metadata = read_json(Path(args.project)/"fonts.json")
                bindings = read_json(Path(args.project)/"bindings.json")
                emit_result([{k: v for k, v in x.items() if k != "codes"} | {"resolved": key in bindings} for key, x in metadata.items()])
            elif args.font_command == "bind":
                emit_result(bind_font(args.project, args.font_id, args.file, args.index))
            elif args.font_command == "check":
                pdf, model = load_project_model(args.project, args.font_id)
                try:
                    result = verify_candidate(model, read_font(args.file, args.index)); emit_result(result)
                    return 0 if result["accepted"] else 2
                finally:
                    pdf.close()
            else:
                emit_result(resolve_font(args.project, args.font_id, args.text, args.online, args.family, args.font_dir))
        elif args.command == "compare":
            from .verify import compare
            result = compare(args.first, args.second, args.report, args.dpi, args.renderer, args.diff_dir)
            emit_result({"identical": result["identical"], "report": str(Path(args.report).resolve())})
            return 0 if result["identical"] else 1
        elif args.command == "render":
            from .verify import contact_sheet, render_page
            if args.page:
                Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                render_page(args.pdf, args.page, args.dpi, args.renderer).save(args.output)
            else:
                contact_sheet(args.pdf, args.output)
            emit_result({"output": str(Path(args.output).resolve())})
        return 0
    except (PDFWorkError, ValueError, KeyError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

