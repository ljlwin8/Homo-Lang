#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""homo 桌面编辑器：Windows / macOS 原生窗口，支持多文件项目与 inm 导入。

运行：
    python3 homo_editor.py [项目目录 | 主程序.homo]

打包见 build_windows.bat / build_macos.sh。
"""

from __future__ import annotations

import codecs
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk
from typing import Optional

import homo

APP_NAME = "homo 编辑器"
APP_VERSION = "1.1.4"
IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt"
MOD = "Command" if IS_MAC else "Control"
FILE_EXTS = (".homo",)
MAX_OUTPUT_CHARS = 1_000_000
SKIP_DIRS = {"__pycache__", ".git", ".svn", ".hg", ".idea", ".vscode",
             "dist", "build", "node_modules", ".venv", "venv"}


# --------------------------------------------------------------------------
# 颜色 / 字体
# --------------------------------------------------------------------------
COLORS = {
    "bg": "#191c22", "panel": "#14171c", "panel2": "#20242c",
    "border": "#2b303a", "fg": "#e7ebf2", "muted": "#8a94a6",
    "accent": "#d7b45b", "accent_text": "#171204",
    # 编辑器/行号底色沿用旧网页版的 --panel-2 与 gutter 配色。
    "editor": "#0f1319", "editor_fg": "#ffffff",
    "gutter": "#0c1015", "gutter_fg": "#465364",
    "select": "#2f4b6e", "current": "#1a2029", "errorline": "#4a2027",
    "cursor": "#ffd479", "ok": "#8fd694", "err": "#ff7b72",
    "warn": "#e3b341", "info": "#79c0ff", "button": "#262b34",
    "button_hover": "#313845", "search": "#3d4b2a", "search_cur": "#7a6a20",
}


def _safe_print(*args, **kwargs) -> None:
    """--windowed 打包后标准流可能是 None，自检/错误回退时不要因此崩溃。"""
    stream = kwargs.get("file") or sys.stdout
    if stream is not None:
        print(*args, **kwargs)


def _no_window_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WIN else 0


def _pick_font(root: tk.Misc, families: set[str], candidates: list[str],
               size: int, weight: str = "normal") -> tkfont.Font:
    for name in candidates:
        if name in families:
            return tkfont.Font(root, family=name, size=size, weight=weight)
    return tkfont.Font(root, size=size, weight=weight)


def normalize_path(path: str) -> str:
    """统一成绝对真实路径，避免 /tmp 与 /private/tmp 这类符号链接造成重复标签。"""
    return os.path.realpath(os.path.abspath(path))


SOURCE_EXTS = (".homo",)


def normalize_save_path(path: str, default_ext: str = ".homo") -> str:
    """修正保存对话框重复追加后缀（main.homo.homo）。"""
    path = os.path.normpath(path)
    # 文件对话框可能把默认后缀再加在已有后缀后面；重复几次也一并收掉。
    changed = True
    while changed:
        changed = False
        lower = path.lower()
        for ext in SOURCE_EXTS:
            if lower.endswith(ext):
                stem = path[:-len(ext)]
                if stem.lower().endswith(SOURCE_EXTS):
                    path = stem
                    changed = True
                    break
    lower = path.lower()
    if not lower.endswith(SOURCE_EXTS):
        base = os.path.basename(path)
        if not os.path.splitext(base)[1]:
            path += default_ext
    return path


def read_text_file(path: str) -> str:
    with open(path, "rb") as f:
        data = f.read()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def write_text_file(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


# --------------------------------------------------------------------------
# 语法着色
# --------------------------------------------------------------------------


QUOTES = set(homo.QUOTE_CLOSERS)
CLOSERS = homo.QUOTE_CLOSERS
PUNCT = set(homo.PUNCTUATION)


# 与旧网页版 #highlight 完全一致的语法配色。
SYNTAX_COLORS = {
    "decl": "#ef8377",       # 你是一个一个一个 / 一坨
    "kw": "#c678dd",         # 控制流、inm、新建等关键字
    "op": "#ffffff",         # 迫真
    "bool": "#2e4fd0",       # 确信 / 大嘘
    "fn": "#c678dd",         # 函数调用
    "cls": "#4ec9b0",        # 类名
    "class": "#ef8377",      # 类
    "param": "#d19a66",      # 函数参数
    "loopvar": "#3b6fe0",    # 回数
    "null": "#2e4fd0",       # 虚无
    "id": "#ffffff",         # 普通变量
    "sep": "#ffffff",        # 事 / 罢 / 噗叽啪 / 运算符
    "num": "#b6c9a8",        # 数字
    "str": "#9cdcfe",        # 字符串
    "tpl": "#9cdcfe",        # 模板大括号
    "punc": "#9cdcfe",       # 标点
    "com": "#7f848e",        # 注释
    "b0": "#ffd866",         # 花括号按嵌套深度循环：黄
    "b1": "#c678dd",         # 紫
    "b2": "#61afef",         # 蓝
}

# 旧网页版 HL_WORDS：关键字按“词”而不是按单个字符着色。
HL_WORDS = {
    homo.DECL: "decl",
    homo.VARIADIC_KW: "decl",
    homo.DECL_END: "sep",
    homo.ASSIGN: "sep",
    homo.STMT_END: "sep",
    homo.CALL_END: "sep",
    homo.IF_HEAD: "kw",
    homo.IF_MID: "kw",
    homo.ELSEIF_HEAD: "kw",
    homo.ELSE: "kw",
    homo.ELSEIF_TAIL: "kw",
    "极霸矛": "kw",
    homo.WHILE_HEAD: "kw",
    homo.SWITCH_HEAD: "kw",
    homo.CASE_KW: "kw",
    homo.DEFAULT_KW: "kw",
    homo.DO_HEAD: "kw",
    homo.DO_TAIL: "kw",
    homo.REPEAT_MID: "kw",
    homo.REPEAT_VAR: "loopvar",
    homo.REPEAT_TAIL: "kw",
    homo.BREAK_KW: "kw",
    homo.CONTINUE_KW: "kw",
    homo.RETURN_KW: "kw",
    homo.CLASS_KW: "class",
    homo.NEW_KW: "kw",
    homo.PUBLIC_KW: "kw",
    homo.PRIVATE_KW: "kw",
    "便加": "sep",
    "便减": "sep",
    "便乘": "sep",
    "便除": "sep",
    homo.NOT_KW: "op",
    homo.MOD_KW: "sep",
    homo.OR_KW: "sep",
    homo.AND_KW: "sep",
    homo.LT_KW: "sep",
    homo.EQ_HEAD: "sep",
    homo.TRUE_KW: "bool",
    homo.FALSE_KW: "bool",
    homo.NULL_KW: "null",
    "脱出": "fn",
    "读入": "fn",
    "读行": "fn",
    homo.RANDOM_KW: "fn",
    homo.LENGTH_KW: "fn",
    "取子串": "fn",
    "查找": "fn",
    "包含": "fn",
    "替换": "fn",
    "分割": "fn",
    "连接": "fn",
    "去空白": "fn",
    "转数字": "fn",
    "转大写": "fn",
    "转小写": "fn",
    "inm": "kw",
    "from": "kw",
}


def _skip_hl_string(src: str, i: int) -> int:
    """从开引号开始跳过整个字符串，返回结束后的下标。"""
    n = len(src)
    opening = src[i]
    j = i + 1
    while j < n:
        if src[j] == "\\" and j + 1 < n:
            j += 2
            continue
        if src[j] in CLOSERS.get(opening, opening):
            return j + 1
        j += 1
    return n


def _hl_matching_paren(src: str, pos: int) -> int:
    """和网页版一样，找到与 pos 处左括号配对的右括号，跳过字符串/注释。"""
    n = len(src)
    depth = 0
    while pos < n:
        ch = src[pos]
        if ch in QUOTES:
            pos = _skip_hl_string(src, pos)
            continue
        if src.startswith("//", pos):
            nl = src.find("\n", pos)
            pos = n if nl < 0 else nl
            continue
        if src.startswith("/*", pos):
            close = src.find("*/", pos + 2)
            pos = n if close < 0 else close + 2
            continue
        if ch in "(（":
            depth += 1
        elif ch in ")）":
            depth -= 1
            if depth == 0:
                return pos
        pos += 1
    return -1


def _hl_definition_params(src: str, paren_pos: int) -> Optional[list[str]]:
    """识别 name(参数, 参数){，返回参数名；普通调用返回 None。"""
    close = _hl_matching_paren(src, paren_pos)
    if close < 0:
        return None
    k = close + 1
    n = len(src)
    while k < n and src[k].isspace():
        k += 1
    if k >= n or src[k] != "{":
        return None
    params: list[str] = []
    for part in re.split(r"[,，]", src[paren_pos + 1:close]):
        piece = part.strip()
        piece = re.sub(r"^一坨\s+", "", piece)
        bits = piece.split()
        name = bits[0] if bits else ""
        if not name:
            continue
        # 与网页版相同的过滤：名字里不能有空白、括号、逗号、引号，也不能以数字开头。
        if re.search(r'[\s()（）,，"]', name):
            continue
        if name[0].isdigit():
            continue
        params.append(name)
    return params


def _hl_defined_functions(src: str) -> set[str]:
    """先扫一遍文件里定义的函数/方法名。"""
    defined: set[str] = set()
    n = len(src)
    i = 0
    while i < n:
        ch = src[i]
        if ch in QUOTES:
            i = _skip_hl_string(src, i)
            continue
        if src.startswith("//", i):
            nl = src.find("\n", i)
            i = n if nl < 0 else nl
            continue
        if src.startswith("/*", i):
            close = src.find("*/", i + 2)
            i = n if close < 0 else close + 2
            continue
        if ch.isspace() or ch in PUNCT:
            i += 1
            continue
        start = i
        while i < n and not src[i].isspace() and src[i] not in PUNCT \
                and src[i] not in QUOTES:
            i += 1
        word = src[start:i]
        p = i
        while p < n and src[p].isspace():
            p += 1
        if p < n and src[p] in "(（" and _hl_definition_params(src, p) is not None:
            defined.add(word)
    return defined


def _inm_line_comment(text: str) -> str:
    """去掉 inm 行里字符串之外的 // 注释。"""
    quote: Optional[str] = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote is not None:
            if ch == "\\":
                i += 2
                continue
            if ch in CLOSERS.get(quote, quote):
                quote = None
            i += 1
            continue
        if ch in QUOTE_CHARS:
            quote = ch
            i += 1
            continue
        if text.startswith("//", i):
            return text[:i]
        i += 1
    return text


def _override_span(spans: list[tuple[int, int, str]], start: int, end: int,
                   kind: str) -> list[tuple[int, int, str]]:
    if end <= start:
        return spans
    out: list[tuple[int, int, str]] = []
    for a, b, k in spans:
        if b <= start or a >= end:
            out.append((a, b, k))
            continue
        if a < start:
            out.append((a, start, k))
        if b > end:
            out.append((end, b, k))
    out.append((start, end, kind))
    return out


def _highlight_inm_targets(source: str,
                           spans: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """inm 后面的文件名按字符串颜色显示。"""
    offset = 0
    for line in source.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        stripped = body.lstrip()
        indent = len(body) - len(stripped)
        if stripped.startswith("inm") and (len(stripped) == 3 or stripped[3].isspace()):
            code_start = offset + indent
            if code_context_at(source, code_start) == "code":
                rest_start = code_start + 3
                rest = _inm_line_comment(source[rest_start:offset + len(body)])
                select = None
                if not rest.lstrip().startswith(tuple(QUOTE_CHARS)):
                    select = re.match(r"\s*(\S+)\s+from\s+(.+?)\s*;?\s*$", rest)
                if select:
                    class_name = select.group(1)
                    class_pos = rest_start + select.start(1)
                    spans = _override_span(spans, class_pos,
                                           class_pos + len(class_name), "cls")
                    raw = select.group(2)
                    target = raw.strip().rstrip(";").rstrip()
                    if target:
                        pos = rest_start + select.start(2) + raw.index(target)
                        spans = _override_span(spans, pos, pos + len(target), "str")
                else:
                    raw = rest.strip()
                    target = raw.rstrip(";").rstrip()
                    if target:
                        pos = rest_start + rest.index(target)
                        spans = _override_span(spans, pos, pos + len(target), "str")
        offset += len(line)
    return spans


def highlight_tokens(source: str) -> list[tuple[int, int, str]]:
    """把源码切成与旧网页版一致的语法 token：返回 (起, 止, 颜色类别)。

    逻辑移植自旧版 index.html 的 highlightHomo：识别函数/方法、参数、
    类名、函数调用，并给嵌套花括号循环上色。
    """
    spans: list[tuple[int, int, str]] = []
    n = len(source)
    i = 0
    scopes: list[Optional[list[str]]] = []
    pending_params: Optional[list[str]] = None
    expect_class = False
    bracket_depth = 0
    defined = _hl_defined_functions(source)

    def emit(kind: str, start: int, end: int) -> None:
        if kind and end > start:
            spans.append((start, end, kind))

    def scan_string() -> None:
        nonlocal i
        opening = source[i]
        segment = i
        i += 1
        while i < n:
            ch = source[i]
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch in CLOSERS.get(opening, opening):
                i += 1
                emit("str", segment, i)
                return
            if ch == "{":
                emit("str", segment, i)
                emit("tpl", i, i + 1)
                i += 1
                scan_code(True)
                if i < n and source[i] == "}":
                    emit("tpl", i, i + 1)
                    i += 1
                segment = i
                continue
            i += 1
        emit("str", segment, n)

    def scan_code(stop_at_brace: bool) -> None:
        nonlocal i, pending_params, expect_class, bracket_depth
        while i < n:
            ch = source[i]
            if stop_at_brace and ch == "}":
                return
            if source.startswith("//", i):
                nl = source.find("\n", i)
                nl = n if nl < 0 else nl
                emit("com", i, nl)
                i = nl
                continue
            if source.startswith("/*", i):
                close = source.find("*/", i + 2)
                close = n if close < 0 else close + 2
                emit("com", i, close)
                i = close
                continue
            if ch in QUOTES:
                scan_string()
                continue
            if ch.isdigit():
                j = i + 1
                while j < n and (source[j].isdigit() or source[j] == "."):
                    j += 1
                emit("num", i, j)
                i = j
                continue
            if ch in "+-*/":
                emit("sep", i, i + 1)
                i += 1
                continue
            if ch == "{":
                emit("b%d" % (len(scopes) % 3), i, i + 1)
                scopes.append(pending_params)
                pending_params = None
                i += 1
                continue
            if ch == "}":
                emit("b%d" % (max(0, len(scopes) - 1) % 3), i, i + 1)
                if scopes:
                    scopes.pop()
                i += 1
                continue
            if ch in "(（[":
                emit("b%d" % ((len(scopes) + bracket_depth) % 3), i, i + 1)
                bracket_depth += 1
                i += 1
                continue
            if ch in ")）]":
                bracket_depth = max(0, bracket_depth - 1)
                emit("b%d" % ((len(scopes) + bracket_depth) % 3), i, i + 1)
                i += 1
                continue
            if ch in PUNCT:
                emit("punc", i, i + 1)
                i += 1
                continue
            if ch.isspace():
                i += 1
                continue
            start = i
            while i < n and not source[i].isspace() and source[i] not in PUNCT \
                    and source[i] not in QUOTES:
                i += 1
            if i == start:
                i += 1
                continue
            word = source[start:i]
            q = i
            while q < n and source[q].isspace():
                q += 1
            has_call = q < n and source[q] in "(（"
            kind = HL_WORDS.get(word)
            # 「说」只在相等比较的右括号后面才算语法；普通变量保持白色。
            if word == "说" and kind is None:
                pb = start - 1
                while pb >= 0 and source[pb].isspace():
                    pb -= 1
                if pb >= 0 and source[pb] in ")）":
                    kind = "sep"
            # 内置函数名只有后面跟括号时才按函数着色。
            if kind == "fn" and not has_call:
                kind = None
            want_class = expect_class
            expect_class = False
            if kind is None:
                if want_class:
                    kind = "cls"
                elif pending_params is not None and word in pending_params:
                    kind = "param"
                elif has_call and word in defined:
                    kind = "fn"
                else:
                    kind = "id"
                    for scope in scopes:
                        if scope is not None and word in scope:
                            kind = "param"
                            break
                if has_call:
                    params = _hl_definition_params(source, q)
                    if params is not None:
                        pending_params = params
            emit(kind, start, i)
            if word in ("类", "新建"):
                expect_class = True

    scan_code(False)
    return _highlight_inm_targets(source, spans)

# 与旧网页版一致的括号 / 引号自动配对。
AUTO_PAIRS = {"{": "}", "(": ")", "[": "]", "（": "）"}
PAIR_CLOSERS = {"}": "}", ")": ")", "]": "]", "）": "）"}
QUOTE_PAIRS = {'"': '"', "'": "'", "“": "”", "‘": "’"}
QUOTE_CLOSERS = {'"': '"', "'": "'", "”": "”", "’": "’"}
QUOTE_FAMILY = {'"': 1, "“": 1, "”": 1, "'": 2, "‘": 2, "’": 2}
QUOTE_CHARS = set(QUOTE_FAMILY)

AC_KIND_LABELS = {
    "decl": "声明", "sep": "语法", "kw": "关键字", "op": "运算符",
    "bool": "布尔", "fn": "内置", "cls": "类名", "loopvar": "循环变量",
    "null": "空值", "class": "关键字",
}
AC_KEYWORDS = [(word, AC_KIND_LABELS.get(kind, "关键字"))
               for word, kind in HL_WORDS.items()]
AC_MAX = 9


def _is_ident_char(ch: str) -> bool:
    return (("0" <= ch <= "9") or ("A" <= ch <= "Z") or ("a" <= ch <= "z")
            or ch == "_" or ("\u4e00" <= ch <= "\u9fff"))


def identifier_prefix(text: str) -> str:
    """取光标前连续的标识符字符。"""
    i = len(text)
    while i > 0 and _is_ident_char(text[i - 1]):
        i -= 1
    return text[i:]


def code_context_at(src: str, pos: int) -> str:
    """pos 处在代码、字符串还是注释中；模板 {…} 内按代码处理。"""
    i = 0
    n = min(pos, len(src))
    opening: Optional[str] = None
    block_comment = False
    templates: list[str] = []
    while i < n:
        ch = src[i]
        if opening is not None:
            if ch == "\\":
                i += 2
                continue
            if ch in CLOSERS.get(opening, opening):
                opening = None
                i += 1
                continue
            if ch == "{":
                templates.append(opening)
                opening = None
                i += 1
                continue
            i += 1
            continue
        if block_comment:
            if ch == "*" and i + 1 < n and src[i + 1] == "/":
                block_comment = False
                i += 2
            else:
                i += 1
            continue
        if src.startswith("//", i):
            while i < n and src[i] != "\n":
                i += 1
            if i >= n:
                return "comment"
            continue
        if src.startswith("/*", i):
            block_comment = True
            i += 2
            continue
        if ch in QUOTE_CHARS:
            opening = ch
            i += 1
            continue
        if ch == "}" and templates:
            opening = templates.pop()
            i += 1
            continue
        i += 1
    if block_comment:
        return "comment"
    if opening is not None:
        return "string"
    return "code"


def collect_doc_names(src: str) -> list[tuple[str, str]]:
    """扫描当前文档代码部分的标识符，跳过字符串、注释和关键字。"""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    n = len(src)
    i = 0
    while i < n:
        ch = src[i]
        if ch in QUOTE_CHARS:
            opening = ch
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] in CLOSERS.get(opening, opening):
                    i += 1
                    break
                i += 1
            continue
        if src.startswith("//", i):
            while i < n and src[i] != "\n":
                i += 1
            continue
        if src.startswith("/*", i):
            close = src.find("*/", i + 2)
            i = n if close < 0 else close + 2
            continue
        if ch.isspace() or ch in PUNCT:
            i += 1
            continue
        start = i
        while i < n and not src[i].isspace() and src[i] not in PUNCT \
                and src[i] not in QUOTE_CHARS:
            i += 1
        word = src[start:i]
        if (word and word not in HL_WORDS and not word[0].isdigit()
                and word not in seen):
            seen.add(word)
            out.append((word, "名字"))
    return out


def scope_depth_at(src: str, pos: int) -> int:
    """光标前未闭合的括号层数；字符串、注释里的括号不算。"""
    depth = 0
    i = 0
    n = min(pos, len(src))
    while i < n:
        ch = src[i]
        if ch in QUOTE_CHARS:
            opening = ch
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] in CLOSERS.get(opening, opening):
                    i += 1
                    break
                i += 1
            continue
        if src.startswith("//", i):
            while i < n and src[i] != "\n":
                i += 1
            continue
        if src.startswith("/*", i):
            close = src.find("*/", i + 2)
            i = n if close < 0 else close + 2
            continue
        if ch in "{([（":
            depth += 1
        elif ch in "})]）":
            depth = max(0, depth - 1)
        i += 1
    return depth


# --------------------------------------------------------------------------
# 编辑器页
# --------------------------------------------------------------------------
class EditorTab(ttk.Frame):
    def __init__(self, app: "HomoEditorApp", key: str, path: Optional[str] = None,
                 text: str = "", parent: Optional[tk.Misc] = None):
        super().__init__(parent or app, style="Editor.TFrame")
        self.app = app
        self.key = key
        self.path = normalize_path(path) if path else None
        self.dirty = False
        self._highlight_job = None
        self._gutter_job = None
        self._completion_win: Optional[tk.Toplevel] = None
        self._completion_list: Optional[tk.Listbox] = None
        self._completion_prefix = ""
        self._completion_values: list[str] = []
        self._completion_bound = False
        self._auto_complete_job = None
        self._suppress_auto = False
        self._ignore_next_modified = False
        self._lint_job = None
        self._lint_line = 0
        self._lint_diag_line = 0
        self._lint_file = ""
        self._lint_path = ""
        self._lint_message = ""
        self._error_line = 0
        self.bind("<Destroy>", self._on_destroy)
        self._build_ui()
        self.set_text(text)
        self.text.edit_reset()
        self.text.edit_modified(False)
        self.apply_highlight()
        self.update_gutter()
        self._update_current_line()

    # -- 构建 ----------------------------------------------------------
    def _build_ui(self) -> None:
        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)
        self.lint_bar = tk.Label(
            self, text="", bg="#3a1f23", fg="#ff9d9d",
            font=self.app.font_small, anchor="w", justify="left",
            padx=10, pady=4, cursor="hand2",
        )
        self.lint_bar.grid(row=0, column=0, columnspan=3, sticky="ew")
        self.lint_bar.grid_remove()
        self.lint_bar.bind("<Button-1>", self._goto_lint)
        self.gutter = tk.Canvas(
            self, width=58, bg=COLORS["gutter"], highlightthickness=0,
            bd=0, takefocus=0,
        )
        self.gutter.grid(row=1, column=0, sticky="ns")
        self.text = tk.Text(
            self, wrap="none", undo=True, maxundo=-1, autoseparators=True,
            bg=COLORS["editor"], fg=COLORS["editor_fg"],
            insertbackground=COLORS["cursor"], insertwidth=2,
            selectbackground=COLORS["select"], selectforeground="#ffffff",
            inactiveselectbackground=COLORS["select"],
            font=self.app.font_code, padx=10, pady=8,
            tabs=(self.app.char_width * 4,),
            relief="flat", borderwidth=0, highlightthickness=0,
            spacing1=0, spacing3=0,
        )
        self.text.grid(row=1, column=1, sticky="nsew")
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self._yview)
        self.vbar.grid(row=1, column=2, sticky="ns")
        self.hbar = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        self.hbar.grid(row=2, column=1, sticky="ew")
        self.text.configure(yscrollcommand=self._on_yscroll, xscrollcommand=self.hbar.set)

        # 语法高亮标签：配色与旧网页版一致
        for _kind, _color in SYNTAX_COLORS.items():
            self.text.tag_configure(_kind, foreground=_color)
        self.text.tag_configure("currentline", background=COLORS["current"])
        self.text.tag_configure("errorline", background=COLORS["errorline"])
        self.text.tag_configure("lintline", underline=True, background="#3a1e24")
        self.text.tag_configure("searchhit", background=COLORS["search"])
        self.text.tag_configure("searchcur", background=COLORS["search_cur"])
        self.text.tag_lower("currentline")

        # 事件
        self.text.bind("<<Modified>>", self._on_modified)
        self.text.bind("<KeyPress>", self._on_key_press, add="+")
        self.text.bind("<BackSpace>", self._on_pair_delete)
        self.text.bind("<Delete>", self._on_pair_delete)
        self.text.bind("<KeyRelease>", self._on_key_release)
        self.text.bind("<ButtonRelease-1>", self._on_cursor_move)
        self.text.bind("<Configure>", lambda e: self._schedule_gutter())
        self.text.bind("<Return>", self._on_return)
        self.text.bind("<KP_Enter>", self._on_return)
        self.text.bind("<Tab>", self._on_tab)
        self.text.bind("<Shift-Tab>", self._on_shift_tab)
        self.text.bind("<ISO_Left_Tab>", self._on_shift_tab)
        self.text.bind("<Control-space>", self.show_completion)
        self.text.bind("<Command-j>", self.show_completion)
        self.text.bind("<Escape>", self._on_escape)
        self.text.bind("<FocusIn>", lambda e: self.app._on_editor_focus(self))
        self.text.bind("<FocusOut>", self._on_focus_out)
        self.text.bind("<Button-2>", self._popup_menu)
        self.text.bind("<Button-3>", self._popup_menu)
        self.text.bind("<MouseWheel>", self._on_mousewheel_hide, add="+")
        self.text.bind("<Button-4>", self._on_mousewheel_hide, add="+")
        self.text.bind("<Button-5>", self._on_mousewheel_hide, add="+")

    def _popup_menu(self, event) -> None:
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="剪切", command=lambda: self.text.event_generate("<<Cut>>"))
        menu.add_command(label="复制", command=lambda: self.text.event_generate("<<Copy>>"))
        menu.add_command(label="粘贴", command=lambda: self.text.event_generate("<<Paste>>"))
        menu.add_separator()
        menu.add_command(label="全选", command=self._select_all)
        menu.add_command(label="查找...", command=self.app.show_find)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _select_all(self) -> None:
        self.text.tag_add("sel", "1.0", "end-1c")
        self.text.mark_set("insert", "1.0")
        return "break"

    # -- 内容 ----------------------------------------------------------
    def set_text(self, text: str) -> None:
        self._suppress_auto = True
        try:
            self.text.delete("1.0", "end")
            self.text.insert("1.0", text)
            self.text.mark_set("insert", "1.0")
            self.text.see("insert")
            self.text.edit_modified(False)
            self.dirty = False
            self.apply_highlight()
            self.update_gutter()
            self._update_current_line()
        finally:
            if self._auto_complete_job is not None:
                try:
                    self.after_cancel(self._auto_complete_job)
                except tk.TclError:
                    pass
                self._auto_complete_job = None
            self._close_completion()
            self._suppress_auto = False
            self._ignore_next_modified = False
            if self._lint_job is not None:
                try:
                    self.after_cancel(self._lint_job)
                except tk.TclError:
                    pass
                self._lint_job = None
            self._clear_lint()
            self._schedule_lint(350)

    def get_text(self) -> str:
        return self.text.get("1.0", "end-1c")

    @property
    def display_name(self) -> str:
        return os.path.basename(self.path) if self.path else "未命名"

    @property
    def is_homo(self) -> bool:
        return bool(self.path and self.path.lower().endswith(".homo"))

    def mark_dirty(self) -> None:
        if not self.dirty:
            self.dirty = True
            self.app.update_tab_title(self)

    # -- 事件 ----------------------------------------------------------
    def _on_modified(self, event=None) -> None:
        if not self.text.edit_modified():
            return
        self.text.edit_modified(False)
        self.mark_dirty()
        self.schedule_highlight()
        self._schedule_gutter()
        if self._ignore_next_modified:
            # 这一轮修改来自“接受补全项”，不要再自动弹出补全。
            self._ignore_next_modified = False
        elif not self._suppress_auto:
            self._schedule_auto_complete()
        if not self._suppress_auto:
            self._schedule_lint()

    # -- 自动配对 ------------------------------------------------------
    def _insert_pair(self, opening: str, closing: str) -> None:
        before = self.text.index("insert")
        self.text.insert("insert", opening + closing)
        self.text.mark_set("insert", f"{before}+1c")
        self._after_pair()

    def _after_pair(self) -> None:
        self.text.see("insert")
        self._update_current_line()
        self._schedule_gutter()
        self._update_cursor()

    def _previous_is_ident(self) -> bool:
        prev = self.text.get("insert-1c", "insert")
        return len(prev) == 1 and _is_ident_char(prev)

    def _on_key_press(self, event) -> Optional[str]:
        # 只处理真正的括号/引号字符。Windows 中文输入法下（ 和 { 的事件
        # 会带 Shift/Alt 类 state 位，之前统一跳过会导致自动配对失效。
        ch = event.char
        if len(ch) != 1 or ch == "\x00":
            return None
        if self.text.tag_ranges("sel"):
            return None

        # 中文输入法里的「」不是 homo 语法：换成 {}。
        if ch in ("「", "」"):
            source = self.get_text()
            caret = len(self.text.get("1.0", "insert"))
            if code_context_at(source, caret) != "code":
                return None
            if ch == "「":
                self._insert_pair("{", "}")
                return "break"
            following = self.text.get("insert", "insert+1c")
            if following == "}":
                self.text.mark_set("insert", "insert+1c")
                self._after_pair()
                return "break"
            self.text.insert("insert", "}")
            self._after_pair()
            return "break"

        # 右括号：正前方已经有同一个右括号时，跳过去而不是再输入一个。
        if ch in PAIR_CLOSERS:
            if self.text.get("insert", "insert+1c") == ch:
                self.text.mark_set("insert", "insert+1c")
                self._after_pair()
                return "break"
            return None

        # 引号：成对补全；同族引号在正前方时跳过。半角引号紧跟字母/数字/汉字时
        # 视为闭合引号，只输入一只。
        if ch in QUOTE_PAIRS:
            family = QUOTE_FAMILY[ch]
            following = self.text.get("insert", "insert+1c")
            if following in QUOTE_FAMILY and QUOTE_FAMILY[following] == family:
                self.text.mark_set("insert", "insert+1c")
                self._after_pair()
                return "break"
            if ch in ('"', "'") and self._previous_is_ident():
                return None
            self._insert_pair(ch, QUOTE_PAIRS[ch])
            return "break"
        if ch in QUOTE_CLOSERS:
            family = QUOTE_FAMILY[ch]
            following = self.text.get("insert", "insert+1c")
            if following in QUOTE_FAMILY and QUOTE_FAMILY[following] == family:
                self.text.mark_set("insert", "insert+1c")
                self._after_pair()
                return "break"
            return None

        if ch in AUTO_PAIRS:
            self._insert_pair(ch, AUTO_PAIRS[ch])
            return "break"
        return None

    def _on_pair_delete(self, event=None) -> Optional[str]:
        if event is not None and event.state & (0x4 | 0x8):
            return None
        if self.text.tag_ranges("sel"):
            return None
        prev = self.text.get("insert-1c", "insert")
        following = self.text.get("insert", "insert+1c")
        if not prev or not following:
            return None
        paired = AUTO_PAIRS.get(prev) == following
        if not paired and prev in QUOTE_FAMILY and following in QUOTE_FAMILY:
            paired = QUOTE_FAMILY[prev] == QUOTE_FAMILY[following]
        if not paired:
            return None
        start = self.text.index("insert-1c")
        self.text.delete(start, "insert+1c")
        self.text.mark_set("insert", start)
        self._after_pair()
        return "break"

    def _suspend_for_dialog(self) -> None:
        """打开文件对话框前取消待执行的编辑器任务。"""
        for attr in ("_highlight_job", "_gutter_job",
                     "_auto_complete_job", "_lint_job"):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attr, None)
        self._close_completion()

    # -- 实时检查 ------------------------------------------------------
    def _schedule_lint(self, delay: int = 550) -> None:
        if getattr(self.app, "_dialog_active", False):
            return
        if not self.text.winfo_exists():
            return
        if self._lint_job is not None:
            try:
                self.after_cancel(self._lint_job)
            except tk.TclError:
                pass
        self._lint_job = self.after(delay, self._run_lint)

    def _run_lint(self) -> None:
        self._lint_job = None
        if getattr(self.app, "_dialog_active", False):
            return
        if not self.text.winfo_exists():
            return
        source = self.get_text()
        if not source.strip() or len(source) > 1_000_000:
            self._clear_lint()
            return
        context = self.app.lint_context(self)
        if context is None:
            self._clear_lint()
            return
        path, root_source, overlay, include_dirs, base_dir = context
        try:
            _program, _source_map, units = homo.check_source(
                root_source, path=path, overlay=overlay,
                include_dirs=include_dirs, base_dir=base_dir,
                require_function=False,
            )
        except homo.HomoError as exc:
            self._show_lint(homo.error_diagnostic(exc))
            return
        except Exception:  # noqa: BLE001 - 实时检查失败不应打断编辑
            self._clear_lint()
            return

        # 当前文件没有包含在主程序上下文里时，再单独检查它。
        if self.path and path:
            try:
                same = (os.path.normcase(normalize_path(self.path))
                        == os.path.normcase(normalize_path(path)))
            except OSError:
                same = False
            if not same:
                included = any(
                    unit.path and os.path.normcase(normalize_path(unit.path))
                    == os.path.normcase(normalize_path(self.path))
                    for unit in units
                )
                if not included:
                    try:
                        homo.check_source(
                            source, path=self.path, overlay=overlay,
                            include_dirs=include_dirs, base_dir=None,
                            require_function=False,
                        )
                    except homo.HomoError as exc:
                        self._show_lint(homo.error_diagnostic(exc))
                        return
                    except Exception:  # noqa: BLE001
                        self._clear_lint()
                        return
        self._clear_lint()

    def _diag_matches_current(self, diag: dict) -> bool:
        path = diag.get("path") or ""
        file = diag.get("file") or ""
        if path:
            if not self.path:
                return False
            try:
                return (os.path.normcase(normalize_path(path))
                        == os.path.normcase(normalize_path(self.path)))
            except OSError:
                return False
        if file in ("", "<内存>"):
            return self.path is None
        if self.path:
            return os.path.basename(file) == os.path.basename(self.path)
        return True

    def _show_lint(self, diag: dict) -> None:
        line = int(diag.get("line") or 0)
        file = diag.get("file") or ""
        path = diag.get("path") or ""
        message = (diag.get("message") or "").replace("\n", " ")
        current = self._diag_matches_current(diag)
        self._lint_line = line if current else 0
        self._lint_diag_line = line
        self._lint_file = file
        self._lint_path = path
        self._lint_message = message
        if current:
            text = f"第 {line} 行：{message}" if line else message
        elif file:
            text = f"{file}:{line}：{message}" if line else f"{file}：{message}"
        else:
            text = message
        if len(text) > 180:
            text = text[:179] + "…"
        self.lint_bar.configure(text=text)
        self.lint_bar.grid()
        self._mark_lint_line(line if current else 0)
        self.update_gutter()
        if self._completion_win is not None:
            self.after_idle(self._completion_place)

    def _mark_lint_line(self, line: int) -> None:
        if not self.text.winfo_exists():
            return
        self.text.tag_remove("lintline", "1.0", "end")
        if line:
            total = int(self.text.index("end-1c").split(".")[0])
            if 1 <= line <= total:
                self.text.tag_add("lintline", f"{line}.0", f"{line}.end")

    def _clear_lint(self) -> None:
        was_visible = False
        try:
            was_visible = bool(self.lint_bar.winfo_ismapped())
        except tk.TclError:
            pass
        changed = bool(self._lint_line or self._lint_file or was_visible)
        self._lint_line = 0
        self._lint_diag_line = 0
        self._lint_file = ""
        self._lint_path = ""
        self._lint_message = ""
        try:
            self.lint_bar.grid_remove()
        except tk.TclError:
            pass
        if self.text.winfo_exists():
            self.text.tag_remove("lintline", "1.0", "end")
        if changed:
            self.update_gutter()
            if self._completion_win is not None:
                self.after_idle(self._completion_place)

    def _goto_lint(self, event=None) -> str:
        line = self._lint_diag_line
        path = self._lint_path
        file = self._lint_file
        if path:
            try:
                if self.path and (os.path.normcase(normalize_path(path))
                                  == os.path.normcase(normalize_path(self.path))):
                    self.goto_line(line, highlight_error=True)
                    return "break"
                if os.path.isfile(path):
                    self.app.open_path(path, line)
                    return "break"
            except OSError:
                pass
        if file:
            base = os.path.basename(file)
            for ed in self.app.docs.values():
                if ed.path and os.path.basename(ed.path) == base:
                    self.app.notebook.select(ed)
                    ed.goto_line(line, highlight_error=True)
                    return "break"
            if self.app.workspace:
                candidate = os.path.join(self.app.workspace, file)
                if os.path.isfile(candidate):
                    self.app.open_path(candidate, line)
                    return "break"
        if line and (not file or file == "<内存>"
                     or (self.path and os.path.basename(file)
                         == os.path.basename(self.path))):
            self.goto_line(line, highlight_error=True)
        return "break"

    # -- 自动补全 ------------------------------------------------------
    def _schedule_auto_complete(self) -> None:
        if getattr(self.app, "_dialog_active", False):
            return
        if self._auto_complete_job is not None:
            try:
                self.after_cancel(self._auto_complete_job)
            except tk.TclError:
                pass
        self._auto_complete_job = self.after(60, self._auto_complete)

    def _auto_complete(self) -> None:
        self._auto_complete_job = None
        if getattr(self.app, "_dialog_active", False):
            return
        if not self.text.winfo_exists() or self._suppress_auto:
            return
        if self.app.current_editor() is not self:
            return
        if self.text.tag_ranges("sel"):
            self._close_completion()
            return
        source = self.get_text()
        caret = len(self.text.get("1.0", "insert"))
        if code_context_at(source, caret) != "code":
            self._close_completion()
            return
        prefix = self._identifier_prefix()
        if not prefix or prefix.isdigit():
            self._close_completion()
            return
        items = self.app.completion_items(prefix, self)
        if not items:
            self._close_completion()
            return
        self._completion_prefix = prefix
        if self._completion_win is None:
            self._open_completion()
        self._fill_completion(items)

    def _on_key_release(self, event=None) -> None:
        self._update_current_line()
        self._schedule_gutter()
        self._update_cursor()
        if self._completion_win is not None:
            self._refresh_completion()

    def _on_cursor_move(self, event=None) -> None:
        self._update_current_line()
        self._schedule_gutter()
        self._update_cursor()

    def _update_cursor(self) -> None:
        line, col = self.text.index("insert").split(".")
        self.app.set_cursor(int(line), int(col) + 1)

    def _update_current_line(self) -> None:
        self.text.tag_remove("currentline", "1.0", "end")
        self.text.tag_add("currentline", "insert linestart", "insert lineend+1c")

    def _on_return(self, event=None) -> str:
        """回车按括号深度缩进；光标正对右括号时，把右括号留在下一行。"""
        if self.text.tag_ranges("sel"):
            self.text.delete("sel.first", "sel.last")
        start = self.text.index("insert")
        source = self.get_text()
        offset = len(self.text.get("1.0", start))
        depth = scope_depth_at(source, offset)
        inner = "    " * depth
        following = self.text.get("insert", "insert+1c")
        if following in PAIR_CLOSERS:
            text = "\n" + inner + "\n" + "    " * max(0, depth - 1)
            caret = 1 + len(inner)
        else:
            text = "\n" + inner
            caret = len(text)
        self.text.insert("insert", text)
        self.text.mark_set("insert", f"{start}+{caret}c")
        self.text.see("insert")
        self._update_current_line()
        self._update_cursor()
        return "break"

    def _selected_line_range(self) -> tuple[int, int]:
        try:
            first = self.text.index("sel.first")
            last = self.text.index("sel.last")
        except tk.TclError:
            line = int(self.text.index("insert").split(".")[0])
            return line, line
        return int(first.split(".")[0]), int(last.split(".")[0])

    def _on_tab(self, event=None) -> Optional[str]:
        first, last = self._selected_line_range()
        multiline = first != last
        if multiline or self.text.tag_ranges("sel"):
            for line in range(first, last + 1):
                self.text.insert(f"{line}.0", "    ")
            self.text.tag_add("sel", f"{first}.0", f"{last}.end")
            return "break"
        self.text.insert("insert", "    ")
        return "break"

    def _on_shift_tab(self, event=None) -> str:
        first, last = self._selected_line_range()
        for line in range(first, last + 1):
            start = f"{line}.0"
            prefix = self.text.get(start, f"{line}.8")
            if prefix.startswith("\t"):
                count = 1
            else:
                count = min(len(prefix) - len(prefix.lstrip(" ")), 4)
            if count:
                self.text.delete(start, f"{start}+{count}c")
        return "break"

    def _on_escape(self, event=None) -> Optional[str]:
        if self._completion_win is not None:
            self._close_completion()
            return "break"
        return None

    def _on_mousewheel_hide(self, event=None) -> None:
        if self._completion_win is not None:
            self._close_completion()
        return None

    def _on_destroy(self, event=None) -> None:
        if event is not None and event.widget is not self:
            return
        for attr in ("_highlight_job", "_gutter_job", "_auto_complete_job", "_lint_job"):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attr, None)
        if self._completion_win is not None:
            try:
                self._completion_win.destroy()
            except tk.TclError:
                pass
            self._completion_win = None
            self._completion_list = None

    def _on_focus_out(self, event=None) -> None:
        if self._completion_win is not None:
            self.after(30, self._close_completion_if_unfocused)

    def _close_completion_if_unfocused(self) -> None:
        if self._completion_win is None or not self.text.winfo_exists():
            return
        try:
            focus = self.focus_get()
        except tk.TclError:
            return
        if focus is not self.text and focus is not self._completion_list:
            self._close_completion()

    def indent_text(self) -> None:
        first, last = self._selected_line_range()
        for line in range(first, last + 1):
            self.text.insert(f"{line}.0", "    ")

    def outdent_text(self) -> None:
        first, last = self._selected_line_range()
        for line in range(first, last + 1):
            start = f"{line}.0"
            prefix = self.text.get(start, f"{line}.8")
            if prefix.startswith("\t"):
                self.text.delete(start, f"{start}+1c")
            else:
                count = min(len(prefix) - len(prefix.lstrip(" ")), 4)
                if count:
                    self.text.delete(start, f"{start}+{count}c")

    def toggle_comment(self) -> None:
        first, last = self._selected_line_range()
        lines = [self.text.get(f"{i}.0", f"{i}.end") for i in range(first, last + 1)]
        filled = [ln for ln in lines if ln.strip()]
        if not filled:
            return
        all_commented = all(ln.lstrip().startswith("//") for ln in filled)
        for line in range(first, last + 1):
            content = self.text.get(f"{line}.0", f"{line}.end")
            if not content.strip():
                continue
            if all_commented:
                idx = content.find("//")
                self.text.delete(f"{line}.{idx}", f"{line}.{idx + 2}")
            else:
                indent = len(content) - len(content.lstrip())
                self.text.insert(f"{line}.{indent}", "//")

    # -- 行号 ----------------------------------------------------------
    def _schedule_gutter(self) -> None:
        if getattr(self.app, "_dialog_active", False):
            return
        if self.text.winfo_exists():
            if self._gutter_job is not None:
                try:
                    self.after_cancel(self._gutter_job)
                except tk.TclError:
                    pass
            self._gutter_job = self.after(30, self.update_gutter)

    def update_gutter(self) -> None:
        self._gutter_job = None
        if not self.gutter.winfo_exists():
            return
        self.gutter.delete("all")
        total = int(self.text.index("end-1c").split(".")[0])
        width = max(46, 18 + len(str(max(total, 1))) * self.app.gutter_char_width)
        self.gutter.configure(width=width)
        try:
            cursor_line = int(self.text.index("insert").split(".")[0])
        except tk.TclError:
            cursor_line = 1
        index = self.text.index("@0,0")
        line = int(index.split(".")[0])
        height = self.gutter.winfo_height()
        while True:
            info = self.text.dlineinfo(f"{line}.0")
            if info is None:
                break
            y = info[1]
            if y > height:
                break
            if self._lint_line and line == self._lint_line:
                fill = "#ff8f8f"
                font = self.app.font_gutter_bold
            elif line == cursor_line:
                fill = COLORS["accent"]
                font = self.app.font_gutter
            else:
                fill = COLORS["gutter_fg"]
                font = self.app.font_gutter
            self.gutter.create_text(
                width - 9, y, anchor="ne", text=str(line),
                fill=fill, font=font,
            )
            line += 1
            if line > total + 1:
                break

    def _on_yscroll(self, first, last) -> None:
        self.vbar.set(first, last)
        self._schedule_gutter()

    def _yview(self, *args) -> None:
        self.text.yview(*args)
        self._schedule_gutter()

    # -- 着色 ----------------------------------------------------------
    def schedule_highlight(self) -> None:
        if getattr(self.app, "_dialog_active", False):
            return
        if self._highlight_job is not None:
            try:
                self.after_cancel(self._highlight_job)
            except tk.TclError:
                pass
        self._highlight_job = self.after(80, self.apply_highlight)

    def apply_highlight(self) -> None:
        self._highlight_job = None
        if getattr(self.app, "_dialog_active", False):
            return
        if not self.text.winfo_exists():
            return
        for tag in SYNTAX_COLORS:
            self.text.tag_remove(tag, "1.0", "end")
        source = self.get_text()
        if len(source) > 400_000:
            return
        for start, end, kind in highlight_tokens(source):
            # 普通变量、分隔符和「迫真」在网页版里是白色，直接用 Text 的默认前景色。
            if kind in ("id", "sep", "op"):
                continue
            self.text.tag_add(kind, f"1.0+{start}c", f"1.0+{end}c")

    # -- 定位 ----------------------------------------------------------
    def goto_line(self, line: int, column: int = 0, highlight_error: bool = False) -> None:
        total = int(self.text.index("end-1c").split(".")[0])
        line = max(1, min(int(line or 1), total))
        self.text.mark_set("insert", f"{line}.0")
        self.text.see("insert")
        if column and column > 1:
            self.text.mark_set("insert", f"{line}.{column - 1}")
        self.text.focus_set()
        self._update_current_line()
        self._schedule_gutter()
        self._update_cursor()
        if highlight_error:
            self.text.tag_remove("errorline", "1.0", "end")
            self.text.tag_add("errorline", f"{line}.0", f"{line}.end+1c")
            self._error_line = line
        else:
            self.text.tag_remove("errorline", "1.0", "end")

    # -- 自动补全 ------------------------------------------------------
    def _identifier_prefix(self) -> str:
        return identifier_prefix(self.text.get("insert linestart", "insert"))

    def show_completion(self, event=None) -> str:
        prefix = self._identifier_prefix()
        if prefix.isdigit():
            return "break"
        items = self.app.completion_items(prefix, self)
        if not items:
            return "break"
        self._completion_prefix = prefix
        if self._completion_win is None:
            self._open_completion()
        self._fill_completion(items)
        return "break"

    def _open_completion(self) -> None:
        self._completion_win = tk.Toplevel(self)
        self._completion_win.wm_overrideredirect(True)
        try:
            self._completion_win.attributes("-topmost", True)
        except tk.TclError:
            pass
        self._completion_list = tk.Listbox(
            self._completion_win, activestyle="none", exportselection=False,
            bg=COLORS["panel2"], fg=COLORS["fg"],
            selectbackground=COLORS["select"], selectforeground="#ffffff",
            font=self.app.font_code, relief="flat", borderwidth=1,
            highlightthickness=1, highlightbackground=COLORS["border"],
        )
        self._completion_list.pack(fill="both", expand=True)
        self._completion_list.bind("<ButtonPress-1>", self._completion_mouse_press)
        self._completion_list.bind("<ButtonRelease-1>", self._completion_click)
        self._completion_list.bind("<Double-Button-1>", self._completion_click)
        for seq, handler in (
            ("<Up>", lambda e: self._completion_move(-1)),
            ("<Down>", lambda e: self._completion_move(1)),
            ("<Return>", lambda e: self._completion_accept(e)),
            ("<KP_Enter>", lambda e: self._completion_accept(e)),
            ("<Tab>", lambda e: self._completion_accept(e)),
        ):
            self.text.bind(seq, handler)
        self._completion_bound = True
        # 保持焦点在文本里，用户才能继续输入来过滤补全项。
        self._completion_list.configure(takefocus=0)
        self.text.bind("<Button-1>", self._completion_text_click, add="+")

    def _fill_completion(self, items: list[tuple[str, str]]) -> None:
        if self._completion_list is None:
            return
        self._completion_list.delete(0, "end")
        self._completion_values = [name for name, _kind in items]
        for name, kind in items:
            self._completion_list.insert("end", f"{name}    {kind}")
        if items:
            self._completion_list.selection_set(0)
            self._completion_list.activate(0)
        self._completion_list.configure(height=min(AC_MAX, max(3, len(items))))
        self._completion_place()

    def _completion_place(self) -> None:
        if self._completion_win is None:
            return
        bbox = self.text.bbox("insert")
        if not bbox:
            self._close_completion()
            return
        x = self.text.winfo_rootx() + bbox[0]
        y = self.text.winfo_rooty() + bbox[1] + bbox[3] + 2
        row_h = self.app.font_code.metrics("linespace") + 4
        height = min(AC_MAX, max(3, self._completion_list.size())) * row_h + 4 \
            if self._completion_list else 120
        width = 300
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        if x + width > screen_w - 8:
            x = max(2, screen_w - width - 8)
        if y + height > screen_h - 20:
            y = self.text.winfo_rooty() + bbox[1] - height - 2
        self._completion_win.geometry(f"{width}x{height}+{x}+{y}")

    def _refresh_completion(self) -> None:
        if self._completion_win is None:
            return
        if self.text.tag_ranges("sel"):
            self._close_completion()
            return
        source = self.get_text()
        caret = len(self.text.get("1.0", "insert"))
        if code_context_at(source, caret) != "code":
            self._close_completion()
            return
        prefix = self._identifier_prefix()
        if not prefix or prefix.isdigit():
            self._close_completion()
            return
        items = self.app.completion_items(prefix, self)
        if not items:
            self._close_completion()
            return
        if prefix != self._completion_prefix:
            self._completion_prefix = prefix
            self._fill_completion(items)

    def _completion_move(self, delta: int) -> str:
        if self._completion_list is None:
            return "break"
        size = self._completion_list.size()
        if size == 0:
            return "break"
        current = self._completion_list.curselection()
        index = (current[0] if current else 0) + delta
        index = max(0, min(index, size - 1))
        self._completion_list.selection_clear(0, "end")
        self._completion_list.selection_set(index)
        self._completion_list.activate(index)
        self._completion_list.see(index)
        return "break"

    def _completion_text_click(self, event=None) -> None:
        self._close_completion()

    def _completion_mouse_press(self, event=None) -> str:
        if self._completion_list is None:
            return "break"
        try:
            index = self._completion_list.nearest(event.y)
        except (tk.TclError, AttributeError):
            return "break"
        if index < 0:
            return "break"
        self._completion_list.selection_clear(0, "end")
        self._completion_list.selection_set(index)
        self._completion_list.activate(index)
        self._completion_accept()
        return "break"

    def _completion_click(self, event=None) -> str:
        self._completion_accept(event)
        return "break"

    def _completion_accept(self, event=None) -> str:
        if self._completion_list is None:
            return "break"
        selection = self._completion_list.curselection()
        if not selection:
            return "break"
        index = selection[0]
        if 0 <= index < len(self._completion_values):
            value = self._completion_values[index]
        else:
            return "break"
        prefix_len = len(self._completion_prefix)
        self._ignore_next_modified = True
        self.after(400, self._clear_ignore_next_modified)
        if prefix_len:
            self.text.delete(f"insert-{prefix_len}c", "insert")
        self.text.insert("insert", value)
        # 插入补全项本身会触发 <<Modified>>；取消这次自动补全，避免立刻重新弹层。
        if self._auto_complete_job is not None:
            try:
                self.after_cancel(self._auto_complete_job)
            except tk.TclError:
                pass
            self._auto_complete_job = None
        self._close_completion()
        self.text.focus_set()
        self._update_current_line()
        self._update_cursor()
        return "break"

    def _clear_ignore_next_modified(self) -> None:
        self._ignore_next_modified = False

    def _close_completion(self) -> None:
        if self._completion_bound:
            for seq in ("<Up>", "<Down>"):
                try:
                    self.text.unbind(seq)
                except tk.TclError:
                    pass
            # 恢复被补全临时覆盖的编辑快捷键。
            self.text.bind("<Return>", self._on_return)
            self.text.bind("<KP_Enter>", self._on_return)
            self.text.bind("<Tab>", self._on_tab)
            self.text.unbind("<Button-1>")
            self._completion_bound = False
        if self._completion_win is not None:
            self._completion_win.destroy()
            self._completion_win = None
            self._completion_list = None
            self._completion_values = []

    def find_all(self, needle: str, nocase: bool) -> int:
        self.text.tag_remove("searchhit", "1.0", "end")
        self.text.tag_remove("searchcur", "1.0", "end")
        if not needle:
            return 0
        count = 0
        start = "1.0"
        while count < 2000:
            idx = self.text.search(needle, start, stopindex="end", nocase=nocase)
            if not idx:
                break
            end = f"{idx}+{len(needle)}c"
            self.text.tag_add("searchhit", idx, end)
            start = end
            count += 1
        return count

    def find_next(self, needle: str, backwards: bool, nocase: bool) -> bool:
        if not needle:
            return False
        start = "insert"
        if backwards:
            idx = self.text.search(needle, start, backwards=True, nocase=nocase)
            if not idx:
                self.text.mark_set("insert", "end")
                idx = self.text.search(needle, "end", backwards=True, nocase=nocase)
        else:
            idx = self.text.search(needle, start, stopindex="end", nocase=nocase)
            if not idx:
                idx = self.text.search(needle, "1.0", stopindex="end", nocase=nocase)
        if not idx:
            return False
        self.text.mark_set("insert", idx)
        self.text.see(idx)
        self.text.tag_remove("searchcur", "1.0", "end")
        self.text.tag_add("searchcur", idx, f"{idx}+{len(needle)}c")
        self._update_current_line()
        return True


class EditorTabs(ttk.Frame):
    """自定义标签栏：每个文件标签右侧带关闭按钮。"""

    def __init__(self, parent: tk.Misc, app: "HomoEditorApp"):
        super().__init__(parent, style="Editor.TFrame")
        self.app = app
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.bar = tk.Frame(self, bg=COLORS["bg"])
        self.bar.grid(row=0, column=0, sticky="ew")
        self.stack = tk.Frame(self, bg=COLORS["editor"], highlightthickness=0)
        self.stack.grid(row=1, column=0, sticky="nsew")
        self.stack.columnconfigure(0, weight=1)
        self.stack.rowconfigure(0, weight=1)
        self._tabs: list[EditorTab] = []
        self._frames: dict[EditorTab, tk.Frame] = {}
        self._labels: dict[EditorTab, tk.Label] = {}
        self._closes: dict[EditorTab, tk.Label] = {}
        self._active: Optional[EditorTab] = None
        self.on_change = app._on_tab_changed
        self.on_close = app.close_editor
        self.on_menu = app._on_tab_menu

    def _short_title(self, title: str) -> str:
        font = self.app.font_ui
        limit = 190 * getattr(self.app, "ui_scale", 1.0)
        if font.measure(title) <= limit:
            return title
        trimmed = title
        while trimmed and font.measure(trimmed + "…") > limit:
            trimmed = trimmed[:-1]
        return trimmed + "…"

    def add(self, tab: EditorTab, title: str) -> None:
        title = self._short_title(title)
        frame = tk.Frame(self.bar, bg=COLORS["panel"])
        frame.pack(side="left", padx=(0, 2), pady=(4, 0))
        label = tk.Label(
            frame, text=title, bg=COLORS["panel"], fg=COLORS["muted"],
            font=self.app.font_ui, padx=10, pady=5, cursor="hand2", anchor="w",
        )
        label.pack(side="left")
        close = tk.Label(
            frame, text="×", bg=COLORS["panel"], fg=COLORS["muted"],
            font=self.app.font_ui, padx=7, pady=5, cursor="hand2",
        )
        close.pack(side="left")
        self._tabs.append(tab)
        self._frames[tab] = frame
        self._labels[tab] = label
        self._closes[tab] = close
        for widget in (frame, label):
            widget.bind("<Button-1>", lambda e, t=tab: self._select_click(e, t))
        close.bind("<Button-1>", lambda e, t=tab: self._close_click(e, t))
        for widget in (frame, label, close):
            widget.bind("<Button-2>", lambda e, t=tab: self.on_menu(e, t))
            widget.bind("<Button-3>", lambda e, t=tab: self.on_menu(e, t))
            widget.bind("<Control-Button-1>", lambda e, t=tab: self.on_menu(e, t))
        close.bind("<Enter>",
                   lambda e, c=close: c.configure(fg="#ff7b72"))
        close.bind("<Leave>",
                   lambda e, t=tab, c=close: c.configure(fg=self._close_fg(t)))
        self.select(tab)

    def _close_fg(self, tab: EditorTab) -> str:
        return COLORS["fg"] if tab is self._active else COLORS["muted"]

    def _select_click(self, event, tab: EditorTab) -> str:
        self.select(tab)
        tab.text.focus_set()
        return "break"

    def _close_click(self, event, tab: EditorTab) -> str:
        self.on_close(tab)
        return "break"

    def select(self, tab: Optional[EditorTab] = None):
        if tab is None:
            return self._active
        if tab not in self._tabs:
            return self._active
        if self._active is tab:
            tab.grid(row=0, column=0, sticky="nsew")
            return tab
        if self._active is not None:
            self._active.grid_remove()
        self._active = tab
        tab.grid(row=0, column=0, sticky="nsew")
        self._refresh_styles()
        if self.on_change is not None:
            self.on_change()
        return tab

    def remove(self, tab: EditorTab) -> None:
        if tab not in self._tabs:
            return
        index = self._tabs.index(tab)
        frame = self._frames.pop(tab, None)
        if frame is not None:
            frame.destroy()
        self._labels.pop(tab, None)
        self._closes.pop(tab, None)
        tab.grid_remove()
        self._tabs.remove(tab)
        if self._active is tab:
            self._active = None
            if self._tabs:
                self.select(self._tabs[min(index, len(self._tabs) - 1)])
            elif self.on_change is not None:
                self.on_change()
        else:
            self._refresh_styles()

    def current(self) -> Optional[EditorTab]:
        return self._active

    def set_title(self, tab: EditorTab, title: str) -> None:
        label = self._labels.get(tab)
        if label is not None:
            label.configure(text=self._short_title(title))

    def clear(self) -> None:
        for frame in self._frames.values():
            frame.destroy()
        for tab in self._tabs:
            tab.grid_remove()
        self._frames.clear()
        self._labels.clear()
        self._closes.clear()
        self._tabs.clear()
        self._active = None

    def _refresh_styles(self) -> None:
        for tab in self._tabs:
            frame = self._frames.get(tab)
            label = self._labels.get(tab)
            close = self._closes.get(tab)
            if frame is None or label is None or close is None:
                continue
            active = tab is self._active
            bg = COLORS["editor"] if active else COLORS["panel"]
            fg = COLORS["fg"] if active else COLORS["muted"]
            frame.configure(bg=bg)
            label.configure(bg=bg, fg=fg)
            close.configure(bg=bg, fg=COLORS["fg"] if active else COLORS["muted"])


# --------------------------------------------------------------------------
# 查找替换窗口
# --------------------------------------------------------------------------
class FindDialog(tk.Toplevel):
    def __init__(self, app: "HomoEditorApp"):
        super().__init__(app)
        self.app = app
        self.title("查找与替换")
        self.transient(app)
        self.resizable(False, False)
        self.configure(bg=COLORS["bg"])
        self._match_index: Optional[str] = None
        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="查找：").grid(row=0, column=0, sticky="e", pady=(0, 6))
        self.find_var = tk.StringVar()
        self.find_entry = ttk.Entry(body, textvariable=self.find_var, width=42)
        self.find_entry.grid(row=0, column=1, columnspan=3, sticky="ew", pady=(0, 6))
        ttk.Label(body, text="替换为：").grid(row=1, column=0, sticky="e", pady=(0, 6))
        self.replace_var = tk.StringVar()
        self.replace_entry = ttk.Entry(body, textvariable=self.replace_var, width=42)
        self.replace_entry.grid(row=1, column=1, columnspan=3, sticky="ew", pady=(0, 6))
        self.nocase = tk.BooleanVar(value=False)
        ttk.Checkbutton(body, text="忽略大小写", variable=self.nocase).grid(
            row=2, column=1, sticky="w", pady=(0, 8))
        buttons = ttk.Frame(body)
        buttons.grid(row=3, column=0, columnspan=4, sticky="e")
        ttk.Button(buttons, text="上一个", command=lambda: self.find(True)).pack(side="left", padx=3)
        ttk.Button(buttons, text="下一个", command=lambda: self.find(False)).pack(side="left", padx=3)
        ttk.Button(buttons, text="替换", command=self.replace_one).pack(side="left", padx=3)
        ttk.Button(buttons, text="全部替换", command=self.replace_all).pack(side="left", padx=3)
        ttk.Button(buttons, text="关闭", command=self.close).pack(side="left", padx=3)
        self.status = ttk.Label(body, text="", style="Muted.TLabel")
        self.status.grid(row=4, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.bind("<Return>", lambda e: self.find(False))
        self.bind("<Shift-Return>", lambda e: self.find(True))
        self.bind("<Escape>", lambda e: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.find_entry.focus_set()
        if app.current_editor() is not None:
            try:
                sel = app.current_editor().text.get("sel.first", "sel.last")
                if sel and "\n" not in sel:
                    self.find_var.set(sel)
            except tk.TclError:
                pass

    def _editor(self) -> Optional[EditorTab]:
        return self.app.current_editor()

    def find(self, backwards: bool) -> None:
        ed = self._editor()
        needle = self.find_var.get()
        if ed is None:
            return
        if not needle:
            self.status.configure(text="请输入要查找的内容")
            return
        ed.find_all(needle, self.nocase.get())
        found = ed.find_next(needle, backwards, self.nocase.get())
        if found:
            self._match_index = ed.text.index("insert")
            self.status.configure(text=f"已定位到第 {self._match_index.split('.')[0]} 行")
        else:
            self.status.configure(text="没有找到")

    def replace_one(self) -> None:
        ed = self._editor()
        needle = self.find_var.get()
        replacement = self.replace_var.get()
        if ed is None or not needle:
            return
        if self._match_index is None:
            self.find(False)
            if self._match_index is None:
                return
        idx = self._match_index
        if idx:
            ed.text.delete(idx, f"{idx}+{len(needle)}c")
            ed.text.insert(idx, replacement)
            self.status.configure(text="已替换 1 处")
            self.find(False)

    def replace_all(self) -> None:
        ed = self._editor()
        needle = self.find_var.get()
        replacement = self.replace_var.get()
        if ed is None or not needle:
            return
        count = 0
        start = "1.0"
        while count < 10000:
            idx = ed.text.search(needle, start, stopindex="end", nocase=self.nocase.get())
            if not idx:
                break
            ed.text.delete(idx, f"{idx}+{len(needle)}c")
            ed.text.insert(idx, replacement)
            start = f"{idx}+{len(replacement)}c"
            count += 1
        ed.find_all(needle, self.nocase.get())
        self.status.configure(text=f"已替换 {count} 处")

    def close(self) -> None:
        for ed in self.app.docs.values():
            try:
                ed.text.tag_remove("searchhit", "1.0", "end")
                ed.text.tag_remove("searchcur", "1.0", "end")
            except tk.TclError:
                pass
        self.destroy()


# --------------------------------------------------------------------------
# 主窗口
# --------------------------------------------------------------------------
class HomoEditorApp(tk.Tk):
    def __init__(self, initial: Optional[str] = None):
        super().__init__()
        self.title(APP_NAME)
        self._set_app_icon()
        try:
            tk_scale = float(self.tk.call("tk", "scaling"))
        except (tk.TclError, ValueError):
            tk_scale = 96 / 72
        # Tk scaling 1.333 是 96 DPI；其余部分需要同步放大像素尺寸。
        self.ui_scale = max(1.0, min(3.0, tk_scale / (96 / 72)))
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        logical_w = screen_w / self.ui_scale
        logical_h = screen_h / self.ui_scale
        width = min(1320, max(940, int(logical_w - 80)))
        height = min(860, max(620, int(logical_h - 120)))
        width = min(screen_w, int(width * self.ui_scale))
        height = min(screen_h, int(height * self.ui_scale))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(min(screen_w, int(940 * self.ui_scale)),
                     min(screen_h, int(620 * self.ui_scale)))
        self.configure(bg=COLORS["bg"])
        self.option_add("*tearOff", False)
        self._setup_fonts()
        self._setup_style()

        self.workspace: Optional[str] = None
        self.root_path: Optional[str] = None
        self.docs: dict[str, EditorTab] = {}
        self._untitled_count = 0
        self._current_key: Optional[str] = None
        self._find_dialog: Optional[FindDialog] = None
        self.job_active = False
        self._dialog_active = False
        self.job_id = 0
        self.cancel_event = threading.Event()
        self.proc: Optional[subprocess.Popen] = None
        self.workdir: Optional[str] = None
        self.event_queue: queue.Queue = queue.Queue()
        self.poll_id = None
        self.output_chars = 0
        self._compiler_label = "正在检测 C++ 编译器…"

        self._build_menu()
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()
        self._bind_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind_all("<Button-1>", self._on_global_click, add="+")
        if IS_MAC:
            # Tk 默认的 ⌘Q 会直接退出，绕开未保存提示；接管成自己的关闭流程。
            self.createcommand("tk::mac::Quit", self.on_close)
            self.createcommand("tk::mac::ShowAbout", self.show_about)
        self._compiler_thread = threading.Thread(target=self._detect_compiler, daemon=True)
        self._compiler_thread.start()
        self.after(400, self._poll_compiler_label)
        self.after(80, lambda: self._startup(initial))
        self._sash_attempts = 0
        self.after(300, self._init_sashes)

    def _set_app_icon(self) -> None:
        """窗口/任务栏图标：沿用网页左上角的 logo。"""
        images = []
        for name in ("icon-256.png", "icon-64.png", "icon-32.png"):
            path = homo.resource_path(os.path.join("assets", name))
            try:
                images.append(tk.PhotoImage(file=path))
            except (tk.TclError, OSError):
                continue
        if images:
            self._icon_photos = images
            try:
                self.iconphoto(True, *images)
            except tk.TclError:
                pass

    # -- 字体 / 主题 ---------------------------------------------------
    def _screen_font_scale(self) -> float:
        """低分辨率屏幕自动缩小字体；高分屏最多保持原来大小。"""
        width = max(1, self.winfo_screenwidth())
        height = max(1, self.winfo_screenheight())
        return max(0.76, min(1.0, height / 1000.0, width / 1600.0))

    def _setup_fonts(self) -> None:
        families = set(tkfont.families(self))
        scale = self._screen_font_scale()
        dpi = getattr(self, "ui_scale", 1.0)
        # Tk 的磅值会随 DPI 再放大；这里按像素指定，保证与 macOS 的相对字号一致。
        code_px = max(12, round(max(10, round(13 * scale)) * dpi))
        ui_px = max(10, round(max(9, round(12 * scale)) * dpi))
        small_px = max(9, round(max(8, round(10 * scale)) * dpi))
        gutter_px = max(9, round(max(8, round(10 * scale)) * dpi))
        self.font_base_code = code_px
        self.font_base_gutter = gutter_px
        self.font_code = _pick_font(
            self, families,
            ["JetBrains Mono", "SF Mono", "Menlo", "Consolas", "DejaVu Sans Mono",
             "Cascadia Mono", "Courier New"],
            -code_px,
        )
        self.font_code_bold = _pick_font(
            self, families,
            ["JetBrains Mono", "SF Mono", "Menlo", "Consolas", "DejaVu Sans Mono"],
            -code_px, "bold",
        )
        self.font_ui = _pick_font(
            self, families,
            ["PingFang SC", "Microsoft YaHei UI", "Microsoft YaHei",
             "Noto Sans CJK SC", "Source Han Sans SC", "Segoe UI", "Helvetica Neue"],
            -ui_px,
        )
        self.font_small = _pick_font(
            self, families,
            ["PingFang SC", "Microsoft YaHei UI", "Segoe UI", "Helvetica Neue"],
            -small_px,
        )
        self.font_gutter = tkfont.Font(
            self, family=self.font_code.cget("family"), size=-gutter_px,
        )
        self.font_gutter_bold = tkfont.Font(
            self, family=self.font_code.cget("family"), size=-gutter_px,
            weight="bold",
        )
        self.char_width = max(6, self.font_code.measure("M"))
        self.gutter_char_width = max(6, self.font_gutter.measure("0"))

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        scale = getattr(self, "ui_scale", 1.0)
        style.configure(".", background=COLORS["bg"], foreground=COLORS["fg"],
                        fieldbackground=COLORS["panel2"], bordercolor=COLORS["border"],
                        focuscolor=COLORS["accent"], font=self.font_ui)
        style.configure("TFrame", background=COLORS["bg"])
        style.configure("Panel.TFrame", background=COLORS["panel"])
        style.configure("Editor.TFrame", background=COLORS["editor"])
        style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["fg"])
        style.configure("Muted.TLabel", background=COLORS["bg"], foreground=COLORS["muted"])
        style.configure("Panel.TLabel", background=COLORS["panel"], foreground=COLORS["muted"])
        style.configure("Title.TLabel", background=COLORS["panel"], foreground=COLORS["fg"],
                        font=self.font_ui)
        style.configure("TButton", background=COLORS["button"], foreground=COLORS["fg"],
                        borderwidth=1, padding=(max(6, round(9 * scale)),
                                                 max(3, round(4 * scale))),
                        relief="flat")
        style.map("TButton",
                  background=[("active", COLORS["button_hover"]), ("disabled", COLORS["panel"])],
                  foreground=[("disabled", COLORS["muted"])])
        style.configure("Accent.TButton", background=COLORS["accent"],
                        foreground=COLORS["accent_text"], font=self.font_ui)
        style.map("Accent.TButton",
                  background=[("active", "#e6c46c"), ("disabled", COLORS["panel"])],
                  foreground=[("disabled", COLORS["muted"])])
        style.configure("Danger.TButton", background="#3a1d23",
                        foreground="#ffb3bd")
        style.map("Danger.TButton", background=[("active", "#4a242c")])
        style.configure("TNotebook", background=COLORS["bg"], borderwidth=0,
                        tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", background=COLORS["panel"],
                        foreground=COLORS["muted"], font=self.font_ui,
                        padding=(max(8, round(15 * scale)),
                                 max(4, round(6 * scale))), borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", COLORS["editor"])],
                  foreground=[("selected", COLORS["fg"])])
        row_height = self.font_ui.metrics("linespace") + max(4, round(6 * scale))
        style.configure("Treeview", background=COLORS["editor"],
                        fieldbackground=COLORS["editor"], foreground=COLORS["fg"],
                        font=self.font_ui, borderwidth=0, rowheight=row_height)
        style.map("Treeview", background=[("selected", COLORS["select"])],
                  foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background=COLORS["panel"],
                        foreground=COLORS["muted"], borderwidth=0,
                        font=self.font_small)
        style.configure("TEntry", fieldbackground=COLORS["panel2"],
                        foreground=COLORS["fg"], insertcolor=COLORS["cursor"],
                        bordercolor=COLORS["border"], padding=max(3, round(5 * scale)))
        style.configure("TScrollbar", background=COLORS["panel2"],
                        troughcolor=COLORS["bg"], borderwidth=0,
                        arrowsize=max(12, round(14 * scale)),
                        arrowcolor=COLORS["muted"])
        style.configure("TPanedwindow", background=COLORS["bg"])
        style.configure("Sash", sashthickness=max(4, round(6 * scale)),
                        background=COLORS["bg"])
        style.configure("TSeparator", background=COLORS["border"])
        style.configure("TCheckbutton", background=COLORS["bg"], foreground=COLORS["fg"])
        style.map("TCheckbutton", background=[("active", COLORS["bg"])])

    def _init_sashes(self) -> None:
        """给上下分栏一个舒服的初始比例：编辑器约 60%，输出约 40%。"""
        self._sash_attempts += 1
        try:
            height = self._right_pane.winfo_height()
            if height > 200:
                # 编辑器约 68%，底部运行结果约 32%。
                self._right_pane.sashpos(0, int(height * 0.68))
            elif self._sash_attempts < 6:
                self.after(200, self._init_sashes)
            width = self._outer_pane.winfo_width()
            if width > 500:
                # 左侧文件栏约 245 像素。
                self._outer_pane.sashpos(0, int(245 * getattr(self, "ui_scale", 1.0)))
            elif self._sash_attempts < 6:
                self.after(200, self._init_sashes)
        except tk.TclError:
            pass

    # -- 菜单 ----------------------------------------------------------
    def _build_menu(self) -> None:
        menubar = tk.Menu(self)

        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="新建文件…", accelerator=f"{MOD}+N",
                              command=self.new_file)
        file_menu.add_command(label="新建项目…", command=self.new_project)
        file_menu.add_separator()
        file_menu.add_command(label="打开文件夹…", accelerator=f"{MOD}+Shift+O",
                              command=self.open_folder)
        file_menu.add_command(label="打开文件…", accelerator=f"{MOD}+O",
                              command=self.open_file)
        file_menu.add_separator()
        file_menu.add_command(label="保存", accelerator=f"{MOD}+S",
                              command=self.save_current)
        file_menu.add_command(label="另存为…", accelerator=f"{MOD}+Shift+S",
                              command=self.save_current_as)
        file_menu.add_command(label="全部保存", command=self.save_all)
        file_menu.add_separator()
        file_menu.add_command(label="关闭文件", accelerator=f"{MOD}+W",
                              command=self.close_current)
        file_menu.add_command(label="退出", accelerator=f"{MOD}+Q",
                              command=self.on_close)
        menubar.add_cascade(label="文件", menu=file_menu)

        edit_menu = tk.Menu(menubar, tearoff=False)
        edit_menu.add_command(label="撤销", accelerator=f"{MOD}+Z",
                              command=lambda: self._edit_action("undo"))
        edit_menu.add_command(label="重做", accelerator=f"{MOD}+Shift+Z",
                              command=lambda: self._edit_action("redo"))
        edit_menu.add_separator()
        edit_menu.add_command(label="剪切", accelerator=f"{MOD}+X",
                              command=lambda: self._edit_action("cut"))
        edit_menu.add_command(label="复制", accelerator=f"{MOD}+C",
                              command=lambda: self._edit_action("copy"))
        edit_menu.add_command(label="粘贴", accelerator=f"{MOD}+V",
                              command=lambda: self._edit_action("paste"))
        edit_menu.add_command(label="全选", accelerator=f"{MOD}+A",
                              command=lambda: self._edit_action("select_all"))
        edit_menu.add_separator()
        edit_menu.add_command(label="查找与替换…", accelerator=f"{MOD}+F",
                              command=self.show_find)
        edit_menu.add_command(label="转到行…", accelerator=f"{MOD}+G",
                              command=self.goto_line_dialog)
        edit_menu.add_command(label="注释 / 取消注释", accelerator=f"{MOD}+/",
                              command=self.toggle_comment)
        edit_menu.add_separator()
        edit_menu.add_command(label="放大字体", accelerator=f"{MOD}+=",
                              command=lambda: self.zoom(1))
        edit_menu.add_command(label="缩小字体", accelerator=f"{MOD}+-",
                              command=lambda: self.zoom(-1))
        edit_menu.add_command(label="重置字体", accelerator=f"{MOD}+0",
                              command=lambda: self.zoom(0))
        menubar.add_cascade(label="编辑", menu=edit_menu)

        run_menu = tk.Menu(menubar, tearoff=False)
        run_menu.add_command(label="编译并运行", accelerator=f"{MOD}+R",
                             command=self.run_current)
        run_menu.add_command(label="仅编译检查", accelerator=f"{MOD}+B",
                             command=self.compile_only)
        run_menu.add_separator()
        run_menu.add_command(label="停止运行", accelerator=f"{MOD}+.",
                             command=self.stop_program)
        run_menu.add_command(label="清空运行输出", command=self.clear_output)
        run_menu.add_separator()
        run_menu.add_command(label="导出可执行文件…", command=self.export_exe)
        menubar.add_cascade(label="运行", menu=run_menu)

        project_menu = tk.Menu(menubar, tearoff=False)
        project_menu.add_command(label="刷新文件列表", command=self.refresh_tree)
        project_menu.add_command(label="把当前文件设为主程序", command=self.set_current_main)
        project_menu.add_command(label="新建文件…", command=self.new_file)
        project_menu.add_separator()
        project_menu.add_command(label="在文件管理器中显示",
                                 command=self.reveal_current)
        menubar.add_cascade(label="项目", menu=project_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="快捷键与用法", command=self.show_help)
        help_menu.add_command(label="关于", command=self.show_about)
        menubar.add_cascade(label="帮助", menu=help_menu)

        self.config(menu=menubar)

    # -- 工具栏 --------------------------------------------------------
    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self, style="Panel.TFrame", padding=(10, 7))
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(8, weight=1)
        ttk.Button(bar, text="打开文件夹", width=0, command=self.open_folder).grid(
            row=0, column=0, padx=(0, 5))
        ttk.Button(bar, text="打开文件", width=0, command=self.open_file).grid(
            row=0, column=1, padx=(0, 5))
        ttk.Button(bar, text="保存", width=0, command=self.save_current).grid(
            row=0, column=2, padx=(0, 5))
        self.run_button = ttk.Button(bar, text="▶  运行", width=0, style="Accent.TButton",
                                     command=self.run_current)
        self.run_button.grid(row=0, column=3, padx=(8, 5))
        self.stop_button = ttk.Button(bar, text="■  停止", width=0,
                                      style="Danger.TButton",
                                      command=self.stop_program, state="disabled")
        self.stop_button.grid(row=0, column=4, padx=(0, 5))
        ttk.Button(bar, text="编译检查", width=0, command=self.compile_only).grid(
            row=0, column=5, padx=(0, 5))
        ttk.Button(bar, text="清空输出", width=0, command=self.clear_output).grid(
            row=0, column=6, padx=(0, 5))
        self.root_label = ttk.Label(bar, text="主程序：未设置", style="Panel.TLabel")
        self.root_label.grid(row=0, column=7, padx=(16, 0), sticky="w")

    # -- 主布局 --------------------------------------------------------
    def _build_body(self) -> None:
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)
        outer = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        outer.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        self._outer_pane = outer

        # 左侧项目树（VS Code 默认布局：文件栏在最左）
        sidebar = ttk.Frame(outer, style="Panel.TFrame",
                            width=int(230 * getattr(self, "ui_scale", 1.0)))
        sidebar.rowconfigure(1, weight=1)
        sidebar.columnconfigure(0, weight=1)
        header = ttk.Frame(sidebar, style="Panel.TFrame", padding=(10, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="项目文件", style="Title.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Button(header, text="刷新", width=5, command=self.refresh_tree).grid(
            row=0, column=1, padx=(4, 0))
        self.tree = ttk.Treeview(sidebar, show="tree", selectmode="browse")
        self.tree.grid(row=1, column=0, sticky="nsew")
        tree_scroll = ttk.Scrollbar(sidebar, orient="vertical", command=self.tree.yview)
        tree_scroll.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.tag_configure("dir", foreground=COLORS["muted"])
        self.tree.tag_configure("file", foreground=COLORS["fg"])
        self.tree.tag_configure("inm", foreground=COLORS["fg"])
        self.tree.tag_configure("main", foreground=COLORS["accent"])
        self.tree.bind("<Double-1>", self._on_tree_double)
        self.tree.bind("<Return>", self._on_tree_double)
        self.tree.bind("<Button-2>", self._on_tree_menu)
        self.tree.bind("<Button-3>", self._on_tree_menu)
        self.tree.bind("<Control-Button-1>", self._on_tree_menu)
        outer.add(sidebar, weight=0)

        # 右侧：编辑区 + 输出区
        right = ttk.PanedWindow(outer, orient=tk.VERTICAL)
        self._right_pane = right
        editor_frame = ttk.Frame(right, style="Editor.TFrame")
        editor_frame.rowconfigure(0, weight=1)
        editor_frame.columnconfigure(0, weight=1)
        self.notebook = EditorTabs(editor_frame, self)
        self.notebook.grid(row=0, column=0, sticky="nsew")
        right.add(editor_frame, weight=3)

        output_frame = ttk.Frame(right, style="Panel.TFrame")
        output_frame.rowconfigure(0, weight=1)
        output_frame.columnconfigure(0, weight=1)
        self.output_book = ttk.Notebook(output_frame)
        self.output_book.grid(row=0, column=0, sticky="nsew")

        run_tab = ttk.Frame(self.output_book, style="Panel.TFrame")
        run_tab.rowconfigure(0, weight=1)
        run_tab.columnconfigure(0, weight=1)
        self.run_text = self._make_readonly_text(run_tab, wrap="word", mono=True)
        self.run_text.grid(row=0, column=0, sticky="nsew")
        run_scroll = ttk.Scrollbar(run_tab, orient="vertical", command=self.run_text.yview)
        run_scroll.grid(row=0, column=1, sticky="ns")
        self.run_text.configure(yscrollcommand=run_scroll.set)
        self.run_text.tag_configure("stderr", foreground=COLORS["err"])
        self.run_text.tag_configure("meta", foreground=COLORS["muted"])
        self.run_text.tag_configure("ok", foreground=COLORS["ok"])
        self.run_text.tag_configure("err", foreground=COLORS["err"])
        self.run_text.tag_configure("info", foreground=COLORS["info"])
        self.output_book.add(run_tab, text="运行结果")

        problems_tab = ttk.Frame(self.output_book, style="Panel.TFrame")
        problems_tab.rowconfigure(0, weight=1)
        problems_tab.columnconfigure(0, weight=1)
        self.problems = ttk.Treeview(
            problems_tab, columns=("sev", "file", "line", "msg"),
            show="headings", selectmode="browse",
        )
        self.problems.heading("sev", text="级别")
        self.problems.heading("file", text="文件")
        self.problems.heading("line", text="行")
        self.problems.heading("msg", text="信息")
        ui_scale = getattr(self, "ui_scale", 1.0)
        self.problems.column("sev", width=int(56 * ui_scale), stretch=False)
        self.problems.column("file", width=int(190 * ui_scale), stretch=False)
        self.problems.column("line", width=int(56 * ui_scale), stretch=False)
        self.problems.column("msg", width=int(520 * ui_scale), stretch=True)
        self.problems.tag_configure("错误", foreground=COLORS["err"])
        self.problems.tag_configure("警告", foreground=COLORS["warn"])
        self.problems.tag_configure("提示", foreground=COLORS["info"])
        self.problems.grid(row=0, column=0, sticky="nsew")
        prob_scroll = ttk.Scrollbar(problems_tab, orient="vertical",
                                    command=self.problems.yview)
        prob_scroll.grid(row=0, column=1, sticky="ns")
        self.problems.configure(yscrollcommand=prob_scroll.set)
        self.problems.bind("<Double-1>", self._on_problem_double)
        self.problems.bind("<Return>", self._on_problem_double)
        self.output_book.add(problems_tab, text="问题")

        cpp_tab = ttk.Frame(self.output_book, style="Panel.TFrame")
        cpp_tab.rowconfigure(0, weight=1)
        cpp_tab.columnconfigure(0, weight=1)
        self.cpp_text = self._make_readonly_text(cpp_tab, wrap="none", mono=True)
        self.cpp_text.grid(row=0, column=0, sticky="nsew")
        cpp_scroll = ttk.Scrollbar(cpp_tab, orient="vertical", command=self.cpp_text.yview)
        cpp_scroll.grid(row=0, column=1, sticky="ns")
        cpp_hscroll = ttk.Scrollbar(cpp_tab, orient="horizontal",
                                    command=self.cpp_text.xview)
        cpp_hscroll.grid(row=1, column=0, sticky="ew")
        self.cpp_text.configure(yscrollcommand=cpp_scroll.set,
                                xscrollcommand=cpp_hscroll.set)
        self.output_book.add(cpp_tab, text="生成的 C++")

        # 标准输入条
        input_bar = ttk.Frame(output_frame, style="Panel.TFrame", padding=(8, 6))
        input_bar.grid(row=1, column=0, sticky="ew")
        input_bar.columnconfigure(1, weight=1)
        ttk.Label(input_bar, text="标准输入", style="Panel.TLabel").grid(
            row=0, column=0, padx=(0, 8))
        self.input_var = tk.StringVar()
        self.input_entry = ttk.Entry(input_bar, textvariable=self.input_var)
        self.input_entry.grid(row=0, column=1, sticky="ew")
        self.input_entry.bind("<Return>", lambda e: self.send_input())
        self.send_button = ttk.Button(input_bar, text="发送", command=self.send_input,
                                      state="disabled")
        self.send_button.grid(row=0, column=2, padx=(8, 0))
        right.add(output_frame, weight=2)
        outer.add(right, weight=1)

        self._problem_map: dict[str, dict] = {}

    def _make_readonly_text(self, parent, wrap: str, mono: bool) -> tk.Text:
        widget = tk.Text(
            parent, wrap=wrap, bg=COLORS["editor"], fg=COLORS["fg"],
            insertbackground=COLORS["cursor"], selectbackground=COLORS["select"],
            selectforeground="#ffffff", font=self.font_code if mono else self.font_ui,
            padx=10, pady=8, relief="flat", borderwidth=0, highlightthickness=0,
            state="disabled",
        )
        return widget

    # -- 状态栏 --------------------------------------------------------
    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self, style="Panel.TFrame", padding=(10, 4))
        bar.grid(row=2, column=0, sticky="ew")
        bar.columnconfigure(1, weight=1)
        self.status_var = tk.StringVar(value="就绪")
        self.cursor_var = tk.StringVar(value="行 1，列 1")
        self.compiler_var = tk.StringVar(value=self._compiler_label)
        ttk.Label(bar, textvariable=self.status_var, style="Panel.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Label(bar, textvariable=self.compiler_var, style="Panel.TLabel").grid(
            row=0, column=1, sticky="e", padx=(0, 16))
        ttk.Label(bar, textvariable=self.cursor_var, style="Panel.TLabel").grid(
            row=0, column=2, sticky="e")

    # -- 快捷键 --------------------------------------------------------
    def _bind_shortcuts(self) -> None:
        bindings = [
            (f"<{MOD}-n>", self.new_file),
            (f"<{MOD}-o>", self.open_file),
            (f"<{MOD}-Shift-O>", self.open_folder),
            (f"<{MOD}-s>", self.save_current),
            (f"<{MOD}-Shift-S>", self.save_current_as),
            (f"<{MOD}-w>", self.close_current),
            (f"<{MOD}-r>", self.run_current),
            (f"<{MOD}-b>", self.compile_only),
            (f"<{MOD}-period>", self.stop_program),
            (f"<{MOD}-f>", self.show_find),
            (f"<{MOD}-g>", self.goto_line_dialog),
            (f"<{MOD}-slash>", self.toggle_comment),
            (f"<{MOD}-equal>", lambda: self.zoom(1)),
            (f"<{MOD}-plus>", lambda: self.zoom(1)),
            (f"<{MOD}-minus>", lambda: self.zoom(-1)),
            (f"<{MOD}-0>", lambda: self.zoom(0)),
            ("<F5>", self.run_current),
            ("<Shift-F5>", self.stop_program),
            ("<F7>", self.compile_only),
        ]
        if not IS_MAC:
            bindings.append((f"<{MOD}-q>", self.on_close))
        for seq, func in bindings:
            self.bind_all(seq, lambda e, f=func: self._run_shortcut(f))

    @staticmethod
    def _run_shortcut(func) -> str:
        func()
        return "break"

    # -- 启动 ----------------------------------------------------------
    def _startup(self, initial: Optional[str]) -> None:
        if initial and not initial.startswith("-psn"):
            path = normalize_path(initial)
            if os.path.isdir(path):
                self.load_workspace(path)
            elif os.path.isfile(path):
                self.load_workspace(os.path.dirname(path))
                self.open_path(path)
                if path.lower().endswith(".homo"):
                    self.set_main_path(path)
            else:
                self._new_untitled(self._starter_text())
        else:
            self._new_untitled(self._starter_text())
            self._append_output(
                "欢迎使用 homo 桌面编辑器\n"
                "────────────────────────\n"
                "1. 「打开文件夹」载入项目；也可以「打开文件」单独打开一个文件，不载入文件夹。\n"
                "2. 在 .homo 里写 inm 其他文件，可直接引用另一个 .homo 里的定义。\n"
                "3. 点运行会保存已命名文件的修改；没有主程序时直接运行当前缓冲区。\n"
                "4. Windows / macOS 均可直接运行；打包脚本见 README。\n"
                "5. 快捷键：Ctrl/⌘+R 运行，Ctrl/⌘+B 编译检查，Ctrl/⌘+F 查找。\n",
                "info",
            )
        self._update_root_label()
        self._update_title()
        self._on_tab_changed()

    @staticmethod
    def _starter_text() -> str:
        return (
            "// 按 Ctrl/⌘+R 编译并运行\n"
            "会员制餐厅(){\n"
            '    脱出("你好下北泽")噗叽啪\n'
            "    (2)人幸终\n"
            "}\n"
        )

    # -- 项目 ----------------------------------------------------------
    def load_workspace(self, path: str) -> None:
        if not self._confirm_close_all():
            return
        self.workspace = normalize_path(path)
        self.root_path = None
        self._clear_editors()
        self._clear_problems()
        self._set_cpp("")
        self.refresh_tree()
        self._pick_default_main()
        if self.root_path:
            self.open_path(self.root_path)
        else:
            self._new_untitled(self._starter_text())
        self._update_root_label()
        self.set_status(f"已打开项目：{self.workspace}")

    def _pick_default_main(self) -> None:
        if not self.workspace:
            return
        for name in ("main.homo", "会员制餐厅.homo", "hello.homo"):
            cand = os.path.join(self.workspace, name)
            if os.path.isfile(cand):
                self.root_path = cand
                return
        try:
            for entry in sorted(os.listdir(self.workspace)):
                cand = os.path.join(self.workspace, entry)
                if entry.lower().endswith(".homo") and os.path.isfile(cand):
                    self.root_path = cand
                    return
        except OSError:
            return

    def refresh_tree(self) -> None:
        if not hasattr(self, "tree"):
            return
        self.tree.delete(*self.tree.get_children())
        def walk(directory: str, parent: str) -> None:
            try:
                entries = list(os.scandir(directory))
            except OSError:
                return
            entries.sort(key=lambda e: (not e.is_dir(follow_symlinks=False),
                                         e.name.lower()))
            for entry in entries:
                name = entry.name
                if name.startswith("."):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if name in SKIP_DIRS:
                        continue
                    iid = entry.path
                    self.tree.insert(parent, "end", iid=iid, text=name,
                                     tags=("dir",), open=True)
                    walk(entry.path, iid)
                elif name.lower().endswith(FILE_EXTS):
                    key = os.path.normcase(normalize_path(entry.path))
                    if self.root_path and key == os.path.normcase(normalize_path(self.root_path)):
                        tags = ["main"]
                    else:
                        tags = ["file"]
                    self.tree.insert(parent, "end", iid=entry.path, text=name,
                                     tags=tuple(tags))

        if self.workspace:
            walk(self.workspace, "")

    def _on_tree_double(self, event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        path = selection[0]
        if os.path.isdir(path):
            self.tree.item(path, open=not bool(self.tree.item(path, "open")))
        else:
            self.open_path(path)
        return "break"

    def _on_tree_menu(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="打开", command=self._on_tree_double)
        menu.add_command(label="设为主程序", command=self.set_selected_main)
        menu.add_separator()
        menu.add_command(label="新建文件…", command=self.new_file)
        menu.add_command(label="在文件管理器中显示", command=self.reveal_selected)
        menu.add_separator()
        menu.add_command(label="刷新", command=self.refresh_tree)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _on_tab_menu(self, event, tab: Optional[EditorTab] = None) -> None:
        if tab is None:
            return
        self.notebook.select(tab)
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="保存", command=lambda: self.save_editor(tab))
        menu.add_command(label="关闭", command=lambda: self.close_editor(tab))
        menu.add_separator()
        menu.add_command(label="查找与替换…", command=self.show_find)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def reveal_selected(self) -> None:
        selection = self.tree.selection()
        if selection:
            self.reveal_path(selection[0])

    def reveal_current(self) -> None:
        ed = self.current_editor()
        if ed and ed.path:
            self.reveal_path(ed.path)

    def reveal_path(self, path: str) -> None:
        try:
            if IS_MAC:
                subprocess.Popen(["open", "-R", path])
            elif IS_WIN:
                subprocess.Popen(["explorer", "/select,", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except OSError as exc:
            messagebox.showerror("打开失败", str(exc), parent=self)

    # -- 文档管理 ------------------------------------------------------
    def _starter_file_text(self) -> str:
        return "// 新建的 homo 文件\n\n"

    def _new_untitled(self, text: str = "") -> EditorTab:
        self._untitled_count += 1
        key = f"untitled:{self._untitled_count}"
        tab = EditorTab(self, key, None, text, parent=self.notebook.stack)
        self._add_doc(tab)
        return tab

    def _add_doc(self, tab: EditorTab) -> None:
        self.docs[tab.key] = tab
        self.notebook.add(tab, tab.display_name)
        tab.text.focus_set()
        self._current_key = tab.key
        self._update_title()

    def open_path(self, path: str, line: Optional[int] = None) -> Optional[EditorTab]:
        path = normalize_path(path)
        for tab in self.docs.values():
            if tab.path and os.path.normcase(tab.path) == os.path.normcase(path):
                self.notebook.select(tab)
                tab.text.focus_set()
                if line:
                    tab.goto_line(line, highlight_error=True)
                return tab
        try:
            text = read_text_file(path)
        except OSError as exc:
            messagebox.showerror("打开失败", f"无法打开文件：\n{exc}", parent=self)
            return None
        tab = EditorTab(self, path, path, text, parent=self.notebook.stack)
        self._add_doc(tab)
        if line:
            tab.goto_line(line, highlight_error=True)
        return tab

    def current_editor(self) -> Optional[EditorTab]:
        current = self.notebook.current() if hasattr(self, "notebook") else None
        if isinstance(current, EditorTab):
            return current
        if self._current_key and self._current_key in self.docs:
            return self.docs[self._current_key]
        return None

    def doc_for_path(self, path: str) -> Optional[EditorTab]:
        target = os.path.normcase(normalize_path(path))
        for tab in self.docs.values():
            if tab.path and os.path.normcase(tab.path) == target:
                return tab
        return None

    def _clear_editors(self) -> None:
        tabs = list(self.docs.values())
        self.docs.clear()
        self._current_key = None
        self.notebook.clear()
        for tab in tabs:
            tab.destroy()

    def _on_tab_changed(self, event=None) -> None:
        ed = self.current_editor()
        if ed is not None:
            self._current_key = ed.key
        self._update_root_label()
        self._update_title()
        if ed is not None:
            ed._update_cursor()
            ed._schedule_lint(250)

    def _on_editor_focus(self, tab: EditorTab) -> None:
        self._current_key = tab.key
        self._update_title()

    def _on_global_click(self, event=None) -> None:
        """点击编辑器外（工具栏、输出区等）时收起补全弹层。"""
        widget = getattr(event, "widget", None)
        for ed in self.docs.values():
            if ed._completion_win is None:
                continue
            if widget is ed.text or widget is ed._completion_list:
                continue
            try:
                if widget is not None and str(widget).startswith(str(ed._completion_win)):
                    continue
            except tk.TclError:
                pass
            ed._close_completion()

    def update_tab_title(self, tab: EditorTab) -> None:
        if tab not in self.docs.values():
            return
        title = ("* " if tab.dirty else "") + tab.display_name
        self.notebook.set_title(tab, title)
        self._update_title()

    def _update_title(self) -> None:
        ed = self.current_editor()
        if ed is None:
            self.title(APP_NAME)
            return
        suffix = (" — " + self.workspace) if self.workspace else ""
        self.title(f"{'*' if ed.dirty else ''}{ed.display_name} — {APP_NAME}{suffix}")

    def _update_root_label(self) -> None:
        if self.root_path:
            name = os.path.basename(self.root_path)
            self.root_label.configure(text=f"主程序：{name}")
        else:
            self.root_label.configure(text="主程序：当前文件")

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def set_cursor(self, line: int, col: int) -> None:
        self.cursor_var.set(f"行 {line}，列 {col}")

    # -- 文件操作 ------------------------------------------------------
    def new_file(self) -> None:
        if not self.workspace:
            messagebox.showinfo("提示", "请先打开一个文件夹，再新建文件。", parent=self)
            self.open_folder()
            if not self.workspace:
                return
        base = self._selected_directory()
        name = simpledialog.askstring(
            "新建文件", "文件名（自动补 .homo）：",
            initialvalue="新文件.homo", parent=self,
        )
        if not name:
            return
        name = name.strip()
        if os.sep in name or (os.altsep and os.altsep in name):
            messagebox.showerror("文件名不合法", "文件名里不能包含路径分隔符。", parent=self)
            return
        if not name.lower().endswith(FILE_EXTS):
            name += ".homo"
        path = os.path.join(base, name)
        if os.path.exists(path):
            messagebox.showerror("文件已存在", path, parent=self)
            return
        try:
            write_text_file(path, self._starter_file_text())
        except OSError as exc:
            messagebox.showerror("创建失败", str(exc), parent=self)
            return
        self.refresh_tree()
        self.open_path(path)
        self.set_status(f"已新建：{name}")

    def _selected_directory(self) -> str:
        if not self.workspace:
            return os.getcwd()
        selection = self.tree.selection()
        if selection:
            path = selection[0]
            if os.path.isdir(path):
                return path
            return os.path.dirname(path)
        ed = self.current_editor()
        if ed and ed.path:
            return os.path.dirname(ed.path)
        return self.workspace

    def _suspend_editor_tasks(self) -> None:
        self._dialog_active = True
        for editor in self.docs.values():
            editor._suspend_for_dialog()

    def _resume_editor_tasks(self) -> None:
        self._dialog_active = False
        for editor in list(self.docs.values()):
            try:
                if editor.winfo_exists():
                    editor._schedule_lint(180)
            except tk.TclError:
                pass

    def _ask_open_file(self, **options) -> str:
        self._suspend_editor_tasks()
        try:
            options.setdefault("parent", self)
            return filedialog.askopenfilename(**options)
        finally:
            self._resume_editor_tasks()

    def _ask_save_file(self, **options) -> str:
        self._suspend_editor_tasks()
        try:
            options.setdefault("parent", self)
            return filedialog.asksaveasfilename(**options)
        finally:
            self._resume_editor_tasks()

    def _ask_directory(self, **options) -> str:
        self._suspend_editor_tasks()
        try:
            options.setdefault("parent", self)
            return filedialog.askdirectory(**options)
        finally:
            self._resume_editor_tasks()

    def new_project(self) -> None:
        if not self._confirm_close_all():
            return
        directory = self._ask_directory(
            title="新建项目：选择一个文件夹",
            initialdir=self.workspace or os.getcwd(),
        )
        if not directory:
            return
        directory = normalize_path(directory)
        main_path = os.path.join(directory, "main.homo")
        greet_path = os.path.join(directory, "问候.homo")
        if os.path.exists(main_path) or os.path.exists(greet_path):
            if not messagebox.askyesno(
                "文件已存在",
                "文件夹里已经有 main.homo 或 问候.homo，要覆盖它们吗？",
                parent=self,
            ):
                return
        try:
            write_text_file(main_path, (
                "inm 问候\n\n"
                "会员制餐厅(){\n"
                '    脱出(问候("下北泽"))噗叽啪\n'
                "    (0)人幸终\n"
                "}\n"
            ))
            write_text_file(greet_path, (
                "问候(地方 事 \"下北泽\"){\n"
                "    (\"你好，\" + 地方 + \"！\")人幸终\n"
                "}\n"
            ))
        except OSError as exc:
            messagebox.showerror("创建项目失败", str(exc), parent=self)
            return
        self.load_workspace(directory)
        self.set_status(f"已创建项目：{directory}")

    def open_folder(self) -> None:
        directory = self._ask_directory(
            title="打开 homo 项目文件夹",
            initialdir=self.workspace or os.getcwd(),
        )
        if directory:
            self.load_workspace(directory)

    def open_file(self) -> None:
        ed = self.current_editor()
        initial_dir = (os.path.dirname(ed.path) if ed and ed.path
                       else (self.workspace or os.getcwd()))
        path = self._ask_open_file(
            title="打开 homo 文件", initialdir=initial_dir,
            filetypes=[("homo 文件", "*.homo"), ("所有文件", "*.*")],
        )
        if not path:
            return
        path = normalize_path(path)
        # 打开单个文件时不自动把所在目录当成项目，也不填充左侧文件树。
        opened = self.open_path(path)
        if opened is not None and len(self.docs) > 1:
            # 第一次打开真实文件时，顺手去掉没有修改过的空白起始标签。
            for blank in list(self.docs.values()):
                if blank is opened or blank.path is not None or blank.dirty:
                    continue
                self.docs.pop(blank.key, None)
                self.notebook.remove(blank)
                blank.destroy()
        if path.lower().endswith(".homo"):
            self.set_main_path(path)
        if self.workspace:
            self.set_status(f"已打开文件：{os.path.basename(path)}")
        else:
            self.set_status(f"已打开单文件：{os.path.basename(path)}（未打开文件夹）")

    def save_current(self) -> None:
        ed = self.current_editor()
        if ed is not None:
            self.save_editor(ed)

    def save_editor(self, ed: EditorTab) -> bool:
        if not ed.path:
            return self.save_editor_as(ed)
        try:
            write_text_file(ed.path, ed.get_text())
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return False
        ed.dirty = False
        self.update_tab_title(ed)
        self.set_status(f"已保存：{os.path.basename(ed.path)}")
        self.refresh_tree()
        return True

    def save_current_as(self) -> None:
        ed = self.current_editor()
        if ed is not None:
            self.save_editor_as(ed)

    def save_editor_as(self, ed: EditorTab) -> bool:
        initial_dir = os.path.dirname(ed.path) if ed.path else (self.workspace or os.getcwd())
        default_ext = ".homo"
        # 已有文件预填完整名字，让对话框自动选中正确类型；新文件不预填后缀，
        # 避免部分平台把默认 .homo 再追加一次。
        initial_file = ed.display_name if ed.path else "main"
        filetypes = [("homo 文件", "*.homo"), ("所有文件", "*.*")]
        path = self._ask_save_file(
            title="保存 homo 文件", initialdir=initial_dir,
            initialfile=initial_file, defaultextension="",
            filetypes=filetypes,
        )
        if not path:
            return False
        path = normalize_path(normalize_save_path(path, default_ext))
        # 另存为到已有文件时明确确认，避免静默覆盖。
        same_as_current = (ed.path is not None
                           and os.path.normcase(path) == os.path.normcase(ed.path))
        if os.path.exists(path) and not same_as_current:
            if not messagebox.askyesno(
                "文件已存在", f"{os.path.basename(path)} 已经存在，要覆盖吗？", parent=self,
            ):
                return False
        other = self.docs.get(path)
        if other is not None and other is not ed:
            messagebox.showerror("文件已打开", "这个文件已经在另一个标签里打开了。", parent=self)
            return False
        try:
            write_text_file(path, ed.get_text())
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return False
        old_key = ed.key
        old_path = ed.path
        ed.path = path
        ed.key = path
        ed.dirty = False
        if old_key != path:
            self.docs.pop(old_key, None)
            self.docs[path] = ed
        if self._current_key == old_key:
            self._current_key = path
        self.update_tab_title(ed)
        if path.lower().endswith(".homo") and (self.root_path is None
                                               or old_path == self.root_path):
            self.set_main_path(path)
        if not self.workspace:
            self.workspace = os.path.dirname(path)
        self.refresh_tree()
        self.set_status(f"已保存：{os.path.basename(path)}")
        return True

    def save_all(self) -> bool:
        ok = True
        for ed in list(self.docs.values()):
            if ed.dirty:
                ok = self.save_editor(ed) and ok
        return ok

    def close_current(self) -> None:
        ed = self.current_editor()
        if ed is not None:
            self.close_editor(ed)

    def close_editor(self, ed: EditorTab) -> bool:
        if ed.dirty:
            answer = messagebox.askyesnocancel(
                "未保存的修改", f"「{ed.display_name}」有未保存的修改，要保存吗？",
                parent=self,
            )
            if answer is None:
                return False
            if answer and not self.save_editor(ed):
                return False
        self.docs.pop(ed.key, None)
        if self._current_key == ed.key:
            self._current_key = None
        self.notebook.remove(ed)
        if ed.path and self.root_path == ed.path:
            self.root_path = None
            self.refresh_tree()
        ed.destroy()
        if not self.docs:
            self._new_untitled(self._starter_text())
        self._on_tab_changed()
        return True

    def _confirm_close_all(self) -> bool:
        for ed in list(self.docs.values()):
            if ed.dirty:
                answer = messagebox.askyesnocancel(
                    "未保存的修改",
                    f"「{ed.display_name}」有未保存的修改，切换项目前要保存吗？",
                    parent=self,
                )
                if answer is None:
                    return False
                if answer and not self.save_editor(ed):
                    return False
        return True

    def set_main_path(self, path: str) -> None:
        self.root_path = normalize_path(path)
        self._update_root_label()
        self.refresh_tree()
        self.set_status(f"主程序：{os.path.basename(self.root_path)}")

    def set_current_main(self) -> None:
        ed = self.current_editor()
        if ed is None or not ed.path:
            messagebox.showinfo("提示", "请先保存当前文件，再把它设为主程序。", parent=self)
            return
        if not ed.is_homo:
            messagebox.showinfo("提示", "只有 .homo 文件可以作为主程序。", parent=self)
            return
        self.set_main_path(ed.path)

    def set_selected_main(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        path = selection[0]
        if not path.lower().endswith(".homo"):
            messagebox.showinfo("提示", "只有 .homo 文件可以作为主程序。", parent=self)
            return
        self.set_main_path(path)
        self.open_path(path)

    # -- 编辑动作 ------------------------------------------------------
    def _edit_action(self, action: str) -> None:
        ed = self.current_editor()
        if ed is None:
            return
        text = ed.text
        text.focus_set()
        if action == "undo":
            try:
                text.edit_undo()
            except tk.TclError:
                pass
        elif action == "redo":
            try:
                text.edit_redo()
            except tk.TclError:
                pass
        elif action == "cut":
            text.event_generate("<<Cut>>")
        elif action == "copy":
            text.event_generate("<<Copy>>")
        elif action == "paste":
            text.event_generate("<<Paste>>")
        elif action == "select_all":
            text.tag_add("sel", "1.0", "end-1c")
            text.mark_set("insert", "1.0")

    def toggle_comment(self) -> None:
        ed = self.current_editor()
        if ed is not None:
            ed.toggle_comment()

    def show_find(self) -> None:
        if self._find_dialog is not None and self._find_dialog.winfo_exists():
            self._find_dialog.lift()
            self._find_dialog.find_entry.focus_set()
            return
        self._find_dialog = FindDialog(self)

    def goto_line_dialog(self) -> None:
        ed = self.current_editor()
        if ed is None:
            return
        line = simpledialog.askinteger(
            "转到行", "行号：", parent=self, minvalue=1,
            initialvalue=int(ed.text.index("insert").split(".")[0]),
        )
        if line:
            ed.goto_line(line)

    def zoom(self, delta: int) -> None:
        base = abs(getattr(self, "font_base_code", 13))
        base_gutter = abs(getattr(self, "font_base_gutter", 10))
        step = max(1, round(getattr(self, "ui_scale", 1.0)))
        current = abs(int(self.font_code.cget("size")))
        size = base if delta == 0 else max(8, min(56, current + delta * step))
        self.font_code.configure(size=-size)
        self.font_code_bold.configure(size=-size)
        gutter = max(8, base_gutter + (size - base))
        self.font_gutter.configure(size=-gutter)
        self.font_gutter_bold.configure(size=-gutter)
        self.char_width = max(6, self.font_code.measure("M"))
        self.gutter_char_width = max(6, self.font_gutter.measure("0"))
        for ed in self.docs.values():
            ed.text.configure(tabs=(self.char_width * 4,))
            ed.update_gutter()

    # -- 自动补全库 ----------------------------------------------------
    def completion_items(self, prefix: str,
                         editor: Optional[EditorTab] = None) -> list[tuple[str, str]]:
        """与旧网页版一致：关键字 + 当前文档里出现过、且不是关键字的标识符。"""
        seen: set[str] = set()
        out: list[tuple[str, str]] = []

        def consider(name: str, kind: str) -> None:
            if not name or name == prefix or name in seen:
                return
            if not name.startswith(prefix):
                return
            seen.add(name)
            out.append((name, kind))

        for name, kind in AC_KEYWORDS:
            consider(name, kind)
        current = editor or self.current_editor()
        if current is not None:
            try:
                for name, kind in collect_doc_names(current.get_text()):
                    consider(name, kind)
            except tk.TclError:
                pass
        out.sort(key=lambda item: (len(item[0]), item[0]))
        return out[:AC_MAX]

    # -- 运行 ----------------------------------------------------------
    def _root_from_path(self, path: str) -> Optional[tuple[Optional[str], str, str]]:
        doc = self.doc_for_path(path)
        if doc is not None:
            return doc.path, doc.get_text(), doc.display_name
        if not os.path.isfile(path):
            return None
        try:
            return path, read_text_file(path), os.path.basename(path)
        except OSError:
            return None

    def _resolve_main_root(self) -> Optional[tuple[Optional[str], str, str]]:
        """运行/检查/导出都只使用已经设置的主程序，与当前编辑哪个文件无关。"""
        if not self.root_path:
            messagebox.showinfo(
                "没有主程序",
                "请先在左侧项目树右键一个 .homo 文件，选择「设为主程序」。",
                parent=self,
            )
            return None
        if not self.root_path.lower().endswith(".homo"):
            messagebox.showinfo(
                "主程序无效",
                "主程序必须是 .homo 文件，请重新设置。",
                parent=self,
            )
            return None
        root = self._root_from_path(self.root_path)
        if root is None:
            messagebox.showerror(
                "主程序不存在",
                f"主程序文件不存在或无法读取：\n{self.root_path}",
                parent=self,
            )
            return None
        return root

    def _overlay(self) -> dict[str, str]:
        overlay: dict[str, str] = {}
        for ed in self.docs.values():
            if ed.path and ed.dirty:
                overlay[os.path.realpath(ed.path)] = ed.get_text()
        return overlay

    def _include_dirs(self) -> list[str]:
        dirs = []
        if self.workspace:
            dirs.append(self.workspace)
        return dirs

    def lint_context(self, editor: "EditorTab"):
        """返回实时检查所需的 (path, source, overlay, include_dirs, base_dir)。"""
        overlay = self._overlay()
        include_dirs = self._include_dirs()
        if editor.is_homo:
            return editor.path, editor.get_text(), overlay, include_dirs, None
        if editor.path is None:
            return None, editor.get_text(), overlay, include_dirs, self.workspace
        return editor.path, editor.get_text(), overlay, include_dirs, None

    def _save_all_dirty(self, skip_untitled: bool = False) -> bool:
        """运行前保存所有打开且有修改的文件；取消或失败则中止运行。"""
        changed = [ed for ed in self.docs.values() if ed.dirty]
        for editor in changed:
            if skip_untitled and editor.path is None:
                continue
            if not self.save_editor(editor):
                return False
        saved = [ed for ed in changed if not (skip_untitled and ed.path is None)]
        if saved:
            self.set_status(f"已自动保存 {len(saved)} 个文件")
        return True

    def run_current(self) -> None:
        if self.job_active:
            return
        editor = self.current_editor()
        run_buffer = (not self.root_path and editor is not None
                      and (editor.path is None or editor.is_homo))
        if not self._save_all_dirty(skip_untitled=run_buffer):
            return
        if run_buffer:
            # 没有设置主程序时，直接运行当前空白/未命名缓冲区。
            root = (editor.path, editor.get_text(), editor.display_name)
        else:
            root = self._resolve_main_root()
        if root is None:
            return
        self._start_job("run", root, None)

    def compile_only(self) -> None:
        if self.job_active:
            return
        root = self._resolve_main_root()
        if root is None:
            return
        self._start_job("check", root, None)

    def export_exe(self) -> None:
        if self.job_active:
            return
        root = self._resolve_main_root()
        if root is None:
            return
        default = os.path.splitext(root[2])[0] + (".exe" if IS_WIN else "")
        path = self._ask_save_file(
            title="导出可执行文件",
            initialdir=self.workspace or os.getcwd(), initialfile=default,
            defaultextension=".exe" if IS_WIN else "",
            filetypes=[("可执行文件", "*.exe")] if IS_WIN else [("所有文件", "*.*")],
        )
        if not path:
            return
        self._start_job("exe", root, normalize_path(path))

    def _start_job(self, mode: str, root: tuple[Optional[str], str, str],
                   exe_target: Optional[str]) -> None:
        self.job_id += 1
        job = self.job_id
        self.job_active = True
        self.cancel_event = threading.Event()
        self.proc = None
        self.workdir = None
        self.output_chars = len(self.run_text.get("1.0", "end-1c"))
        self._clear_problems()
        self.run_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        self.send_button.configure(state="disabled")
        self.input_entry.configure(state="disabled")
        self.input_var.set("")
        self.output_book.select(0)
        path, source, name = root
        mode_label = {"run": "运行", "check": "编译检查", "exe": "导出"}[mode]
        self._append_output(f"\n── {mode_label} {name} ──\n", "meta")
        self.set_status(f"正在{mode_label}：{name}")
        overlay = self._overlay()
        include_dirs = self._include_dirs()
        base_dir = self.workspace if path is None else None
        threading.Thread(
            target=self._job_worker,
            args=(job, mode, path, source, overlay, include_dirs, base_dir,
                  exe_target, self.cancel_event),
            daemon=True,
        ).start()
        self._poll_job(job)
        self._update_title()

    def _job_worker(self, job: int, mode: str, path: Optional[str], source: str,
                    overlay: dict[str, str], include_dirs: list[str],
                    base_dir: Optional[str],
                    exe_target: Optional[str], cancel: threading.Event) -> None:
        q = self.event_queue
        try:
            compiled = homo.compile_source(
                source, path=path, overlay=overlay, include_dirs=include_dirs,
                base_dir=base_dir,
            )
        except homo.HomoError as exc:
            q.put((job, "homo_error", homo.format_error(exc),
                   [homo.error_diagnostic(exc)]))
            return
        except Exception as exc:  # noqa: BLE001
            q.put((job, "fatal", f"内部错误：{exc}", []))
            return
        try:
            result = homo.build(compiled)
        except homo.HomoError as exc:
            q.put((job, "homo_error", homo.format_error(exc),
                   [homo.error_diagnostic(exc)]))
            return
        except Exception as exc:  # noqa: BLE001
            q.put((job, "fatal", f"内部错误：{exc}", []))
            return
        if cancel.is_set():
            shutil.rmtree(result.get("workdir") or "", ignore_errors=True)
            q.put((job, "cancelled",))
            return
        q.put((job, "build_done", result, mode))
        if not result["ok"]:
            shutil.rmtree(result["workdir"], ignore_errors=True)
            return
        if mode == "check":
            shutil.rmtree(result["workdir"], ignore_errors=True)
            return
        if cancel.is_set():
            shutil.rmtree(result["workdir"], ignore_errors=True)
            q.put((job, "cancelled",))
            return
        if mode == "exe":
            try:
                assert exe_target is not None
                shutil.copyfile(result["exe"], exe_target)
                if not IS_WIN:
                    os.chmod(exe_target, 0o755)
                q.put((job, "exe_saved", exe_target))
            except OSError as exc:
                q.put((job, "fatal", f"保存可执行文件失败：{exc}", []))
            finally:
                shutil.rmtree(result["workdir"], ignore_errors=True)
            return
        if cancel.is_set():
            shutil.rmtree(result["workdir"], ignore_errors=True)
            q.put((job, "cancelled",))
            return
        run_cwd = base_dir or (os.path.dirname(path) if path else os.getcwd())
        try:
            proc = subprocess.Popen(
                [result["exe"]],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0,
                cwd=run_cwd,
                creationflags=_no_window_flags(),
            )
        except OSError as exc:
            shutil.rmtree(result["workdir"], ignore_errors=True)
            q.put((job, "fatal", f"无法启动程序：{exc}", []))
            return
        if cancel.is_set():
            try:
                proc.kill()
            except OSError:
                pass
            shutil.rmtree(result["workdir"], ignore_errors=True)
            q.put((job, "cancelled",))
            return
        q.put((job, "started", proc, result["workdir"]))

        def pump(stream, tag: str) -> None:
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    text = decoder.decode(chunk)
                    if text:
                        q.put((job, tag, text))
                tail = decoder.decode(b"", final=True)
                if tail:
                    q.put((job, tag, tail))
            except (OSError, ValueError):
                pass

        t_out = threading.Thread(target=pump, args=(proc.stdout, "stdout"), daemon=True)
        t_err = threading.Thread(target=pump, args=(proc.stderr, "stderr"), daemon=True)
        t_out.start()
        t_err.start()
        code = proc.wait()
        t_out.join(timeout=2)
        t_err.join(timeout=2)
        shutil.rmtree(result["workdir"], ignore_errors=True)
        q.put((job, "exited", code))

    def _poll_job(self, job: int) -> None:
        if self.poll_id is not None:
            try:
                self.after_cancel(self.poll_id)
            except tk.TclError:
                pass
        self.poll_id = None
        while True:
            try:
                event = self.event_queue.get_nowait()
            except queue.Empty:
                break
            if event[0] != job:
                continue
            self._handle_event(event)
        if self.job_active and job == self.job_id:
            self.poll_id = self.after(40, lambda: self._poll_job(job))

    def _show_inline_diagnostics(self, diagnostics: list[dict]) -> None:
        current = self.current_editor()
        if current is None or not diagnostics:
            return
        for diag in diagnostics:
            if current._diag_matches_current(diag):
                current._show_lint(diag)
                return
        # 错误发生在别的文件时，也把文件名显示在当前编辑器的检查栏里。
        current._show_lint(diagnostics[0])

    def _handle_event(self, event: tuple) -> None:
        _, kind = event[0], event[1]
        if kind == "homo_error":
            _, _, text, diagnostics = event
            self._append_output(text + "\n", "err")
            self._set_problems(diagnostics)
            self._show_inline_diagnostics(diagnostics)
            self.output_book.select(1)
            self.set_status("编译失败")
            self._finish_job()
            return
        if kind == "fatal":
            _, _, text, diagnostics = event
            self._append_output(text + "\n", "err")
            self._set_problems(diagnostics)
            self._show_inline_diagnostics(diagnostics)
            self.set_status("失败")
            self._finish_job()
            return
        if kind == "build_done":
            _, _, result, mode = event
            self._set_cpp(result.get("cpp", ""))
            self._set_problems(result.get("diagnostics", []))
            if not result.get("ok"):
                output = (result.get("compile_output") or "").strip()
                if output:
                    self._append_output(output + "\n", "err")
                self._show_inline_diagnostics(result.get("diagnostics", []))
                self.output_book.select(1)
                self.set_status("编译失败")
                self._finish_job()
                return
            current = self.current_editor()
            if current is not None:
                current._clear_lint()
            if mode == "check":
                self._append_output("✓ 编译通过，没有发现错误\n", "ok")
                self.set_status("编译通过")
                self._finish_job()
            elif mode == "run":
                self.output_book.select(0)
            return
        if kind == "cancelled":
            self._append_output("\n[已取消]\n", "meta")
            self.set_status("已取消")
            self._finish_job()
            return
        if kind == "exe_saved":
            _, _, path = event
            self._append_output(f"已导出可执行文件：{path}\n", "ok")
            self.set_status(f"已导出：{os.path.basename(path)}")
            self._finish_job()
            return
        if kind == "started":
            _, _, proc, workdir = event
            self.proc = proc
            self.workdir = workdir
            self.stop_button.configure(state="normal")
            self.input_entry.configure(state="normal")
            self.send_button.configure(state="normal")
            self.input_entry.focus_set()
            self.set_status("运行中…（可在下方输入标准输入）")
            return
        if kind == "stdout":
            self._append_output(event[2])
            return
        if kind == "stderr":
            self._append_output(event[2], "stderr")
            return
        if kind == "exited":
            _, _, code = event
            if code == 2:
                self._append_output("\n[进程结束，退出码 2：正常结束]\n", "ok")
                self.set_status("运行结束（正常）")
            elif code in (None, 0):
                self._append_output(f"\n[进程结束，退出码 {code}]\n", "meta")
                self.set_status("运行结束")
            else:
                self._append_output(f"\n[进程结束，退出码 {code}]\n", "err")
                self.set_status(f"运行结束（退出码 {code}）")
            self.proc = None
            self._finish_job()
            return

    def _finish_job(self) -> None:
        self.job_active = False
        self.proc = None
        self.run_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.send_button.configure(state="disabled")
        self.input_entry.configure(state="disabled")
        self._update_title()

    def stop_program(self) -> None:
        if self.job_active:
            self.cancel_event.set()
        if self.proc is not None:
            self._append_output("\n[正在停止程序…]\n", "meta")
            try:
                self.proc.kill()
            except OSError:
                pass
            self.set_status("已请求停止")
        elif self.job_active:
            self.set_status("正在取消编译…")

    def send_input(self) -> None:
        if self.proc is None or self.proc.stdin is None:
            return
        text = self.input_var.get()
        self.input_var.set("")
        try:
            self.proc.stdin.write((text + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except (OSError, ValueError):
            self._append_output("\n[程序已经结束，无法再输入]\n", "meta")
            self.send_button.configure(state="disabled")
            self.input_entry.configure(state="disabled")

    # -- 输出 / 问题 ---------------------------------------------------
    def _append_output(self, text: str, tag: Optional[str] = None) -> None:
        widget = self.run_text
        widget.configure(state="normal")
        if tag:
            widget.insert("end", text, tag)
        else:
            widget.insert("end", text)
        self.output_chars += len(text)
        if self.output_chars > MAX_OUTPUT_CHARS:
            excess = self.output_chars - MAX_OUTPUT_CHARS
            widget.delete("1.0", f"1.0+{excess}c")
            self.output_chars = MAX_OUTPUT_CHARS
        widget.see("end")
        widget.configure(state="disabled")

    def clear_output(self) -> None:
        self.run_text.configure(state="normal")
        self.run_text.delete("1.0", "end")
        self.run_text.configure(state="disabled")
        self.output_chars = 0

    def _set_cpp(self, cpp: str) -> None:
        self.cpp_text.configure(state="normal")
        self.cpp_text.delete("1.0", "end")
        self.cpp_text.insert("1.0", cpp or "")
        self.cpp_text.configure(state="disabled")

    def _clear_problems(self) -> None:
        self._problem_map.clear()
        self.problems.delete(*self.problems.get_children())

    def _set_problems(self, diagnostics: list[dict]) -> None:
        self._clear_problems()
        for diag in diagnostics or []:
            severity = diag.get("severity") or "错误"
            file = diag.get("file") or ""
            if file and os.path.basename(file) == "homo_runtime.hpp":
                continue
            line = diag.get("line") or 0
            message = (diag.get("message") or "").replace("\n", " ")
            iid = self.problems.insert(
                "", "end",
                values=(severity, os.path.basename(file) if file else "—", line, message),
                tags=(severity if severity in ("错误", "警告", "提示") else "错误",),
            )
            self._problem_map[iid] = diag
        if self._problem_map:
            self.output_book.select(1)

    def _on_problem_double(self, event=None) -> None:
        selection = self.problems.selection()
        if not selection:
            return
        diag = self._problem_map.get(selection[0])
        if diag is None:
            return
        self._open_diagnostic(diag)

    def _open_diagnostic(self, diag: dict) -> None:
        file = diag.get("file") or ""
        path = diag.get("path") or ""
        line = int(diag.get("line") or 0)
        candidate = None
        if path and os.path.isfile(path):
            candidate = path
        elif file and os.path.isfile(file):
            candidate = file
        elif file and self.workspace and os.path.isfile(os.path.join(self.workspace, file)):
            candidate = os.path.join(self.workspace, file)
        if candidate is None and file:
            base = os.path.basename(file)
            for ed in self.docs.values():
                if ed.path and os.path.basename(ed.path) == base:
                    ed.goto_line(line, highlight_error=True)
                    self.notebook.select(ed)
                    return
            if self.workspace:
                for root, _, files in os.walk(self.workspace):
                    if base in files:
                        candidate = os.path.join(root, base)
                        break
        if candidate:
            ed = self.open_path(candidate, line)
            if ed is not None:
                ed.goto_line(line, highlight_error=True)
        elif file in ("<内存>", "—", "") and self.current_editor() is not None:
            self.current_editor().goto_line(line, highlight_error=True)

    # -- 编译检测 ------------------------------------------------------
    def _detect_compiler(self) -> None:
        try:
            compiler = homo.find_compiler()
            self._compiler_label = "C++ 编译器：" + (compiler.label or compiler.path)
        except homo.HomoError:
            self._compiler_label = "未找到 C++ 编译器（g++ / clang++ / MSVC）"
        except Exception as exc:  # noqa: BLE001
            self._compiler_label = f"检测 C++ 编译器失败：{exc}"

    def _poll_compiler_label(self) -> None:
        if self._compiler_label:
            self.compiler_var.set(self._compiler_label)
        if "正在检测" in self._compiler_label:
            self.after(400, self._poll_compiler_label)
        else:
            self.compiler_var.set(self._compiler_label)

    # -- 帮助 ----------------------------------------------------------
    def show_help(self) -> None:
        messagebox.showinfo(
            "快捷键与用法",
            "文件\n"
            f"  {MOD}+N 新建文件    {MOD}+O 打开文件    {MOD}+Shift+O 打开文件夹\n"
            "  工具栏「打开文件」可单独打开一个文件，不必先打开文件夹；标签右侧 × 可关闭。\n"
            "  多文件用 inm 其他文件 直接引用；inm 类名 from 文件 只引用指定类。\n"
            "  函数声明：你是一个一个一个 函数名（参数）哼哼哼啊啊啊啊啊\n"
            "  主程序显示屎黄色；其他文件为白色。\n"
            "  运行/编译检查/导出始终从设置的主程序开始，与当前编辑哪个文件无关。\n"
            "  macOS 使用系统原生文件/文件夹面板（按钮为英文）；Windows 使用系统原生对话框。\n"
            f"  {MOD}+S 保存        {MOD}+Shift+S 另存为    {MOD}+W 关闭\n\n"
            "编辑\n"
            f"  {MOD}+F 查找替换    {MOD}+G 转到行    {MOD}+/ 注释\n"
            f"  {MOD}+= / {MOD}+- 缩放字体    输入时自动弹出补全；Ctrl+Space / ⌘+J 手动打开\n"
            "  输入 ( [ { 和引号会自动补全右半边；输入已有右括号时自动跳过，退格可删整对。\n"
            "  停止输入约半秒后自动做语法/名字检查，错误行会在行号区和代码行标出。\n\n"
            "运行\n"
            f"  {MOD}+R 编译并运行    {MOD}+B 仅编译检查    {MOD}+. 停止\n"
            "  运行期间可以在下方「标准输入」里给程序发送输入。\n"
            "  标签上右键可以保存 / 关闭当前文件。\n\n"
            "多文件\n"
            "  inm 其他文件        引用整个 .homo\n"
            "  inm 类名 from 文件  只引用该文件里的这个类\n"
            "  同一文件多次 inm 只加载一次；.inm 扩展名和 import 关键字已废除。\n\n"
            "编译器\n"
            "  编译器按 HOMO_CXX 环境变量、g++、clang++、c++、clang-cl、cl 的顺序查找。\n",
            parent=self,
        )

    def show_about(self) -> None:
        messagebox.showinfo(
            "关于",
            f"{APP_NAME} {APP_VERSION}\n\n"
            "homo 语言的桌面编辑器与编译器。\n"
            "把 .homo 翻译成 C++17，再调用系统 C++ 编译器生成原生程序。\n"
            "支持 Windows 与 macOS，支持多文件 inm 导入。\n",
            parent=self,
        )

    # -- 关闭 ----------------------------------------------------------
    def on_close(self) -> None:
        if self.job_active:
            if not messagebox.askyesno(
                "程序正在运行", "程序还在运行，确定要退出吗？", parent=self,
            ):
                return
        for ed in list(self.docs.values()):
            if ed.dirty:
                answer = messagebox.askyesnocancel(
                    "未保存的修改",
                    f"「{ed.display_name}」有未保存的修改，退出前要保存吗？",
                    parent=self,
                )
                if answer is None:
                    return
                if answer and not self.save_editor(ed):
                    return
        self.cancel_event.set()
        if self.proc is not None:
            try:
                self.proc.kill()
            except OSError:
                pass
        self.destroy()


def _pump(app: tk.Tk, seconds: float, until=None) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        if until is not None and until():
            return True
        time.sleep(0.02)
    app.update()
    return until is None or bool(until())


def self_test(project: Optional[str]) -> int:
    """无界面驱动一次「打开项目 → 编译运行」，用于打包后的快速验证。"""
    app = HomoEditorApp(project)
    if not _pump(app, 10, lambda: app.current_editor() is not None):
        _safe_print("自检失败：编辑器没有打开任何文件", file=sys.stderr)
        app.destroy()
        return 2
    app.run_current()
    _pump(app, 60, lambda: not app.job_active)
    output = app.run_text.get("1.0", "end-1c")
    _safe_print(output)
    ok = "[进程结束，退出码 2：正常结束]" in output
    if not ok:
        _safe_print(f"自检失败：状态={app.status_var.get()}", file=sys.stderr)
        for iid, diag in app._problem_map.items():
            _safe_print(f"  {diag.get('file')}:{diag.get('line')} "
                        f"{diag.get('message')}", file=sys.stderr)
    app.destroy()
    return 0 if ok else 1


def main() -> int:
    args = sys.argv[1:]
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    if "--self-test" in args:
        index = args.index("--self-test")
        project = args[index + 1] if index + 1 < len(args) else None
        return self_test(project)
    initial = args[0] if args else None
    try:
        app = HomoEditorApp(initial)
        app.mainloop()
    except Exception as exc:  # noqa: BLE001
        try:
            messagebox.showerror(APP_NAME, f"启动失败：\n{exc}")
        except Exception:  # noqa: BLE001
            _safe_print(f"启动失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
