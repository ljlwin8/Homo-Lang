#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""homo 语言编译器：homo → C++17 → 原生可执行文件。

homo 源码里所有变量都用「你是一个一个一个」声明，不写类型；
编译器负责自动推导变量类型（整数 / 浮点数 / 字符串），
并把「事」「便乘」等词形算子翻译成 C++ 调用。

支持用 inm 引用 .homo 文件里的声明与定义；
Windows / macOS 上都可以调用系统 C++ 编译器生成原生程序。
"""

from __future__ import annotations

import argparse
import dataclasses
import locale
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Optional

# --------------------------------------------------------------------------
# 关键字
# --------------------------------------------------------------------------
DECL = "你是一个一个一个"          # 变量声明
DECL_END = "哼哼哼啊啊啊啊啊"      # 无初值声明的结尾
ASSIGN = "事"                     # =（是）
STMT_END = "罢"                   # 赋值语句结尾
CALL_END = "噗叽啪"               # 调用语句结尾

IF_HEAD = "老爷你下面的"           # if
IF_MID = "都白了"
ELSEIF_HEAD = "戳啦"              # else if
ELSEIF_TAIL = "嘛"
ELSE = "戳啦极霸矛嘛"             # else

WHILE_HEAD = "动起来啊动起来"      # while
TRUE_KW = "确信"                  # 真
FALSE_KW = "大嘘"                 # 假
NULL_KW = "虚无"                  # 空值
NOT_KW = "迫真"                   # 逻辑非
LT_KW = "甚至"                    # <
AND_KW = "且"                     # 逻辑与
EQ_HEAD = "先辈"                  # 相等比较：(a)先辈(b)说
EQ_TAIL = "说"
OR_KW = "或"                      # 逻辑或
MOD_KW = "化粪"                   # 取模
CONTINUE_KW = "速达哟"              # continue
SWITCH_HEAD = "人类有三大欲望"     # switch
CASE_KW = "食欲"                  # case
DEFAULT_KW = "性欲睡眠欲"          # default
DO_HEAD = "是栗老师让我"           # do
DO_TAIL = "动起来的"
REPEAT_MID = "回啊"               # (x)回啊(y)回：x==y 走 x 次，否则走 |x-y| 次
REPEAT_TAIL = "回"
REPEAT_VAR = "回数"               # 循环里的迭代变量（从 0 开始）
BREAK_KW = "我已经等不及了"        # break
RETURN_KW = "人幸终"              # return（值写在前面）
CLASS_KW = "类"                   # 类定义
NEW_KW = "新建"                   # 创建对象
RANDOM_KW = "随机"                # 随机整数
VARIADIC_KW = "一坨"              # 参数表里的可变参数标记
LENGTH_KW = "长度"                # 数组/字符串长度
INIT_KW = "首"                    # 新建对象时自动调用的方法
PUBLIC_KW = "想看就看个够"         # 类体里切换为公开成员
PRIVATE_KW = "摄影禁止"            # 类体里切换为私有成员
# 字符串内置函数：名字 -> (最少参数, 最多参数)
STRING_BUILTIN_ARITY = {
    "取子串": (2, 3), "查找": (2, 2), "包含": (2, 2), "替换": (3, 3),
    "分割": (2, 2), "连接": (2, 2), "去空白": (1, 1),
    "转数字": (1, 1), "转大写": (1, 1), "转小写": (1, 1),
}

COMPOUND_OPS = {"便乘": "*", "便减": "-", "便加": "+", "便除": "/"}
ASSIGN_OPS = {ASSIGN} | set(COMPOUND_OPS)
PUNCTUATION = set("{}()[],;+-*/.:")
# 半角/全角引号都可以当字符串边界，且可以混用（开一种、闭另一种）。
QUOTE_CHARS = set("\"“”‘’'")
# 字符串可以用任意引号开头，由「同族的引号」闭合：
# 全角开头的字符串，同族任意全角引号或半角引号都能闭合（“…” 与 ”…“ 都成立），
# 半角开头只由半角或右全角闭合，这样 "他说“好”呀" 里的全角引号是普通字符。
QUOTE_CLOSERS = {
    '"': '"”', "“": '"“”', "”": '"“”',
    "'": "'’", "‘": "'‘’", "’": "'‘’",
}
# 允许全角/半角括号、逗号混用；token 内统一成半角文本。
FULLWIDTH_PUNC = {"（": "(", "）": ")", "，": ",", "；": ";", "：": ":"}
PUNCTUATION |= set(FULLWIDTH_PUNC)


class HomoError(Exception):
    def __init__(self, msg: str, line: int = 0, file: Optional[str] = None,
                 path: Optional[str] = None):
        super().__init__(msg)
        self.msg = msg
        self.line = line          # 加载多文件后是全局行号，由 SourceMap 换算
        self.file = file          # 展示用路径（相对项目根目录）
        self.path = path          # 绝对路径
        self.source_map = None


# --------------------------------------------------------------------------
# 词法分析
# --------------------------------------------------------------------------
@dataclass
class Token:
    kind: str   # id / num / str / punc
    text: str
    line: int


def tokenize(src: str) -> list[Token]:
    tokens: list[Token] = []
    i, n, line = 0, len(src), 1
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c.isspace():
            i += 1
            continue
        if src.startswith("//", i):
            while i < n and src[i] != "\n":
                i += 1
            continue
        if src.startswith("/*", i):
            i += 2
            while i < n and not src.startswith("*/", i):
                if src[i] == "\n":
                    line += 1
                i += 1
            i = min(n, i + 2)
            continue
        if c in QUOTE_CHARS:
            start_line = line
            j = i + 1
            closed = False
            while j < n:
                if src[j] == "\\" and j + 1 < n:
                    if src[j + 1] == "\n":
                        line += 1
                    j += 2
                    continue
                if src[j] in QUOTE_CLOSERS[c]:
                    j += 1
                    closed = True
                    break
                if src[j] == "\n":
                    line += 1
                j += 1
            if not closed:
                raise HomoError("字符串没有闭合", start_line)
            tokens.append(Token("str", src[i:j], start_line))
            i = j
            continue
        if c.isdigit():
            j = i
            while j < n and (src[j].isdigit() or src[j] == "."):
                j += 1
            tokens.append(Token("num", src[i:j], line))
            i = j
            continue
        if c in PUNCTUATION:
            tokens.append(Token("punc", FULLWIDTH_PUNC.get(c, c), line))
            i += 1
            continue
        j = i
        while j < n and not src[j].isspace() and src[j] not in PUNCTUATION and src[j] not in QUOTE_CHARS:
            j += 1
        tokens.append(Token("id", src[i:j], line))
        i = j
    return tokens


# --------------------------------------------------------------------------
# 语法树
# --------------------------------------------------------------------------
class Expr:
    line: int = 0


@dataclass
class Num(Expr):
    text: str
    line: int = 0


@dataclass
class Str(Expr):
    raw: str
    line: int = 0


@dataclass
class Ident(Expr):
    name: str
    line: int = 0


@dataclass
class BoolLit(Expr):
    value: bool = True
    line: int = 0


@dataclass
class NullLit(Expr):
    line: int = 0


@dataclass
class Call(Expr):
    name: str
    args: list[Expr] = field(default_factory=list)
    line: int = 0


@dataclass
class New(Expr):
    class_name: str = ""
    args: list[Expr] = field(default_factory=list)
    line: int = 0


@dataclass
class MethodCall(Expr):
    obj: Expr = None
    name: str = ""
    args: list[Expr] = field(default_factory=list)
    line: int = 0


@dataclass
class Not(Expr):
    expr: Expr = None
    line: int = 0


@dataclass
class Lt(Expr):
    left: Expr = None
    right: Expr = None
    line: int = 0


@dataclass
class Eq(Expr):
    left: Expr = None
    right: Expr = None
    line: int = 0


@dataclass
class And(Expr):
    left: Expr = None
    right: Expr = None
    line: int = 0


@dataclass
class Or(Expr):
    left: Expr = None
    right: Expr = None
    line: int = 0


@dataclass
class Bin(Expr):
    op: str = ""
    left: Expr = None
    right: Expr = None
    line: int = 0


@dataclass
class Index(Expr):
    obj: Expr = None
    index: Expr = None
    line: int = 0


@dataclass
class Field(Expr):
    obj: Expr = None
    name: str = ""
    line: int = 0


@dataclass
class ArrayLit(Expr):
    items: list[Expr] = field(default_factory=list)
    line: int = 0


@dataclass
class LoopVar(Expr):
    line: int = 0


class Stmt:
    line: int = 0


@dataclass
class Decl(Stmt):
    name: str
    init: Optional[Expr]
    line: int = 0
    # 「名字[n][m]…」按顺序列出每一维的长度；空列表表示普通变量。
    sizes: list[Expr] = field(default_factory=list)
    private: bool = False


@dataclass
class Assign(Stmt):
    name: str
    op: str
    expr: Expr
    line: int = 0
    index: Optional[Expr] = None
    target: Optional[Expr] = None


@dataclass
class CallStmt(Stmt):
    expr: Expr
    line: int = 0


@dataclass
class If(Stmt):
    cond: Expr
    then: list[Stmt]
    elifs: list[tuple[Expr, list[Stmt]]]
    els: Optional[list[Stmt]]
    line: int = 0


@dataclass
class While(Stmt):
    cond: Expr
    body: list[Stmt]
    line: int = 0


@dataclass
class Repeat(Stmt):
    start: Expr
    end: Expr
    body: list[Stmt]
    line: int = 0


@dataclass
class DoWhile(Stmt):
    body: list[Stmt]
    cond: Expr
    line: int = 0


@dataclass
class Switch(Stmt):
    expr: Expr
    cases: list[tuple[Optional[Expr], list[Stmt]]]
    line: int = 0


@dataclass
class Break(Stmt):
    line: int = 0


@dataclass
class Continue(Stmt):
    line: int = 0


@dataclass
class Return(Stmt):
    expr: Expr
    line: int = 0


@dataclass
class Param:
    name: str
    default: Optional[Expr] = None
    variadic: bool = False
    line: int = 0


@dataclass
class Func:
    name: str
    params: list[Param]
    body: list[Stmt]
    line: int = 0
    private: bool = False


@dataclass
class Class:
    name: str
    fields: list[Decl]
    methods: list[Func]
    line: int = 0


@dataclass
class ImportDecl:
    target: str
    line: int = 0
    selected_class: Optional[str] = None


@dataclass
class FuncDecl:
    """函数声明（没有函数体），定义由同文件或链接的 .homo 文件提供。"""
    name: str
    params: list[Param]
    line: int = 0
    private: bool = False


@dataclass
class ClassDecl:
    """类声明（.inm），实现由 .homo 里的类定义提供。"""
    name: str
    fields: list[Decl]
    methods: list[FuncDecl]
    line: int = 0


@dataclass
class Program:
    functions: list[Func]
    classes: list[Class] = field(default_factory=list)
    globals: list[Decl] = field(default_factory=list)
    imports: list[ImportDecl] = field(default_factory=list)
    declarations: list[FuncDecl] = field(default_factory=list)
    global_decls: list[Decl] = field(default_factory=list)
    class_decls: list[ClassDecl] = field(default_factory=list)
    entry: Optional[Func] = None


# --------------------------------------------------------------------------
# 语法分析
# --------------------------------------------------------------------------
class Parser:
    def __init__(self, tokens: list[Token], declaration_mode: bool = False):
        self.tokens = tokens
        self.pos = 0
        self.declaration_mode = declaration_mode

    # -- 基础工具 ------------------------------------------------------
    def peek(self, k: int = 0) -> Optional[Token]:
        idx = self.pos + k
        return self.tokens[idx] if idx < len(self.tokens) else None

    def at_end(self) -> bool:
        return self.pos >= len(self.tokens)

    def at_id(self, *names: str) -> bool:
        t = self.peek()
        return t is not None and t.kind == "id" and t.text in names

    def at_punc(self, ch: str) -> bool:
        t = self.peek()
        return t is not None and t.kind == "punc" and t.text == ch

    def last_line(self) -> int:
        if not self.tokens:
            return 1
        return self.tokens[min(self.pos, len(self.tokens) - 1)].line

    def consume(self) -> Token:
        t = self.peek()
        if t is None:
            raise HomoError("意外的文件结尾", self.last_line())
        self.pos += 1
        return t

    def expect_id(self, name: Optional[str] = None) -> Token:
        t = self.peek()
        if t is None or t.kind != "id":
            got = t.text if t else "文件结尾"
            raise HomoError(f"这里需要一个标识符，但读到了「{got}」", t.line if t else self.last_line())
        if name is not None and t.text != name:
            raise HomoError(f"这里应该是「{name}」，但读到了「{t.text}」", t.line)
        self.pos += 1
        return t

    def expect_punc(self, ch: str) -> Token:
        t = self.peek()
        if t is None or t.kind != "punc" or t.text != ch:
            got = t.text if t else "文件结尾"
            raise HomoError(f"这里应该是「{ch}」，但读到了「{got}」", t.line if t else self.last_line())
        self.pos += 1
        return t

    # -- 顶层 ----------------------------------------------------------
    def parse_program(self) -> Program:
        funcs: list[Func] = []
        classes: list[Class] = []
        globals_: list[Decl] = []
        imports: list[ImportDecl] = []
        declarations: list[FuncDecl] = []
        global_decls: list[Decl] = []
        class_decls: list[ClassDecl] = []
        while not self.at_end():
            if self.at_id("inm"):
                imports.append(self.parse_inm())
                continue
            if self.at_id("import"):
                t = self.peek()
                raise HomoError("import 已改名为 inm；现在写成 inm 模块名（引用 .homo 文件）",
                                t.line if t else 1)
            if self.at_punc(";"):
                self.consume()
                continue
            if self.at_id(CLASS_KW):
                if self.declaration_mode:
                    class_decls.append(self.parse_class_declaration())
                else:
                    classes.append(self.parse_class())
                continue
            if self.at_id(DECL):
                if self._looks_like_function_decl():
                    if not self.declaration_mode:
                        t = self.peek()
                        raise HomoError(
                            "函数声明只能写在 .inm 文件里；.homo 里请写完整实现",
                            t.line if t else 1,
                        )
                    declarations.append(self.parse_declared_function())
                elif self.declaration_mode:
                    global_decls.append(self.parse_global_declaration())
                else:
                    globals_.append(self.parse_decl())
                continue
            t = self.peek()
            nxt = self.peek(1)
            nxt2 = self.peek(2)
            if (t is not None and t.kind == "id"
                    and nxt is not None and nxt.kind == "punc" and nxt.text == ":"
                    and nxt2 is not None and nxt2.kind == "punc" and nxt2.text == ":"):
                raise HomoError(
                    "类外实现「类名::成员」已删除；请写完整的 类 类名 { ... } 定义",
                    t.line,
                )
            if t is not None and t.kind == "id" and nxt is not None and nxt.kind == "punc" and nxt.text == "(":
                if self.declaration_mode:
                    if self._old_function_declaration_ahead():
                        raise HomoError(
                            "旧的函数声明写法已删除；请在 .inm 里写"
                            "「你是一个一个一个 函数名（参数）哼哼哼啊啊啊啊啊」",
                            t.line,
                        )
                    raise HomoError(
                        ".inm 里只能写声明，函数实现请写到 .homo 文件里",
                        t.line,
                    )
                funcs.append(self.parse_top_function())
            else:
                raise HomoError(
                    "顶层只能写 import、变量声明、类声明/定义、函数声明/定义",
                    t.line if t else 1,
                )
        return Program(functions=funcs, classes=classes, globals=globals_,
                       imports=imports, declarations=declarations,
                       global_decls=global_decls, class_decls=class_decls)

    def parse_global_declaration(self) -> Decl:
        """解析 .inm 全局变量声明：你是一个一个一个 名字 哼哼哼啊啊啊啊啊"""
        head = self.expect_id(DECL)
        name = self.expect_id()
        check_variable_name(name.text, name.line)
        if self.at_punc("["):
            raise HomoError(
                f".inm 里的变量「{name.text}」只写名字；数组长度和初值请在 .homo 里定义",
                name.line,
            )
        if self.at_id(ASSIGN):
            raise HomoError(
                f".inm 里的变量「{name.text}」不能给初值；请在 .homo 里定义",
                name.line,
            )
        if not self.at_id(DECL_END):
            raise HomoError(
                f"变量声明「{name.text}」末尾要写「{DECL_END}」",
                name.line,
            )
        self.consume()
        if self.at_punc(";"):
            self.consume()
        return Decl(name.text, None, head.line)

    def parse_class_declaration(self) -> ClassDecl:
        """解析 .inm 类声明：类 名字 哼哼哼啊啊啊啊啊 或 类 名字 { 声明... }"""
        head = self.expect_id(CLASS_KW)
        name = self.expect_id()
        if self.at_id(DECL_END):
            self.consume()
            return ClassDecl(name.text, [], [], head.line)
        self.expect_punc("{")
        fields: list[Decl] = []
        methods: list[FuncDecl] = []
        private = False
        while not self.at_punc("}"):
            if self.at_end():
                raise HomoError("类声明没有闭合（缺少 }）", self.last_line())
            if self.at_punc(";"):
                self.consume()
                continue
            if self.at_id(PUBLIC_KW):
                self.consume()
                private = False
                continue
            if self.at_id(PRIVATE_KW):
                self.consume()
                private = True
                continue
            if self.at_id(DECL):
                if self._looks_like_function_decl():
                    method = self.parse_declared_function()
                    method.private = private
                    methods.append(method)
                else:
                    fld = self.parse_global_declaration()
                    fld.private = private
                    fields.append(fld)
                continue
            t = self.peek()
            nxt = self.peek(1)
            if t is not None and t.kind == "id" and nxt is not None and nxt.kind == "punc" and nxt.text == "(":
                raise HomoError(
                    "类声明里的方法要写成"
                    "「你是一个一个一个 方法（参数）哼哼哼啊啊啊啊啊」",
                    t.line,
                )
            raise HomoError("类声明里只能写字段声明或方法声明",
                            t.line if t else self.last_line())
        self.expect_punc("}")
        return ClassDecl(name.text, fields, methods, head.line)

    def parse_inm(self) -> ImportDecl:
        """inm 模块名 / inm 类名 from 类定义 / inm "路径.homo"。"""
        head = self.expect_id("inm")
        first = self.peek()
        if first is None:
            raise HomoError("inm 后面要写要引用的 .homo 文件", head.line)
        second = self.peek(1)
        if (first.kind == "id" and second is not None
                and second.kind == "id" and second.text == "from"):
            class_name = self.consume().text
            self.consume()  # from
            target = self.parse_inm_target()
            if self.at_punc(";"):
                self.consume()
            return ImportDecl(target, head.line, class_name)
        target = self.parse_inm_target(first)
        if self.at_punc(";"):
            self.consume()
        return ImportDecl(target, head.line)

    def parse_inm_target(self, first: Optional[Token] = None) -> str:
        target = ""
        if first is None:
            first = self.peek()
        if first is None:
            raise HomoError("inm 后面要写要引用的 .homo 文件", self.last_line())
        if first.kind == "str":
            self.consume()
            target = string_inner(first.text).strip()
        elif first.kind == "id" or (first.kind == "punc" and first.text in (".", "/")):
            line = first.line
            parts: list[str] = []
            expect_component = True
            while True:
                cur = self.peek()
                if cur is None or cur.line != line:
                    break
                if expect_component:
                    if cur.kind == "id":
                        parts.append(cur.text)
                        self.consume()
                        expect_component = False
                    elif cur.kind == "punc" and cur.text in (".", "/"):
                        parts.append(cur.text)
                        self.consume()
                    else:
                        break
                else:
                    if cur.kind == "punc" and cur.text in (".", "/"):
                        parts.append(cur.text)
                        self.consume()
                        expect_component = True
                    else:
                        break
            target = "".join(parts)
        else:
            raise HomoError("inm 后面要写 .homo 文件名或只导入类时的类名", first.line)
        if not target:
            raise HomoError("inm 的文件名不能为空", first.line)
        return target

    def parse_class(self) -> Class:
        head = self.expect_id(CLASS_KW)
        name = self.expect_id()
        self.expect_punc("{")
        fields: list[Decl] = []
        methods: list[Func] = []
        private = False
        while not self.at_punc("}"):
            if self.at_end():
                raise HomoError("类定义没有闭合（缺少 }）", self.last_line())
            if self.at_punc(";"):
                self.consume()
                continue
            if self.at_id(PUBLIC_KW):
                self.consume()
                private = False
                continue
            if self.at_id(PRIVATE_KW):
                self.consume()
                private = True
                continue
            if self.at_id(DECL):
                fld = self.parse_decl()
                fld.private = private
                fields.append(fld)
                continue
            t = self.peek()
            nxt = self.peek(1)
            if t is not None and t.kind == "id" and nxt is not None and nxt.kind == "punc" and nxt.text == "(":
                method = self.parse_function()
                method.private = private
                methods.append(method)
                continue
            raise HomoError("类体里只能写访问标记、字段声明（你是一个一个一个）或方法",
                            t.line if t else self.last_line())
        self.expect_punc("}")
        if not methods:
            raise HomoError(f"类「{name.text}」至少要有一个方法", name.line)
        return Class(name.text, fields, methods, head.line)

    def parse_params(self) -> list[Param]:
        self.expect_punc("(")
        params: list[Param] = []
        seen_default = False
        if not self.at_punc(")"):
            while True:
                nxt = self.peek(1)
                is_variadic = (
                    self.at_id(VARIADIC_KW)
                    and nxt is not None
                    and nxt.kind == "id"
                    and nxt.text != ASSIGN
                )
                if is_variadic:
                    marker = self.consume()
                    name = self.expect_id()
                    check_variable_name(name.text, name.line)
                    params.append(Param(name.text, None, True, marker.line))
                    if self.at_punc(","):
                        raise HomoError("可变参数只能用一次，而且要写在参数列表最后", marker.line)
                    break
                name = self.expect_id()
                check_variable_name(name.text, name.line)
                default = None
                if self.at_id(ASSIGN):
                    self.consume()
                    default = self.parse_expr()
                    seen_default = True
                elif seen_default:
                    raise HomoError(
                        f"带默认值的参数后面不能再有必填参数「{name.text}」", name.line
                    )
                params.append(Param(name.text, default, False, name.line))
                if self.at_punc(","):
                    self.consume()
                    continue
                break
        self.expect_punc(")")
        return params

    def _old_function_declaration_ahead(self) -> bool:
        """检测 .inm 里是否用了已删除的 name(参数); 写法。"""
        head = self.peek()
        if head is None or head.kind != "id":
            return False
        if self.peek(1) is None or self.peek(1).kind != "punc" or self.peek(1).text != "(":
            return False
        depth = 0
        i = self.pos + 1
        while i < len(self.tokens):
            tok = self.tokens[i]
            if tok.kind == "punc" and tok.text == "(":
                depth += 1
            elif tok.kind == "punc" and tok.text == ")":
                depth -= 1
                if depth == 0:
                    nxt = self.tokens[i + 1] if i + 1 < len(self.tokens) else None
                    return nxt is not None and nxt.kind == "punc" and nxt.text == ";"
            i += 1
        return False

    def _looks_like_function_decl(self) -> bool:
        """判断「你是一个一个一个 名字(...)」是函数声明还是变量声明。"""
        if not self.at_id(DECL):
            return False
        name = self.peek(1)
        paren = self.peek(2)
        return (name is not None and name.kind == "id"
                and paren is not None and paren.kind == "punc"
                and paren.text == "(")

    def parse_declared_function(self) -> FuncDecl:
        """解析：你是一个一个一个 函数名（参数）哼哼哼啊啊啊啊啊"""
        self.expect_id(DECL)
        name_tok = self.expect_id()
        params = self.parse_params()
        if self.at_punc("{"):
            raise HomoError(
                f"带函数体的定义请写成「{name_tok.text}(参数){{...}}」，"
                f"不要加「{DECL}」前缀",
                name_tok.line,
            )
        if not self.at_id(DECL_END):
            raise HomoError(
                f"函数声明「{name_tok.text}」末尾要写「{DECL_END}」",
                name_tok.line,
            )
        self.consume()
        if self.at_punc(";"):
            self.consume()
        return FuncDecl(name_tok.text, params, name_tok.line)

    def parse_top_function(self) -> Func:
        """顶层函数定义：name(参数){...}。旧的 name(参数); 声明写法已删除。"""
        name_tok = self.expect_id()
        params = self.parse_params()
        if self.at_punc(";"):
            raise HomoError(
                "旧的函数声明写法已删除；请在 .inm 里写"
                "「你是一个一个一个 函数名（参数）哼哼哼啊啊啊啊啊」",
                name_tok.line,
            )
        body = self.parse_block()
        return Func(name_tok.text, params, body, name_tok.line)

    def parse_function(self) -> Func:
        name_tok = self.expect_id()
        params = self.parse_params()
        body = self.parse_block()
        return Func(name_tok.text, params, body, name_tok.line)

    def parse_block(self) -> list[Stmt]:
        self.expect_punc("{")
        stmts: list[Stmt] = []
        while not self.at_punc("}"):
            if self.at_end():
                raise HomoError("代码块没有闭合（缺少 }）", self.last_line())
            stmts.append(self.parse_stmt())
            if self.at_punc(";"):
                self.consume()
        self.expect_punc("}")
        return stmts

    # -- 语句 ----------------------------------------------------------
    def parse_stmt(self) -> Stmt:
        t = self.peek()
        if t is None:
            raise HomoError("意外的文件结尾", self.last_line())
        if t.kind == "id":
            if t.text in (PUBLIC_KW, PRIVATE_KW):
                raise HomoError(f"「{t.text}」只能写在类体里", t.line)
            if t.text == DECL:
                return self.parse_decl()
            if t.text == IF_HEAD:
                return self.parse_if()
            if t.text == WHILE_HEAD:
                return self.parse_while()
            if t.text == SWITCH_HEAD:
                return self.parse_switch()
            if t.text == DO_HEAD:
                return self.parse_dowhile()
            if t.text == BREAK_KW:
                self.consume()
                return Break(t.line)
            if t.text == CONTINUE_KW:
                self.consume()
                return Continue(t.line)
            nxt = self.peek(1)
            if nxt is not None and nxt.text in ASSIGN_OPS:
                return self.parse_assign()
            if nxt is not None and nxt.kind == "punc" and nxt.text in ("[", "."):
                return self.parse_assign()
            expr = self.parse_expr()
            if not self.at_id(CALL_END):
                raise HomoError(f"调用语句结尾缺少「{CALL_END}」", expr.line)
            self.consume()
            if not isinstance(expr, (Call, MethodCall)):
                raise HomoError("这里只能写函数或方法调用语句", expr.line)
            return CallStmt(expr, expr.line)
        if t.kind == "punc" and t.text == "(":
            return self.parse_paren_stmt()
        # 赋值目标可以不是普通变量：字符串/数组字面量取下标、新建对象的字段等。
        if t.kind == "str" or (t.kind == "id" and t.text == NEW_KW) or (
                t.kind == "punc" and t.text == "["):
            return self.parse_assign()
        raise HomoError(f"无法识别的语句「{t.text}」", t.line)

    def parse_paren_stmt(self) -> Stmt:
        lp = self.consume()
        expr = self.parse_expr()
        self.expect_punc(")")
        if self.at_id(REPEAT_MID):
            self.consume()
            self.expect_punc("(")
            end = self.parse_expr()
            self.expect_punc(")")
            self.expect_id(REPEAT_TAIL)
            body = self.parse_block()
            return Repeat(expr, end, body, lp.line)
        if self.at_id(RETURN_KW):
            self.consume()
            return Return(expr, lp.line)
        raise HomoError(f"括号语句后面应该是「{REPEAT_MID}」或「{RETURN_KW}」", self.last_line())

    def parse_decl(self) -> Decl:
        head = self.consume()
        name = self.expect_id()
        if name.text == REPEAT_VAR:
            raise HomoError(f"「{REPEAT_VAR}」是循环里的迭代变量，不能用作变量名", name.line)
        check_variable_name(name.text, name.line)
        sizes: list[Expr] = []
        while self.at_punc("["):
            self.consume()
            sizes.append(self.parse_expr())
            self.expect_punc("]")
        if sizes:
            if self.at_id(ASSIGN):
                self.consume()
                init = self.parse_expr()
                self.expect_id(STMT_END)
                return Decl(name.text, init, head.line, sizes=sizes)
            self.expect_id(DECL_END)
            return Decl(name.text, None, head.line, sizes=sizes)
        if self.at_id(DECL_END):
            self.consume()
            return Decl(name.text, None, head.line)
        if self.at_id(ASSIGN):
            self.consume()
            init = self.parse_expr()
            self.expect_id(STMT_END)
            return Decl(name.text, init, head.line)
        raise HomoError(f"变量声明「{name.text}」后面应该是「{DECL_END}」、「{ASSIGN}」或「[长度]」", self.last_line())

    def parse_assign(self) -> Assign:
        target = self.parse_expr()
        if isinstance(target, Ident) and target.name == REPEAT_VAR:
            raise HomoError(f"「{REPEAT_VAR}」是循环里的迭代变量，不能赋值", target.line)
        # 「对象.方法()噗叽啪」也走这里，遇到调用语句结尾就转成调用语句。
        if self.at_id(CALL_END) and isinstance(target, (Call, MethodCall)):
            self.consume()
            return CallStmt(target, target.line)
        tok = self.peek()
        if tok is None or tok.text not in ASSIGN_OPS:
            got = "结尾" if tok is None else f"「{tok.text}」"
            raise HomoError(f"赋值语句需要一个赋值算子，但读到了{got}", target.line)
        self.consume()
        op = tok.text
        expr = self.parse_expr()
        self.expect_id(STMT_END)
        name = ""
        index = None
        if isinstance(target, Ident):
            name = target.name
        elif isinstance(target, Index) and isinstance(target.obj, Ident):
            name = target.obj.name
            index = target.index
        if name:
            check_variable_name(name, target.line)
        return Assign(name, op, expr, target.line, index, target)

    def parse_if(self) -> If:
        head = self.expect_id(IF_HEAD)
        self.expect_punc("(")
        cond = self.parse_expr()
        self.expect_punc(")")
        self.expect_id(IF_MID)
        then = self.parse_block()
        elifs: list[tuple[Expr, list[Stmt]]] = []
        els: Optional[list[Stmt]] = None
        while True:
            if self.at_id(ELSE):
                self.consume()
                els = self.parse_block()
                break
            nxt = self.peek(1)
            if self.at_id(ELSEIF_HEAD) and nxt is not None and nxt.kind == "id" and nxt.text == "极霸矛":
                self.consume()
                self.consume()
                self.expect_id(ELSEIF_TAIL)
                els = self.parse_block()
                break
            if self.at_id(ELSEIF_HEAD):
                self.consume()
                self.expect_punc("(")
                c = self.parse_expr()
                self.expect_punc(")")
                self.expect_id(ELSEIF_TAIL)
                elifs.append((c, self.parse_block()))
                continue
            break
        return If(cond, then, elifs, els, head.line)

    def parse_while(self) -> While:
        head = self.expect_id(WHILE_HEAD)
        self.expect_punc("(")
        cond = self.parse_expr()
        self.expect_punc(")")
        body = self.parse_block()
        return While(cond, body, head.line)

    def parse_dowhile(self) -> DoWhile:
        head = self.expect_id(DO_HEAD)
        body = self.parse_block()
        self.expect_id(DO_TAIL)
        self.expect_punc("(")
        cond = self.parse_expr()
        self.expect_punc(")")
        return DoWhile(body, cond, head.line)

    def parse_switch(self) -> Switch:
        head = self.expect_id(SWITCH_HEAD)
        self.expect_punc("(")
        expr = self.parse_expr()
        self.expect_punc(")")
        self.expect_punc("{")
        cases: list[tuple[Optional[Expr], list[Stmt]]] = []
        current: Optional[tuple[Optional[Expr], list[Stmt]]] = None
        while not self.at_punc("}"):
            if self.at_end():
                raise HomoError("多分支没有闭合（缺少 }）", self.last_line())
            if self.at_id(CASE_KW):
                self.consume()
                self.expect_punc("(")
                ce = self.parse_expr()
                self.expect_punc(")")
                current = (ce, [])
                cases.append(current)
            elif self.at_id(DEFAULT_KW):
                self.consume()
                current = (None, [])
                cases.append(current)
            else:
                if current is None:
                    raise HomoError("多分支里的第一条语句必须是「食欲(值)」或「性欲睡眠欲」", self.last_line())
                current[1].append(self.parse_stmt())
                if self.at_punc(";"):
                    self.consume()
        self.expect_punc("}")
        return Switch(expr, cases, head.line)

    # -- 表达式 --------------------------------------------------------
    def parse_expr(self) -> Expr:
        return self.parse_or()

    def parse_or(self) -> Expr:
        left = self.parse_and()
        while self.at_id(OR_KW):
            t = self.consume()
            left = Or(left, self.parse_and(), t.line)
        return left

    def parse_and(self) -> Expr:
        left = self.parse_cmp()
        while self.at_id(AND_KW):
            t = self.consume()
            left = And(left, self.parse_cmp(), t.line)
        return left

    def parse_cmp(self) -> Expr:
        left = self.parse_add()
        while True:
            if self.at_id(LT_KW):
                t = self.consume()
                left = Lt(left, self.parse_add(), t.line)
            else:
                break
        return left

    def parse_add(self) -> Expr:
        left = self.parse_mul()
        while self.at_punc("+") or self.at_punc("-"):
            t = self.consume()
            left = Bin(t.text, left, self.parse_mul(), t.line)
        return left

    def parse_mul(self) -> Expr:
        left = self.parse_unary()
        while self.at_punc("*") or self.at_punc("/") or self.at_id(MOD_KW):
            if self.at_id(MOD_KW):
                t = self.consume()
                op = "%"
            else:
                t = self.consume()
                op = t.text
            left = Bin(op, left, self.parse_unary(), t.line)
        return left

    def parse_unary(self) -> Expr:
        t = self.peek()
        if t is None:
            raise HomoError("表达式不完整", self.last_line())
        if t.kind == "punc" and t.text == "-":
            self.consume()
            return Bin("-", Num("0", t.line), self.parse_unary(), t.line)
        if t.kind == "id" and t.text == NOT_KW:
            self.consume()
            return Not(self.parse_unary(), t.line)
        if t.kind == "id" and t.text.startswith(NOT_KW) and len(t.text) > len(NOT_KW):
            # 形如「迫真i」连写
            self.consume()
            return Not(Ident(t.text[len(NOT_KW):], t.line), t.line)
        return self.parse_primary()

    def parse_paren_args(self) -> list[Expr]:
        self.expect_punc("(")
        args: list[Expr] = []
        if not self.at_punc(")"):
            while True:
                args.append(self.parse_expr())
                if self.at_punc(","):
                    self.consume()
                    continue
                break
        self.expect_punc(")")
        return args

    def parse_postfix(self, node: Expr) -> Expr:
        while True:
            if self.at_punc("["):
                self.consume()
                idx = self.parse_expr()
                self.expect_punc("]")
                node = Index(node, idx, node.line)
                continue
            if self.at_punc("."):
                self.consume()
                name = self.expect_id()
                if self.at_punc("("):
                    node = MethodCall(node, name.text, self.parse_paren_args(), name.line)
                else:
                    node = Field(node, name.text, name.line)
                continue
            break
        return node

    def parse_primary(self) -> Expr:
        t = self.peek()
        if t is None:
            raise HomoError("表达式不完整", self.last_line())
        if t.kind == "num":
            self.consume()
            node: Expr = Num(t.text, t.line)
        elif t.kind == "str":
            self.consume()
            node = Str(t.text, t.line)
        elif t.kind == "id":
            if t.text == REPEAT_VAR:
                self.consume()
                return LoopVar(t.line)
            if t.text == TRUE_KW:
                self.consume()
                return BoolLit(True, t.line)
            if t.text == FALSE_KW:
                self.consume()
                return BoolLit(False, t.line)
            if t.text == NULL_KW:
                self.consume()
                return NullLit(t.line)
            if t.text == NEW_KW:
                self.consume()
                cls = self.expect_id()
                node = New(cls.text, self.parse_paren_args(), t.line)
            else:
                self.consume()
                if self.at_punc("("):
                    node = Call(t.text, self.parse_paren_args(), t.line)
                else:
                    node = Ident(t.text, t.line)
        elif t.kind == "punc" and t.text == "[":
            self.consume()
            items: list[Expr] = []
            if not self.at_punc("]"):
                while True:
                    items.append(self.parse_expr())
                    if self.at_punc(","):
                        self.consume()
                        continue
                    break
            self.expect_punc("]")
            node = ArrayLit(items, t.line)
        elif t.kind == "punc" and t.text == "(":
            self.consume()
            node = self.parse_expr()
            self.expect_punc(")")
            # 相等比较写成 (a)先辈(b)说；两个操作数各自带括号，不会被中缀优先级影响。
            if self.at_id(EQ_HEAD):
                op = self.consume()
                if not self.at_punc("("):
                    raise HomoError("等值比较要写成「(a)先辈(b)说」", op.line)
                self.consume()
                right = self.parse_expr()
                self.expect_punc(")")
                tail = self.peek()
                if tail is None or tail.kind != "id" or tail.text != EQ_TAIL:
                    raise HomoError("等值比较要写成「(a)先辈(b)说」", op.line)
                self.consume()
                node = Eq(node, right, op.line)
        else:
            raise HomoError(f"无法识别的表达式「{t.text}」", t.line)
        return self.parse_postfix(node)


# --------------------------------------------------------------------------
# 类型推导（结果随 transpile/build 返回给调用方）
# --------------------------------------------------------------------------
def string_inner(raw: str) -> str:
    """去掉字符串字面量两端的引号（引号可以是半角或全角，且两端不必同种）。"""
    return raw[1:-1] if len(raw) >= 2 else ""


def cpp_quote(text: str) -> str:
    """把文本包成 C++ 的 "..." 字面量；内容里未被转义的 " 需要再转义一层。

    全角引号本身在 C++ 字符串里没有特殊含义，所以 \\“ 这类转义只用于免被 homo
    词法当成结束引号，输出时去掉反斜杠即可。
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt in QUOTE_CHARS and nxt != '"':
                out.append(nxt)
            else:
                out.append(text[i:i + 2])
            i += 2
            continue
        if ch == '"':
            out.append('\\"')
        else:
            out.append(ch)
        i += 1
    return '"' + "".join(out) + '"'


def split_template(raw: str, line: int) -> list[tuple[str, object]]:
    """把 脱出("文本{表达式}文本") 的字符串拆成文本段与表达式段。

    返回 [("text", 原文) | ("expr", Expr)]。用 \\{ 和 \\} 表示字面大括号。
    """
    inner = string_inner(raw)
    parts: list[tuple[str, object]] = []
    buf: list[str] = []
    i = 0
    while i < len(inner):
        c = inner[i]
        if c == "\\" and i + 1 < len(inner):
            nxt = inner[i + 1]
            buf.append(nxt if nxt in "{}" else c + nxt)
            i += 2
            continue
        if c == "{":
            j = inner.find("}", i + 1)
            if j < 0:
                raise HomoError("模板里的「{」没有闭合", line)
            if buf:
                parts.append(("text", "".join(buf)))
                buf = []
            tokens = tokenize(inner[i + 1:j])
            sub = Parser(tokens)
            if not tokens:
                raise HomoError("模板里的「{}」不能为空", line)
            try:
                expr = sub.parse_expr()
            except HomoError as exc:
                exc.line = line
                raise
            if not sub.at_end():
                raise HomoError("模板里的表达式无法解析", line)
            # 模板表达式是在子串上重新分词的，行号从 1 开始；
            # 统一改回整条语句所在的行，运行期报错才能指对位置。
            _shift_ast_lines(expr, line - 1)
            parts.append(("expr", expr))
            i = j + 1
            continue
        buf.append(c)
        i += 1
    if buf:
        parts.append(("text", "".join(buf)))
    return parts


TYPE_LABELS = {"int": "整数", "double": "浮点数", "string": "字符串", "array": "数组", "object": "对象", "null": "空值"}


def analyze_types(program: Program) -> list[dict]:
    kinds: dict[str, list[str]] = {}
    order: list[str] = []
    lines: dict[str, int] = {}

    def note(name: str, kind: Optional[str], line: int) -> None:
        if name not in kinds:
            kinds[name] = []
            order.append(name)
            lines[name] = line
        if kind and kind not in kinds[name]:
            kinds[name].append(kind)

    def infer(e: Optional[Expr]) -> Optional[str]:
        if e is None:
            return None
        if isinstance(e, Num):
            return "double" if "." in e.text else "int"
        if isinstance(e, Str):
            return "string"
        if isinstance(e, BoolLit):
            return "bool"
        if isinstance(e, NullLit):
            return "null"
        if isinstance(e, New):
            return "object"
        if isinstance(e, ArrayLit):
            return "array"
        if isinstance(e, LoopVar):
            return "int"
        if isinstance(e, Ident):
            lst = kinds.get(e.name)
            return lst[-1] if lst else None
        if isinstance(e, (Not, Lt, Eq, And, Or)):
            return "int"
        if isinstance(e, Call):
            if e.name in ("取子串", "替换", "去空白", "转大写", "转小写", "连接", "读行"):
                return "string"
            if e.name in ("查找", "包含", LENGTH_KW, RANDOM_KW, "读入"):
                return "int"
            if e.name == "分割":
                return "array"
            return None
        if isinstance(e, Bin):
            left, right = infer(e.left), infer(e.right)
            if "double" in (left, right):
                return "double"
            if e.op == "+" and "string" in (left, right):
                return "string"
            return "int"
        return None

    def walk(stmts: list[Stmt]) -> None:
        for s in stmts:
            if isinstance(s, Decl):
                if s.sizes:
                    note(s.name, "array", s.line)
                else:
                    note(s.name, infer(s.init), s.line)
            elif isinstance(s, Assign):
                if not s.name:
                    continue
                if s.index is not None:
                    if s.name not in kinds:
                        note(s.name, "array", s.line)
                    continue
                if s.name not in kinds:
                    note(s.name, None, s.line)
                if s.op == ASSIGN:
                    note(s.name, infer(s.expr), s.line)
                else:
                    k = infer(s.expr)
                    if k == "double" or "double" in kinds[s.name]:
                        note(s.name, "double", s.line)
                    elif k is not None:
                        note(s.name, "int", s.line)
            elif isinstance(s, If):
                walk(s.then)
                for _, body in s.elifs:
                    walk(body)
                if s.els is not None:
                    walk(s.els)
            elif isinstance(s, (While, Repeat, DoWhile)):
                walk(s.body)
            elif isinstance(s, Switch):
                for _, body in s.cases:
                    walk(body)

    walk(program.globals)
    for f in program.functions:
        walk(f.body)
    for c in program.classes:
        for fld in c.fields:
            if fld.sizes:
                note(fld.name, "array", fld.line)
            else:
                note(fld.name, infer(fld.init), fld.line)
        for m in c.methods:
            walk(m.body)

    result = []
    for name in order:
        ks = kinds[name]
        result.append({
            "name": name,
            "line": lines[name],
            "kinds": [TYPE_LABELS.get(k, k) for k in ks],
            "dynamic": len(ks) > 1,
        })
    return result


# --------------------------------------------------------------------------
# 静态名字检查（供编辑器实时提示，也避免把未定义名字拖到 C++ 编译阶段）
# --------------------------------------------------------------------------
BUILTIN_CALLS = {"脱出", "读入", "读行", RANDOM_KW, LENGTH_KW} | set(STRING_BUILTIN_ARITY)

# 语法关键字在解析表达式/语句时有固定含义，再拿来当变量名会让同一行代码
# 出现两种解释（比如「类 事 …」看起来像类定义）。变量、参数、字段都查。
# 内置函数名（长度、随机、脱出等）不在其列：它们可以像普通名字一样被遮蔽。
RESERVED_VAR_NAMES = {
    DECL, DECL_END, ASSIGN, STMT_END, CALL_END,
    IF_HEAD, IF_MID, ELSEIF_HEAD, ELSEIF_TAIL, ELSE, "极霸矛",
    WHILE_HEAD, TRUE_KW, FALSE_KW, NULL_KW, NOT_KW, LT_KW, AND_KW, EQ_HEAD, OR_KW,
    MOD_KW, CONTINUE_KW, SWITCH_HEAD, CASE_KW, DEFAULT_KW, DO_HEAD, DO_TAIL,
    REPEAT_MID, REPEAT_TAIL, REPEAT_VAR, BREAK_KW, RETURN_KW, CLASS_KW, NEW_KW,
    VARIADIC_KW, PUBLIC_KW, PRIVATE_KW, "import",
} | set(COMPOUND_OPS)


def check_variable_name(name: str, line: int) -> None:
    if name in RESERVED_VAR_NAMES:
        raise HomoError(f"「{name}」是关键字，不能用作变量名", line)


def param_arity(params: list[Param]) -> tuple[int, Optional[int]]:
    """返回 (必填个数, 最多个数)；可变参数没有上限，最多个数为 None。"""
    required = 0
    for p in params:
        if p.variadic:
            return required, None
        if p.default is None:
            required += 1
    return required, len(params)


def function_signature(params: list[Param]) -> tuple[tuple[int, Optional[int]], Optional[int]]:
    """声明/定义匹配用的简化签名：(必填/最大参数个数, 可变参数位置)。"""
    variadic = next((i for i, p in enumerate(params) if p.variadic), None)
    return param_arity(params), variadic


def arity_message(kind: str, name: str, params: list[Param], given: int) -> str:
    required, maximum = param_arity(params)
    if maximum is None or given < required:
        if given == 0:
            return f"{kind}「{name}」至少需要 {required} 个参数，但一个都没给"
        return f"{kind}「{name}」至少需要 {required} 个参数，但只给了 {given} 个"
    if required == maximum:
        return f"{kind}「{name}」需要 {required} 个参数，但给了 {given} 个"
    return f"{kind}「{name}」最多需要 {maximum} 个参数，但给了 {given} 个"


def check_string_builtin_arity(name: str, given: int, line: int) -> None:
    required, maximum = STRING_BUILTIN_ARITY[name]
    if required == maximum:
        if given != required:
            raise HomoError(f"{name} 需要 {required} 个参数，但给了 {given} 个", line)
    elif given < required or given > maximum:
        raise HomoError(f"{name} 需要 {required} 到 {maximum} 个参数，但给了 {given} 个", line)


class Scopes:
    """块级作用域栈：每进入一对花括号压一层，离开时弹掉。"""

    def __init__(self, *frames: set[str]):
        self.frames = [set(f) for f in frames]

    def push(self) -> None:
        self.frames.append(set())

    def pop(self) -> None:
        self.frames.pop()

    def declare(self, name: str) -> None:
        self.frames[-1].add(name)

    def in_current(self, name: str) -> bool:
        return name in self.frames[-1]

    def __contains__(self, name: str) -> bool:
        return any(name in frame for frame in self.frames)


def reject_duplicate_declarations(program: Program) -> None:
    """.inm 声明检查：同一个 .inm 只加载一次；不同 .inm 的重复声明报错。"""
    seen_globals: set[str] = set()
    for d in program.global_decls:
        if d.name in seen_globals:
            raise HomoError(f"变量「{d.name}」重复声明", d.line)
        seen_globals.add(d.name)

    seen_funcs: set[tuple[str, tuple]] = set()
    for d in program.declarations:
        key = (d.name, function_signature(d.params))
        if key in seen_funcs:
            raise HomoError(f"函数「{d.name}」重复声明", d.line)
        seen_funcs.add(key)

    seen_classes: set[str] = set()
    for cd in program.class_decls:
        if cd.name in seen_classes:
            raise HomoError(f"类「{cd.name}」重复声明", cd.line)
        seen_classes.add(cd.name)


def check_names(program: Program) -> None:
    reject_duplicate_declarations(program)
    seen_functions: set[str] = set()
    for f in program.functions:
        if f.name in seen_functions:
            raise HomoError(f"函数「{f.name}」重复定义", f.line)
        seen_functions.add(f.name)
    seen_classes: set[str] = set()
    for c in program.classes:
        if c.name in seen_classes:
            raise HomoError(f"类「{c.name}」重复定义", c.line)
        seen_classes.add(c.name)
    functions_by_name = {f.name: f for f in program.functions}
    declarations_by_name: dict[str, list[FuncDecl]] = {}
    for d in program.declarations:
        if d.name in BUILTIN_CALLS:
            raise HomoError(f"「{d.name}」是内置函数名，不能用作函数名", d.line)
        declarations_by_name.setdefault(d.name, []).append(d)
    for name, decls in declarations_by_name.items():
        signature = function_signature(decls[0].params)
        for d in decls[1:]:
            if function_signature(d.params) != signature:
                raise HomoError(f"函数「{name}」的声明互相冲突", d.line)
        definition = functions_by_name.get(name)
        if definition is None:
            raise HomoError(
                f"函数「{name}」只有声明，没有定义；请在 .homo 中给出实现，或链接实现它的 .homo 文件",
                decls[0].line,
            )
        if function_signature(definition.params) != signature:
            raise HomoError(f"函数「{name}」的声明和定义参数不匹配", decls[0].line)
    function_names = set(functions_by_name) | set(declarations_by_name)
    class_names = {c.name for c in program.classes}
    classes_by_name = {c.name: c for c in program.classes}

    # .inm 类声明必须能在 .homo 里找到完整类定义，且声明的成员要存在。
    class_decls_by_name: dict[str, list[ClassDecl]] = {}
    for cd in program.class_decls:
        class_decls_by_name.setdefault(cd.name, []).append(cd)
    for name, decls in class_decls_by_name.items():
        definition = classes_by_name.get(name)
        if definition is None:
            raise HomoError(
                f"类「{name}」只有声明，没有定义；请在 .homo 中给出实现",
                decls[0].line,
            )
        fields_by_name = {fld.name: fld for fld in definition.fields}
        methods_by_signature = {(m.name, function_signature(m.params)): m
                                for m in definition.methods}
        for cd in decls:
            for fld in cd.fields:
                target = fields_by_name.get(fld.name)
                if target is None:
                    raise HomoError(
                        f"类「{name}」声明了字段「{fld.name}」，但 .homo 的类实现里没有",
                        fld.line,
                    )
                # .inm 声明的可见性优先于 .homo 里的写法。
                target.private = fld.private
            for m in cd.methods:
                target = methods_by_signature.get((m.name, function_signature(m.params)))
                if target is None:
                    raise HomoError(
                        f"类「{name}」声明了方法「{m.name}」，但 .homo 里没有匹配的实现",
                        m.line,
                    )
                target.private = m.private

    # .inm 全局变量声明必须有 .homo 里的定义。
    global_defs_by_name = {g.name: g for g in program.globals}
    for d in program.global_decls:
        if d.name not in global_defs_by_name:
            raise HomoError(
                f"变量「{d.name}」只有声明，没有定义；请在 .homo 中定义",
                d.line,
            )

    all_global_names = ({g.name for g in program.globals}
                        | {d.name for d in program.global_decls})
    for f in program.functions:
        if f.name in BUILTIN_CALLS:
            raise HomoError(f"「{f.name}」是内置函数名，不能用作函数名", f.line)
    repeat_depth = 0
    loop_depth = 0

    def check_expr(e: Optional[Expr], declared: Scopes, calls: set[str]) -> None:
        if e is None:
            return
        if isinstance(e, Ident):
            if e.name not in declared:
                if e.name in all_global_names:
                    raise HomoError(
                        f"全局变量「{e.name}」还没声明；全局初始值只能引用前面已声明的全局",
                        e.line,
                    )
                raise HomoError(f"未定义的变量「{e.name}」", e.line)
        elif isinstance(e, Call):
            if e.name not in calls:
                raise HomoError(f"未定义的函数「{e.name}」", e.line)
            if e.name == LENGTH_KW:
                if len(e.args) != 1:
                    raise HomoError(f"长度 需要 1 个参数，但给了 {len(e.args)} 个", e.line)
            elif e.name in STRING_BUILTIN_ARITY:
                check_string_builtin_arity(e.name, len(e.args), e.line)
            else:
                f = functions_by_name.get(e.name)
                if f is not None:
                    required, maximum = param_arity(f.params)
                    if len(e.args) < required or (maximum is not None
                                                  and len(e.args) > maximum):
                        raise HomoError(
                            arity_message("函数", e.name, f.params, len(e.args)), e.line
                        )
            for a in e.args:
                check_expr(a, declared, calls)
        elif isinstance(e, MethodCall):
            check_expr(e.obj, declared, calls)
            for a in e.args:
                check_expr(a, declared, calls)
        elif isinstance(e, New):
            if e.class_name not in class_names:
                raise HomoError(f"未定义的类「{e.class_name}」", e.line)
            cls = classes_by_name[e.class_name]
            inits = [m for m in cls.methods if m.name == INIT_KW]
            if not inits:
                if e.args:
                    raise HomoError(
                        f"类「{e.class_name}」没有「{INIT_KW}」，新建时不能带参数", e.line
                    )
            else:
                # 多个「首」按实参个数选择，和运行时分发时的顺序一致。
                matching = [
                    m for m in inits
                    if param_arity(m.params)[0] <= len(e.args)
                    and (param_arity(m.params)[1] is None
                         or len(e.args) <= param_arity(m.params)[1])
                ]
                if not matching:
                    if len(inits) == 1:
                        raise HomoError(
                            arity_message("函数", f"{e.class_name}.{INIT_KW}",
                                          inits[0].params, len(e.args)),
                            e.line,
                        )
                    raise HomoError(
                        f"类「{e.class_name}」的「{INIT_KW}」没有接受 {len(e.args)} 个参数的版本",
                        e.line,
                    )
            for a in e.args:
                check_expr(a, declared, calls)
        elif isinstance(e, Index):
            check_expr(e.obj, declared, calls)
            check_expr(e.index, declared, calls)
        elif isinstance(e, Field):
            check_expr(e.obj, declared, calls)
        elif isinstance(e, ArrayLit):
            for item in e.items:
                check_expr(item, declared, calls)
        elif isinstance(e, LoopVar):
            if repeat_depth == 0:
                raise HomoError(f"「{REPEAT_VAR}」只能在（n）回啊（n）回 循环里使用", e.line)
        elif isinstance(e, Not):
            check_expr(e.expr, declared, calls)
        elif isinstance(e, (Lt, Eq, And, Or)):
            check_expr(e.left, declared, calls)
            check_expr(e.right, declared, calls)
        elif isinstance(e, Bin):
            check_expr(e.left, declared, calls)
            check_expr(e.right, declared, calls)

    def check_stmts(stmts: list[Stmt], scopes: Scopes, calls: set[str]) -> None:
        nonlocal repeat_depth, loop_depth
        for s in stmts:
            if isinstance(s, Decl):
                if scopes.in_current(s.name):
                    raise HomoError(f"变量「{s.name}」在同一作用域里重复声明", s.line)
                for size in s.sizes:
                    check_expr(size, scopes, calls)
                check_expr(s.init, scopes, calls)
                scopes.declare(s.name)
            elif isinstance(s, Assign):
                if not s.name:
                    # 目标是「对象.字段」这类表达式：只检查，不声明新变量。
                    check_expr(s.target, scopes, calls)
                    check_expr(s.expr, scopes, calls)
                    continue
                if s.index is not None:
                    if s.name not in scopes:
                        raise HomoError(f"未定义的变量「{s.name}」", s.line)
                    check_expr(s.index, scopes, calls)
                check_expr(s.expr, scopes, calls)
                # 与代码生成一致：对未声明的名字赋值等价于在当前块声明。
                if s.name not in scopes:
                    scopes.declare(s.name)
            elif isinstance(s, CallStmt):
                check_expr(s.expr, scopes, calls)
            elif isinstance(s, If):
                check_expr(s.cond, scopes, calls)
                scopes.push()
                check_stmts(s.then, scopes, calls)
                scopes.pop()
                for cond, body in s.elifs:
                    check_expr(cond, scopes, calls)
                    scopes.push()
                    check_stmts(body, scopes, calls)
                    scopes.pop()
                if s.els is not None:
                    scopes.push()
                    check_stmts(s.els, scopes, calls)
                    scopes.pop()
            elif isinstance(s, While):
                check_expr(s.cond, scopes, calls)
                loop_depth += 1
                scopes.push()
                check_stmts(s.body, scopes, calls)
                scopes.pop()
                loop_depth -= 1
            elif isinstance(s, Repeat):
                check_expr(s.start, scopes, calls)
                check_expr(s.end, scopes, calls)
                repeat_depth += 1
                loop_depth += 1
                scopes.push()
                check_stmts(s.body, scopes, calls)
                scopes.pop()
                loop_depth -= 1
                repeat_depth -= 1
            elif isinstance(s, DoWhile):
                loop_depth += 1
                scopes.push()
                check_stmts(s.body, scopes, calls)
                scopes.pop()
                loop_depth -= 1
                check_expr(s.cond, scopes, calls)
            elif isinstance(s, Switch):
                check_expr(s.expr, scopes, calls)
                for _, body in s.cases:
                    scopes.push()
                    check_stmts(body, scopes, calls)
                    scopes.pop()
            elif isinstance(s, Continue):
                if loop_depth == 0:
                    raise HomoError(f"「{CONTINUE_KW}」只能在循环里使用", s.line)
            elif isinstance(s, Return):
                check_expr(s.expr, scopes, calls)

    global_calls = function_names | BUILTIN_CALLS
    global_names: set[str] = set()
    for g in program.globals:
        if g.name in global_names:
            raise HomoError(f"全局变量「{g.name}」重复声明", g.line)
        if g.name in function_names:
            raise HomoError(f"全局变量「{g.name}」不能和函数重名", g.line)
        if g.name in class_names:
            raise HomoError(f"全局变量「{g.name}」不能和类重名", g.line)
        for size in g.sizes:
            check_expr(size, Scopes(global_names), global_calls)
        check_expr(g.init, Scopes(global_names), global_calls)
        global_names.add(g.name)
    for f in program.functions:
        for p in f.params:
            if p.default is not None:
                # 默认值在调用点求值，检查时能看到全局变量，不能看到参数和调用方局部变量。
                check_expr(p.default, Scopes(global_names), global_calls)
        check_stmts(f.body, Scopes(global_names, {p.name for p in f.params}), global_calls)
    for c in program.classes:
        field_names = {fld.name for fld in c.fields}
        # 同名方法靠参数个数区分；C++ 无法区分参数个数也相同的两个成员函数。
        signatures: set[tuple[str, int]] = set()
        for m in c.methods:
            sig = (m.name, len(m.params))
            if sig in signatures:
                raise HomoError(
                    f"类「{c.name}」有多个「{m.name}」都接受 {len(m.params)} 个参数",
                    m.line,
                )
            signatures.add(sig)
        for fld in c.fields:
            for size in fld.sizes:
                check_expr(size, Scopes(global_names, field_names), global_calls)
            check_expr(fld.init, Scopes(global_names, field_names), global_calls)
        method_calls = global_calls | {m.name for m in c.methods}
        for m in c.methods:
            for p in m.params:
                if p.default is not None:
                    check_expr(p.default, Scopes(global_names), global_calls)
            check_stmts(m.body, Scopes(global_names, field_names,
                                       {p.name for p in m.params}), method_calls)


# --------------------------------------------------------------------------
# C++ 代码生成
# --------------------------------------------------------------------------
class CEmitter:
    def __init__(self, program: Program, source_map: Optional[SourceMap] = None):
        self.program = program
        self.source_map = source_map
        self.lines: list[tuple[str, int]] = []   # (代码行, homo 全局行号)
        self.indent = 0
        self.loop_counter = 0
        # 块级作用域：名字 -> "scalar" / "array" / "unknown"
        self.scopes: list[dict[str, str]] = []
        self.repeat_stack: list[int] = []
        self.break_targets: list[Optional[str]] = []
        self.functions = {f.name: f for f in program.functions}
        self.classes = {c.name: c for c in program.classes}
        # 全局变量在所有函数、方法和字段初值里都可见，作为最外层作用域。
        self.globals_frame = {
            g.name: ("array" if g.sizes or isinstance(g.init, ArrayLit) else "scalar")
            for g in program.globals
        }

    def param_frame(self, params: list[Param]) -> dict[str, str]:
        return {p.name: ("array" if p.variadic else "unknown") for p in params}

    def emit_call_args(self, args: list[Expr], params: list[Param]) -> str:
        variadic = next((i for i, p in enumerate(params) if p.variadic), None)
        fixed = variadic if variadic is not None else len(params)
        parts: list[str] = []
        for i in range(fixed):
            if i < len(args):
                parts.append(self.expr(args[i]))
            else:
                parts.append(self.expr(params[i].default))
        if variadic is not None:
            rest = ", ".join(self.expr(a) for a in args[fixed:])
            parts.append(f"homo_array_lit({{{rest}}})" if rest else "homo_array_lit({})")
        return ", ".join(parts)

    def add(self, text: str, line: int) -> None:
        self.lines.append(("    " * self.indent + text, line))

    def push_scope(self) -> None:
        self.scopes.append({})

    def pop_scope(self) -> None:
        self.scopes.pop()

    def lookup(self, name: str) -> Optional[str]:
        for frame in reversed(self.scopes):
            if name in frame:
                return frame[name]
        return None

    def declare(self, name: str, kind: str) -> None:
        self.scopes[-1][name] = kind

    def blank(self) -> None:
        self.lines.append(("", 0))

    def emit_location_table(self) -> None:
        """生成运行期位置表：全局行号 -> (文件名, 文件内行号)。

        homo_runtime_error 通过 homo_lookup_location 把生成代码里的全局行号
        换回用户真正看到的文件和行号，多文件时也不会指错文件。
        """
        rows = self.source_map.rows() if self.source_map else []
        self.add("homo_source_location homo_lookup_location(long long line) {", 0)
        self.indent += 1
        if rows:
            self.add("static const struct _homo_row { long long start; long long end; "
                     "const char* file; } _homo_rows[] = {", 0)
            self.indent += 1
            for start, end, display in rows:
                self.add("{%d, %d, %s}," % (start, end, cpp_quote(display)), 0)
            self.add("{0, 0, \"\"},", 0)
            self.indent -= 1
            self.add("};", 0)
            self.add("for (const auto& row : _homo_rows) {", 0)
            self.indent += 1
            self.add("if (line >= row.start && line < row.end) "
                     "return {row.file, line - row.start + 1};", 0)
            self.indent -= 1
            self.add("}", 0)
        self.add('return {"", line};', 0)
        self.indent -= 1
        self.add("}", 0)

    def emit(self) -> tuple[str, list[int]]:
        # Windows 控制台默认不是 UTF-8；把输入/输出代码页切成 UTF-8，
        # 直接运行生成的 exe 时中文才是可读的（走管道时不受影响）。
        # 这里手写 kernel32 声明，避免把 windows.h 的大量宏带进来和用户标识符冲突。
        self.add("#ifdef _WIN32", 0)
        self.add('extern "C" __declspec(dllimport) int __stdcall '
                 'SetConsoleOutputCP(unsigned int);', 0)
        self.add('extern "C" __declspec(dllimport) int __stdcall '
                 'SetConsoleCP(unsigned int);', 0)
        self.add("#endif", 0)
        self.add('#include "homo_runtime.hpp"', 0)
        self.add("", 0)
        self.emit_location_table()
        self.blank()
        if self.program.functions:
            for f in self.program.functions:
                params = ", ".join(f"homo_any {cxx_name(p.name)}" for p in f.params)
                self.add(f"homo_any {cxx_fn(f.name)}({params});", f.line)
            self.blank()
        # 全局变量先只声明，初值统一放到 main 里按源码顺序赋值，
        # 这样带「新建」的初值能用到完整的类定义，初始化顺序也确定。
        for g in self.program.globals:
            self.add(f"homo_any {cxx_name(g.name)};", g.line)
        if self.program.globals:
            self.blank()
        self.scopes = [dict(self.globals_frame)]
        for c in self.program.classes:
            self.emit_class(c)
            self.blank()
        for f in self.program.functions:
            self.emit_function(f)
            self.blank()
        entry = self.pick_entry()
        self.add("int main() {", 0)
        self.indent += 1
        # 输出走管道时默认全缓冲，提示语会迟迟不出现；改成行缓冲以便交互。
        # 注意：Windows 的 setvbuf 不支持 _IOLBF（等同全缓冲），直接关掉缓冲。
        self.add("#ifdef _WIN32", 0)
        self.add("SetConsoleOutputCP(65001u);", 0)
        self.add("SetConsoleCP(65001u);", 0)
        self.add("std::setvbuf(stdout, nullptr, _IONBF, 0);", 0)
        self.add("#else", 0)
        self.add("std::setvbuf(stdout, nullptr, _IOLBF, 0);", 0)
        self.add("#endif", 0)
        # 全局变量按源码顺序初始化；只有前面声明过的全局能出现在初值里。
        self.scopes = [dict(self.globals_frame)]
        for g in self.program.globals:
            self.emit_global_init(g)
        # 入口函数返回 0 表示成功，进程退出码为 2；返回非 0 时原样作为退出码。
        self.add(f"int _homo_code = (int){cxx_fn(entry.name)}().as_int();", 0)
        self.add("return _homo_code == 0 ? 2 : _homo_code;", 0)
        self.indent -= 1
        self.add("}", 0)
        text = "\n".join(t for t, _ in self.lines) + "\n"
        linemap = [ln for _, ln in self.lines]
        return text, linemap

    def pick_entry(self) -> Func:
        if self.program.entry is not None:
            f = self.program.entry
        else:
            names = [f.name for f in self.program.functions]
            entry = "会员制餐厅" if "会员制餐厅" in names else names[0]
            f = next(x for x in self.program.functions if x.name == entry)
        if f.params:
            raise HomoError("入口函数不能带参数", f.line)
        return f

    def emit_global_init(self, g: Decl) -> None:
        cname = cxx_name(g.name)
        if g.sizes:
            if g.init is not None and not isinstance(g.init, ArrayLit):
                raise HomoError(f"数组全局变量「{g.name}」的初值必须是 [ … ] 字面量", g.line)
            self.add(f"{cname} = {self.array_value(g.init, g.sizes)};", g.line)
        elif g.init is not None:
            if isinstance(g.init, ArrayLit):
                self.add(f"{cname} = {self.array_literal(g.init)};", g.line)
            else:
                self.add(f"{cname} = {self.expr(g.init)};", g.line)

    def emit_function(self, f: Func) -> None:
        params = ", ".join(f"homo_any {cxx_name(p.name)}" for p in f.params)
        self.add(f"homo_any {cxx_fn(f.name)}({params}) {{", f.line)
        self.indent += 1
        # 参数在编译期不知道装的是标量还是数组，两种形式都放行。
        self.scopes = [dict(self.globals_frame), self.param_frame(f.params)]
        self.repeat_stack = []
        self.break_targets = []
        for s in f.body:
            self.emit_stmt(s)
        if not f.body or not isinstance(f.body[-1], Return):
            self.add("return homo_value();", f.line)
        self.indent -= 1
        self.add("}", f.line)

    def emit_class(self, c: Class) -> None:
        self.add(f"struct {cxx_class(c.name)} : homo_object {{", c.line)
        self.indent += 1
        for fld in c.fields:
            self.emit_field(fld)
        if c.fields:
            self.blank()
        for m in c.methods:
            self.emit_method(c, m)
            self.blank()
        if c.methods:
            self.emit_dispatch(c)
            self.blank()
        if c.fields:
            self.emit_field_access(c)
            self.blank()
        self.emit_repr(c)
        self.blank()
        self.indent -= 1
        self.add("};", c.line)

    def emit_dispatch(self, c: Class) -> None:
        # 对象在运行时要能按方法名分发，方法名到具体成员函数的对应表。
        self.add("homo_any call(const std::string& _name, std::vector<homo_any> _args, "
                 "long long _homo_line) override {", c.line)
        self.indent += 1
        # 私有方法只能在本类方法里用裸名字调用；「新建」对「首」的调用不受限制。
        for m in c.methods:
            if m.private and m.name != INIT_KW:
                msg = f"「{c.name}.{m.name}」是私有方法，不能在类外调用"
                self.add(f"if (_name == {cpp_quote(m.name)}) homo_runtime_error("
                         f"_homo_line, {cpp_quote(msg)});", m.line)
        for m in c.methods:
            required, maximum = param_arity(m.params)
            cond = f"_name == {cpp_quote(m.name)}"
            cond += f" && _args.size() >= {required}"
            if maximum is not None:
                cond += f" && _args.size() <= {maximum}"
            self.add(f"if ({cond}) {{", m.line)
            self.indent += 1
            parts: list[str] = []
            for i, p in enumerate(m.params):
                if p.variadic:
                    parts.append(f"homo_rest(_args, {i})")
                elif p.default is None:
                    parts.append(f"_args[{i}]")
                else:
                    parts.append(f"(_args.size() > {i} ? _args[{i}] "
                                 f": homo_any({self.expr(p.default)}))")
            self.add(f"return {cxx_name(m.name)}({', '.join(parts)});", m.line)
            self.indent -= 1
            self.add("}", m.line)
        # 名字对但参数个数不对时给出具体原因，而不是「对象没有方法」。
        for m in c.methods:
            required, maximum = param_arity(m.params)
            if required == 0 and maximum is None:
                continue
            if maximum is None:
                prefix = f"方法「{m.name}」至少需要 {required} 个参数，但给了 "
            elif required == maximum:
                prefix = f"方法「{m.name}」需要 {required} 个参数，但给了 "
            else:
                prefix = f"方法「{m.name}」最多需要 {maximum} 个参数，但给了 "
            self.add(f"if (_name == {cpp_quote(m.name)}) homo_runtime_error("
                     f"_homo_line, {cpp_quote(prefix)} + "
                     f"std::to_string(_args.size()) + \" 个\");", m.line)
        self.add('homo_runtime_error(_homo_line, "对象没有方法「" + _name + "」");', c.line)
        self.indent -= 1
        self.add("}", c.line)

    def emit_field_access(self, c: Class) -> None:
        # 字段按名字读写，这样任何持有对象的表达式都能「对象.字段」。
        self.add("homo_any& get_field(const std::string& _name, "
                 "long long _homo_line) override {", c.line)
        self.indent += 1
        # 私有字段只能在本类方法里用裸名字读写；「对象.字段」一律拒绝。
        for fld in c.fields:
            if fld.private:
                msg = f"「{c.name}.{fld.name}」是私有字段，不能在类外访问"
                self.add(f"if (_name == {cpp_quote(fld.name)}) homo_runtime_error("
                         f"_homo_line, {cpp_quote(msg)});", fld.line)
        for fld in c.fields:
            self.add(f"if (_name == {cpp_quote(fld.name)}) return {cxx_name(fld.name)};", fld.line)
        self.add('homo_runtime_error(_homo_line, "对象没有字段「" + _name + "」");', c.line)
        self.indent -= 1
        self.add("}", c.line)
        self.add("void set_field(const std::string& _name, homo_any _v, "
                 "long long _homo_line) override {", c.line)
        self.indent += 1
        for fld in c.fields:
            if fld.private:
                msg = f"「{c.name}.{fld.name}」是私有字段，不能在类外访问"
                self.add(f"if (_name == {cpp_quote(fld.name)}) homo_runtime_error("
                         f"_homo_line, {cpp_quote(msg)});", fld.line)
        for fld in c.fields:
            self.add(
                f"if (_name == {cpp_quote(fld.name)}) {{ {cxx_name(fld.name)} = std::move(_v); return; }}",
                fld.line,
            )
        self.add('homo_runtime_error(_homo_line, "对象没有字段「" + _name + "」");', c.line)
        self.indent -= 1
        self.add("}", c.line)

    def emit_repr(self, c: Class) -> None:
        # 打印对象时按声明顺序展开字段。
        name = cpp_quote(c.name)
        self.add("std::string repr(int _homo_depth) const override {", c.line)
        self.indent += 1
        if not c.fields:
            self.add(f"return std::string({name}) + \" {{}}\";", c.line)
        else:
            self.add(f"std::string _homo_s = std::string({name}) + \" {{\";", c.line)
            for i, fld in enumerate(c.fields):
                sep = " " if i == 0 else ", "
                label = cpp_quote(f"{sep}{fld.name}: ")
                self.add(f"_homo_s += {label};", fld.line)
                self.add(
                    f"_homo_s += homo_repr(this->{cxx_name(fld.name)}, true, _homo_depth + 1);",
                    fld.line,
                )
            self.add('_homo_s += " }";', c.line)
            self.add("return _homo_s;", c.line)
        self.indent -= 1
        self.add("}", c.line)

    def array_value(self, lit: Optional[ArrayLit], sizes: list[Expr]) -> str:
        if lit is not None and not isinstance(lit, ArrayLit):
            raise HomoError("数组的初值必须是 [ … ] 字面量", lit.line)
        base = self.array_literal(lit) if lit is not None else "homo_any()"
        dims = ", ".join(self.expr(size) for size in sizes)
        if len(sizes) == 1:
            if lit is None:
                return f"homo_make_array({dims})"
            return f"homo_array_size({base}, {dims})"
        return f"homo_array_fit({base}, {{{dims}}})"

    def emit_field(self, fld: Decl) -> None:
        if fld.sizes:
            if fld.init is not None and not isinstance(fld.init, ArrayLit):
                raise HomoError(f"数组字段「{fld.name}」的初值必须是 [ … ] 字面量", fld.line)
            self.add(f"homo_any {cxx_name(fld.name)} = {self.array_value(fld.init, fld.sizes)};", fld.line)
        elif fld.init is not None:
            if isinstance(fld.init, ArrayLit):
                self.add(f"homo_any {cxx_name(fld.name)} = {self.array_literal(fld.init)};", fld.line)
            else:
                self.add(f"homo_any {cxx_name(fld.name)} = {self.expr(fld.init)};", fld.line)
        else:
            self.add(f"homo_any {cxx_name(fld.name)};", fld.line)

    def emit_method(self, cls: Class, m: Func) -> None:
        params = ", ".join(f"homo_any {cxx_name(p.name)}" for p in m.params)
        self.add(f"homo_any {cxx_name(m.name)}({params}) {{", m.line)
        self.indent += 1
        # 方法里可以直接使用字段名，等价于 this->字段；参数在内层，可以遮蔽字段。
        fields = {
            fld.name: ("array" if fld.sizes or isinstance(fld.init, ArrayLit) else "scalar")
            for fld in cls.fields
        }
        self.scopes = [dict(self.globals_frame), fields, self.param_frame(m.params)]
        self.repeat_stack = []
        self.break_targets = []
        for s in m.body:
            self.emit_stmt(s)
        if not m.body or not isinstance(m.body[-1], Return):
            self.add("return homo_value();", m.line)
        self.indent -= 1
        self.add("}", m.line)

    def emit_target_assign(self, s: Assign) -> None:
        # 赋值目标是「对象.字段」或更复杂的下标表达式时走这里。
        helpers = {"便乘": "homo_mul", "便减": "homo_sub",
                   "便加": "homo_add", "便除": "homo_div"}
        if s.op != ASSIGN and s.op not in helpers:
            raise HomoError(f"未知的赋值算子「{s.op}」", s.line)
        target = s.target
        rhs = self.expr(s.expr)
        if isinstance(target, Field):
            obj = self.expr(target.obj)
            name = cpp_quote(target.name)
            if s.op == ASSIGN:
                self.add(f"homo_set_field({obj}, {name}, {rhs}, {s.line});", s.line)
            else:
                ref = f"homo_get_field({obj}, {name}, {s.line})"
                if s.op == "便除":
                    value = f"homo_div({ref}, {rhs}, {s.line})"
                else:
                    value = f"{helpers[s.op]}({ref}, {rhs})"
                self.add(f"homo_set_field({obj}, {name}, {value}, {s.line});", s.line)
            return
        if isinstance(target, Index):
            if isinstance(target.obj, Str):
                raise HomoError("字符串不能按下标赋值", s.line)
            ref = (f"homo_index({self.expr(target.obj, ref=True)}, "
                   f"{self.expr(target.index)}, {s.line})")
            if s.op == ASSIGN:
                self.add(f"{ref} = {rhs};", s.line)
            elif s.op == "便除":
                self.add(f"{ref} = homo_div({ref}, {rhs}, {s.line});", s.line)
            else:
                self.add(f"{ref} = {helpers[s.op]}({ref}, {rhs});", s.line)
            return
        raise HomoError("这里不能赋值：赋值目标只能是变量、数组元素或对象字段", s.line)

    def emit_var_decl(self, name: str, init_cpp: Optional[str], kind: str, line: int) -> None:
        cname = cxx_name(name)
        if init_cpp is None:
            self.add(f"homo_any {cname};", line)
        elif self.lookup(name) is not None:
            # 遮蔽外层同名变量时，初值里的同名引用指向外层，先落临时再声明。
            tmp = f"_homo_shadow{self.loop_counter}"
            self.loop_counter += 1
            self.add(f"homo_any {tmp} = {init_cpp};", line)
            self.add(f"homo_any {cname} = {tmp};", line)
        else:
            self.add(f"homo_any {cname} = {init_cpp};", line)
        self.declare(name, kind)

    def emit_stmt(self, s: Stmt) -> None:
        if isinstance(s, Decl):
            if s.sizes:
                if s.init is not None and not isinstance(s.init, ArrayLit):
                    raise HomoError(f"数组声明「{s.name}」的初值必须是 [ … ] 字面量", s.line)
                self.emit_var_decl(s.name, self.array_value(s.init, s.sizes), "array", s.line)
            elif s.init is not None:
                if isinstance(s.init, ArrayLit):
                    self.emit_var_decl(s.name, self.array_literal(s.init), "array", s.line)
                else:
                    self.emit_var_decl(s.name, self.expr(s.init), "scalar", s.line)
            else:
                self.emit_var_decl(s.name, None, "scalar", s.line)
        elif isinstance(s, Assign):
            if not s.name:
                self.emit_target_assign(s)
                return
            if s.index is not None:
                idx = self.expr(s.index)
                ref = f"homo_index({cxx_name(s.name)}, {idx}, {s.line})"
                rhs = self.expr(s.expr)
                if s.op == ASSIGN:
                    self.add(f"{ref} = {rhs};", s.line)
                else:
                    helpers = {"便乘": "homo_mul", "便减": "homo_sub",
                               "便加": "homo_add", "便除": "homo_div"}
                    if s.op not in helpers:
                        raise HomoError(f"未知的赋值算子「{s.op}」", s.line)
                    if s.op == "便除":
                        self.add(f"{ref} = homo_div({ref}, {rhs}, {s.line});", s.line)
                    else:
                        self.add(f"{ref} = {helpers[s.op]}({ref}, {rhs});", s.line)
                return
            kind = self.lookup(s.name)
            if s.op == ASSIGN and isinstance(s.expr, ArrayLit):
                # 只写「名字 事 [ … ] 罢」时按字面量长度建数组。
                value = self.array_literal(s.expr)
                if kind is None:
                    self.emit_var_decl(s.name, value, "array", s.line)
                elif kind in ("array", "unknown"):
                    self.add(f"{cxx_name(s.name)} = {value};", s.line)
                else:
                    raise HomoError(f"「{s.name}」不是数组，不能用数组字面量赋值", s.line)
                return
            if kind is None:
                if s.op == ASSIGN:
                    self.emit_var_decl(s.name, self.expr(s.expr), "scalar", s.line)
                    return
                self.emit_var_decl(s.name, None, "scalar", s.line)
            rhs = self.expr(s.expr)
            if s.op == ASSIGN:
                self.add(f"{cxx_name(s.name)} = {rhs};", s.line)
            elif s.op == "便乘":
                self.add(f"{cxx_name(s.name)} = homo_mul({cxx_name(s.name)}, {rhs});", s.line)
            elif s.op == "便减":
                self.add(f"{cxx_name(s.name)} = homo_sub({cxx_name(s.name)}, {rhs});", s.line)
            elif s.op == "便加":
                self.add(f"{cxx_name(s.name)} = homo_add({cxx_name(s.name)}, {rhs});", s.line)
            elif s.op == "便除":
                self.add(f"{cxx_name(s.name)} = homo_div({cxx_name(s.name)}, {rhs}, {s.line});", s.line)
            else:
                raise HomoError(f"未知的赋值算子「{s.op}」", s.line)
        elif isinstance(s, CallStmt):
            self.emit_call_stmt(s.expr, s.line)
        elif isinstance(s, If):
            self.add(f"if (homo_truthy({self.expr(s.cond)})) {{", s.line)
            self.indent += 1
            self.push_scope()
            for b in s.then:
                self.emit_stmt(b)
            self.pop_scope()
            self.indent -= 1
            for cond, body in s.elifs:
                self.add(f"}} else if (homo_truthy({self.expr(cond)})) {{", s.line)
                self.indent += 1
                self.push_scope()
                for b in body:
                    self.emit_stmt(b)
                self.pop_scope()
                self.indent -= 1
            if s.els is not None:
                self.add("} else {", s.line)
                self.indent += 1
                self.push_scope()
                for b in s.els:
                    self.emit_stmt(b)
                self.pop_scope()
                self.indent -= 1
            self.add("}", s.line)
        elif isinstance(s, While):
            self.add(f"while (homo_truthy({self.expr(s.cond)})) {{", s.line)
            self.indent += 1
            self.break_targets.append(None)
            self.push_scope()
            for b in s.body:
                self.emit_stmt(b)
            self.pop_scope()
            self.break_targets.pop()
            self.indent -= 1
            self.add("}", s.line)
        elif isinstance(s, Repeat):
            n = self.loop_counter
            self.loop_counter += 1
            self.add(f"long long _homo_a{n} = ({self.expr(s.start)}).as_int();", s.line)
            self.add(f"long long _homo_b{n} = ({self.expr(s.end)}).as_int();", s.line)
            # 两端相同走 n 次；不同则走两数之差 |a-b| 次。
            self.add(f"long long _homo_d{n} = _homo_a{n} - _homo_b{n};", s.line)
            self.add(f"if (_homo_d{n} < 0) _homo_d{n} = -_homo_d{n};", s.line)
            self.add(
                f"long long _homo_n{n} = (_homo_a{n} == _homo_b{n}) "
                f"? _homo_a{n} : _homo_d{n};",
                s.line,
            )
            # 回数 的值：两端相同时从 0 数到 n-1；不同时从起始值走向结束值（不含）。
            self.add(
                f"long long _homo_v{n} = (_homo_a{n} == _homo_b{n}) ? 0 : _homo_a{n};",
                s.line,
            )
            self.add(f"long long _homo_s{n} = (_homo_b{n} < _homo_a{n}) ? -1 : 1;", s.line)
            # 回数的推进放在 for 的递增表达式里，这样 continue 也不会漏掉它。
            self.add(
                f"for (long long _homo_i{n} = 0; _homo_i{n} < _homo_n{n}; "
                f"++_homo_i{n}, _homo_v{n} += _homo_s{n}) {{",
                s.line,
            )
            self.indent += 1
            self.repeat_stack.append(n)
            self.break_targets.append(None)
            self.push_scope()
            for b in s.body:
                self.emit_stmt(b)
            self.pop_scope()
            self.break_targets.pop()
            self.repeat_stack.pop()
            self.indent -= 1
            self.add("}", s.line)
        elif isinstance(s, DoWhile):
            self.add("do {", s.line)
            self.indent += 1
            self.break_targets.append(None)
            self.push_scope()
            for b in s.body:
                self.emit_stmt(b)
            self.pop_scope()
            self.break_targets.pop()
            self.indent -= 1
            self.add(f"}} while (homo_truthy({self.expr(s.cond)}));", s.line)
        elif isinstance(s, Switch):
            # C++ 的 case 只接受整型常量，这里生成等值比较链，任何类型的值都能匹配。
            n = self.loop_counter
            self.loop_counter += 1
            subj = f"_homo_sw{n}"
            label = f"_homo_sw_end{n}"
            self.add(f"auto {subj} = {self.expr(s.expr)};", s.line)
            default_body = None
            arms = 0
            self.break_targets.append(label)
            for ce, body in s.cases:
                if ce is None:
                    default_body = body
                    continue
                guard = f"homo_truthy(homo_eq_any({subj}, {self.expr(ce)}))"
                self.add(("if (" if arms == 0 else "} else if (") + guard + ") {", s.line)
                arms += 1
                self.indent += 1
                self.push_scope()
                for b in body:
                    self.emit_stmt(b)
                self.pop_scope()
                self.indent -= 1
            if default_body is not None:
                self.add(("{" if arms == 0 else "} else {"), s.line)
                self.indent += 1
                self.push_scope()
                for b in default_body:
                    self.emit_stmt(b)
                self.pop_scope()
                self.indent -= 1
            self.break_targets.pop()
            if arms or default_body is not None:
                self.add("}", s.line)
            self.add(f"{label}: ;", s.line)
        elif isinstance(s, Break):
            target = self.break_targets[-1] if self.break_targets else None
            if target is not None:
                self.add(f"goto {target};", s.line)
            else:
                self.add("break;", s.line)
        elif isinstance(s, Continue):
            self.add("continue;", s.line)
        elif isinstance(s, Return):
            self.add(f"return {self.expr(s.expr)};", s.line)
        else:
            raise HomoError("无法翻译的语句", getattr(s, "line", 0))

    def emit_call_stmt(self, call: Expr, line: int) -> None:
        if isinstance(call, MethodCall):
            args = ", ".join(self.expr(a) for a in call.args)
            self.add(f"(void)homo_call({self.expr(call.obj)}, {cpp_quote(call.name)}, "
                     f"{{{args}}}, {line});", line)
            return
        if call.name == "脱出":
            if len(call.args) != 1:
                raise HomoError('脱出 只接受一个参数，例如 脱出("年龄：{年龄}")', line)
            self.emit_print(call.args[0], line)
            return
        self.add(f"(void){self.expr(call)};", line)

    def emit_print(self, arg: Expr, line: int) -> None:
        if isinstance(arg, Str):
            pieces = []
            for kind, value in split_template(arg.raw, line):
                if kind == "text":
                    pieces.append(f"homo_value({cpp_quote(value)})")
                else:
                    pieces.append(self.expr(value))
            self.add(f"homo_print_parts({{{', '.join(pieces)}}});", line)
        else:
            self.add(f"homo_print_value({self.expr(arg)});", line)

    def array_literal(self, lit: ArrayLit) -> str:
        if not lit.items:
            return "std::vector<homo_any>()"
        items = ", ".join(self.expr(item) for item in lit.items)
        return f"homo_array_lit({{{items}}})"

    def expr(self, e: Expr, ref: bool = False) -> str:
        if isinstance(e, Num):
            return f"homo_value({e.text})"
        if isinstance(e, Str):
            return f"homo_value({cpp_quote(string_inner(e.raw))})"
        if isinstance(e, BoolLit):
            return "homo_value(true)" if e.value else "homo_value(false)"
        if isinstance(e, NullLit):
            return "homo_null()"
        if isinstance(e, Ident):
            return cxx_name(e.name)
        if isinstance(e, New):
            # 显式写全局作用域：局部变量可以和类同名，不能让它遮住构造函数。
            obj = f"homo_object_of(::{cxx_class(e.class_name)}())"
            cls = self.classes.get(e.class_name)
            init = next((m for m in cls.methods if m.name == INIT_KW), None) if cls else None
            if init is None:
                return obj
            # 实参原样交给运行时分发：默认值和可变参数由 call 里的分发补齐。
            args = ", ".join(self.expr(a) for a in e.args)
            return f"homo_init_object({obj}, {cpp_quote(INIT_KW)}, {{{args}}}, {e.line})"
        if isinstance(e, MethodCall):
            args = ", ".join(self.expr(a) for a in e.args)
            return (f"homo_call({self.expr(e.obj)}, {cpp_quote(e.name)}, "
                    f"{{{args}}}, {e.line})")
        if isinstance(e, Call):
            if e.name == "脱出":
                raise HomoError("脱出 只能作为独立语句使用", e.line)
            if e.name == "读入":
                if e.args:
                    raise HomoError("读入 不需要参数", e.line)
                return "homo_read_int()"
            if e.name == "读行":
                if e.args:
                    raise HomoError("读行 不需要参数", e.line)
                return "homo_read_line()"
            if e.name == RANDOM_KW:
                if len(e.args) != 1:
                    raise HomoError("随机 需要一个参数（上界），例如 随机(81)", e.line)
                return f"homo_random({self.expr(e.args[0])})"
            if e.name == LENGTH_KW:
                if len(e.args) != 1:
                    raise HomoError(f"长度 需要 1 个参数，但给了 {len(e.args)} 个", e.line)
                return f"homo_length({self.expr(e.args[0])}, {e.line})"
            if e.name in STRING_BUILTIN_ARITY:
                # 字符串模板里的调用在检查阶段看不到，这里再查一次参数个数。
                check_string_builtin_arity(e.name, len(e.args), e.line)
                if e.name == "取子串":
                    if len(e.args) == 2:
                        return (f"homo_substr({self.expr(e.args[0])}, "
                                f"{self.expr(e.args[1])}, {e.line})")
                    return (f"homo_substr_len({self.expr(e.args[0])}, "
                            f"{self.expr(e.args[1])}, {self.expr(e.args[2])}, {e.line})")
                helper = {
                    "查找": "homo_find", "包含": "homo_contains", "替换": "homo_replace",
                    "分割": "homo_split", "连接": "homo_join", "去空白": "homo_trim",
                    "转数字": "homo_to_number", "转大写": "homo_upper", "转小写": "homo_lower",
                }[e.name]
                args = ", ".join(self.expr(a) for a in e.args)
                return f"{helper}({args}, {e.line})"
            f = self.functions.get(e.name)
            if f is not None:
                return f"{cxx_fn(e.name)}({self.emit_call_args(e.args, f.params)})"
            args = ", ".join(self.expr(a) for a in e.args)
            return f"{cxx_fn(e.name)}({args})"
        if isinstance(e, Index):
            helper = "homo_index" if ref else "homo_index_get"
            return (f"{helper}({self.expr(e.obj, ref=ref)}, "
                    f"{self.expr(e.index)}, {e.line})")
        if isinstance(e, Field):
            return f"homo_get_field({self.expr(e.obj)}, {cpp_quote(e.name)}, {e.line})"
        if isinstance(e, ArrayLit):
            return self.array_literal(e)
        if isinstance(e, LoopVar):
            if not self.repeat_stack:
                raise HomoError(f"「{REPEAT_VAR}」只能在（n）回啊（n）回 循环里使用", e.line)
            return f"homo_value(_homo_v{self.repeat_stack[-1]})"
        if isinstance(e, Not):
            return f"homo_not({self.expr(e.expr)})"
        if isinstance(e, Lt):
            return f"homo_lt_any({self.expr(e.left)}, {self.expr(e.right)}, {e.line})"
        if isinstance(e, Eq):
            return f"homo_eq_any({self.expr(e.left)}, {self.expr(e.right)})"
        if isinstance(e, And):
            return (f"homo_value((homo_truthy({self.expr(e.left)}) "
                    f"&& homo_truthy({self.expr(e.right)})))")
        if isinstance(e, Or):
            return (f"homo_value((homo_truthy({self.expr(e.left)}) "
                    f"|| homo_truthy({self.expr(e.right)})))")
        if isinstance(e, Bin):
            if e.op in ("/", "%"):
                helper = "homo_div" if e.op == "/" else "homo_mod"
                return (f"{helper}({self.expr(e.left)}, {self.expr(e.right)}, "
                        f"{e.line})")
            helper = {"+": "homo_add", "-": "homo_sub", "*": "homo_mul"}[e.op]
            return f"{helper}({self.expr(e.left)}, {self.expr(e.right)})"
        raise HomoError("无法翻译的表达式", getattr(e, "line", 0))


# C++ 关键字和生成代码自己占用的名字。homo 标识符直接写进 C++ 会撞上它们
# （比如用户把变量叫 int），统一加前缀换成合法且不会冲突的标识符。
CPP_KEYWORDS = {
    "alignas", "alignof", "and", "and_eq", "asm", "auto", "bitand", "bitor",
    "bool", "break", "case", "catch", "char", "char8_t", "char16_t", "char32_t",
    "class", "compl", "concept", "const", "consteval", "constexpr", "constinit",
    "const_cast", "continue", "co_await", "co_return", "co_yield", "decltype",
    "default", "delete", "do", "double", "dynamic_cast", "else", "enum",
    "explicit", "export", "extern", "false", "float", "for", "friend", "goto",
    "if", "inline", "int", "long", "mutable", "namespace", "new", "noexcept",
    "not", "not_eq", "nullptr", "operator", "or", "or_eq", "private",
    "protected", "public", "register", "reinterpret_cast", "requires", "return",
    "short", "signed", "sizeof", "static", "static_assert", "static_cast",
    "struct", "switch", "template", "this", "thread_local", "throw", "true",
    "try", "typedef", "typeid", "typename", "union", "unsigned", "using",
    "virtual", "void", "volatile", "wchar_t", "while", "xor", "xor_eq",
    # 生成代码用到的全局名字（std::、运行时 homo_* 已由前缀规则覆盖）。
    "main", "std",
}


def cxx_name(name: str) -> str:
    if (name in CPP_KEYWORDS or name.startswith("homo_")
            or name.startswith("_homo_")):
        return "homo_kw_" + name
    return name


def cxx_class(name: str) -> str:
    # 类名单独加前缀：C++ 不允许成员函数或字段和类同名，
    # 例如「类 首」里的构造方法「首」会生成 `struct 首 { homo_any 首(); }`。
    return "homo_cls_" + cxx_name(name)


def cxx_fn(name: str) -> str:
    return cxx_name(name)


# --------------------------------------------------------------------------
# 多文件：import .inm
# --------------------------------------------------------------------------
def resource_path(name: str) -> str:
    """资源文件路径；PyInstaller 打包后从临时解包目录读取。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


RUNTIME_PATH = resource_path("homo_runtime.hpp")


@dataclass
class SourceUnit:
    path: Optional[str]       # 绝对路径；内存源码为 None
    display: str              # 展示用路径（相对项目根目录）
    source: str
    start_line: int           # 在所有模块拼起来后的全局起始行
    line_count: int


class SourceMap:
    """把生成的 C++ 行号 / 全局行号换算回具体文件和文件内行号。"""

    def __init__(self, root_dir: Optional[str] = None):
        self.root_dir = os.path.realpath(root_dir) if root_dir else None
        self.units: list[SourceUnit] = []
        self.next_line = 1

    def display_path(self, path: Optional[str]) -> str:
        if not path:
            return "<内存>"
        path = os.path.realpath(path)
        if self.root_dir:
            try:
                rel = os.path.relpath(path, self.root_dir)
            except ValueError:
                rel = path
            if rel != ".." and not rel.startswith(".." + os.sep) and not os.path.isabs(rel):
                return rel.replace(os.sep, "/")
        return path.replace(os.sep, "/")

    def add(self, path: Optional[str], source: str) -> SourceUnit:
        real = os.path.realpath(path) if path else None
        unit = SourceUnit(
            path=real,
            display=self.display_path(real),
            source=source,
            start_line=self.next_line,
            line_count=source.count("\n") + 1,
        )
        self.units.append(unit)
        self.next_line += unit.line_count
        return unit

    def resolve(self, global_line: int) -> tuple[Optional[str], int]:
        if not global_line or global_line <= 0:
            return None, 0
        for unit in reversed(self.units):
            if unit.start_line <= global_line < unit.start_line + unit.line_count:
                return unit.display, global_line - unit.start_line + 1
        return None, global_line

    def resolve_path(self, global_line: int) -> tuple[Optional[str], Optional[str], int]:
        display, line = self.resolve(global_line)
        for unit in self.units:
            if unit.display == display:
                return unit.path, display, line
        return None, display, line

    def path_for_display(self, display: str) -> Optional[str]:
        wanted = display.replace("\\", "/")
        for unit in self.units:
            if unit.display == wanted:
                return unit.path
        return None

    def rows(self) -> list[tuple[int, int, str]]:
        """(全局起始行, 全局结束行（不含）, 展示路径)"""
        return [(u.start_line, u.start_line + u.line_count, u.display) for u in self.units]


def _shift_ast_lines(node: object, delta: int) -> None:
    """把模块自己的局部行号整体平移到全局行号。"""
    if not delta or node is None:
        return
    if isinstance(node, list):
        for item in node:
            _shift_ast_lines(item, delta)
        return
    if isinstance(node, tuple):
        for item in node:
            _shift_ast_lines(item, delta)
        return
    if isinstance(node, dict):
        for item in node.values():
            _shift_ast_lines(item, delta)
        return
    if not dataclasses.is_dataclass(node):
        return
    for f in dataclasses.fields(node):
        value = getattr(node, f.name)
        if f.name == "line" and isinstance(value, int):
            if value > 0:
                setattr(node, f.name, value + delta)
        else:
            _shift_ast_lines(value, delta)


def format_error(e: HomoError) -> str:
    """统一的人读错误格式：文件:行: 信息。"""
    sm = e.source_map
    file, line = e.file, e.line
    if sm is not None and e.line:
        mapped_file, mapped_line = sm.resolve(e.line)
        if mapped_file:
            file, line = mapped_file, mapped_line
    if file:
        where = f"{file}:{line}" if line else file
        return f"{where}: {e.msg}"
    if line:
        return f"第 {line} 行：{e.msg}"
    return e.msg


def error_diagnostic(e: HomoError) -> dict:
    sm = e.source_map
    path, file, line = e.path, e.file, e.line
    if sm is not None and e.line:
        mapped_path, mapped_file, mapped_line = sm.resolve_path(e.line)
        if mapped_file:
            path, file, line = mapped_path, mapped_file, mapped_line
    return {
        "severity": "错误",
        "file": file or "",
        "path": path or "",
        "line": line or 0,
        "column": 0,
        "message": e.msg,
    }



def _normalize_import_target(target: str) -> str:
    t = target.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"“”‘’'":
        t = t[1:-1]
    return t.replace("\\", "/")


def _import_candidates(target: str, importer: Optional[str],
                       include_dirs: list[str],
                       base_dir: Optional[str] = None) -> list[str]:
    if target.lower().endswith(".homo"):
        names = [target]
    elif target.lower().endswith(".inm"):
        names = []
    else:
        names = [target + ".homo", target]
    bases = [os.path.dirname(importer) if importer else (base_dir or os.getcwd())]
    bases.extend(include_dirs)
    out: list[str] = []
    seen: set[str] = set()
    for base in bases:
        for name in names:
            cand = os.path.normpath(os.path.join(base, name))
            if cand not in seen:
                seen.add(cand)
                out.append(cand)
    return out


def _find_import(target: str, importer: Optional[str], include_dirs: list[str],
                 line: int, sm: SourceMap,
                 base_dir: Optional[str] = None) -> Optional[str]:
    if target.lower().endswith(".inm"):
        err = HomoError(
            "不再支持 .inm 文件；inm 只能引用 .homo 文件",
            line, file=sm.display_path(importer) if importer else None,
            path=importer,
        )
        err.source_map = sm
        raise err
    candidates = _import_candidates(target, importer, include_dirs, base_dir)
    for cand in candidates:
        if os.path.isfile(cand):
            return os.path.realpath(cand)
    tried = "、".join(sm.display_path(c) for c in candidates[:4])
    more = " 等" if len(candidates) > 4 else ""
    err = HomoError(
        f"找不到 inm「{target}」；找过：{tried}{more}",
        line, file=sm.display_path(importer) if importer else None,
        path=importer,
    )
    err.source_map = sm
    raise err


def load_program(root_path: Optional[str], root_source: Optional[str] = None,
                 overlay: Optional[dict[str, str]] = None,
                 include_dirs: Optional[list[str]] = None,
                 base_dir: Optional[str] = None,
                 require_function: bool = True) -> tuple[Program, SourceMap, list[SourceUnit]]:
    """读取主文件及其 import 的 .inm，按依赖顺序合并成一个程序。

    overlay 用「绝对路径 -> 源码」覆盖磁盘内容，供桌面编辑器编译未保存的缓冲区。
    """
    root_path = os.path.abspath(root_path) if root_path else None
    root_dir = os.path.realpath(
        os.path.dirname(root_path) if root_path
        else base_dir if base_dir else os.getcwd()
    )
    sm = SourceMap(root_dir)
    include_dirs = [os.path.abspath(d) for d in (include_dirs or [])]
    overlay = {os.path.realpath(k): v for k, v in (overlay or {}).items()}

    def read_source(path: str) -> str:
        real = os.path.realpath(path)
        if real in overlay:
            return overlay[real]
        try:
            with open(real, "rb") as f:
                data = f.read()
        except OSError as exc:
            err = HomoError(f"读取模块失败：{exc}", 0,
                            file=sm.display_path(real), path=real)
            err.source_map = sm
            raise err from exc
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            err = HomoError(f"文件不是 UTF-8 编码：{exc}", 0,
                            file=sm.display_path(real), path=real)
            err.source_map = sm
            raise err from exc

    def parse_unit(path: Optional[str], source: str) -> tuple[Program, SourceUnit]:
        unit = sm.add(path, source)
        declaration_mode = bool(path and path.lower().endswith(".inm"))
        try:
            program = Parser(tokenize(source), declaration_mode=declaration_mode).parse_program()
        except HomoError as e:
            if e.line:
                e.line += unit.start_line - 1
            e.file = unit.display
            e.path = unit.path
            e.source_map = sm
            raise
        _shift_ast_lines(program, unit.start_line - 1)
        return program, unit

    modules: list[tuple[Program, Optional[str]]] = []
    parsed_cache: dict[str, tuple[Program, SourceUnit]] = {}
    load_state: dict[str, Optional[set[str]]] = {}  # None=整文件，set=已选类
    imports_loaded: set[str] = set()
    active: list[str] = []
    root_program: Optional[Program] = None
    root_unit: Optional[SourceUnit] = None

    def cached_parse(real: str, source_override: Optional[str] = None):
        if real in parsed_cache:
            return parsed_cache[real]
        source = source_override if source_override is not None else read_source(real)
        program, unit = parse_unit(real, source)
        parsed_cache[real] = (program, unit)
        return program, unit

    def filter_class(program: Program, class_name: str,
                     importer: Optional[str], line: int) -> Program:
        classes = [c for c in program.classes if c.name == class_name]
        if not classes:
            err = HomoError(
                f"文件里没有类「{class_name}」",
                line, file=sm.display_path(importer) if importer else None,
                path=importer,
            )
            err.source_map = sm
            raise err
        return Program(functions=[], classes=classes, globals=[], imports=[],
                       declarations=[], global_decls=[], class_decls=[])

    def ensure_imports(program: Program, real: str) -> None:
        if real in imports_loaded:
            return
        imports_loaded.add(real)
        load_imports(program, real)

    def load(path: str, *, is_root: bool = False,
             source_override: Optional[str] = None,
             selected_class: Optional[str] = None,
             importer: Optional[str] = None,
             import_line: int = 0) -> None:
        nonlocal root_program, root_unit
        real = os.path.realpath(path)
        state = load_state.get(real, "unloaded")
        if selected_class is None:
            if state is None:
                return
            if state != "unloaded":
                err = HomoError(
                    "同一个文件不能同时整体 inm 和只 inm 某个类",
                    import_line, file=sm.display_path(importer) if importer else None,
                    path=importer,
                )
                err.source_map = sm
                raise err
            program, unit = cached_parse(real, source_override)
            if is_root:
                root_program, root_unit = program, unit
            load_state[real] = None
            active.append(real)
            try:
                ensure_imports(program, real)
            finally:
                active.pop()
            modules.append((program, real))
            return

        # 只导入某个类
        if state is None:
            return
        if state == "unloaded":
            state = set()
            load_state[real] = state
        assert isinstance(state, set)
        if selected_class in state:
            return
        program, unit = cached_parse(real, source_override)
        state.add(selected_class)
        # 只导入这个类，不加载该文件顶部的 inm 依赖；整文件 inm 时才加载。
        modules.append((filter_class(program, selected_class, importer, import_line),
                        real))

    def load_imports(program: Program, importer: Optional[str],
                     import_base: Optional[str] = None) -> None:
        for imp in program.imports:
            target = _normalize_import_target(imp.target)
            dep = _find_import(target, importer, include_dirs, imp.line, sm, import_base)
            if dep is None:
                continue
            dep_real = os.path.realpath(dep)
            if dep_real in active:
                idx = active.index(dep_real)
                chain = active[idx:] + [dep_real]
                err = HomoError(
                    "检测到循环 inm：" + " → ".join(sm.display_path(p) for p in chain),
                    imp.line, file=sm.display_path(importer) if importer else None,
                    path=importer,
                )
                err.source_map = sm
                raise err
            load(dep, selected_class=imp.selected_class,
                 importer=importer, import_line=imp.line)

    if root_path:
        load(root_path, is_root=True, source_override=root_source)
    else:
        root_program, root_unit = parse_unit(None, root_source or "")
        active.append("<memory>")
        try:
            load_imports(root_program, None, root_dir)
        finally:
            active.pop()
        modules.append((root_program, None))

    merged = Program(functions=[], classes=[], globals=[], imports=[],
                     declarations=[], global_decls=[], class_decls=[])
    for module, _module_path in modules:
        merged.functions.extend(module.functions)
        merged.classes.extend(module.classes)
        merged.globals.extend(module.globals)
        merged.declarations.extend(module.declarations)
        merged.global_decls.extend(module.global_decls)
        merged.class_decls.extend(module.class_decls)

    if require_function:
        # 入口只从主程序 .homo 里找「会员制餐厅」，不退回其他 .homo。
        main_functions = root_program.functions if root_program else []
        entry = next((f for f in main_functions if f.name == "会员制餐厅"), None)
        if entry is None:
            line = root_unit.start_line if root_unit else 1
            err = HomoError(
                "没有主函数入口：主程序 .homo 里必须定义「会员制餐厅」",
                line, file=root_unit.display if root_unit else None,
                path=root_unit.path if root_unit else None,
            )
            err.source_map = sm
            raise err
        merged.entry = entry
    if not merged.functions:
        return merged, sm, list(sm.units)
    return merged, sm, list(sm.units)


@dataclass
class Compiled:
    cpp: str
    types: list[dict]
    linemap: list[int]
    source_map: SourceMap
    program: Program
    units: list[SourceUnit]


def compile_source(source: str, *, path: Optional[str] = None,
                   overlay: Optional[dict[str, str]] = None,
                   include_dirs: Optional[list[str]] = None,
                   base_dir: Optional[str] = None) -> Compiled:
    program, sm, units = load_program(path, source, overlay, include_dirs, base_dir)
    try:
        check_names(program)
        types = analyze_types(program)
        cpp, linemap = CEmitter(program, sm).emit()
    except HomoError as e:
        e.source_map = e.source_map or sm
        if e.path is None and e.line:
            path, display, _ = sm.resolve_path(e.line)
            e.path = path
            e.file = e.file or display
        raise
    return Compiled(cpp, types, linemap, sm, program, units)


def check_source(source: str, *, path: Optional[str] = None,
                 overlay: Optional[dict[str, str]] = None,
                 include_dirs: Optional[list[str]] = None,
                 base_dir: Optional[str] = None,
                 require_function: bool = False) -> tuple[Program, SourceMap, list[SourceUnit]]:
    """编辑器的实时检查：解析、import、名字和作用检查，不生成/编译 C++。

    默认不要求入口函数，因此可以直接检查 .inm 库文件。
    """
    program, sm, units = load_program(path, source, overlay, include_dirs,
                                      base_dir, require_function=require_function)
    try:
        check_names(program)
        analyze_types(program)
    except HomoError as e:
        e.source_map = e.source_map or sm
        if e.path is None and e.line:
            resolved_path, display, _ = sm.resolve_path(e.line)
            e.path = resolved_path
            e.file = e.file or display
        raise
    return program, sm, units


def transpile(source: str, path: Optional[str] = None, *,
              overlay: Optional[dict[str, str]] = None,
              include_dirs: Optional[list[str]] = None,
              base_dir: Optional[str] = None) -> tuple[str, list[dict], list[int]]:
    compiled = compile_source(source, path=path, overlay=overlay,
                              include_dirs=include_dirs, base_dir=base_dir)
    return compiled.cpp, compiled.types, compiled.linemap


def lint(source: str, path: Optional[str] = None, *,
         overlay: Optional[dict[str, str]] = None,
         include_dirs: Optional[list[str]] = None,
         base_dir: Optional[str] = None) -> None:
    """快速检查：词法、语法、inm、未定义名字、代码生成约束；不调用 C++ 编译器。"""
    compile_source(source, path=path, overlay=overlay, include_dirs=include_dirs,
                   base_dir=base_dir)


# --------------------------------------------------------------------------
# C++ 编译器查找
# --------------------------------------------------------------------------
@dataclass
class Compiler:
    path: str
    kind: str = "gnu"          # gnu / msvc
    vcvars: Optional[str] = None
    label: str = ""


def _extra_compiler_dirs() -> list[str]:
    if os.name == "nt":
        home = os.path.expanduser("~")
        return [
            r"C:\msys64\mingw64\bin", r"C:\msys64\ucrt64\bin",
            r"C:\mingw64\bin", r"C:\mingw32\bin",
            r"C:\Program Files\LLVM\bin",
            os.path.join(home, r"scoop\apps\mingw\current\bin"),
            os.path.join(home, r"scoop\apps\llvm\current\bin"),
        ]
    return ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]


def _find_msvc() -> Optional[Compiler]:
    if os.name != "nt":
        return None
    vswhere = os.path.join(
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        "Microsoft Visual Studio", "Installer", "vswhere.exe",
    )
    if not os.path.isfile(vswhere):
        return None
    try:
        proc = subprocess.run(
            [vswhere, "-latest", "-products", "*",
             "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
             "-property", "installationPath"],
            capture_output=True, text=True, timeout=20,
            creationflags=_subprocess_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    install = (proc.stdout or "").strip().splitlines()
    if not install:
        return None
    base = install[0].strip()
    vcvars = os.path.join(base, "VC", "Auxiliary", "Build", "vcvars64.bat")
    tools = os.path.join(base, "VC", "Tools", "MSVC")
    try:
        versions = sorted(os.listdir(tools), reverse=True)
    except OSError:
        versions = []
    for version in versions:
        cl = os.path.join(tools, version, "bin", "Hostx64", "x64", "cl.exe")
        if os.path.isfile(cl):
            return Compiler(cl, "msvc", vcvars if os.path.isfile(vcvars) else None,
                            f"MSVC（{base}）")
    return None


def find_compiler() -> Compiler:
    """按 HOMO_CXX、g++、clang++、c++、clang-cl、cl 的顺序查找。"""
    override = os.environ.get("HOMO_CXX")
    if override:
        path = override if os.path.isfile(override) else shutil.which(override)
        if not path:
            raise HomoError(f"HOMO_CXX 指定的编译器不存在：{override}", 0)
        kind = "msvc" if os.path.basename(path).lower() in ("cl.exe", "clang-cl.exe") else "gnu"
        return Compiler(path, kind, None, override)

    search_path = os.pathsep.join(
        [p for p in [os.environ.get("PATH", "")] + _extra_compiler_dirs() if p]
    )
    for name in ("g++", "clang++", "c++", "clang-cl", "cl"):
        path = shutil.which(name, path=search_path)
        if path:
            kind = "msvc" if os.path.basename(path).lower() in ("cl.exe", "clang-cl.exe") else "gnu"
            return Compiler(path, kind, None, f"{name}（{path}）")
    msvc = _find_msvc()
    if msvc is not None:
        return msvc
    raise HomoError(
        "没有找到 C++ 编译器。请安装支持 C++17 的编译器："
        "Windows 可安装 MinGW-w64（g++）或 Visual Studio；macOS 可安装 Xcode Command Line Tools（clang++）",
        0,
    )


def _subprocess_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _run_cpp_compiler(compiler: Compiler, workdir: str, cpp_path: str, exe: str):
    flags = _subprocess_flags()
    if compiler.kind == "msvc":
        cpp_name = os.path.basename(cpp_path)
        exe_name = os.path.basename(exe)
        cline = (f'"{compiler.path}" /nologo /std:c++17 /EHsc /O2 /utf-8 '
                 f'"{cpp_name}" /Fe:"{exe_name}"')
        if compiler.vcvars:
            # 通过 cmd 先加载 VS 环境；命令走 Unicode 命令行，避免批处理编码问题。
            cmdline = f'call "{compiler.vcvars}" >nul && {cline}'
            argv = ["cmd.exe", "/d", "/c", cmdline]
        else:
            # cl.exe 已在 PATH 上（通常已经进过 VS 环境），直接调用避免 cmd 引号问题。
            argv = [compiler.path, "/nologo", "/std:c++17", "/EHsc", "/O2", "/utf-8",
                    cpp_name, f"/Fe:{exe_name}"]
        encoding = locale.getpreferredencoding(False) or "utf-8"
        return subprocess.run(
            argv, cwd=workdir,
            capture_output=True, text=True, encoding=encoding, errors="replace",
            timeout=120, creationflags=flags,
        )
    args = [compiler.path, "-std=c++17", "-O0", "-o", exe, cpp_path]
    if os.name == "nt":
        # MinGW 默认按系统代码页处理源码/字面量；显式锁定 UTF-8，
        # 中文标识符和字符串在 Windows 上才不会乱码。
        args[1:1] = ["-finput-charset=UTF-8", "-fexec-charset=UTF-8"]
    return subprocess.run(
        args,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, creationflags=flags,
    )


# --------------------------------------------------------------------------
# 编译 / 运行
# --------------------------------------------------------------------------
CPP_DIAG_RE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+)(?::(?P<col>\d+))?:\s*"
    r"(?P<sev>fatal error|error|warning|note|错误|警告|注意)\s*[:：]?\s*(?P<msg>.*)$",
    re.IGNORECASE,
)
SEVERITY_LABELS = {
    "error": "错误", "fatal error": "错误", "错误": "错误",
    "warning": "警告", "警告": "警告",
    "note": "提示", "注意": "提示",
}


def rewrite_diagnostics(text: str, linemap: list[int],
                        source_map: Optional[SourceMap] = None) -> str:
    text = re.sub(r"[A-Za-z]:[\\/][^\s:]*main\.cpp", "main.cpp", text)
    text = re.sub(r"\S*[\\/]main\.cpp", "main.cpp", text)

    def location(n: int) -> Optional[tuple[str, int]]:
        if not (0 < n <= len(linemap)) or not linemap[n - 1]:
            return None
        if source_map is None:
            return None
        file, line = source_map.resolve(linemap[n - 1])
        return (file, line) if file else None

    def repl_paren(m: re.Match) -> str:
        loc = location(int(m.group(1)))
        if loc is None:
            return m.group(0)
        col = m.group(2)
        return f"{loc[0]}:{loc[1]}" + (f":{col}" if col else "")

    text = re.sub(r"main\.cpp\((\d+)(?:,(\d+))?\)", repl_paren, text)

    def repl_colon(m: re.Match) -> str:
        loc = location(int(m.group(1)))
        if loc is None:
            return m.group(0)
        col = m.group(2)
        return f"{loc[0]}:{loc[1]}" + (f":{col}" if col else "")

    text = re.sub(r"main\.cpp:(\d+)(?::(\d+))?", repl_colon, text)

    kept = []
    for line in text.splitlines():
        if re.match(r"^\s*\d+\s*\|", line):      # 生成代码的行号片段
            continue
        if re.match(r"^\s*\|", line):            # 插入符 / 波浪线片段
            continue
        if re.match(r"^\s*\^+", line):
            continue
        kept.append(line)
    return "\n".join(kept)


def cpp_diagnostics(text: str, source_map: Optional[SourceMap] = None) -> list[dict]:
    out: list[dict] = []
    for raw in text.splitlines():
        m = CPP_DIAG_RE.match(raw.strip())
        if not m:
            continue
        sev_raw = m.group("sev").lower()
        severity = SEVERITY_LABELS.get(sev_raw, "错误")
        file = m.group("file")
        path = None
        if source_map is not None:
            path = source_map.path_for_display(file)
        out.append({
            "severity": severity,
            "file": file,
            "path": path or "",
            "line": int(m.group("line")),
            "column": int(m.group("col") or 0),
            "message": m.group("msg").strip(),
        })
    return out


def build(source=None, workdir: Optional[str] = None, *,
          compiled: Optional[Compiled] = None,
          path: Optional[str] = None,
          overlay: Optional[dict[str, str]] = None,
          include_dirs: Optional[list[str]] = None,
          base_dir: Optional[str] = None) -> dict:
    if isinstance(source, Compiled):
        compiled = source
    if compiled is None:
        compiled = compile_source(source or "", path=path, overlay=overlay,
                                  include_dirs=include_dirs, base_dir=base_dir)
    cpp, types, linemap, sm = compiled.cpp, compiled.types, compiled.linemap, compiled.source_map
    auto_workdir = workdir is None
    if workdir is None:
        workdir = tempfile.mkdtemp(prefix="homo_build_")
    cpp_path = os.path.join(workdir, "main.cpp")
    with open(cpp_path, "w", encoding="utf-8") as f:
        f.write(cpp)
    shutil.copyfile(RUNTIME_PATH, os.path.join(workdir, "homo_runtime.hpp"))
    exe = os.path.join(workdir, "homo_program.exe" if os.name == "nt" else "homo_program")
    try:
        compiler = find_compiler()
        proc = _run_cpp_compiler(compiler, workdir, cpp_path, exe)
    except subprocess.TimeoutExpired:
        return {
            "ok": False, "cpp": cpp, "types": types, "linemap": linemap,
            "source_map": sm, "units": compiled.units, "diagnostics": [],
            "compile_output": "编译超时（超过 120 秒）", "workdir": workdir, "exe": None,
            "compiler": None,
        }
    except HomoError:
        if auto_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
        raise
    except OSError as exc:
        return {
            "ok": False, "cpp": cpp, "types": types, "linemap": linemap,
            "source_map": sm, "units": compiled.units, "diagnostics": [],
            "compile_output": f"无法启动 C++ 编译器：{exc}", "workdir": workdir,
            "exe": None, "compiler": None,
        }
    raw_output = (proc.stdout or "") + (proc.stderr or "")
    output = rewrite_diagnostics(raw_output, linemap, sm).strip()
    ok = proc.returncode == 0
    if ok and not os.path.isfile(exe):
        ok = False
        output = (output + "\n编译器没有生成可执行文件").strip()
    diagnostics = cpp_diagnostics(output, sm)
    if not ok and not diagnostics:
        diagnostics = [{
            "severity": "错误", "file": "", "path": "", "line": 0, "column": 0,
            "message": "C++ 编译失败（编译器没有给出可解析的错误信息）",
        }]
    return {
        "ok": ok, "cpp": cpp, "types": types, "linemap": linemap,
        "source_map": sm, "units": compiled.units, "diagnostics": diagnostics,
        "compile_output": output, "workdir": workdir, "exe": exe if ok else None,
        "compiler": compiler.label,
    }


def run(source: str, timeout: Optional[int] = 5, input_text: Optional[str] = "",
        inherit_io: bool = False, on_start=None, *,
        path: Optional[str] = None,
        overlay: Optional[dict[str, str]] = None,
        include_dirs: Optional[list[str]] = None,
        base_dir: Optional[str] = None,
        keep_workdir: bool = False) -> dict:
    result = build(source, path=path, overlay=overlay, include_dirs=include_dirs,
                   base_dir=base_dir)
    if not result["ok"]:
        result.update({"ran": False, "stdout": "", "stderr": "", "exit_code": None})
        if not keep_workdir:
            shutil.rmtree(result["workdir"], ignore_errors=True)
        return result
    if inherit_io:
        # 交互模式：直接继承终端，输出实时可见。
        try:
            proc = subprocess.run([result["exe"]], timeout=timeout, input=input_text)
            result.update({
                "ran": True,
                "stdout": "",
                "stderr": "",
                "exit_code": proc.returncode,
            })
        except subprocess.TimeoutExpired:
            result.update({
                "ran": True,
                "stdout": "",
                "stderr": f"运行超时（超过 {timeout} 秒）",
                "exit_code": None,
            })
        if not keep_workdir:
            shutil.rmtree(result["workdir"], ignore_errors=True)
        return result

    proc: Optional[subprocess.Popen] = None
    try:
        proc = subprocess.Popen(
            [result["exe"]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        if on_start is not None:
            on_start(proc)
        out, err = proc.communicate(input=input_text, timeout=timeout)
        result.update({
            "ran": True,
            "stdout": out,
            "stderr": err,
            "exit_code": proc.returncode,
        })
    except subprocess.TimeoutExpired:
        out, err = "", ""
        if proc is not None:
            proc.kill()
            out, err = proc.communicate()
        result.update({
            "ran": True,
            "stdout": out,
            "stderr": f"运行超时（超过 {timeout} 秒）",
            "exit_code": None,
        })
    if not keep_workdir:
        shutil.rmtree(result["workdir"], ignore_errors=True)
    return result


# --------------------------------------------------------------------------
# 命令行入口
# --------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    if not sys.stdin.isatty():
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="homo 语言编译器：homo → C++ → 原生程序")
    ap.add_argument("source", nargs="?", help="homo 主源文件；不写则从标准输入读取")
    ap.add_argument("-o", "--output", help="把可执行文件复制到这个路径")
    ap.add_argument("-I", "--include", action="append", default=[],
                    help="import 的额外搜索目录，可以重复")
    ap.add_argument("--run", action="store_true", help="编译后立即运行")
    ap.add_argument("--check", action="store_true", help="只做词法/语法/名字检查")
    ap.add_argument("--keep", action="store_true", help="保留生成的 C++ 构建目录")
    args = ap.parse_args(argv)

    include_dirs = args.include
    if args.source:
        try:
            with open(args.source, "rb") as f:
                src = f.read().decode("utf-8-sig")
        except OSError as exc:
            print(f"读取源文件失败：{exc}", file=sys.stderr)
            return 2
        except UnicodeDecodeError as exc:
            print(f"源文件不是 UTF-8 编码：{exc}", file=sys.stderr)
            return 2
        source_path = os.path.abspath(args.source)
    else:
        src = sys.stdin.read()
        source_path = None

    try:
        if args.check:
            lint(src, path=source_path, include_dirs=include_dirs)
            print("检查通过")
            return 0

        if args.run:
            interactive = sys.stdin.isatty()
            timeout = None if interactive else 60
            result = run(src, timeout=timeout, input_text=None, inherit_io=interactive,
                         path=source_path, include_dirs=include_dirs,
                         keep_workdir=args.keep)
            if not result["ok"]:
                print(result["compile_output"], file=sys.stderr)
                if args.keep:
                    print(f"构建目录：{result['workdir']}", file=sys.stderr)
                else:
                    shutil.rmtree(result["workdir"], ignore_errors=True)
                return 51
            sys.stdout.write(result["stdout"])
            if result["stderr"]:
                sys.stderr.write(result["stderr"])
            sys.stdout.flush()
            if args.keep:
                print(f"构建目录：{result['workdir']}", file=sys.stderr)
            else:
                shutil.rmtree(result["workdir"], ignore_errors=True)
            code = result["exit_code"]
            return 0 if code is None else code

        if args.output:
            result = build(src, path=source_path, include_dirs=include_dirs)
            if not result["ok"]:
                print(result["compile_output"], file=sys.stderr)
                if args.keep:
                    print(f"构建目录：{result['workdir']}", file=sys.stderr)
                else:
                    shutil.rmtree(result["workdir"], ignore_errors=True)
                return 1
            try:
                shutil.copyfile(result["exe"], args.output)
                if os.name != "nt":
                    os.chmod(args.output, 0o755)
            except OSError as exc:
                print(f"写入可执行文件失败：{exc}", file=sys.stderr)
                return 1
            if args.keep:
                print(f"构建目录：{result['workdir']}", file=sys.stderr)
            else:
                shutil.rmtree(result["workdir"], ignore_errors=True)
            print(f"已生成：{args.output}")
            return 0

        compiled = compile_source(src, path=source_path, include_dirs=include_dirs)
        sys.stdout.write(compiled.cpp)
        return 0

    except HomoError as e:
        print(f"编译错误：{format_error(e)}", file=sys.stderr)
        return 114


if __name__ == "__main__":
    sys.exit(main())
