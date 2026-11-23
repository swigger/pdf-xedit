from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


class PDFWorkError(Exception):
    """An actionable, safe failure; the destination PDF is not published."""


def read_json(path):
    return decode_json(Path(path).read_bytes(), path)


def decode_json(data, source="JSON input"):
    try:
        return json.loads(data.decode("utf-8-sig"))
    except UnicodeError as exc:
        raise PDFWorkError(
            f"{source}: JSON input must be UTF-8 (an optional UTF-8 BOM is accepted). "
            "Save it as UTF-8, or export it again using -o/--output instead of shell redirection."
        ) from exc


def write_json(path, value):
    # Encode before opening the destination: no BOM, no platform newline conversion.
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def configure_stdio():
    """Make CLI text independent of the Windows locale and PYTHONIOENCODING."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict", newline="\n")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def inside(root, name):
    root = Path(root).resolve()
    result = (root / name).resolve()
    if not result.is_relative_to(root):
        raise PDFWorkError(f"Project path escapes its directory: {name}")
    return result


def ref(obj):
    return f"{obj.objgen[0]} {obj.objgen[1]} R"

