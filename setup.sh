#!/usr/bin/env bash
set -euo pipefail

project_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
venv_dir="$project_dir/.venv"
python="$venv_dir/bin/python"

if [[ ! -x "$python" ]]; then
    if [[ -e "$venv_dir" || -L "$venv_dir" ]]; then
        printf '错误：%s 不是可用的 Unix 虚拟环境。请先移走旧环境，再重新运行 setup.sh。\n' "$venv_dir" >&2
        exit 2
    fi
    python_command="${PYTHON:-python3}"
    if ! command -v "$python_command" >/dev/null 2>&1; then
        printf '错误：找不到 %s。请安装 Python 3.11+，或用 PYTHON=/path/to/python3 指定解释器。\n' "$python_command" >&2
        exit 2
    fi
    "$python_command" -c 'import sys; sys.exit("需要 Python 3.11 或更新版本") if sys.version_info < (3, 11) else None'
    "$python_command" -m venv "$venv_dir"
fi

"$python" -c 'import sys; sys.exit("现有 .venv 需要 Python 3.11+；请移走旧环境后重新初始化") if sys.version_info < (3, 11) else None'
"$python" -m pip install --editable "${project_dir}[test]"
"$python" -m pip check

printf '\n环境已就绪：%s\n' "$venv_dir"
printf 'PDF 入口："%s/pdfwork.sh" --help\n' "$project_dir"
printf '二维码："%s" "%s/qr.py" --help\n' "$python" "$project_dir"
printf '图片转换："%s" "%s/binimg.py" --help\n' "$python" "$project_dir"
if ! command -v pdftoppm >/dev/null 2>&1; then
    printf '可选：brew install poppler，启用 compare 的 Poppler 渲染器；未安装时请用 --renderer mupdf。\n'
fi
