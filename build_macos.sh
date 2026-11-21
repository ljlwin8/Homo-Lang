#!/usr/bin/env bash
# 把 homo 桌面编辑器打包成 macOS .app。
# 用法：./build_macos.sh
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
VENV=".build-venv"

if ! command -v "$PY" >/dev/null 2>&1; then
    echo "找不到 Python：$PY" >&2
    echo "请安装 Python 3.9+，或用 PYTHON=/path/to/python3 ./build_macos.sh 指定。" >&2
    exit 1
fi

if ! "$PY" -c 'import tkinter' >/dev/null 2>&1; then
    echo "当前 Python 没有 Tk。请安装 python.org 的 Python，或 brew install python-tk。" >&2
    exit 1
fi

if [ ! -x "$VENV/bin/python" ]; then
    echo "创建打包环境：$VENV"
    "$PY" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --upgrade pip pyinstaller >/dev/null

echo "正在打包 macOS 应用…"
python -m PyInstaller \
    --noconfirm --clean --windowed \
    --name "HomoEditor" \
    --icon "assets/icon.icns" \
    --osx-bundle-identifier "link.homo.editor" \
    --add-data "homo_runtime.hpp:." \
    --add-data "assets/icon-256.png:assets" \
    --add-data "assets/icon-64.png:assets" \
    --add-data "assets/icon-32.png:assets" \
    --distpath dist --workpath build/pyinstaller \
    homo_editor.py

echo
echo "完成：dist/HomoEditor.app"
echo "运行：open dist/HomoEditor.app"
echo "注意：目标机器仍需安装 C++ 编译器（clang++ / g++）。"
