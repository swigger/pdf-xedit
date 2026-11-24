#!/bin/bash
set -x

init(){
    rm -fr PDFDIR || true
    ../pdfwork.sh decompile input.pdf PDFDIR
    ../pdfwork.sh list PDFDIR -o input0.json
    if [ ! -f "input1.json" ] ; then
        cp input0.json input1.json
    fi
}

fixqr(){
    if [ "$1" = "" ] ; then
        return 1
    fi
    if [ ! -f "o.txt" ] ; then
        ../.venv/bin/python3 ../binimg.py bin2img $1 qr0.png
        ../.venv/bin/python3 ../qr.py scan qr0.png > o0.txt
        cp o0.txt o.txt
    fi
    WIDTH=$(python3 -c "import math;print(int(math.sqrt($(wc -c < $1)/3)))")
    cat o.txt | tr -d '\n' | ../.venv/bin/python3 ../qr.py gen --border 2 -w $WIDTH -o newqr.png
    ../.venv/bin/python3 ../binimg.py img2bin newqr.png $1
}

mkpdf(){
    ../pdfwork.sh batchedit PDFDIR input1.json
    ../pdfwork.sh compile PDFDIR out.pdf
}

if [ $(type -t "$1") = "function" ] ; then
    "$@"
fi
