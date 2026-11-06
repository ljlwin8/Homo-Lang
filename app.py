#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""homo 语言在线编辑器：本地 HTTP 服务。

启动后在浏览器打开 http://127.0.0.1:8765 。
页面用 /api/run 启动一个可交互会话，通过 /api/output 拉取实时输出、
/api/input 写入标准输入、/api/stop 终止进程、/api/close 释放构建目录。
"""

from __future__ import annotations

import atexit
import codecs
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import homo

ROOT = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(ROOT, "index.html")
MAX_BODY = 1_000_000
MAX_OUTPUT = 300_000   # 单次运行最多保留的输出字符数
MAX_INPUT = 4096

# 页面每次打开时载入的初始代码。
STARTER_SOURCE = """import homocore

会员制餐厅(){
    脱出("你好下北泽")噗叽啪
    (0)人幸终
}
"""


class RunSession:
    """一个正在运行的子进程：输出累积在内存里，标准输入保持打开。"""

    def __init__(self, sid: str, proc: subprocess.Popen, workdir: str) -> None:
        self.id = sid
        self.proc = proc
        self.workdir = workdir
        self.lock = threading.Lock()
        self.chunks: list[str] = []
        self.size = 0
        self.truncated = False
        self.done = False
        self.exit_code = None
        self.stopped = False
        self.touched = time.monotonic()

    def start(self) -> None:
        self._stdout_thread = threading.Thread(
            target=self._pump, args=(self.proc.stdout,), daemon=True)
        self._stderr_thread = threading.Thread(
            target=self._pump, args=(self.proc.stderr,), daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        threading.Thread(target=self._wait, daemon=True).start()

    def _pump(self, stream) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                self.append(decoder.decode(chunk))
            self.append(decoder.decode(b"", final=True))
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _wait(self) -> None:
        self.proc.wait()
        self._stdout_thread.join()
        self._stderr_thread.join()
        with self.lock:
            self.done = True
            self.exit_code = self.proc.returncode

    def append(self, text: str) -> None:
        if not text:
            return
        with self.lock:
            if self.truncated:
                return
            room = MAX_OUTPUT - self.size
            if room <= 0:
                self.truncated = True
                return
            if len(text) > room:
                marker = "\n[输出过多，已截断]\n"
                text = text[:max(0, room - len(marker))] + marker
                self.truncated = True
            self.chunks.append(text)
            self.size += len(text)

    def read_since(self, offset: int) -> dict:
        with self.lock:
            self.touched = time.monotonic()
            text = ""
            if 0 <= offset < self.size:
                text = "".join(self.chunks)[offset:]
            return {
                "text": text,
                "offset": self.size,
                "done": self.done,
                "exit_code": self.exit_code,
                "stopped": self.stopped,
                "truncated": self.truncated,
            }

    def write(self, text: str) -> None:
        with self.lock:
            self.touched = time.monotonic()
            done = self.done
        if done:
            raise ValueError("程序已经结束")
        try:
            self.proc.stdin.write(text.encode("utf-8"))
            self.proc.stdin.flush()
        except (OSError, ValueError):
            raise ValueError("程序已经结束")

    def kill(self) -> None:
        with self.lock:
            self.stopped = True
        try:
            self.proc.kill()
        except OSError:
            pass


SESSIONS: dict[str, RunSession] = {}
SESSIONS_LOCK = threading.Lock()
IDLE_LIMIT = 3600  # 一小时无人访问的会话会被回收


def prune_sessions() -> None:
    now = time.monotonic()
    stale: list[RunSession] = []
    with SESSIONS_LOCK:
        for sid, sess in list(SESSIONS.items()):
            if now - sess.touched > IDLE_LIMIT:
                stale.append(sess)
                SESSIONS.pop(sid, None)
    for sess in stale:
        sess.kill()
        shutil.rmtree(sess.workdir, ignore_errors=True)


def close_session(sid: str) -> None:
    with SESSIONS_LOCK:
        sess = SESSIONS.pop(sid, None)
    if sess is not None:
        sess.kill()
        shutil.rmtree(sess.workdir, ignore_errors=True)


def shutdown_sessions() -> None:
    with SESSIONS_LOCK:
        sessions = list(SESSIONS.values())
        SESSIONS.clear()
    for sess in sessions:
        sess.kill()
        shutil.rmtree(sess.workdir, ignore_errors=True)


atexit.register(shutdown_sessions)


class Handler(BaseHTTPRequestHandler):
    server_version = "HomoCompiler/1.0"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_body(self, code: int, body, content_type: str = "application/json; charset=utf-8") -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code: int, payload: dict) -> None:
        self.send_body(code, json.dumps(payload, ensure_ascii=False))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            try:
                with open(INDEX_PATH, "rb") as f:
                    data = f.read()
            except OSError as e:
                self.send_json(500, {"ok": False, "error": f"读取页面失败：{e}"})
                return
            self.send_body(200, data, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/sample":
            params = parse_qs(parsed.query)
            name = (params.get("name") or ["test.homo"])[0]
            if os.path.basename(name) != name or not name.endswith(".homo"):
                self.send_json(400, {"ok": False, "error": "示例文件名不合法"})
                return
            try:
                with open(os.path.join(ROOT, name), encoding="utf-8") as f:
                    source = f.read()
            except OSError:
                self.send_json(404, {"ok": False, "error": f"没有找到示例 {name}"})
                return
            self.send_json(200, {"source": source})
            return
        if parsed.path == "/api/starter":
            self.send_json(200, {"source": STARTER_SOURCE})
            return
        if parsed.path == "/api/output":
            params = parse_qs(parsed.query)
            sid = (params.get("sessionId") or [""])[0]
            try:
                offset = int((params.get("offset") or ["0"])[0])
            except ValueError:
                offset = 0
            with SESSIONS_LOCK:
                sess = SESSIONS.get(sid)
            if sess is None:
                self.send_json(404, {"ok": False, "error": "会话不存在或已经结束"})
                return
            self.send_json(200, {"ok": True, **sess.read_since(offset)})
            return
        self.send_json(404, {"ok": False, "error": "not found"})

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("请求头 Content-Length 不合法")
        if length <= 0:
            raise ValueError("请求体为空")
        if length > MAX_BODY:
            raise ValueError("请求过大")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("请求不是合法的 JSON")
        if not isinstance(data, dict):
            raise ValueError("请求必须是 JSON 对象")
        return data

    @staticmethod
    def require_source(data: dict) -> str:
        source = data.get("source", "")
        if not isinstance(source, str):
            raise ValueError("source 必须是字符串")
        return source

    @staticmethod
    def require_session(data: dict) -> RunSession:
        sid = data.get("sessionId", "")
        if not isinstance(sid, str) or not sid:
            raise ValueError("sessionId 不能为空")
        with SESSIONS_LOCK:
            sess = SESSIONS.get(sid)
        if sess is None:
            raise ValueError("会话不存在或已经结束")
        return sess

    def do_POST(self) -> None:
        if self.path not in ("/api/compile", "/api/check", "/api/run", "/api/stop",
                             "/api/input", "/api/close"):
            self.send_json(404, {"ok": False, "error": "not found"})
            return
        try:
            data = self.read_json()

            if self.path == "/api/check":
                homo.lint(self.require_source(data))
                self.send_json(200, {"ok": True, "phase": "lint"})
                return

            if self.path == "/api/compile":
                result = homo.build(self.require_source(data))
                try:
                    self.send_json(200, {
                        "ok": result["ok"],
                        "phase": "compile",
                        "cpp": result["cpp"],
                        "types": result["types"],
                        "compile_output": result["compile_output"],
                    })
                finally:
                    shutil.rmtree(result["workdir"], ignore_errors=True)
                return

            if self.path == "/api/run":
                prune_sessions()
                result = homo.build(self.require_source(data))
                if not result["ok"]:
                    self.send_json(200, {
                        "ok": False,
                        "phase": "compile",
                        "cpp": result["cpp"],
                        "types": result["types"],
                        "compile_output": result["compile_output"],
                    })
                    shutil.rmtree(result["workdir"], ignore_errors=True)
                    return
                proc = subprocess.Popen(
                    [result["exe"]],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                )
                sid = uuid.uuid4().hex
                sess = RunSession(sid, proc, result["workdir"])
                sess.start()
                with SESSIONS_LOCK:
                    SESSIONS[sid] = sess
                self.send_json(200, {
                    "ok": True,
                    "phase": "run",
                    "sessionId": sid,
                    "cpp": result["cpp"],
                    "types": result["types"],
                    "compile_output": result["compile_output"],
                })
                return

            if self.path == "/api/stop":
                sess = self.require_session(data)
                sess.kill()
                self.send_json(200, {"ok": True, "stopped": True})
                return

            if self.path == "/api/input":
                text = data.get("text", "")
                if not isinstance(text, str):
                    raise ValueError("text 必须是字符串")
                if len(text) > MAX_INPUT:
                    raise ValueError("单次输入过长")
                self.require_session(data).write(text)
                self.send_json(200, {"ok": True})
                return

            # /api/close
            sid = data.get("sessionId", "")
            if not isinstance(sid, str) or not sid:
                raise ValueError("sessionId 不能为空")
            close_session(sid)
            self.send_json(200, {"ok": True})
        except homo.HomoError as e:
            self.send_json(200, {
                "ok": False,
                "phase": "parse",
                "error": e.msg,
                "line": e.line,
            })
        except ValueError as e:
            self.send_json(400, {"ok": False, "error": str(e)})
        except Exception as e:  # noqa: BLE001 - 本地工具，直接把异常回给页面
            self.send_json(500, {"ok": False, "error": f"服务器内部错误：{e}"})


def main() -> None:
    port = 8765
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"端口不合法：{sys.argv[1]}", file=sys.stderr)
            sys.exit(2)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"homo 编译器已启动：http://127.0.0.1:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。", file=sys.stderr)


if __name__ == "__main__":
    main()
