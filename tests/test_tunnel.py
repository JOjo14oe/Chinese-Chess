# -*- coding: utf-8 -*-
"""一键联机（服务器 + Cloudflare 隧道）的单元测试。

**完全不联网**：用假的 cloudflared 进程验证进程编排与公网地址解析；
真实隧道只在文档里手工验证（需要出网）。
"""

import os
import platform
import tempfile
import unittest

from xiangqi.net import tunnel


class TestUrlAndAssets(unittest.TestCase):
    def test_url_pattern_extracts_quick_tunnel(self):
        text = "2026-10-03T04:00:00Z INF |  https://calm-river-lake.trycloudflare.com  |"
        matched = tunnel.URL_PATTERN.search(text)
        self.assertIsNotNone(matched)
        self.assertEqual(matched.group(0), "https://calm-river-lake.trycloudflare.com")

    def test_url_pattern_ignores_other_hosts(self):
        self.assertIsNone(tunnel.URL_PATTERN.search("https://api.trycloudflare.com/v1"))
        self.assertIsNone(tunnel.URL_PATTERN.search("https://example.com/"))
        self.assertIsNone(tunnel.URL_PATTERN.search("http://plain.trycloudflare.com"))

    def test_asset_name_matches_platform(self):
        name = tunnel.asset_name()
        self.assertTrue(name.startswith("cloudflared-"), name)
        self.assertTrue(name.endswith(".exe") == (platform.system() == "Windows"), name)
        self.assertIn("windows" if platform.system() == "Windows" else
                      ("darwin" if platform.system() == "Darwin" else "linux"), name)

    def test_binary_name(self):
        self.assertTrue(tunnel.binary_name().startswith("cloudflared"))
        self.assertTrue(tunnel.binary_name().endswith(".exe") == (platform.system() == "Windows"))


class _TempBin(object):
    """把 tunnel.BIN_DIR 指到临时目录，避免动到仓库里的 tools/bin。"""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = tunnel.BIN_DIR
        tunnel.BIN_DIR = self.tmp.name
        return self.tmp.name

    def __exit__(self, *exc):
        tunnel.BIN_DIR = self.original
        self.tmp.cleanup()
        return False


class TestCloudflaredDiscovery(unittest.TestCase):
    def test_local_binary_absent_then_present(self):
        with _TempBin() as tmp:
            self.assertIsNone(tunnel.local_binary())
            fake = os.path.join(tmp, tunnel.binary_name())
            with open(fake, "wb") as handle:
                handle.write(b"stub")
            self.assertEqual(tunnel.local_binary(), fake)
            self.assertEqual(tunnel.find_cloudflared(), fake)

    def test_ensure_cloudflared_reuses_existing_file(self):
        """已有本地二进制时绝不下载（不联网）。"""
        with _TempBin() as tmp:
            fake = os.path.join(tmp, tunnel.binary_name())
            with open(fake, "wb") as handle:
                handle.write(b"stub")
            self.assertEqual(tunnel.ensure_cloudflared(quiet=True), fake)

    def test_authenticode_status_shape(self):
        status, subject = tunnel.authenticode_status(__file__)
        self.assertIsInstance(status, str)
        self.assertIsInstance(subject, str)
        if platform.system() != "Windows":
            self.assertEqual(status, "unknown")


def _fake_cloudflared(directory, with_url=True, hold_seconds=3):
    """造一个假的 cloudflared：可选打印一行带公网地址的日志，然后短暂挂起。"""
    if platform.system() == "Windows":
        path = os.path.join(directory, "fake-cloudflared.cmd")
        lines = ["@echo off"]
        if with_url:
            lines.append("echo 2026-01-01T00:00:00Z INF Visit it at "
                         "https://fake-tunnel-abc.trycloudflare.com")
            lines.append("ping -n %d 127.0.0.1 >nul" % (hold_seconds + 1))
        with open(path, "w", encoding="ascii", newline="") as handle:
            handle.write("\r\n".join(lines) + "\r\n")
    else:
        path = os.path.join(directory, "fake-cloudflared")
        lines = ["#!/bin/sh"]
        if with_url:
            lines.append("echo '2026-01-01T00:00:00Z INF Visit it at "
                         "https://fake-tunnel-abc.trycloudflare.com'")
            lines.append("sleep %d" % hold_seconds)
        with open(path, "w", encoding="ascii") as handle:
            handle.write("\n".join(lines) + "\n")
        os.chmod(path, 0o755)
    return path


class TestTunnelProcess(unittest.TestCase):
    def test_start_reads_public_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = _fake_cloudflared(tmp, with_url=True)
            tun = tunnel.Tunnel(port=1, exe=exe, quiet=True)
            self.addCleanup(tun.stop)
            self.assertEqual(tun.start(timeout=15), "https://fake-tunnel-abc.trycloudflare.com")
            self.assertTrue(tun.running)
            self.assertEqual(tun.url, "https://fake-tunnel-abc.trycloudflare.com")
            tun.stop()
            self.assertFalse(tun.running)
            self.assertIsNone(tun.url)

    def test_start_raises_without_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = _fake_cloudflared(tmp, with_url=False)
            tun = tunnel.Tunnel(port=1, exe=exe, quiet=True)
            self.addCleanup(tun.stop)
            with self.assertRaises(tunnel.TunnelError):
                tun.start(timeout=10)

    def test_start_with_fallback_raises_original_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = _fake_cloudflared(tmp, with_url=False)
            tun = tunnel.Tunnel(port=1, exe=exe, quiet=True)
            self.addCleanup(tun.stop)
            with self.assertRaises(tunnel.TunnelError):
                tun.start_with_fallback(timeout=5)

    def test_save_url_writes_plain_ascii(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = tunnel.URL_FILE
            tunnel.URL_FILE = os.path.join(tmp, "public-url.txt")
            try:
                path = tunnel.save_url("https://x-y-z.trycloudflare.com")
                with open(path, "r", encoding="ascii") as handle:
                    self.assertEqual(handle.read().strip(), "https://x-y-z.trycloudflare.com")
            finally:
                tunnel.URL_FILE = original


class TestEntryWiring(unittest.TestCase):
    def test_main_exposes_tunnel_flags(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "main.py"), "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('"--tunnel"', text)
        self.assertIn('"--protocol"', text)
        self.assertIn("xiangqi.net.tunnel", text)
        self.assertIn("serve_public", text)

    def test_gui_has_online_entry(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "xiangqi", "gui.py"), "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("联机对战", text)
        self.assertIn("PublicSession", text)
        # 核心层不能顶层导入网络模块（单机必须保持离线）
        self.assertNotIn("\nimport urllib", text)
        self.assertNotIn("\nimport webbrowser", text)


if __name__ == "__main__":
    unittest.main()
