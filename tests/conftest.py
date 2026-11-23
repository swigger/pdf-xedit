import io
from pathlib import Path

import pytest
import reportlab
from fontTools.ttLib import TTFont as FontToolsFont
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


@pytest.fixture
def vera():
    return Path(reportlab.__file__).parent / "fonts/Vera.ttf"


@pytest.fixture
def bold():
    return Path(reportlab.__file__).parent / "fonts/VeraBd.ttf"


@pytest.fixture
def make_pdf(tmp_path, vera):
    def make(text="AV 1234567890", variable=False, tail=False):
        path = tmp_path / "source.pdf"
        font_path = vera
        if variable:
            font = FontToolsFont(vera)
            cmap = font.getBestCmap()
            for char, width in (("1", 650), ("8", 1900)):
                glyph = cmap[ord(char)]
                font["hmtx"][glyph] = (width, font["hmtx"][glyph][1])
            # ReportLab caches dynamic fonts by internal PostScript name.
            for record in font["name"].names:
                if record.nameID == 6:
                    record.string = "PDFWorkVariableFixture".encode(record.getEncoding())
            font_path = tmp_path / "variable.ttf"; font.save(font_path)
        name = "Fixture" + str(id(font_path))
        pdfmetrics.registerFont(TTFont(name, str(font_path)))
        c = canvas.Canvas(str(path), pagesize=(500, 300))
        c.setFillColorRGB(.93, .95, .98); c.rect(0, 0, 500, 300, fill=1, stroke=0)
        c.setFillColorRGB(.1, .2, .3)
        t = c.beginText(40, 200); t.setFont(name, 24)
        t.textOut(text)
        if tail:
            t.textLine(""); t.textOut("Unchanged following line")
        c.drawText(t)
        c.setStrokeColorRGB(.2, .4, .8); c.line(20, 80, 470, 80)
        c.save()
        return path
    return make

