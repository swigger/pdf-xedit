"""Import the editable view exported by list, matching stable line IDs."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile

from .common import PDFWorkError, decode_json, read_json


EDITABLE_FIELDS = ("text", "mode", "width", "align", "overflow", "min_scale",
                   "tracking", "offset_x", "offset_y")


def read_pages(project, snapshots=None):
    paths = sorted((Path(project) / "pages").glob("*.json"))
    if not paths:
        raise PDFWorkError(f"No page JSON files found in {Path(project) / 'pages'}")
    pages, ids = {}, set()
    for path in paths:
        original = path.read_bytes()
        page = decode_json(original, path)
        if snapshots is not None:
            snapshots[path] = original
        if (not isinstance(page, dict) or type(page.get("page")) is not int
                or page["page"] <= 0 or not isinstance(page.get("lines"), list)):
            raise PDFWorkError(f"Invalid page JSON: {path}")
        for line in page["lines"]:
            if not isinstance(line, dict) or not isinstance(line.get("id"), str) or not line["id"]:
                raise PDFWorkError(f"Missing/invalid line ID in {path}")
            if line["id"] in ids:
                raise PDFWorkError(f"Duplicate line ID in project: {line['id']}")
            ids.add(line["id"])
        pages[path] = page
    return pages


def list_lines(project, page_number=None, contains=""):
    return [{"page": page["page"], **line}
            for page in read_pages(project).values()
            if page_number is None or page_number == page["page"]
            for line in page["lines"] if contains in line["text"]]


def validate_value(identifier, key, value):
    def invalid(reason):
        raise PDFWorkError(f"{identifier}.{key}: {reason}")

    enums = {"mode": ("natural", "slots"), "align": ("left", "center", "right"),
             "overflow": ("error", "shrink")}
    if key == "text":
        if not isinstance(value, str) or any(c in value for c in "\r\n\t"):
            invalid("must be single-line text without tabs")
        try:
            value.encode("utf-8")
        except UnicodeError:
            invalid("must contain valid Unicode characters")
    elif key in enums:
        if not isinstance(value, str) or value not in enums[key]:
            invalid("must be one of " + ", ".join(enums[key]))
    elif key == "tracking" and value == "auto":
        return
    else:
        try:
            finite = type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            invalid("must be a finite JSON number" + (" or 'auto'" if key == "tracking" else ""))
        if key == "width" and value <= 0:
            invalid("must be positive")
        if key == "min_scale" and not 0 < value <= 1:
            invalid("must be greater than 0 and at most 1")


def stage_file(path, data, suffix):
    """Stage a complete file on the same filesystem as its destination."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.batch-",
                                         suffix=suffix, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return temporary
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def commit_pages(changes):
    """Validate/stage everything first; roll back ordinary commit I/O failures.

    Individual file replacements are atomic. This is not a database transaction
    against process termination or simultaneous external writers.
    """
    staged, committed, keep, warnings = {}, [], set(), []
    try:
        for path, (original, updated) in changes.items():
            staged[path] = [stage_file(path, original, ".old")]
            staged[path].append(stage_file(path, updated, ".new"))
        # Detect modifications made while the batch was being prepared.
        for path, (original, _) in changes.items():
            if path.read_bytes() != original:
                raise PDFWorkError(f"Page changed during batchedit; no edits applied: {path}")
        try:
            for path, (_, new) in staged.items():
                os.replace(new, path)
                committed.append(path)
        except OSError as exc:
            failures = []
            for path in reversed(committed):
                old = staged[path][0]
                try:
                    os.replace(old, path)
                except OSError as rollback_error:
                    keep.add(old)
                    failures.append(f"{path}: restore from {old} ({rollback_error})")
            if failures:
                raise PDFWorkError("Batch write failed and some pages could not be restored. "
                                   "Original backups retained: " + "; ".join(failures)) from exc
            raise PDFWorkError(f"Batch write failed; changes rolled back: {exc}") from exc
    finally:
        for files in staged.values():
            for path in files:
                if path not in keep:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError as exc:
                        warnings.append(f"Temporary file could not be removed: {path} ({exc})")
    return warnings


def batchedit(project, input_file, dry_run=False):
    rows = read_json(input_file)
    if not isinstance(rows, list):
        raise PDFWorkError("batchedit input must be the JSON array exported by list")
    # Decode precisely the bytes used as each original snapshot.
    snapshots = {}
    pages = read_pages(project, snapshots)
    index = {line["id"]: (path, page["page"], line)
             for path, page in pages.items() for line in page["lines"]}
    updates, details, seen = [], [], set()
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            raise PDFWorkError(f"Input item {number}: an object with a non-empty string id is required")
        identifier = row["id"]
        if identifier in seen:
            raise PDFWorkError(f"Duplicate input ID: {identifier}")
        seen.add(identifier)
        if identifier not in index:
            raise PDFWorkError(f"Unknown line ID: {identifier}")
        path, page_number, current = index[identifier]
        if "page" in row and (type(row["page"]) is not int or row["page"] != page_number):
            raise PDFWorkError(f"{identifier}: page is read-only (expected {page_number})")
        allowed = set(current) | set(EDITABLE_FIELDS) | {"page"}
        for key, value in row.items():
            if key not in allowed:
                raise PDFWorkError(f"{identifier}: unknown field {key!r}")
            if key not in EDITABLE_FIELDS and key != "page" and value != current.get(key):
                raise PDFWorkError(f"{identifier}.{key}: read-only metadata cannot be changed")
        different = {key: value for key, value in row.items()
                     if key in EDITABLE_FIELDS and (key not in current or value != current[key])}
        if not different:
            continue
        if current.get("read_only"):
            raise PDFWorkError(f"{identifier}: read-only text: {current['read_only']}")
        for key, value in different.items():
            validate_value(identifier, key, value)
        details.append({"id": identifier, "page": page_number,
                        "fields": {key: {"before": current.get(key), "after": value}
                                   for key, value in different.items()}})
        updates.append((path, current, different))
    changed_paths = {path for path, _, _ in updates}
    report = {"dry_run": dry_run, "submitted": len(rows), "changed": len(updates),
              "unchanged": len(rows) - len(updates), "pages_changed": len(changed_paths), "changes": details}
    if dry_run or not updates:
        return report
    for _, current, different in updates:
        current.update(different)
    changes = {path: (snapshots[path], (json.dumps(pages[path], ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
               for path in pages if path in changed_paths}
    warnings = commit_pages(changes)
    if warnings:
        report["warnings"] = warnings
    return report
