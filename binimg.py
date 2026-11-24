#!/usr/bin/env python3
"""无文件头的 RGB24 BIN 与无损 PNG 互转。

BIN 像素顺序：从左到右、从上到下，每像素 R/G/B 各一字节，无行填充。
未经修改的 bin2img 输出再经 img2bin 转换，所得 BIN 与原文件逐字节一致。
依赖：Pillow，统一由项目的 setup.sh（Windows 用 setup.ps1）安装。
用法：.venv/bin/python binimg.py bin2img input.bin output.png
"""

import argparse
import math
from pathlib import Path
import sys


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("尺寸必须是正整数")
    return number


def dimensions(byte_count, width=None, height=None):
    if byte_count == 0 or byte_count % 3:
        raise ValueError("RGB24 数据不能为空，且文件大小必须是 3 的倍数")
    pixels = byte_count // 3
    if width is None and height is None:
        width = height = math.isqrt(pixels)
        if width * height != pixels:
            raise ValueError("无法推断正方形尺寸；请用 --width 或 --height 指定尺寸")
    elif width is None:
        width, remainder = divmod(pixels, height)
        if remainder or width == 0:
            raise ValueError("文件大小与指定的高度不匹配")
    elif height is None:
        height, remainder = divmod(pixels, width)
        if remainder or height == 0:
            raise ValueError("文件大小与指定的宽度不匹配")
    if width * height * 3 != byte_count:
        raise ValueError(
            f"尺寸 {width}×{height} 需要 {width * height * 3} 字节，"
            f"实际为 {byte_count} 字节"
        )
    return width, height


def bin2img(args, Image):
    if args.output.suffix.lower() != ".png":
        raise ValueError("为保证无损，bin2img 仅输出 PNG；请使用 .png 扩展名")
    data = args.input.read_bytes()
    size = dimensions(len(data), args.width, args.height)
    with Image.frombytes("RGB", size, data) as image:
        image.save(args.output, format="PNG")
    return size, len(data)


def img2bin(args, Image):
    with Image.open(args.input) as image:
        if getattr(image, "n_frames", 1) != 1:
            raise ValueError("不支持多帧图片，请先导出单帧 PNG")
        if image.mode not in ("1", "L", "LA", "P", "RGB", "RGBA"):
            raise ValueError(f"不支持图片模式 {image.mode}；请使用 8 位 RGB PNG")
        if "A" in image.getbands() or "transparency" in image.info:
            with image.convert("RGBA") as rgba:
                if rgba.getextrema()[3] != (255, 255):
                    raise ValueError("图片含透明像素；RGB24 无法保存透明度，请先去除透明度")
        # 不应用 EXIF 旋转、色彩管理、缩放或上下翻转，只读取存储的像素。
        with image.convert("RGB") as rgb:
            data = rgb.tobytes()
            size = rgb.size
    args.output.write_bytes(data)
    return size, len(data)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="RGB24 原始 BIN 与无损 PNG 互转（左上角为原点，逐行，无填充）",
        epilog="示例：%(prog)s bin2img 8-0.bin output.png；"
        "%(prog)s img2bin output.png restored.bin",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    to_image = commands.add_parser("bin2img", help="原始 RGB24 BIN → PNG")
    to_image.add_argument("input", type=Path, help="输入 BIN")
    to_image.add_argument("output", type=Path, help="输出 PNG")
    to_image.add_argument("--width", type=positive_int, help="宽度；仅指定宽度时推算高度")
    to_image.add_argument("--height", type=positive_int, help="高度；默认尝试正方形尺寸")
    to_image.set_defaults(convert=bin2img)
    to_bin = commands.add_parser("img2bin", help="图片 → 原始 RGB24 BIN")
    to_bin.add_argument("input", type=Path, help="输入图片，建议使用无损 PNG")
    to_bin.add_argument("output", type=Path, help="输出 BIN")
    to_bin.set_defaults(convert=img2bin)
    args = parser.parse_args(argv)

    try:
        from PIL import Image
    except ImportError:
        parser.exit(1, "错误：缺少 Pillow；请先运行项目的 setup.sh（Windows 用 setup.ps1），再使用 .venv 中的 Python\n")

    try:
        same_path = args.input.resolve() == args.output.resolve()
        same_file = args.output.exists() and args.input.samefile(args.output)
        if same_path or same_file:
            raise ValueError("输入和输出不能是同一个文件")
        (width, height), byte_count = args.convert(args, Image)
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        parser.exit(1, f"错误：{error}\n")
    print(f"{args.output}: {width}×{height}, RGB24, {byte_count} 字节原始像素")
    return 0


if __name__ == "__main__":
    sys.exit(main())
