#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""homo 语言编译器：homo → C++ → g++。

homo 源码里所有变量都用「你是一个一个一个」声明，不写类型；
编译器负责自动推导变量类型（整数 / 浮点数 / 字符串），
并把「事」「便乘」等词形算子翻译成 C++ 调用。
"""

from __future__ import annotations

import argparse
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
NULL_KW = "虚无"                  # 空值
NOT_KW = "迫真"                   # 逻辑非
LT_KW = "甚至"                    # <
AND_KW = "且"                     # 逻辑与
EQ_KW = "就事"                    # ==
OR_KW = "或"                      # 逻辑或
MOD_KW = "撅"                     # 取模
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
INIT_KW = "初始化"                # 新建对象时自动调用的方法
# 字符串内置函数：名字 -> (最少参数, 最多参数)
STRING_BUILTIN_ARITY = {
    "取子串": (2, 3), "查找": (2, 2), "包含": (2, 2), "替换": (3, 3),
    "分割": (2, 2), "连接": (2, 2), "去空白": (1, 1),
    "转数字": (1, 1), "转大写": (1, 1), "转小写": (1, 1),
}

COMPOUND_OPS = {"便乘": "*", "便减": "-", "便加": "+", "便除": "/"}
ASSIGN_OPS = {ASSIGN} | set(COMPOUND_OPS)
PUNCTUATION = set("{}()[],;+-*/.")
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
FULLWIDTH_PUNC = {"（": "(", "）": ")", "，": ","}
PUNCTUATION |= set(FULLWIDTH_PUNC)


class HomoError(Exception):
    def __init__(self, msg: str, line: int = 0):
        super().__init__(msg)
        self.msg = msg
        self.line = line


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


@dataclass
class Class:
    name: str
    fields: list[Decl]
    methods: list[Func]
    line: int = 0


@dataclass
class Program:
    functions: list[Func]
    classes: list[Class] = field(default_factory=list)


# --------------------------------------------------------------------------
# 语法分析
# --------------------------------------------------------------------------
class Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0

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
        while not self.at_end():
            if self.at_id("import"):
                self.consume()
                self.expect_id()
                if self.at_punc(";"):
                    self.consume()
                continue
            if self.at_punc(";"):
                self.consume()
                continue
            if self.at_id(CLASS_KW):
                classes.append(self.parse_class())
                continue
            t = self.peek()
            nxt = self.peek(1)
            if t is not None and t.kind == "id" and nxt is not None and nxt.kind == "punc" and nxt.text == "(":
                funcs.append(self.parse_function())
            else:
                raise HomoError("顶层只能写 import、类定义或函数定义（函数名后要跟括号）", t.line if t else 1)
        if not funcs:
            raise HomoError("程序里至少要有一个函数（入口请命名为「会员制餐厅」）", 1)
        return Program(funcs, classes)

    def parse_class(self) -> Class:
        head = self.expect_id(CLASS_KW)
        name = self.expect_id()
        self.expect_punc("{")
        fields: list[Decl] = []
        methods: list[Func] = []
        while not self.at_punc("}"):
            if self.at_end():
                raise HomoError("类定义没有闭合（缺少 }）", self.last_line())
            if self.at_punc(";"):
                self.consume()
                continue
            if self.at_id(DECL):
                fields.append(self.parse_decl())
                continue
            t = self.peek()
            nxt = self.peek(1)
            if t is not None and t.kind == "id" and nxt is not None and nxt.kind == "punc" and nxt.text == "(":
                methods.append(self.parse_function())
                continue
            raise HomoError("类体里只能写字段声明（你是一个一个一个）或方法", t.line if t else self.last_line())
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
                    params.append(Param(name.text, None, True, marker.line))
                    if self.at_punc(","):
                        raise HomoError("可变参数只能用一次，而且要写在参数列表最后", marker.line)
                    break
                name = self.expect_id()
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
            if nxt is not None and nxt.kind == "id" and nxt.text == "=":
                raise HomoError("homo 的赋值要写成「事 … 罢」，「=」不是赋值符号", t.line)
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
            if tok is not None and tok.text == "=":
                raise HomoError("homo 的赋值要写成「事 … 罢」，「=」不是赋值符号", target.line)
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
            elif self.at_id(EQ_KW):
                t = self.consume()
                left = Eq(left, self.parse_add(), t.line)
            elif self.at_id("与") or self.at_id("完全一致"):
                raise HomoError("等值比较要写成「x 就事 y」", self.peek(0).line)
            elif self.at_id("和"):
                raise HomoError("逻辑与要写成「x 且 y」", self.peek(0).line)
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
                return BoolLit(t.line)
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
            expr = sub.parse_expr()
            if not sub.at_end():
                raise HomoError("模板里的表达式无法解析", line)
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
            return "int"
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


def param_arity(params: list[Param]) -> tuple[int, Optional[int]]:
    """返回 (必填个数, 最多个数)；可变参数没有上限，最多个数为 None。"""
    required = 0
    for p in params:
        if p.variadic:
            return required, None
        if p.default is None:
            required += 1
    return required, len(params)


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


def check_names(program: Program) -> None:
    function_names = {f.name for f in program.functions}
    functions_by_name = {f.name: f for f in program.functions}
    class_names = {c.name for c in program.classes}
    classes_by_name = {c.name: c for c in program.classes}
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
            init = next((m for m in cls.methods if m.name == INIT_KW), None)
            if init is None:
                if e.args:
                    raise HomoError(
                        f"类「{e.class_name}」没有「{INIT_KW}」，新建时不能带参数", e.line
                    )
            else:
                required, maximum = param_arity(init.params)
                if len(e.args) < required or (maximum is not None
                                              and len(e.args) > maximum):
                    raise HomoError(
                        arity_message("函数", f"{e.class_name}.{INIT_KW}",
                                      init.params, len(e.args)),
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
    for f in program.functions:
        for p in f.params:
            if p.default is not None:
                # 默认值在调用点求值，检查时不能看到参数和调用方局部变量。
                check_expr(p.default, Scopes(), global_calls)
        check_stmts(f.body, Scopes({p.name for p in f.params}), global_calls)
    for c in program.classes:
        field_names = {fld.name for fld in c.fields}
        for fld in c.fields:
            for size in fld.sizes:
                check_expr(size, Scopes(field_names), global_calls)
            check_expr(fld.init, Scopes(field_names), global_calls)
        method_calls = global_calls | {m.name for m in c.methods}
        for m in c.methods:
            for p in m.params:
                if p.default is not None:
                    check_expr(p.default, Scopes(), global_calls)
            check_stmts(m.body, Scopes(field_names, {p.name for p in m.params}),
                        method_calls)


# --------------------------------------------------------------------------
# C++ 代码生成
# --------------------------------------------------------------------------
class CEmitter:
    def __init__(self, program: Program):
        self.program = program
        self.lines: list[tuple[str, int]] = []   # (代码行, homo 源码行)
        self.indent = 0
        self.loop_counter = 0
        # 块级作用域：名字 -> "scalar" / "array" / "unknown"
        self.scopes: list[dict[str, str]] = []
        self.repeat_stack: list[int] = []
        self.break_targets: list[Optional[str]] = []
        self.functions = {f.name: f for f in program.functions}
        self.classes = {c.name: c for c in program.classes}

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

    def emit(self) -> tuple[str, list[int]]:
        self.add('#include "homo_runtime.hpp"', 0)
        self.blank()
        if self.program.functions:
            for f in self.program.functions:
                params = ", ".join(f"homo_any {cxx_name(p.name)}" for p in f.params)
                self.add(f"homo_any {cxx_fn(f.name)}({params});", f.line)
            self.blank()
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
        self.add("std::setvbuf(stdout, nullptr, _IONBF, 0);", 0)
        self.add("#else", 0)
        self.add("std::setvbuf(stdout, nullptr, _IOLBF, 0);", 0)
        self.add("#endif", 0)
        # 入口函数的返回值作为进程退出码；没有返回值时是 0。
        self.add(f"return (int){cxx_fn(entry.name)}().as_int();", 0)
        self.indent -= 1
        self.add("}", 0)
        text = "\n".join(t for t, _ in self.lines) + "\n"
        linemap = [ln for _, ln in self.lines]
        return text, linemap

    def pick_entry(self) -> Func:
        names = [f.name for f in self.program.functions]
        entry = "会员制餐厅" if "会员制餐厅" in names else names[0]
        f = next(x for x in self.program.functions if x.name == entry)
        if f.params:
            raise HomoError("入口函数不能带参数", f.line)
        return f

    def emit_function(self, f: Func) -> None:
        params = ", ".join(f"homo_any {cxx_name(p.name)}" for p in f.params)
        self.add(f"homo_any {cxx_fn(f.name)}({params}) {{", f.line)
        self.indent += 1
        # 参数在编译期不知道装的是标量还是数组，两种形式都放行。
        self.scopes = [self.param_frame(f.params)]
        self.repeat_stack = []
        self.break_targets = []
        for s in f.body:
            self.emit_stmt(s)
        if not f.body or not isinstance(f.body[-1], Return):
            self.add("return homo_value();", f.line)
        self.indent -= 1
        self.add("}", f.line)

    def emit_class(self, c: Class) -> None:
        self.add(f"struct {cxx_name(c.name)} : homo_object {{", c.line)
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
        for m in c.methods:
            required, maximum = param_arity(m.params)
            cond = f"_name == {cpp_quote(m.name)}"
            if m.params:
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
            if not m.params:
                continue
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
        for fld in c.fields:
            self.add(f"if (_name == {cpp_quote(fld.name)}) return {cxx_name(fld.name)};", fld.line)
        self.add('homo_runtime_error(_homo_line, "对象没有字段「" + _name + "」");', c.line)
        self.indent -= 1
        self.add("}", c.line)
        self.add("void set_field(const std::string& _name, homo_any _v, "
                 "long long _homo_line) override {", c.line)
        self.indent += 1
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
        self.scopes = [fields, self.param_frame(m.params)]
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
            return "homo_value(true)"
        if isinstance(e, NullLit):
            return "homo_null()"
        if isinstance(e, Ident):
            return cxx_name(e.name)
        if isinstance(e, New):
            # 显式写全局作用域：局部变量可以和类同名，不能让它遮住构造函数。
            obj = f"homo_object_of(::{cxx_name(e.class_name)}())"
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


def cxx_fn(name: str) -> str:
    return cxx_name(name)


# --------------------------------------------------------------------------
# 编译 / 运行
# --------------------------------------------------------------------------
RUNTIME_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "homo_runtime.hpp")


def transpile(source: str) -> tuple[str, list[dict], list[int]]:
    tokens = tokenize(source)
    program = Parser(tokens).parse_program()
    check_names(program)
    types = analyze_types(program)
    cpp, linemap = CEmitter(program).emit()
    return cpp, types, linemap


def lint(source: str) -> None:
    """快速检查：词法、语法、未定义名字、代码生成约束；不调用 C++ 编译器。"""
    transpile(source)


def find_compiler() -> str:
    for name in ("g++", "clang++", "c++"):
        path = shutil.which(name)
        if path:
            return path
    raise HomoError("没有找到 C++ 编译器（需要 g++ 或 clang++）", 0)


def rewrite_diagnostics(text: str, linemap: list[int]) -> str:
    text = re.sub(r"\S*/main\.cpp", "main.cpp", text)

    def repl(m: re.Match) -> str:
        n = int(m.group(1))
        homo_line = linemap[n - 1] if 0 < n <= len(linemap) else 0
        return f"homo 源码第 {homo_line} 行" if homo_line else m.group(0)

    text = re.sub(r"main\.cpp:(\d+)(?::\d+)?", repl, text)
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


def build(source: str, workdir: Optional[str] = None) -> dict:
    cpp, types, linemap = transpile(source)
    if workdir is None:
        workdir = tempfile.mkdtemp(prefix="homo_build_")
    cpp_path = os.path.join(workdir, "main.cpp")
    with open(cpp_path, "w", encoding="utf-8") as f:
        f.write(cpp)
    shutil.copyfile(RUNTIME_PATH, os.path.join(workdir, "homo_runtime.hpp"))
    exe = os.path.join(workdir, "homo_program")
    compiler = find_compiler()
    try:
        proc = subprocess.run(
            [compiler, "-std=c++17", "-O0", "-o", exe, cpp_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False, "cpp": cpp, "types": types, "linemap": linemap,
            "compile_output": "编译超时（超过 60 秒）", "workdir": workdir, "exe": None,
        }
    output = rewrite_diagnostics((proc.stdout or "") + (proc.stderr or ""), linemap).strip()
    ok = proc.returncode == 0
    return {
        "ok": ok, "cpp": cpp, "types": types, "linemap": linemap,
        "compile_output": output, "workdir": workdir, "exe": exe if ok else None,
    }


def run(source: str, timeout: Optional[int] = 5, input_text: Optional[str] = "",
        inherit_io: bool = False, on_start=None) -> dict:
    result = build(source)
    if not result["ok"]:
        result.update({"ran": False, "stdout": "", "stderr": "", "exit_code": None})
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
    return result


# --------------------------------------------------------------------------
# 命令行入口
# --------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    if not sys.stdin.isatty():
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="homo 语言编译器：homo → C++ → g++")
    ap.add_argument("source", nargs="?", help="homo 源文件；不写则从标准输入读取")
    ap.add_argument("-o", "--output", help="把可执行文件复制到这个路径")
    ap.add_argument("--run", action="store_true", help="编译后立即运行")
    ap.add_argument("--keep", action="store_true", help="保留生成的 C++ 构建目录")
    args = ap.parse_args(argv)

    if args.source:
        with open(args.source, encoding="utf-8") as f:
            src = f.read()
    else:
        src = sys.stdin.read()

    try:
        if args.run:
            interactive = sys.stdin.isatty()
            timeout = None if interactive else 60
            result = run(src, timeout=timeout, input_text=None, inherit_io=interactive)
            if not result["ok"]:
                print(result["compile_output"], file=sys.stderr)
                if args.keep:
                    print(f"构建目录：{result['workdir']}", file=sys.stderr)
                return 1
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
            result = build(src)
            if not result["ok"]:
                print(result["compile_output"], file=sys.stderr)
                if args.keep:
                    print(f"构建目录：{result['workdir']}", file=sys.stderr)
                return 1
            shutil.copyfile(result["exe"], args.output)
            os.chmod(args.output, 0o755)
            if not args.keep:
                shutil.rmtree(result["workdir"], ignore_errors=True)
            print(f"已生成：{args.output}")
            return 0

        cpp, _, _ = transpile(src)
        sys.stdout.write(cpp)
        return 0

    except HomoError as e:
        where = f"（第 {e.line} 行）" if e.line else ""
        print(f"编译错误{where}：{e.msg}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
