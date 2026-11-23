import base64
import codecs
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from pdfwork.common import PDFWorkError, read_json, write_json


ROOT = Path(__file__).resolve().parents[1]
TEXT = "简体中文、繁體、café、Ω、😀"
WINPS = Path(os.environ.get("WINDIR", "C:/Windows"))/"System32/WindowsPowerShell/v1.0/powershell.exe"
SHELLS = [("Windows PowerShell 5.1", str(WINPS)), ("PowerShell 7", shutil.which("pwsh"))]


@pytest.fixture
def text_project(tmp_path):
    project = tmp_path / "中文工程"
    write_json(project / "pages/001.json", {"page": 1, "lines": [{"id": "p001-l0001", "text": TEXT}]})
    write_json(project / "fonts.json", {"f1_0": {"name": "中文字体", "characters": TEXT}})
    write_json(project / "bindings.json", {})
    return project


def cli(*args):
    env = dict(os.environ, PYTHONUTF8="0", PYTHONIOENCODING="cp1252")
    return subprocess.run([sys.executable, "-m", "pdfwork", *map(str, args)],
                          cwd=ROOT, env=env, capture_output=True, timeout=30)


def assert_utf8_json(path):
    data = path.read_bytes()
    assert not data.startswith((codecs.BOM_UTF8, codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
    assert b"\r\n" not in data
    assert data.endswith(b"\n")
    return json.loads(data.decode("utf-8"))


def test_json_input_output_unicode_and_bom(tmp_path):
    path = tmp_path / "中文.json"
    write_json(path, {"text": TEXT})
    assert assert_utf8_json(path) == {"text": TEXT}
    path.write_bytes(codecs.BOM_UTF8 + path.read_bytes())
    assert read_json(path) == {"text": TEXT}
    path.write_text(json.dumps({"text": TEXT}, ensure_ascii=False), encoding="utf-16")
    with pytest.raises(PDFWorkError, match="UTF-8"):
        read_json(path)


def test_stdout_is_utf8_despite_legacy_environment(text_project):
    result = cli("list", text_project)
    assert result.returncode == 0, result.stderr
    assert TEXT.encode("utf-8") in result.stdout
    assert json.loads(result.stdout.decode("utf-8"))[0]["text"] == TEXT


@pytest.mark.parametrize("option", ["-o", "--output"])
def test_direct_output_is_utf8_and_stdout_is_empty(text_project, tmp_path, option):
    output = tmp_path / "中文路径/结果.json"
    result = cli("list", text_project, "--contains", "😀", option, output)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b""
    assert assert_utf8_json(output)[0]["text"] == TEXT


def test_fonts_list_and_edit_result_use_utf8(text_project, tmp_path):
    output = tmp_path / "字体结果.json"
    result = cli("fonts", "list", text_project, "-o", output)
    assert result.returncode == 0, result.stderr
    assert assert_utf8_json(output)[0]["name"] == "中文字体"
    replacement = "修改后：" + TEXT
    result = cli("edit", text_project, "p001-l0001", "--text", replacement, "-o", output)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b""
    assert assert_utf8_json(output)["text"] == replacement
    assert assert_utf8_json(text_project / "pages/001.json")["lines"][0]["text"] == replacement


def test_bad_input_error_is_utf8_and_not_written_as_result(text_project, tmp_path):
    page = text_project / "pages/001.json"
    page.write_text(page.read_text("utf-8"), encoding="utf-16")
    output = tmp_path / "result.json"
    result = cli("list", text_project, "-o", output)
    assert result.returncode == 2
    assert result.stdout == b""
    assert "中文工程" in result.stderr.decode("utf-8")
    assert "UTF-8" in result.stderr.decode("utf-8")
    assert not output.exists()


def test_pdf_and_json_outputs_are_separate(make_pdf, tmp_path):
    source = make_pdf()
    project, manifest = tmp_path / "project", tmp_path / "manifest.json"
    result = cli("decompile", source, project, "-o", manifest)
    assert result.returncode == 0, result.stderr
    assert assert_utf8_json(manifest)["pages"] == 1
    pdf, summary = tmp_path / "output.pdf", tmp_path / "result.json"
    result = cli("compile", project, pdf, "-o", summary)
    assert result.returncode == 0, result.stderr
    assert pdf.read_bytes().startswith(b"%PDF-")
    assert assert_utf8_json(summary)["changed_lines"] == []
    original = pdf.read_bytes()
    result = cli("compile", project, pdf, "-o", pdf)
    assert result.returncode == 2
    assert pdf.read_bytes() == original
    assert "different path" in result.stderr.decode("utf-8")


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def powershell(executable, code):
    if not executable or not Path(executable).is_file():
        pytest.skip("PowerShell version is not installed")
    encoded = base64.b64encode(code.encode("utf-16-le")).decode("ascii")
    return subprocess.run([executable, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                          capture_output=True, cwd=ROOT, timeout=30)


@pytest.mark.parametrize("label,executable", SHELLS, ids=[s[0] for s in SHELLS])
def test_powershell_direct_file_bypasses_redirection_encoding(text_project, tmp_path, label, executable):
    output = tmp_path / "导出.json"
    result = powershell(executable,
        "$ErrorActionPreference = 'Stop'; "
        f"& {ps_quote(ROOT/'pdfwork.ps1')} list {ps_quote(text_project)} --contains {ps_quote('😀')} -o {ps_quote(output)}; "
        "exit $LASTEXITCODE")
    assert result.returncode == 0, (label, result.stderr)
    assert result.stdout == b""
    assert assert_utf8_json(output)[0]["text"] == TEXT


@pytest.mark.parametrize("label,executable", SHELLS, ids=[s[0] for s in SHELLS])
def test_powershell_pipeline_text_and_encoding_restore(text_project, tmp_path, label, executable):
    # Simulate a legacy code page; check Unicode inside the PowerShell pipeline
    # and verify the wrapper restores the caller's settings on success and failure.
    result = powershell(executable,
        "$ErrorActionPreference = 'Stop'; "
        "[Console]::OutputEncoding = [Text.Encoding]::GetEncoding(936); "
        "[Console]::InputEncoding = [Text.Encoding]::GetEncoding(936); "
        "$OutputEncoding = [Text.Encoding]::ASCII; "
        f"$data = & {ps_quote(ROOT/'pdfwork.ps1')} list {ps_quote(text_project)} | ConvertFrom-Json; "
        f"if ($data[0].text -cne {ps_quote(TEXT)}) {{ throw 'Pipeline text was corrupted' }}; "
        "if ([Console]::OutputEncoding.CodePage -ne 936 -or [Console]::InputEncoding.CodePage -ne 936 "
        "-or $OutputEncoding.CodePage -ne 20127) { throw 'Encoding settings were not restored' }; "
        "$ErrorActionPreference = 'Continue'; "
        f"& {ps_quote(ROOT/'pdfwork.ps1')} edit {ps_quote(text_project)} bad-id --text ABC 2>$null; "
        "if ($LASTEXITCODE -ne 2) { throw 'Exit status was lost' }; "
        "if ([Console]::OutputEncoding.CodePage -ne 936 -or [Console]::InputEncoding.CodePage -ne 936 "
        "-or $OutputEncoding.CodePage -ne 20127) { throw 'Encoding settings were not restored on failure' }; "
        "Write-Output 'PASS'; exit 0")
    assert result.returncode == 0, (label, result.stderr)
    assert b"PASS" in result.stdout
