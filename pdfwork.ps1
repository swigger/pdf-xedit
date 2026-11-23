$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    Write-Error 'Run setup.ps1 first to install the project environment.'
    exit 2
}
# PowerShell 5.1 may otherwise decode Python's UTF-8 bytes using an ANSI/OEM
# codepage. The caller still controls the encoding of its own > / Out-File;
# use the CLI's -o FILE for portable UTF-8 files on every PowerShell version.
$previousInputEncoding = [Console]::InputEncoding
$previousConsoleEncoding = [Console]::OutputEncoding
$previousOutputEncoding = $OutputEncoding
$exitCode = 2
try {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [Console]::InputEncoding = $utf8
    [Console]::OutputEncoding = $utf8
    $OutputEncoding = $utf8
    # Windows PowerShell 5.1 can promote native stderr into NativeCommandError
    # under Stop (notably with 2> redirection). Python's exit status is authoritative.
    $ErrorActionPreference = 'Continue'
    & $python -X utf8 -m pdfwork @args
    $exitCode = $LASTEXITCODE
} finally {
    $ErrorActionPreference = 'Stop'
    [Console]::InputEncoding = $previousInputEncoding
    [Console]::OutputEncoding = $previousConsoleEncoding
    $OutputEncoding = $previousOutputEncoding
}
exit $exitCode

