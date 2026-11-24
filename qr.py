#!/usr/bin/env python3
"""二维码识别与生成；链接和其他内容均只作为文本，不打开或执行。

用法：
    .venv/bin/python qr.py scan 8.png
    .venv/bin/python qr.py scan --no-newline 8.png > decoded.txt
    .venv/bin/python qr.py gen --width 600 --output code.png < input.txt
    .venv/bin/python qr.py gen -o - < input.txt > code.png

先运行 ./setup.sh 安装项目统一依赖；激活 .venv 后也可直接运行 ./qr.py。
输入为 UTF-8 文本，原样保留空格和换行。scan 默认在每个结果后追加换行。
"""

import argparse
from pathlib import Path
import sys


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def nonnegative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("不能是负数")
    return number


def scan(args, Image):
    import zxingcpp
    from PIL import ImageOps

    with Image.open(args.image) as source:
        # 应用照片方向，并把透明区域合成到白底，避免透明背景变黑。
        rgba = ImageOps.exif_transpose(source).convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        image = Image.alpha_composite(background, rgba).convert("RGB")
        codes = zxingcpp.read_barcodes(
            image,
            formats=zxingcpp.BarcodeFormat.QRCode,
            text_mode=zxingcpp.TextMode.Plain,
        )
    if not codes:
        raise ValueError(f"未识别到二维码：{args.image}")
    # 内容只写入标准输出，不解析 URL，也不调用浏览器或 shell。
    text = "\n".join(code.text for code in codes)
    sys.stdout.write(text + ("" if args.no_newline else "\n"))
    sys.stdout.flush()


def gen(args, Image):
    import qrcode
    from qrcode.exceptions import DataOverflowError

    if sys.stdin.isatty():
        raise ValueError("请通过重定向或管道提供 UTF-8 文本，例如：gen < input.txt")
    data = sys.stdin.buffer.read()
    if not data:
        raise ValueError("输入文本不能为空")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("输入不是有效的 UTF-8 文本") from error

    code = qrcode.QRCode(
        error_correction=getattr(qrcode.constants, f"ERROR_CORRECT_{args.error_correction}"),
        box_size=1,
        border=args.border,
    )
    code.add_data(text)
    try:
        code.make(fit=True)
    except DataOverflowError as error:
        raise ValueError("文本超过单个二维码的容量，请缩短文本或降低纠错等级") from error

    modules = code.modules_count + 2 * args.border
    scale = args.width // modules
    if scale < 1:
        raise ValueError(f"宽度太小：当前内容和留白至少需要 {modules} 像素")
    # 整数倍放大保持每个模块等宽，多出的像素作为外部留白，最终尺寸精确匹配 width。
    code.box_size = scale
    symbol = code.make_image(fill_color="black", back_color="white").convert("RGB")
    image = Image.new("RGB", (args.width, args.width), "white")
    padding = (args.width - symbol.width) // 2
    image.paste(symbol, (padding, padding))
    if args.output == "-":
        image.save(sys.stdout.buffer, format="PNG")
        sys.stdout.buffer.flush()
    else:
        image.save(Path(args.output), format="PNG")


def main(argv=None):
    parser = argparse.ArgumentParser(description="二维码识别与 PNG 生成；所有内容均只作为文本")
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser("scan", help="识别图片中的二维码，文本写入标准输出")
    read.add_argument("image", type=Path, help="输入图片路径，例如 8.png")
    read.add_argument(
        "-n", "--no-newline", action="store_true",
        help="不追加末尾换行（单个二维码时可原样还原文本）；多个结果仍以换行分隔",
    )
    read.set_defaults(run=scan)

    write = commands.add_parser("gen", help="从标准输入读取 UTF-8 文本，生成 PNG")
    write.add_argument("-w", "--width", type=positive_int, default=512,
                       help="输出正方形的边长，单位像素（默认：512）")
    write.add_argument("-o", "--output", default="qr.png",
                       help="输出文件名（默认：qr.png）；使用 - 将 PNG 写入标准输出")
    write.add_argument("--border", type=nonnegative_int, default=4,
                       help="留白宽度，单位为二维码模块（默认：4，建议至少 4）")
    write.add_argument("-e", "--error-correction", type=str.upper,
                       choices=("L", "M", "Q", "H"), default="M",
                       help="纠错等级，由低到高 L/M/Q/H（默认：M）")
    write.set_defaults(run=gen)
    args = parser.parse_args(argv)

    try:
        from PIL import Image
    except ImportError:
        parser.exit(1, "错误：缺少 Pillow；请先运行项目的 setup.sh（Windows 用 setup.ps1），再使用 .venv 中的 Python\n")
    try:
        args.run(args, Image)
    except ImportError as error:
        parser.exit(1, f"错误：缺少依赖 {error.name}；请先运行项目的 setup.sh（Windows 用 setup.ps1），"
                    "再使用 .venv 中的 Python\n")
    except BrokenPipeError:
        # 管道下游提前结束（例如 head）时，不打印 traceback 或再次刷新失败的管道。
        import os
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
        return 1
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        parser.exit(1, f"错误：{error}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
