# -*- coding: utf-8 -*-
"""离线 / 零依赖自证测试。

架构约定（本项目刻意划定的边界）：

* ``board`` / ``notation`` / ``ai`` / ``cli`` / ``gui`` 以及 ``main.py`` 是**单机离线核心**，
  不得导入任何网络模块——单人下棋、AI、图形界面完全不联网；
* ``xiangqi.net`` 是**唯一**允许使用 ``socket`` / ``http.server`` 的联机层，供浏览器对战使用；
* 全项目不得依赖任何第三方包（只允许标准库与项目内部模块）。
"""

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_DIR = os.path.join(ROOT, "xiangqi")
NET_DIR = os.path.join(PACKAGE_DIR, "net")
WEB_DIR = os.path.join(NET_DIR, "web")

NETWORK_MODULES = {
    "socket", "ssl", "urllib", "http", "httplib", "ftplib", "smtplib",
    "telnetlib", "xmlrpc", "asyncio", "socketserver", "poplib", "imaplib",
    "nntplib", "selectors", "webbrowser",
}

THIRD_PARTY_FORBIDDEN = {
    "requests", "flask", "fastapi", "django", "aiohttp", "tornado", "bottle",
    "pyramid", "websockets", "websocket", "socketio", "starlette", "uvicorn",
    "httpx", "gunicorn", "numpy", "pandas", "pytest", "websocket_client",
}

IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)")


def stdlib_names():
    """标准库顶层模块名集合。

    ``sys.stdlib_module_names`` 是 3.10 才有的；为了在 3.9 上也能做这项检查，
    回退为扫描标准库目录（``sysconfig`` 的 stdlib / platstdlib，Windows 还有 ``DLLs``）。
    """
    names = set(getattr(sys, "stdlib_module_names", ()))
    if names:
        return names
    names.update(sys.builtin_module_names)
    paths = []
    try:
        import sysconfig
        paths.append(sysconfig.get_paths().get("stdlib"))
        paths.append(sysconfig.get_paths().get("platstdlib"))
        paths.append(os.path.join(sys.base_prefix, "DLLs"))   # Windows 扩展模块
    except Exception:  # noqa: BLE001 - 拿不到就退化成“只查第三方黑名单”
        pass
    import pkgutil
    for path in paths:
        if path and os.path.isdir(path):
            try:
                for module in pkgutil.iter_modules([path]):
                    names.add(module.name)
            except Exception:  # noqa: BLE001
                continue
    return names


STDLIB = stdlib_names()


def iter_sources(directory, skip_dirs=()):
    """遍历目录下的 .py 文件（跳过指定子目录）。"""
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if os.path.isdir(path):
            if name in skip_dirs:
                continue
            for item in iter_sources(path, skip_dirs):
                yield item
        elif name.endswith(".py"):
            yield path


def core_sources():
    """单机离线核心：xiangqi 包（不含 net）与根目录 main.py。"""
    files = [path for path in iter_sources(PACKAGE_DIR) if not path.startswith(NET_DIR)]
    main_py = os.path.join(ROOT, "main.py")
    if os.path.isfile(main_py):
        files.append(main_py)
    return files


def net_sources():
    if not os.path.isdir(NET_DIR):
        return []
    return list(iter_sources(NET_DIR))


def imported_tops(path):
    tops = set()
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            match = IMPORT_RE.match(line)
            if match:
                tops.add(match.group(1).split(".")[0])
    return tops


class TestOfflineCore(unittest.TestCase):
    def test_core_never_imports_network_modules(self):
        offenders = []
        for path in core_sources():
            for top in imported_tops(path):
                if top in NETWORK_MODULES:
                    offenders.append("%s -> %s"
                                     % (os.path.relpath(path, ROOT), top))
        self.assertEqual(offenders, [],
                         "单机核心模块不得导入网络模块：%s" % "; ".join(offenders))

    def test_core_does_not_import_net_package(self):
        """核心模块不得在模块级依赖联机层（main.py 的 --serve 分支是函数内延迟导入）。"""
        offenders = []
        for path in core_sources():
            with open(path, "r", encoding="utf-8") as handle:
                for lineno, line in enumerate(handle, 1):
                    if "xiangqi.net" in line and not line.startswith((" ", "\t")):
                        offenders.append("%s:%d" % (os.path.relpath(path, ROOT), lineno))
        self.assertEqual(offenders, [],
                         "模块级不得导入联机层：%s" % "; ".join(offenders))

    def test_cli_and_gui_modules_are_offline(self):
        for module in ("cli", "gui", "board", "ai", "notation", "constants"):
            path = os.path.join(PACKAGE_DIR, module + ".py")
            self.assertTrue(os.path.isfile(path), path)
            self.assertFalse(imported_tops(path) & NETWORK_MODULES,
                             "%s 不应导入网络模块" % module)


class TestNetLayer(unittest.TestCase):
    def test_net_layer_exists_and_is_stdlib_only(self):
        sources = net_sources()
        self.assertTrue(sources, "联机层源码缺失")
        offenders = []
        for path in sources:
            for top in imported_tops(path):
                if top in THIRD_PARTY_FORBIDDEN:
                    offenders.append("%s -> %s"
                                     % (os.path.relpath(path, ROOT), top))
                elif top not in STDLIB and top != "xiangqi":
                    offenders.append("%s -> %s（非标准库）"
                                     % (os.path.relpath(path, ROOT), top))
        self.assertEqual(offenders, [], "联机层必须只用标准库：%s" % "; ".join(offenders))

    def test_whole_project_is_stdlib_only(self):
        offenders = []
        for path in core_sources() + net_sources():
            for top in imported_tops(path):
                if top in THIRD_PARTY_FORBIDDEN:
                    offenders.append(os.path.relpath(path, ROOT))
                elif top not in STDLIB and top != "xiangqi":
                    offenders.append("%s -> %s"
                                     % (os.path.relpath(path, ROOT), top))
        self.assertEqual(offenders, [], "项目不得依赖第三方包：%s" % "; ".join(offenders))

    def test_web_client_is_self_contained(self):
        """网页客户端不得引用任何外部资源（离线可用、无 CDN）。"""
        self.assertTrue(os.path.isdir(WEB_DIR), "网页客户端目录缺失")
        names = sorted(os.listdir(WEB_DIR))
        for required in ("index.html", "app.js", "logic.js", "style.css"):
            self.assertIn(required, names)
        for name in names:
            path = os.path.join(WEB_DIR, name)
            if not os.path.isfile(path):
                continue
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            for needle in ("http://", "https://", "//cdn", "//unpkg",
                           "//fonts.googleapis", "integrity="):
                self.assertNotIn(needle, text,
                                 "%s 出现了外部资源引用：%s" % (name, needle))

    def test_html_references_only_local_assets(self):
        path = os.path.join(WEB_DIR, "index.html")
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        for asset in ("app.js", "logic.js", "style.css"):
            self.assertIn(asset, text)
        for marker in ('<script src="http', '<link href="http', 'src="//',
                       "src='//"):
            self.assertNotIn(marker, text)


class TestPlayableEntryPoints(unittest.TestCase):
    def test_serve_entry_importable_without_side_effects(self):
        """导入联机层不应真的监听端口（只有 start_server 才监听）。"""
        import xiangqi.net.protocol as protocol
        import xiangqi.net.rooms as rooms
        import xiangqi.net.server as server
        import xiangqi.net.ws as ws
        self.assertTrue(callable(server.start_server))
        self.assertTrue(callable(rooms.RoomManager))
        self.assertTrue(callable(ws.accept_key))
        self.assertEqual(protocol.DEFAULT_PORT, 8000)

    def test_main_exposes_serve_flag(self):
        with open(os.path.join(ROOT, "main.py"), "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('"--serve"', text)
        self.assertIn("xiangqi.net.server", text)


class TestDeploymentScripts(unittest.TestCase):
    """部署脚本的编码约定——这三条都是实际踩过的坑，写进测试防止回归。

    * ``.cmd`` / ``.bat``：``cmd.exe`` 用**当前 OEM 代码页**（中文系统是 936）读取批处理，
      文件里的 UTF-8 中文会被解码成乱码并被当作命令执行（实测报一堆
      “不是内部或外部命令”）。所以必须**纯 ASCII、无 BOM**；中文提示交给 PowerShell 脚本。
    * ``.ps1``：Windows PowerShell 5.1 对**没有 BOM** 的文件按 ANSI 解码，中文会破坏语法
      （实测报“字符串缺少终止符”）。所以必须带 **UTF-8 BOM**。
    * ``.sh``：CRLF 会让 bash 报 ``\r: command not found``；BOM 会破坏 shebang。
      所以必须 **LF、无 BOM、带 shebang**。
    """

    BOM = b"\xef\xbb\xbf"
    SKIP_DIRS = {".git", "__pycache__", "bin", "node_modules"}

    def script_files(self, *suffixes):
        found = []
        for base, dirs, names in os.walk(ROOT):
            dirs[:] = [name for name in dirs if name not in self.SKIP_DIRS]
            for name in names:
                if name.lower().endswith(suffixes):
                    found.append(os.path.join(base, name))
        return sorted(found)

    def read_bytes(self, path):
        with open(path, "rb") as handle:
            return handle.read()

    def test_windows_batch_files_are_ascii_and_bomless(self):
        files = self.script_files(".cmd", ".bat")
        self.assertTrue(files, "应当存在 Windows 一键脚本")
        for path in files:
            data = self.read_bytes(path)
            self.assertFalse(data.startswith(self.BOM),
                             "%s 带了 BOM，cmd.exe 第一行就会报错" % path)
            offenders = [i for i, byte in enumerate(data) if byte > 127]
            self.assertEqual(
                offenders, [],
                "%s 有 %d 个非 ASCII 字节（首个在偏移 %d）：cmd.exe 会把中文当命令执行"
                % (os.path.basename(path), len(offenders), offenders[0] if offenders else -1))

    def test_powershell_scripts_have_utf8_bom(self):
        files = self.script_files(".ps1")
        self.assertTrue(files, "应当存在 PowerShell 部署脚本")
        for path in files:
            data = self.read_bytes(path)
            self.assertTrue(
                data.startswith(self.BOM),
                "%s 缺少 UTF-8 BOM：Windows PowerShell 5.1 会按 ANSI 解码，中文会破坏语法"
                % os.path.basename(path))

    def test_shell_scripts_are_lf_bomless_with_shebang(self):
        files = self.script_files(".sh")
        self.assertTrue(files, "应当存在 Linux/macOS 部署脚本")
        for path in files:
            data = self.read_bytes(path)
            name = os.path.basename(path)
            self.assertFalse(data.startswith(self.BOM), "%s 带 BOM 会破坏 shebang" % name)
            self.assertTrue(data.startswith(b"#!"), "%s 缺少 shebang" % name)
            self.assertNotIn(b"\r\n", data, "%s 用了 CRLF，bash 会报 \\r: command not found" % name)


if __name__ == "__main__":
    unittest.main()
