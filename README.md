# PDFWork：可往返的 PDF 编辑工程

把 PDF 拆成 **完整对象图、原始内容指令、字体、图片和可编辑文字框**，修改后重新编译。未修改文字不会重新排版；修改文字保留原来字体、颜色、描边、变换和周围内容。实现对应本目录 `proj.txt`。

## 立即使用

工作目录：`D:\tmp\f`。已安装本地 `.venv`。PowerShell 中运行：

```powershell
Set-Location D:\tmp\f
.\pdfwork.ps1 decompile a.pdf my-project
.\pdfwork.ps1 list my-project --page 1
.\pdfwork.ps1 edit my-project p001-l0004 --text '2026年11月8日'
.\pdfwork.ps1 compile my-project edited.pdf
```

也可以直接用编辑器修改 `my-project/pages/001.json` 中的 `text`。项目目录必须为空；输出另存为新 PDF。编译失败不会覆盖已有输出。新电脑先运行 `setup.ps1`，需要 Python 3.11 或更新版本。`pdfwork.ps1` 可在其他工作目录调用，输入输出路径相对当前目录。

## UTF-8 输入输出与导出 JSON

所有程序写入的 JSON 文件统一为 **UTF-8、无 BOM、LF 换行**。JSON 输入使用 UTF-8，也兼容带 UTF-8 BOM 的文件；其他编码会给出明确错误。Python 的标准输入、标准输出和错误输出统一使用 UTF-8。

**保存文字结果请使用 `-o FILE` 或 `--output FILE`，由 Python 直接写文件。** Windows PowerShell 5.1 的 `>`/`Out-File` 默认会把输出重新编码成 UTF-16 LE；即使 Python 输出 UTF-8，外层重定向仍可能改变最终编码。PowerShell 启动脚本会在调用期间正确传递 UTF-8，并在结束后恢复原编码设置。

```powershell
.\pdfwork.ps1 list D:\tmp\f\b-work\BBB -o b1.json
.\pdfwork.ps1 list my-project --page 1 --output page1.json
.\pdfwork.ps1 fonts list my-project -o fonts-list.json
.\pdfwork.ps1 edit my-project p001-l0004 --text '2026年11月8日' -o edit-result.json
.\pdfwork.ps1 compile my-project edited.pdf -o build-result.json
```

所有产生 JSON 结果的子命令都支持该参数。指定 `-o` 后，结果只写入文件，错误仍写入 stderr；不指定时结果照常写入 stdout。`compile`、`render` 的 PDF/图像输出仍由原来的位置参数指定，`-o` 用于另存 JSON 摘要，`compare --report` 仍是完整比较报告。

主程本身就是 Python，PowerShell 只负责启动。也可以直接运行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pdfwork list D:\tmp\f\b-work\BBB -o b1.json
```

## 批量编辑 list 导出的 JSON

可以直接修改 `list` 导出的数组，再用 `batchedit` 导回工程：

```powershell
.\pdfwork.ps1 list D:\tmp\f\b-work\BBB -o b1.json
# 用编辑器修改 b1.json 中的 text、width、align 等可编辑字段，并以 UTF-8 保存。
.\pdfwork.ps1 batchedit D:\tmp\f\b-work\BBB b1.json
.\pdfwork.ps1 compile D:\tmp\f\b-work\BBB b1-edited.pdf
```

可选的预览和报告：

```powershell
.\pdfwork.ps1 batchedit D:\tmp\f\b-work\BBB b1.json --dry-run
.\pdfwork.ps1 batchedit D:\tmp\f\b-work\BBB b1.json -o batch-result.json
```

导入以当前工程为基准，**按 `id` 匹配，顺序无关，只写入实际有变化的页面文件**。同一份输入重复导入时，第二次的 `changed` 为 0，页面文件不会重写。报告列出提交数量、修改数量、未变化数量、修改页数，以及逐字段的修改前后值。

- 支持完整列表，也支持 `list --page`、`list --contains` 的筛选结果。输入中没有列出的记录保持不变，不会隐式删除。
- 可修改字段与单项 `edit` 相同：`text`、`mode`、`width`、`align`、`overflow`、`min_scale`、`tracking`、`offset_x`、`offset_y`。
- `id` 用于定位；`page`、`font`、`font_size`、`origin`、`fit_rms_pt`、`read_only` 等说明字段应保持不变。修改只读字段或增加未知字段会报错，避免悄悄忽略操作。
- 也支持精简记录，例如 `[{"id":"p001-l0003","text":"项目总结"}]`。记录中省略的字段保持原值。删除文字时把 `text` 设为空字符串。
- 重复 ID、未知 ID、页码不匹配、只读文字被修改和不合法的字段值，都会在写入任何页面之前拒绝整批操作。UTF-8 输入可带 BOM，结果文件为无 BOM UTF-8。
- 校验完成后先准备所有新页面文件，再逐个替换；普通写入错误会回滚已修改页面。如果回滚也遇到文件锁等错误，会保留原文件备份并报告恢复路径。此机制不承诺在进程被强杀、断电或其他程序同时写文件时实现数据库级事务。
- `--dry-run` 完成相同的输入校验和对照，但不修改工程。`batchedit` 更新工程 JSON，字体缺字、排版超宽等 PDF 构建检查仍由后续 `compile` 完成。

## 工程里是什么

| 文件 | 内容与用途 |
|---|---|
| `pages/001.json` 等 | 按页组织的可编辑单行文本框。修改 `text` 和排版参数 |
| `objects.json` | 完整 qpdf JSON v2 对象图，包括页面、资源、注释、书签等 |
| `streams/*.pdfops` | 已解压的页面与 Form 绘制指令，可直接作底层编辑 |
| `streams/*.bin` | 其他流，包括 CMap、ICC 等；格式由对象字典说明 |
| `fonts/*.ttf/otf/cff` | 真正参与编译的嵌入字体资源，objects.json 引用这些文件 |
| `images/*` | 原始图像流。JPEG/JP2 保留对应文件格式；其他原始像素流用 bin，解释参数在对象字典中 |
| `fonts.json` | 字体名称、已知字符、编码、字宽和字体资源位置的索引 |
| `layout.json` | 文字框到原指令的映射、原始参数和拟合结果；高层编辑时保留此文件 |
| `bindings.json` | 已通过校验的扩展字体绑定 |
| `project.json` | 工程版本、源文件哈希、页数、可编辑覆盖范围和警告 |

**工程没有暗藏原 PDF，也不会靠复制原 PDF 实现往返。** 编译完整读取上述对象和资源；原 PDF 被移走后也能编译，整个工程可以复制到其他目录。

选择一种编辑层级：直接修改文字框，或者直接修改原始对象/指令。若某内容流已被底层修改，又修改它的文字框，编译会检测冲突并要求先重新反编译。修改字体或图像原始数据时，需要同时维护相应对象字典参数。

## 单行文本框

```json
{
  "id": "p001-l0003",
  "text": "项目总结与展望",
  "mode": "natural",
  "width": 400,
  "align": "center",
  "overflow": "error",
  "min_scale": 0.6,
  "tracking": "auto",
  "offset_x": -100,
  "offset_y": 0
}
```

字段省略时继承原设置。其余 `font`、`font_size`、`origin`、`fit_rms_pt` 为说明字段，不是修改字体/字号的接口。

- `natural`：按每个字形的实际字宽重新排布，支持增加和删除文字，适合数字变宽和变长的修改。原来的基线和起点不变；字距通过原字形位置做最小二乘拟合。
- `slots`：每个新字形沿用对应旧字形的位置，必须字形数量相同。适合逐字替换；如果新字更宽，可能占满相邻槽位，通常优先用 natural。
- `width`：文本框宽度，单位为 PDF 用户空间的点；常见 PDF 中 72 点等于一英寸。
- `align`：left / center / right，按文本框左右边界对齐。
- `overflow`：error 表示放不下就报错；shrink 表示保持单行并等比缩小，低于 min_scale 则报错。不会偷偷换字体或跨行排版。
- `tracking`：auto 使用拟合值；数字表示额外字距，单位为点。
- `offset_x/offset_y`：相对原起点的平移；通常 x 向右、y 向上。坐标在页面旋转前的 PDF 用户空间。
- 删除文字时设 `text` 为 `""`；不要删除整个条目。ID 必须保持稳定。

不同字体、字号、颜色或绘制状态的内容保留为独立文字框。相同样式、相同基线、距离合理的相邻绘制片段可以合并；不会把相邻表格列强行合成一行。

## 复用、寻找和校验字体

```powershell
.\pdfwork.ps1 fonts list my-project

# 自动寻找 Windows / 用户字体目录，按名称筛选，再逐字形验证
.\pdfwork.ps1 fonts resolve my-project f5_0 --text '项目总结与展望'

# 指定候选字体，先验证再绑定；TTC 可指定 --index
.\pdfwork.ps1 fonts check my-project f5_0 C:\Windows\Fonts\simkai.ttf
.\pdfwork.ps1 fonts bind  my-project f5_0 C:\Windows\Fonts\simkai.ttf

# 本地没有时，自动从嵌入字体的家族、字重、斜体信息查询 Google Fonts
.\pdfwork.ps1 fonts resolve my-project FONT_ID --text 'NEW TEXT' --online

# 也可纠正不规范的家族名称，或增加本地搜索目录
.\pdfwork.ps1 fonts resolve my-project FONT_ID --text 'NEW TEXT' --online --family 'ABeeZee'
.\pdfwork.ps1 fonts resolve my-project FONT_ID --text 'NEW TEXT' --font-dir D:\fonts
```

本来存在的数字、字母或汉字直接复用嵌入子集，不要求安装字体。缺字时编译会给出字体 ID 和缺少的字符；不做替代字体降级。绑定成功后，原子集已有字符仍使用原字形，仅新增字形进入新的嵌入子集；复制和搜索仍有 Unicode 映射。

验证比较所有可对应的、非空白嵌入字形的归一化轮廓与水平字宽，至少需要三个有轮廓字形。轮廓 RMS 容差为 0.004 em，最大局部距离为 0.016 em，字宽容差为 0.004 em；另检查带方向的轮廓面积（容差 0.004 em²），防止边界相同但孔洞被填实的字形误通过。不同名称但相同外形的手动候选也可验证；**仅仅同名绝不构成通过条件**。编译重新检查绑定文件 SHA-256，并再次校验实际源字体。

Google Fonts 不是所有商业字体的镜像。找不到、下载不全或轮廓不匹配时会失败；可手动提供对应字体。`--online` 会向 Google 发送字体家族以及用于验证和补字的字符集合。下载的字体通常只含本次字符集合；下次补更多字时可能需要再次 resolve。

## 验证与示例

```powershell
.\pdfwork.ps1 compile my-project rebuilt.pdf
.\pdfwork.ps1 compare a.pdf rebuilt.pdf --report compare.json --renderer both --dpi 300
.\pdfwork.ps1 render rebuilt.pdf preview.png --page 1 --dpi 144
.\.venv\Scripts\python.exe -m pytest -q
```

`compare` 对所有页面逐 RGB 像素比较，分别使用 MuPDF 和 Poppler；需要 `pdftoppm` 在 PATH 上。本机已有。没有 Poppler 的机器可以指定 `--renderer mupdf`。两个引擎各自比较前后文件，不要求两个不同渲染引擎互相产生相同抗锯齿像素。

`examples/` 中包含：

1. `01-roundtrip.pdf`：a.pdf 完整拆装，文字未修改。
2. `02-digits.pdf`：日期改为“2026年11月8日”，未添加任何字体。
3. `03-chinese.pdf`：标题改为“项目总结”，校验系统楷体后补入“总结”。
4. `04-textbox.pdf`：标题增为“项目总结与展望”，扩大文本框并居中。
5. `05-variable-*.pdf`：人工构造宽度不同的 1 和 8；原文、编辑结果及直接排版的参考结果。
6. `06-google-*.pdf`：从 ABeeZee 子集出发，自动下载、验证并补入 XYZ。

每个示例都有可继续编辑的工程；`reports/` 保存实际验证数据。重新生成全部验收示例（包含联网验证）可以运行：

```powershell
.\.venv\Scripts\python.exe build_examples.py --output examples-new
```

离线测试：`python -m pytest -q -m 'not online'`。在线测试显式标为 `online`，实际访问 Google CDN，没有用模拟请求代替下载验证。

## 支持范围与边界

- 完整对象往返不依赖文本能否识别；无法高层编辑的绘制内容仍完整保留。按本机实际渲染测试报告描述保真结果，不承诺任意损坏 PDF 都能正常解析。
- 高层编辑支持水平、从左到右的单行文字，常见简单字体和 Type0 Identity-H / 显式编码流，包含嵌套或多次引用的 Form。修改共享 Form 时克隆被编辑的调用路径，不改变其他实例。
- 字体形状校验和扩展支持 TrueType、OpenType 和原始 CFF 字体。旧式 Type1 PFB 字体可在已知编码范围内编辑；其完整字体匹配目前不支持。Type3、竖排、复杂脚本重塑、继承外部命名 CMap、文本剪裁和 ActualText 等情况可能标记为只读，原因在工程中显示；原始对象仍可编辑。
- 不恢复 Word 段落样式、不做跨行或跨页自动排版，不提供 OCR。透明度、描边、颜色和原先的字体变换通过保留上下文维持。
- 对未修改文字直接保留原始指令，因此不引入重排误差。编辑后的排版以 MuPDF 实测字宽校准；不同引擎的字体度量取整可能有微小差别，需用 compare 或预览评估具体文件。
- 输出文件会重新序列化、压缩和建立交叉引用，二进制不要求一致。带数字签名文件的重新编译不保留原签名有效性；程序不是签名工具。

## 实现位置

- `pdfwork/project.py`：完整对象与资源拆装、原子发布输出。
- `pdfwork/text.py`：PDF 文字状态解释、水平单行识别、拟合和局部指令改写。
- `pdfwork/editing.py`：list 视图、按 ID 批量对照、字段校验和页面写入回滚。
- `pdfwork/fonts.py`：编码、字体子集复用、系统/网络查找、轮廓校验、缺字子集嵌入。
- `pdfwork/verify.py`：双渲染器逐像素验证与预览。
- `tests/`：可重复的行为和保真验证。

依赖 API 说明：[pikepdf](https://pikepdf.readthedocs.io/en/latest/)、[qpdf JSON](https://qpdf.readthedocs.io/en/stable/json.html)、[MuPDF](https://pymupdf.readthedocs.io/)、[FontTools](https://fonttools.readthedocs.io/)。依赖项目各自的许可证保持适用，尤其 PyMuPDF 的 AGPL/商业双许可；本目录实现没有变更这些依赖的许可。
