# -*- coding: utf-8 -*-
"""
音乐解密转换器 —— 本地网页版 / 免安装打包版

支持两类加密格式：
  网易云音乐   .ncm                              -> MP3 / FLAC（取决于原始码率）
  QQ音乐       .mflac .mgg .qmc* .tkm .bkc* 等    -> FLAC / OGG / MP3

底层分别是 ncmdump.exe（外部二进制）与 qmc_decrypt.py（作为模块导入），
不在本项目里重复实现解密算法。纯标准库，无需安装第三方包。

打包说明：本文件同时支持「直接运行」和「PyInstaller 单文件 exe」两种形态。
  - 打包后没有独立的 Python 解释器，所以 qmc_decrypt 改为 import 调用；
  - 文件夹选择框需要独立进程，改为重新执行本程序自身（--pick-folder）。
"""

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- 路径

FROZEN = getattr(sys, "frozen", False)

# 打包后资源解压在 _MEIPASS；直接运行时就是本文件所在目录
RES_ROOT = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))

TOOLS_DIR = os.path.join(RES_ROOT, "tools")
INDEX_FILE = os.path.join(RES_ROOT, "index.html")

DEFAULT_PORT = 8090

# ---------------------------------------------------------------- 格式表

NCM_EXTS = {".ncm"}

QMC_EXTS = {
    ".mflac", ".mflac0", ".mflac1", ".mgg", ".mgg0", ".mgg1", ".mggl",
    ".mmp4", ".qmcflac", ".qmcogg", ".qmc0", ".qmc2", ".qmc3",
    ".qmc4", ".qmc6", ".qmc8", ".tkm",
    ".bkcmp3", ".bkcm4a", ".bkcflac", ".bkcwav", ".bkcape", ".bkcogg", ".bkcwma",
}

SUPPORTED_EXTS = NCM_EXTS | QMC_EXTS

NCMDUMP_CANDIDATES = [
    os.path.join(TOOLS_DIR, "ncmdump.exe"),
    os.path.join(RES_ROOT, "ncmdump.exe"),
    r"C:\Users\xth26\Tools\ncmdump\ncmdump.exe",
]

NCMDUMP = None
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_QMC_MOD = None

# ---------------------------------------------------------------- 状态

STATE = {
    "running": False,
    "total": 0,
    "done": 0,
    "current": "",
    "outdir": "",
    "results": [],
    "summary": "",
    "error": "",
}
STATE_LOCK = threading.Lock()

# ---------------------------------------------------------------- 基础工具


def _first_existing(paths):
    for path in paths:
        if os.path.isfile(path):
            return path
    return None


def _silence_stdio():
    """打包成 --noconsole 的 exe 后，sys.stdout/stderr 是 None，print 会直接崩。"""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = sys.stdout


def alert(message, title="音乐解密转换器"):
    """无控制台环境下也能让用户看到错误。"""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:  # noqa: BLE001
        try:
            print(message)
        except Exception:  # noqa: BLE001
            pass


def _pause():
    try:
        input("\n按回车键退出...")
    except Exception:  # noqa: BLE001
        pass


DUP_SUFFIX = re.compile(r"\s*\(\d+\)$")


def parse_filename(filename, ext):
    """从 '歌手 - 歌名.xxx' 拆出歌手和歌名。"""
    stem = filename[: -len(ext)] if ext else filename
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
    else:
        artist, title = "", stem
    return artist.strip(), title.strip()


def kind_of(ext):
    if ext in NCM_EXTS:
        return "ncm"
    if ext in QMC_EXTS:
        return "qmc"
    return ""


def scan_dir(path):
    """扫描目录下所有受支持的加密音频，返回 (items, error)。"""
    if not path or not os.path.isdir(path):
        return None, "文件夹不存在"
    items = []
    try:
        names = sorted(os.listdir(path))
    except OSError as exc:
        return None, "无法读取文件夹：%s" % exc

    for name in names:
        ext = os.path.splitext(name)[1].lower()
        if ext not in SUPPORTED_EXTS:
            continue
        full = os.path.join(path, name)
        if not os.path.isfile(full):
            continue
        artist, title = parse_filename(name, ext)
        try:
            size = os.path.getsize(full)
        except OSError:
            size = 0
        items.append({
            "file": full,
            "name": name,
            "ext": ext.lstrip("."),
            "kind": kind_of(ext),
            "artist": artist,
            "title": title,
            "size": size,
            "sortkey": (DUP_SUFFIX.sub("", title).lower(), artist.lower()),
        })

    counter = Counter(i["sortkey"] for i in items)
    for item in items:
        item["dup"] = counter[item["sortkey"]] > 1
    return items, None


# ---------------------------------------------------------------- 文件夹选择框

def _self_command():
    """构造「重新执行自己」的命令行。

    打包后 sys.executable 就是 exe 本身；直接运行时需要带上脚本路径，
    否则 python.exe 会把 --pick-folder 当成自己的参数。
    """
    if FROZEN:
        return [sys.executable]
    return [sys.executable, os.path.abspath(__file__)]


def pick_folder_cli(initial, out_file):
    """子进程入口：弹出选择框，把结果写进 out_file。

    走文件而不是 stdout —— --noconsole 打包后 stdout 不可靠。
    """
    import tkinter as tk
    from tkinter import filedialog

    path = ""
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        path = filedialog.askdirectory(
            title="选择文件夹", initialdir=initial if os.path.isdir(initial) else None)
        root.destroy()
    except Exception:  # noqa: BLE001
        path = ""
    try:
        with open(out_file, "w", encoding="utf-8") as handle:
            handle.write(path or "")
    except OSError:
        pass
    return 0


def pick_folder(initial=""):
    """弹出一个 Windows 文件夹选择框，返回选中的路径。"""
    out_file = os.path.join(tempfile.gettempdir(), "music_pick_%d.txt" % os.getpid())
    try:
        os.remove(out_file)
    except OSError:
        pass

    try:
        subprocess.run(
            _self_command() + ["--pick-folder", initial or "", out_file],
            timeout=900, creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001
        return {"path": "", "error": str(exc)}

    path = ""
    try:
        with open(out_file, "r", encoding="utf-8") as handle:
            path = handle.read().strip()
    except OSError:
        path = ""
    finally:
        try:
            os.remove(out_file)
        except OSError:
            pass
    return {"path": path}


# ---------------------------------------------------------------- 解密引擎

def run_ncm(src, outdir):
    """网易云：ncmdump.exe <src> -o <outdir>"""
    proc = subprocess.run(
        [NCMDUMP, src, "-o", outdir],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", creationflags=CREATE_NO_WINDOW,
    )
    stdout = (proc.stdout or "").strip()
    ok = proc.returncode == 0
    target = ""
    match = re.search(r"->\s*'(.+?)'\s*$", stdout, re.MULTILINE)
    if match:
        target = match.group(1)
    if ok and target and not os.path.isfile(target):
        ok = False
    return {"ok": ok, "out": target,
            "msg": (stdout or (proc.stderr or "").strip())[-400:]}


def load_qmc_module():
    """导入 qmc_decrypt 模块（打包后不再有独立的 Python 解释器可用）。"""
    global _QMC_MOD
    if _QMC_MOD is None:
        import importlib
        if TOOLS_DIR not in sys.path:
            sys.path.insert(0, TOOLS_DIR)
        _QMC_MOD = importlib.import_module("qmc_decrypt")
    return _QMC_MOD


def run_qmc(src, outdir):
    """QQ音乐：直接在进程内调用 qmc_decrypt 的 CLI 主函数。"""
    module = load_qmc_module()
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = module.main([src, "-o", outdir, "-f"])
    except SystemExit as exc:            # 模块内部若触发退出
        code = exc.code or 0
    except Exception as exc:             # noqa: BLE001
        return {"ok": False, "out": "", "msg": "%s: %s" % (type(exc).__name__, exc)}

    output = buffer.getvalue()
    ok = code == 0
    target = ""
    for line in output.splitlines():
        if line.startswith("[OK]"):
            after = line.split("->", 1)[-1].strip()
            target = re.sub(r"\s+\([A-Za-z0-9]+\)$", "", after).strip()
    if ok and target and not os.path.isfile(target):
        ok = False
    message = output.strip()
    if not ok and not message:
        message = "解密失败"
    return {"ok": ok, "out": target, "msg": message[-400:]}


def convert_one(src, outdir):
    ext = os.path.splitext(src)[1].lower()
    try:
        if ext in NCM_EXTS:
            result = run_ncm(src, outdir)
        else:
            result = run_qmc(src, outdir)
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "out": "", "msg": "%s: %s" % (type(exc).__name__, exc)}
    result.update(src=src, name=os.path.basename(src),
                  kind=kind_of(ext), ext=ext.lstrip("."))
    return result


def convert_worker(files, outdir):
    try:
        os.makedirs(outdir, exist_ok=True)
    except OSError as exc:
        with STATE_LOCK:
            STATE.update(running=False, error="无法创建输出文件夹：%s" % exc)
        return

    succeeded = 0
    failed = []
    for index, src in enumerate(files):
        with STATE_LOCK:
            STATE["current"] = os.path.basename(src)
        result = convert_one(src, outdir)
        if result["ok"]:
            succeeded += 1
        else:
            failed.append(os.path.basename(src))
        with STATE_LOCK:
            STATE["results"].append(result)
            STATE["done"] = index + 1

    summary = "转换完成，成功 %d 个" % succeeded
    if failed:
        summary += "，失败 %d 个：%s" % (len(failed), "、".join(failed))
    with STATE_LOCK:
        STATE.update(running=False, current="", summary=summary)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "MusicConverter/2.1"

    def log_message(self, *args):
        pass

    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if not raw.strip():
            return {}
        return json.loads(raw.decode("utf-8"))

    def _ok(self, payload):
        self._send(200, json.dumps(payload, ensure_ascii=False))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                with open(INDEX_FILE, "rb") as handle:
                    self._send(200, handle.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, "找不到 index.html", "text/plain; charset=utf-8")
        elif path == "/api/status":
            with STATE_LOCK:
                self._ok(dict(STATE))
        else:
            self._send(404, json.dumps({"error": "未知路径"}))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            payload = self._read_json()
        except Exception as exc:  # noqa: BLE001
            self._send(400, json.dumps({"error": "请求格式错误：%s" % exc}))
            return

        if path == "/api/pick-folder":
            self._ok(pick_folder(payload.get("initial", "")))

        elif path == "/api/scan":
            items, error = scan_dir(payload.get("dir", ""))
            self._ok({"items": items or [], "error": error or ""})

        elif path == "/api/convert":
            with STATE_LOCK:
                if STATE["running"]:
                    self._ok({"started": False, "error": "已有转换任务在进行"})
                    return
            files = [f for f in payload.get("files", []) if os.path.isfile(f)]
            outdir = payload.get("outdir", "")
            if not files:
                self._ok({"started": False, "error": "没有可转换的文件"})
                return
            if not outdir:
                self._ok({"started": False, "error": "请先选择输出文件夹"})
                return
            with STATE_LOCK:
                STATE.update(running=True, total=len(files), done=0, current="",
                             outdir=outdir, results=[], summary="", error="")
            threading.Thread(target=convert_worker, args=(files, outdir),
                             daemon=True).start()
            self._ok({"started": True, "total": len(files)})

        else:
            self._send(404, json.dumps({"error": "未知路径"}))


# ---------------------------------------------------------------- 启动

def main():
    global NCMDUMP
    NCMDUMP = _first_existing(NCMDUMP_CANDIDATES)
    qmc_ok = os.path.isfile(os.path.join(TOOLS_DIR, "qmc_decrypt.py"))

    missing = []
    if not NCMDUMP:
        missing.append("ncmdump.exe（处理网易云 .ncm）")
    if not qmc_ok:
        missing.append("qmc_decrypt.py（处理 QQ音乐 .mflac/.mgg）")
    if missing:
        message = "缺少以下依赖文件：\n\n" + "\n".join(missing) + \
                  "\n\n请确认 tools 目录下存在这些文件：\n" + TOOLS_DIR
        print("× " + message.replace("\n", "\n  "))
        alert(message)
        _pause()
        return

    print("√ 网易云引擎：%s" % NCMDUMP)
    print("√ QQ音乐引擎：%s" % os.path.join(TOOLS_DIR, "qmc_decrypt.py"))

    port = DEFAULT_PORT
    httpd = None
    for _ in range(10):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    if httpd is None:
        message = "端口 %d 起连续 10 个都被占用，无法启动。" % DEFAULT_PORT
        print("× " + message)
        alert(message)
        _pause()
        return

    url = "http://127.0.0.1:%d/" % port
    print("√ 服务已启动：%s" % url)
    print("  浏览器会自动打开。关闭本程序即可退出。\n")
    if not os.environ.get("NCM_NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    _silence_stdio()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    # 子进程入口：只弹文件夹选择框，然后退出
    if len(sys.argv) > 1 and sys.argv[1] == "--pick-folder":
        arg_initial = sys.argv[2] if len(sys.argv) > 2 else ""
        arg_out = sys.argv[3] if len(sys.argv) > 3 else ""
        sys.exit(pick_folder_cli(arg_initial, arg_out))

    main()
