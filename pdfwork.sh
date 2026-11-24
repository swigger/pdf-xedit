#!/usr/bin/env bash
set -euo pipefail

project_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python="$project_dir/.venv/bin/python"
if [[ ! -x "$python" ]]; then
    printf '错误：请先运行 "%s/setup.sh" 安装项目环境。\n' "$project_dir" >&2
    exit 2
fi

# 不切换调用方的工作目录；输入输出路径仍相对当前目录。
exec "$python" -X utf8 -m pdfwork "$@"
