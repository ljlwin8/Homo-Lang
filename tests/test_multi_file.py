#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""inm .homo 导入与选择性类导入的回归测试。

运行：python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import homo  # noqa: E402

try:
    homo.find_compiler()
    HAVE_COMPILER = True
except homo.HomoError:
    HAVE_COMPILER = False

NEEDS_COMPILER = unittest.skipUnless(HAVE_COMPILER, "没有可用的 C++ 编译器")


class InmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="homo_test_")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    @staticmethod
    def read(path: str) -> str:
        with open(path, encoding="utf-8") as f:
            return f.read()

    def write(self, name: str, source: str) -> str:
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path) or self.tmp, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(textwrap.dedent(source).lstrip("\n"))
        return path

    @NEEDS_COMPILER
    def test_basic_inm_and_runtime(self) -> None:
        self.write("库.homo", """
            你是一个一个一个 库名字 事 "库" 罢
            问候(地方 事 "下北泽"){
                ("你好，" + 地方)人幸终
            }
            类 计数器 {
                你是一个一个一个 总数 事 0 罢
                首(初值 事 0){ 总数 事 初值 罢 }
                加(n){ 总数 便加 n 罢 (总数)人幸终 }
            }
        """)
        main = self.write("main.homo", """
            inm 库
            会员制餐厅(){
                脱出(库名字)噗叽啪
                脱出(问候("北京"))噗叽啪
                你是一个一个一个 计 事 新建 计数器(10) 罢
                脱出(计.加(5))噗叽啪
                (0)人幸终
            }
        """)
        result = homo.run(self.read(main), path=main, timeout=30)
        self.assertTrue(result["ok"], result["compile_output"])
        self.assertIn("库", result["stdout"])
        self.assertIn("你好，北京", result["stdout"])
        self.assertIn("15", result["stdout"])

    @NEEDS_COMPILER
    def test_nested_inm_and_dedup(self) -> None:
        self.write("工具.homo", "平方(n){ ((n) * (n))人幸终 }\n")
        self.write("库.homo", """
            inm 工具
            立方(n){ (平方(n) * n)人幸终 }
        """)
        main = self.write("main.homo", """
            inm 库
            inm 库.homo
            inm 工具
            会员制餐厅(){
                脱出(立方(3))噗叽啪
                (0)人幸终
            }
        """)
        result = homo.run(self.read(main), path=main, timeout=30)
        self.assertTrue(result["ok"], result["compile_output"])
        self.assertIn("27", result["stdout"])

    def test_relative_dot_slash_inm(self) -> None:
        self.write("lib/a.homo", "你是一个一个一个 点值 事 5 罢\n")
        main = self.write("main.homo", """
            inm ./lib/a.homo
            会员制餐厅(){ 脱出(点值)噗叽啪 (0)人幸终 }
        """)
        compiled = homo.compile_source(self.read(main), path=main)
        self.assertIn("点值", compiled.cpp)
        self.assertIn("lib/a.homo", compiled.cpp)

    def test_inm_homocore_is_not_builtin(self) -> None:
        main = self.write("main.homo", """
            inm homocore
            会员制餐厅(){ (0)人幸终 }
        """)
        with self.assertRaises(homo.HomoError) as ctx:
            homo.compile_source(self.read(main), path=main)
        self.assertIn("找不到 inm「homocore」", homo.format_error(ctx.exception))

    def test_missing_inm(self) -> None:
        main = self.write("main.homo", """
            inm 不存在
            会员制餐厅(){ (0)人幸终 }
        """)
        with self.assertRaises(homo.HomoError) as ctx:
            homo.compile_source(self.read(main), path=main)
        message = homo.format_error(ctx.exception)
        self.assertIn("找不到 inm「不存在」", message)
        self.assertIn("main.homo:1", message)

    def test_cycle(self) -> None:
        self.write("a.homo", "inm b\n")
        self.write("b.homo", "inm a\n")
        main = self.write("main.homo", """
            inm a
            会员制餐厅(){ (0)人幸终 }
        """)
        with self.assertRaises(homo.HomoError) as ctx:
            homo.compile_source(self.read(main), path=main)
        self.assertIn("循环 inm", homo.format_error(ctx.exception))

    def test_duplicate_function_definition(self) -> None:
        main = self.write("main.homo", """
            同名(){ (1)人幸终 }
            同名(){ (2)人幸终 }
            会员制餐厅(){ (0)人幸终 }
        """)
        with self.assertRaises(homo.HomoError) as ctx:
            homo.compile_source(self.read(main), path=main)
        self.assertIn("函数「同名」重复定义", homo.format_error(ctx.exception))

    def test_error_line_in_imported_file(self) -> None:
        self.write("库.homo", """
            // 第 1 行
            坏函数(){ (未知 + 1)人幸终 }
        """)
        main = self.write("main.homo", """
            inm 库
            会员制餐厅(){ (0)人幸终 }
        """)
        with self.assertRaises(homo.HomoError) as ctx:
            homo.compile_source(self.read(main), path=main)
        message = homo.format_error(ctx.exception)
        self.assertIn("库.homo:2", message)
        self.assertIn("未定义的变量「未知」", message)

    @NEEDS_COMPILER
    def test_globals_initialize_in_dependency_order(self) -> None:
        self.write("库.homo", "你是一个一个一个 库值 事 41 罢\n")
        main = self.write("main.homo", """
            inm 库
            你是一个一个一个 主值 事 库值 + 1 罢
            会员制餐厅(){ 脱出(主值)噗叽啪 (0)人幸终 }
        """)
        result = homo.run(self.read(main), path=main, timeout=30)
        self.assertTrue(result["ok"], result["compile_output"])
        self.assertIn("42", result["stdout"])

        bad = self.write("bad_main.homo", """
            inm 库
            你是一个一个一个 主值 事 1 罢
            会员制餐厅(){ (0)人幸终 }
        """)
        badlib = self.write("badlib.homo", "你是一个一个一个 库值 事 主值 + 1 罢\n")
        bad_src = self.read(bad).replace("inm 库", "inm badlib")
        bad2 = self.write("bad2.homo", bad_src)
        with self.assertRaises(homo.HomoError) as ctx:
            homo.compile_source(self.read(bad2), path=bad2)
        self.assertIn("还没声明", homo.format_error(ctx.exception))

    def test_check_source_allows_library_without_entry(self) -> None:
        source = """
            类 空类 { 试探(){ (1)人幸终 } }
            你是一个一个一个 常量 事 7 罢
        """
        path = os.path.join(self.tmp, "onlyimpl.homo")
        homo.check_source(source, path=path, require_function=False)

    def test_check_source_reports_syntax_error(self) -> None:
        source = "坏函数(){\n    (1)人幸终\n"
        path = os.path.join(self.tmp, "bad.homo")
        with self.assertRaises(homo.HomoError) as ctx:
            homo.check_source(source, path=path, require_function=False)
        self.assertIn("代码块没有闭合", homo.format_error(ctx.exception))

    def test_entry_only_comes_from_main_homo(self) -> None:
        self.write("helper.homo", "工具入口(){ (1)人幸终 }\n")
        main = self.write("main.homo", """
            inm helper
            会员制餐厅(){ (0)人幸终 }
        """)
        compiled = homo.compile_source(self.read(main), path=main)
        self.assertIn("int _homo_code = (int)会员制餐厅()", compiled.cpp)

    def test_cannot_use_imported_entry(self) -> None:
        self.write("helper.homo", "会员制餐厅(){ (1)人幸终 }\n")
        main = self.write("main.homo", """
            inm helper
            普通函数(){ (0)人幸终 }
        """)
        with self.assertRaises(homo.HomoError) as ctx:
            homo.compile_source(self.read(main), path=main)
        self.assertIn("没有主函数入口", homo.format_error(ctx.exception))

    def test_class_external_implementation_rejected(self) -> None:
        main = self.write("main.homo", """
            计数器::总数 事 0 罢
            会员制餐厅(){ (0)人幸终 }
        """)
        with self.assertRaises(homo.HomoError) as ctx:
            homo.compile_source(self.read(main), path=main)
        self.assertIn("类外实现「类名::成员」已删除",
                      homo.format_error(ctx.exception))

    def test_old_import_keyword_rejected(self) -> None:
        main = self.write("main.homo", """
            import 库
            会员制餐厅(){ (0)人幸终 }
        """)
        with self.assertRaises(homo.HomoError) as ctx:
            homo.compile_source(self.read(main), path=main)
        self.assertIn("import 已改名为 inm", homo.format_error(ctx.exception))

    def test_inm_extension_rejected(self) -> None:
        main = self.write("main.homo", """
            inm 库.inm
            会员制餐厅(){ (0)人幸终 }
        """)
        with self.assertRaises(homo.HomoError) as ctx:
            homo.compile_source(self.read(main), path=main)
        self.assertIn("不再支持 .inm 文件", homo.format_error(ctx.exception))

    @NEEDS_COMPILER
    def test_same_homo_imported_twice_loads_once(self) -> None:
        self.write("common.homo", "f(){ (1)人幸终 }\n")
        # 这里故意让 common 同时被 a/b inm，验证文件级去重。
        self.write("a.homo", "inm common\n")
        self.write("b.homo", "inm common\n")
        main = self.write("main.homo", """
            inm a
            inm b
            会员制餐厅(){ 脱出(f())噗叽啪 (0)人幸终 }
        """)
        result = homo.run(self.read(main), path=main, timeout=30)
        self.assertTrue(result["ok"], result["compile_output"])
        self.assertIn("1", result["stdout"])

    @NEEDS_COMPILER
    def test_selective_class_import(self) -> None:
        self.write("类定义.homo", """
            类 A { 你是一个一个一个 x 事 1 罢 加(){ (x)人幸终 } }
            类 B { 你是一个一个一个 y 事 2 罢 加(){ (y)人幸终 } }
        """)
        main = self.write("main.homo", """
            inm A from 类定义
            会员制餐厅(){
                你是一个一个一个 a 事 新建 A() 罢
                脱出(a.加())噗叽啪
                (0)人幸终
            }
        """)
        result = homo.run(self.read(main), path=main, timeout=30)
        self.assertTrue(result["ok"], result["compile_output"])
        self.assertIn("1", result["stdout"])

        bad = self.write("bad.homo", """
            inm A from 类定义
            会员制餐厅(){
                你是一个一个一个 b 事 新建 B() 罢
                (0)人幸终
            }
        """)
        with self.assertRaises(homo.HomoError) as ctx:
            homo.compile_source(self.read(bad), path=bad)
        self.assertIn("未定义的类「B」", homo.format_error(ctx.exception))

    @NEEDS_COMPILER
    @NEEDS_COMPILER
    def test_selective_class_does_not_load_file_imports(self) -> None:
        self.write("数学.homo", "平方(n){ ((n) * (n))人幸终 }\n")
        self.write("全局.homo", '你是一个一个一个 库名字 事 "库" 罢\n')
        self.write("计数器.homo", """
            inm 数学
            inm 全局
            类 计数器 {
                你是一个一个一个 总数 事 0 罢
                首(初值 事 0){ 总数 事 初值 罢 }
                加(n){ 总数 便加 n 罢 (总数)人幸终 }
            }
        """)
        main = self.write("main.homo", """
            inm 计数器 from 计数器
            会员制餐厅(){
                你是一个一个一个 计 事 新建 计数器(10) 罢
                脱出(计.加(5))噗叽啪
                (0)人幸终
            }
        """)
        result = homo.run(self.read(main), path=main, timeout=30)
        self.assertTrue(result["ok"], result["compile_output"])
        self.assertIn("15", result["stdout"])

        bad = self.write("bad.homo", """
            inm 计数器 from 计数器
            会员制餐厅(){
                脱出(平方(3))噗叽啪
                (0)人幸终
            }
        """)
        with self.assertRaises(homo.HomoError) as ctx:
            homo.compile_source(self.read(bad), path=bad)
        self.assertIn("未定义的函数「平方」", homo.format_error(ctx.exception))

        full = self.write("full.homo", """
            inm 计数器
            会员制餐厅(){
                脱出(平方(3))噗叽啪
                脱出(库名字)噗叽啪
                (0)人幸终
            }
        """)
        result = homo.run(self.read(full), path=full, timeout=30)
        self.assertTrue(result["ok"], result["compile_output"])
        self.assertIn("9", result["stdout"])
        self.assertIn("库", result["stdout"])

    def test_selective_class_missing(self) -> None:
        self.write("类定义.homo", "类 A { 加(){ (1)人幸终 } }\n")
        main = self.write("main.homo", """
            inm 不存在 from 类定义
            会员制餐厅(){ (0)人幸终 }
        """)
        with self.assertRaises(homo.HomoError) as ctx:
            homo.compile_source(self.read(main), path=main)
        self.assertIn("没有类「不存在」", homo.format_error(ctx.exception))

    @NEEDS_COMPILER
    def test_full_and_selective_same_file_loads_each_class_once(self) -> None:
        self.write("类定义.homo", """
            类 A { 值(){ (1)人幸终 } }
            类 B { 值(){ (2)人幸终 } }
        """)
        # 先选择性导入 A，再整文件导入：整文件只补充 B，不能重复定义 A。
        main = self.write("main.homo", """
            inm A from 类定义
            inm 类定义
            会员制餐厅(){
                你是一个一个一个 a 事 新建 A() 罢
                你是一个一个一个 b 事 新建 B() 罢
                脱出(a.值())噗叽啪
                脱出(b.值())噗叽啪
                (0)人幸终
            }
        """)
        result = homo.run(self.read(main), path=main, timeout=30)
        self.assertTrue(result["ok"], result["compile_output"])
        self.assertIn("1", result["stdout"])
        self.assertIn("2", result["stdout"])

        # 反过来先整文件、再选择性导入：选择性导入应当是空操作。
        reverse = self.write("reverse.homo", """
            inm 类定义
            inm A from 类定义
            会员制餐厅(){
                你是一个一个一个 a 事 新建 A() 罢
                脱出(a.值())噗叽啪
                (0)人幸终
            }
        """)
        result = homo.run(self.read(reverse), path=reverse, timeout=30)
        self.assertTrue(result["ok"], result["compile_output"])

    def test_selective_class_still_checked_after_full_import(self) -> None:
        self.write("类定义.homo", "类 A { 值(){ (1)人幸终 } }\n")
        main = self.write("main.homo", """
            inm 类定义
            inm Missing from 类定义
            会员制餐厅(){ (0)人幸终 }
        """)
        with self.assertRaises(homo.HomoError) as ctx:
            homo.compile_source(self.read(main), path=main)
        self.assertIn("文件里没有类「Missing」", homo.format_error(ctx.exception))

    @NEEDS_COMPILER
    def test_multiple_imported_homo_files(self) -> None:
        self.write("全局.homo", '你是一个一个一个 库名字 事 "多文件" 罢\n')
        self.write("数学.homo", "平方(n){ ((n) * (n))人幸终 }\n")
        self.write("计数器.homo", """
            类 计数器 {
                你是一个一个一个 总数 事 10 罢
                加(n){ 总数 便加 n 罢 (总数)人幸终 }
            }
        """)
        main = self.write("main.homo", """
            inm 全局
            inm 数学
            inm 计数器 from 计数器
            会员制餐厅(){
                脱出(库名字)噗叽啪
                脱出(平方(9))噗叽啪
                你是一个一个一个 计 事 新建 计数器() 罢
                脱出(计.加(5))噗叽啪
                (0)人幸终
            }
        """)
        result = homo.run(self.read(main), path=main, timeout=30)
        self.assertTrue(result["ok"], result["compile_output"])
        self.assertIn("多文件", result["stdout"])
        self.assertIn("81", result["stdout"])
        self.assertIn("15", result["stdout"])

    def test_overlay_uses_unsaved_homo_source(self) -> None:
        lib = self.write("库.homo", '你是一个一个一个 值 事 "磁盘" 罢\n')
        main = self.write("main.homo", """
            inm 库
            会员制餐厅(){ 脱出(值)噗叽啪 (0)人幸终 }
        """)
        overlay = {os.path.realpath(lib): '你是一个一个一个 值 事 "内存" 罢\n'}
        compiled = homo.compile_source(self.read(main), path=main, overlay=overlay)
        self.assertIn("内存", compiled.cpp)
        self.assertNotIn("磁盘", compiled.cpp)

    def test_base_dir_for_untitled_source(self) -> None:
        self.write("库.homo", '你是一个一个一个 值 事 7 罢\n')
        compiled = homo.compile_source(
            "inm 库\n会员制餐厅(){ 脱出(值)噗叽啪 (0)人幸终 }\n",
            base_dir=self.tmp,
        )
        self.assertIn("值", compiled.cpp)

    @NEEDS_COMPILER
    def test_runtime_error_reports_imported_file(self) -> None:
        self.write("库.homo", """
            // 第 1 行
            坏函数(a, b){ (a / b)人幸终 }
        """)
        main = self.write("main.homo", """
            inm 库
            会员制餐厅(){ 脱出(坏函数(1, 0))噗叽啪 (0)人幸终 }
        """)
        result = homo.run(self.read(main), path=main, timeout=30)
        self.assertTrue(result["ok"], result["compile_output"])
        self.assertIn("运行错误（库.homo 第 2 行）", result["stderr"])
        self.assertIn("除数不能为 0", result["stderr"])


    def test_msvc_linker_error_is_parsed(self) -> None:
        diagnostics = homo.cpp_diagnostics(
            "main.obj : error LNK2019: unresolved external symbol \"foo\"\n"
            "LINK : fatal error LNK1104: cannot open file 'foo.lib'\n"
        )
        self.assertEqual(len(diagnostics), 2)
        self.assertEqual(diagnostics[0]["severity"], "错误")
        self.assertEqual(diagnostics[0]["file"], "main.obj")
        self.assertIn("LNK2019", diagnostics[0]["message"])
        self.assertEqual(diagnostics[1]["severity"], "错误")
        self.assertIn("LNK1104", diagnostics[1]["message"])
        self.assertEqual(diagnostics[1]["line"], 0)

    def test_msvc_command_line_error_is_parsed(self) -> None:
        diagnostics = homo.cpp_diagnostics(
            "cl : Command line error D8021 : invalid numeric argument '/bad'\n"
        )
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["file"], "cl")
        self.assertIn("D8021", diagnostics[0]["message"])

    def test_msvc_compile_error_maps_to_source(self) -> None:
        source_map = homo.SourceMap()
        source_map.add(None, "a\nb\nc\n")
        linemap = [0, 1, 3]
        rewritten = homo.rewrite_diagnostics(
            "main.cpp(3): error C2059: syntax error: ';'\n", linemap, source_map,
        )
        diagnostics = homo.cpp_diagnostics(rewritten, source_map)
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["line"], 3)
        self.assertEqual(diagnostics[0]["message"], "C2059: syntax error: ';'")

    def test_gbk_compiler_output_decodes(self) -> None:
        raw = "main.cpp(3): error C2059: 语法错误:“;”\n".encode("gbk")
        text = homo.decode_compiler_output(raw, "cp936")
        self.assertIn("语法错误", text)
        self.assertIn("main.cpp(3)", text)

    def test_unparseable_compiler_output_is_preserved(self) -> None:
        diagnostics = homo.raw_compiler_diagnostics(
            "'cl.exe' is not recognized as an internal or external command\n", 1,
        )
        self.assertEqual(len(diagnostics), 1)
        self.assertIn("cl.exe", diagnostics[0]["message"])
        empty = homo.raw_compiler_diagnostics("", 1)
        self.assertIn("退出代码 1", empty[0]["message"])


    def test_build_reports_msvc_linker_error(self) -> None:
        completed = subprocess.CompletedProcess(
            [], 1,
            b"main.obj : error LNK2019: unresolved external symbol \"foo\"\r\n",
            b"",
        )
        with mock.patch("homo.find_compiler",
                        return_value=homo.Compiler("cl.exe", "msvc", None, "MSVC")), \
                mock.patch("homo._run_cpp_compiler", return_value=completed):
            result = homo.build("会员制餐厅(){ (0)人幸终 }\n")
        self.assertFalse(result["ok"])
        self.assertTrue(result["diagnostics"])
        self.assertIn("LNK2019", result["diagnostics"][0]["message"])

    def test_build_preserves_unparseable_compiler_output(self) -> None:
        completed = subprocess.CompletedProcess(
            [], 1, b"'cl.exe' is not recognized\r\n", b"",
        )
        with mock.patch("homo.find_compiler",
                        return_value=homo.Compiler("cl.exe", "msvc", None, "MSVC")), \
                mock.patch("homo._run_cpp_compiler", return_value=completed):
            result = homo.build("会员制餐厅(){ (0)人幸终 }\n")
        self.assertFalse(result["ok"])
        self.assertIn("cl.exe", result["diagnostics"][0]["message"])
        self.assertNotIn("没有给出可解析", result["diagnostics"][0]["message"])


    def test_msvc_vcvars_is_passed_as_separate_argv(self) -> None:
        compiler = homo.Compiler(
            r"C:\Program Files\VS\VC\Tools\MSVC\bin\cl.exe",
            "msvc",
            r"C:\Program Files\VS\VC\Auxiliary\Build\vcvars64.bat",
            "MSVC",
        )
        completed = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch("homo.subprocess.run", return_value=completed) as run:
            homo._run_cpp_compiler(compiler, ".", "main.cpp", "homo.exe")
        argv = run.call_args.args[0]
        self.assertIn("call", argv)
        self.assertIn(compiler.vcvars, argv)
        self.assertFalse(any("call " in str(part) and "&&" in str(part)
                             for part in argv))


if __name__ == "__main__":
    unittest.main()
