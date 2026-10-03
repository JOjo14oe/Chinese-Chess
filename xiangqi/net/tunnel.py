# -*- coding: utf-8 -*-
"""公网隧道：自动准备 cloudflared、拉起 Cloudflare 快速隧道、给出公网地址。

属于联机层（``xiangqi.net``），因此这里可以使用标准库的网络/子进程模块；
单机离线核心（board/ai/cli/gui）不会导入本模块，只有用户主动选择“联机对战”时
才会被 ``main.py --serve --tunnel`` 或图形界面的按钮调用。

设计要点：

* **零第三方依赖**：只用 subprocess / urllib / threading 等标准库；
* **自动准备 cloudflared**：优先用 ``tools/bin/cloudflared[.exe]``，其次 PATH，
  都没有就从官方 Release 下载（先官方直链，再 GitHub API 兜底——本机实测
  github.com 直连会被重置，而 api.github.com 通常可达），Windows 上还会校验
  Authenticode 签名必须是 Cloudflare, Inc.；
* **可中断**：隧道是子进程，``Tunnel.stop()`` 一定回收；Ctrl+C 也能收干净。
"""

import http.client
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOLS_DIR = os.path.join(PROJECT_ROOT, "tools")
BIN_DIR = os.path.join(TOOLS_DIR, "bin")
URL_FILE = os.path.join(TOOLS_DIR, "public-url.txt")

URL_PATTERN = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+)+\.trycloudflare\.com")
RELEASE_API = "https://api.github.com/repos/cloudflare/cloudflared/releases/latest"
RELEASE_DOWNLOAD = "https://github.com/cloudflare/cloudflared/releases/latest/download/%s"
USER_AGENT = "xiangqi-tunnel"


class TunnelError(RuntimeError):
    """隧道相关错误（下载失败、启动失败、拿不到公网地址等）。"""


def binary_name():
    """当前平台下 cloudflared 的文件名。"""
    return "cloudflared.exe" if platform.system() == "Windows" else "cloudflared"


def asset_name():
    """当前平台对应的官方资产名。"""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        os_name = "windows"
    elif system == "darwin":
        os_name = "darwin"
    elif system == "linux":
        os_name = "linux"
    else:
        raise TunnelError("不认识的系统：%s（请手动安装 cloudflared 后重试）" % platform.system())
    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        raise TunnelError("不认识的架构：%s（请手动安装 cloudflared 后重试）" % platform.machine())
    suffix = ".exe" if os_name == "windows" else ""
    return "cloudflared-%s-%s%s" % (os_name, arch, suffix)


def local_binary():
    """本地已下载的 cloudflared 路径（不存在则为 None）。"""
    path = os.path.join(BIN_DIR, binary_name())
    return path if os.path.isfile(path) else None


def find_cloudflared():
    """按顺序找可用的 cloudflared：tools/bin -> PATH。"""
    local = local_binary()
    if local:
        return local
    found = shutil.which("cloudflared")
    return found


def _download(url, target, timeout=900):
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response, open(target, "wb") as handle:
        shutil.copyfileobj(response, handle)


def _download_via_api(target):
    """走 GitHub API 取资产直链（本机 github.com 会被重置时的兜底）。"""
    import urllib.request

    request = urllib.request.Request(
        RELEASE_API, headers={"User-Agent": USER_AGENT,
                              "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        release = json.loads(response.read().decode("utf-8"))
    wanted = asset_name()
    for asset in release.get("assets", []):
        if asset.get("name") == wanted:
            direct = asset.get("browser_download_url") or asset.get("url")
            _download(direct, target)
            return release.get("tag_name") or ""
    raise TunnelError("官方 Release 里找不到 %s" % wanted)


def authenticode_status(path):
    """Windows 上用 PowerShell 校验 Authenticode 签名，返回 (status, subject)。"""
    if platform.system() != "Windows":  # pragma: no cover - 平台相关
        return ("unknown", "")
    escaped = str(path).replace("'", "''")
    script = ("$sig = Get-AuthenticodeSignature -LiteralPath '%s'; "
              "Write-Output ($sig.Status.ToString() + '|' + "
              "$(if ($sig.SignerCertificate) { $sig.SignerCertificate.Subject } else { '' }))"
              % escaped)
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:  # pragma: no cover
        return ("error", str(error))
    line = (proc.stdout or "").strip().splitlines()
    if not line:
        return ("error", (proc.stderr or "").strip())
    status, _, subject = line[-1].partition("|")
    return (status.strip(), subject.strip())


def sha256_of(path):
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_cloudflared(quiet=False):
    """确保有可用的 cloudflared，返回其路径（必要时下载 + 校验签名）。"""
    found = find_cloudflared()
    if found:
        return found

    target = os.path.join(BIN_DIR, binary_name())
    os.makedirs(BIN_DIR, exist_ok=True)
    asset = asset_name()
    if not quiet:
        print("[tunnel] 正在下载 cloudflared（约 40-60 MB）…")
    try:
        _download(RELEASE_DOWNLOAD % asset, target)
    except Exception as error:  # noqa: BLE001 - 直链失败很常见，接着走 API
        if not quiet:
            print("[tunnel] 官方直链失败（%s），改用 GitHub API 取直链…" % error)
        try:
            _download_via_api(target)
        except Exception as api_error:  # noqa: BLE001
            if os.path.isfile(target):
                os.remove(target)
            raise TunnelError(
                "无法自动下载 cloudflared：%s\n"
                "请手动下载 %s 放到 %s，或用 tools/serve-public.ps1/serve-public.sh，"
                "或自己安装 cloudflared 后加入 PATH。" % (api_error, asset, target))

    if platform.system() != "Windows":
        os.chmod(target, 0o755)

    if platform.system() == "Windows":
        status, subject = authenticode_status(target)
        if status != "Valid" or "Cloudflare" not in subject:
            os.remove(target)
            raise TunnelError("cloudflared 签名校验失败（%s / %s），已删除。" % (status, subject))
        if not quiet:
            print("[tunnel] 签名校验通过：Cloudflare, Inc.")
    else:
        if not quiet:
            print("[tunnel] sha256 = %s（请与 Cloudflare 官方公布值核对）" % sha256_of(target))
    return target


class Tunnel(object):
    """一条 Cloudflare 快速隧道。"""

    def __init__(self, port, exe=None, protocol=None, quiet=False):
        self.port = int(port)
        self.exe = exe or ensure_cloudflared(quiet=quiet)
        self.protocol = protocol
        self.quiet = quiet
        self.url = None
        self.log = []
        self._proc = None
        self._thread = None
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 内部
    def _reader(self):
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            text = line.rstrip("\r\n")
            with self._lock:
                self.log.append(text)
                if len(self.log) > 200:
                    del self.log[:100]
                match = URL_PATTERN.search(text)
                if match and not self.url:
                    self.url = match.group(0)
            if not self.quiet and (URL_PATTERN.search(text) or "ERR" in text):
                print("[tunnel] %s" % text)

    def _spawn(self):
        args = [self.exe, "tunnel", "--url", "http://127.0.0.1:%d" % self.port, "--no-autoupdate"]
        if self.protocol:
            args += ["--protocol", self.protocol]
        creation = 0
        if platform.system() == "Windows" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            creation = subprocess.CREATE_NO_WINDOW      # 图形界面里别弹黑窗口
        self._proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, bufsize=1, creationflags=creation)
        self._thread = threading.Thread(target=self._reader, name="cloudflared-log", daemon=True)
        self._thread.start()

    # ---------------------------------------------------------------- 对外
    def start(self, timeout=60.0):
        """启动隧道并等待公网地址；失败抛 TunnelError。"""
        self._spawn()
        try:
            return self.wait_url(timeout)
        except TunnelError:
            self.stop()
            raise

    def wait_url(self, timeout=60.0):
        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            if self.url:
                return self.url
            if self._proc is not None and self._proc.poll() is not None:
                break
            time.sleep(0.25)
        with self._lock:
            tail = "\n".join(self.log[-15:])
        raise TunnelError("60 秒内没拿到公网地址。cloudflared 日志：\n%s\n"
                          "（常见原因：UDP 7844 被封，可加 --protocol http2 重试）" % tail)

    def start_with_fallback(self, timeout=60.0):
        """先按默认（QUIC）起，拿不到地址就退回 HTTP/2 再试一次。"""
        try:
            return self.start(timeout)
        except TunnelError as first:
            if self.protocol:
                raise
            self.stop()
            self.protocol = "http2"
            try:
                return self.start(timeout)
            except TunnelError:
                raise first

    def stop(self):
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.kill()
        except OSError:  # pragma: no cover
            pass
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:  # pragma: no cover
                pass
        self.url = None

    @property
    def running(self):
        return self._proc is not None and self._proc.poll() is None


def save_url(url):
    """把公网地址写到 tools/public-url.txt（和部署脚本行为一致）。"""
    os.makedirs(TOOLS_DIR, exist_ok=True)
    with open(URL_FILE, "w", encoding="ascii") as handle:
        handle.write(url + "\n")
    return URL_FILE


def open_page(url):
    """在默认浏览器里打开页面（联机层允许用 webbrowser）。"""
    try:
        return webbrowser.open(url)
    except Exception:  # noqa: BLE001 - 打不开浏览器不影响服务
        return False


class PublicSession(object):
    """给图形界面用的“后台开服 + 开隧道”句柄（主线程不被阻塞）。

    ``on_ready(url)`` 在拿到公网地址后于后台线程里调用；``on_error(text)`` 在失败时调用。
    调用方（GUI）负责把回调切回主线程再更新界面。
    """

    def __init__(self, host="127.0.0.1", port=None, verbose=False,
                 protocol=None, on_ready=None, on_error=None, manager=None):
        self.host = host
        self.port = int(port or 8000)
        self.verbose = verbose
        self.protocol = protocol
        self.on_ready = on_ready
        self.on_error = on_error
        self.manager = manager
        self.url = None
        self._handle = None
        self._tunnel = None
        self._thread = None
        self._stopped = threading.Event()

    def start(self):
        self._thread = threading.Thread(target=self._run, name="xiangqi-public", daemon=True)
        self._thread.start()
        return self

    def _run(self):
        from xiangqi.net.server import start_server

        bind = "127.0.0.1" if self.host in (None, "", "0.0.0.0", "::") else self.host
        try:
            self._handle = start_server(bind, self.port, quiet=not self.verbose,
                                        manager=self.manager)
            self._tunnel = Tunnel(self.port, protocol=self.protocol, quiet=True)
            self.url = self._tunnel.start_with_fallback()
            save_url(self.url)
        except Exception as error:  # noqa: BLE001 - 统一交给回调报错
            self.stop()
            if self.on_error:
                self.on_error(str(error))
            return
        if self.on_ready:
            self.on_ready(self.url)
        while not self._stopped.is_set() and self._tunnel and self._tunnel.running:
            time.sleep(0.5)
        self.stop()

    def stop(self):
        self._stopped.set()
        if self._tunnel:
            self._tunnel.stop()
            self._tunnel = None
        if self._handle:
            self._handle.stop()
            self._handle = None
        self.url = None


def serve_public(host="127.0.0.1", port=None, verbose=False,
                 open_browser=False, protocol=None, manager=None):
    """一条龙：启动本机服务器 + 公网隧道，打印可发给朋友的地址，直到 Ctrl+C。

    返回进程退出码。可直接被 ``main.py --serve --tunnel`` 或 GUI 按钮调用。
    """
    from xiangqi.net import protocol as net_protocol
    from xiangqi.net.server import start_server

    port = int(port or net_protocol.DEFAULT_PORT)
    bind = "127.0.0.1" if host in (None, "", "0.0.0.0", "::") else host

    handle = start_server(bind, port, quiet=not verbose, manager=manager)
    print("[serve] 本机服务： %s" % handle.url)
    if bind != host:
        print("[serve] 提示：走隧道时只监听 127.0.0.1（外部访问只经 Cloudflare），"
              "已忽略 --host %s。" % host)

    tunnel = Tunnel(port, protocol=protocol)
    try:
        url = tunnel.start_with_fallback()
    except TunnelError as error:
        handle.stop()
        print("[tunnel] 建立隧道失败：%s" % error)
        return 1

    save_url(url)
    public = url + "/"
    print("")
    print("================ 公网对战已就绪（Cloudflare 隧道） ================")
    print("发给国外朋友： %s" % public)
    print("朋友打开后点「加入房间」并输入你的 6 位房间号（或直接点你发的 ?room=XXXXXX 链接）。")
    print("你自己也打开： %s  → 点「创建房间」→ 把房间号/链接发给对方" % public)
    print("")
    print("传输说明（经 Cloudflare 快速隧道的实测结论）：")
    print("  · WebSocket 正常 —— 浏览器会自动优先用它，对战体验最好；")
    print("  · 长轮询正常 —— 备用通道，实测被对手走子唤醒约 2～3 秒；")
    print("  · SSE 会被快速隧道整包缓冲（不实时），客户端约 60 秒后自动降级到长轮询。")
    print("")
    print("公网地址也保存在： %s" % URL_FILE)
    print("按 Ctrl+C 结束（同时关闭隧道）。")
    print("=================================================================")
    if open_browser:
        open_page(public)

    try:
        while tunnel.running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[serve] 正在关闭…")
    finally:
        tunnel.stop()
        handle.stop()
    return 0
