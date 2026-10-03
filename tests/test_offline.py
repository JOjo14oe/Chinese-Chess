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
STDLIB = set(getattr(sys, "stdlib_module_names", ()))


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


if __name__ == "__main__":
    unittest.main()
