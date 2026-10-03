# -*- coding: utf-8 -*-
"""联机对战服务器：标准库 HTTP + REST + SSE + WebSocket（无第三方依赖）。

启动::

    python main.py --serve                  # http://127.0.0.1:8000
    python main.py --serve --port 9000      # 指定端口
    python main.py --serve --host 0.0.0.0   # 允许其它机器/公网访问（跨国对战时用）

同一台机器上开两个浏览器窗口：一个「创建房间」，另一个用房间号「加入房间」。

跨国（高延迟/易丢包）相关设计
----------------------------
* **传输自动降级**：WebSocket → SSE → 长轮询，三者共用同一套事件协议，换连接不断局。
* **序号 + 快照**：客户端只记住 ``since``（已收到的最大 ``seq``），重连时精确补齐；
  位置太旧则整包快照，永远不会“漏步”。
* **幂等重发**：走子带 ``cid``，重复发送只执行一次；``base_seq`` 只与**改变棋局的事件**
  比较，因此对手上线/掉线不会把在途的一步棋判为过期。
* **反代友好**：SSE 带 ``X-Accel-Buffering: no`` 与 15 秒心跳，避免 nginx 缓冲导致“卡住”。
* **无实时钟**：不做走子限时，链路慢不会直接判负。
"""

import http.server
import os
import socket
import threading
import time
from urllib.parse import parse_qs, unquote, urlparse

from . import protocol as P
from . import ws as wsmod
from .rooms import RoomError, RoomManager

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}

STREAM_MAX_SECONDS = 30 * 60      # 单条 SSE/WS 连接的最长存活时间
WS_READ_TIMEOUT = 1.0             # WS 读超时（用于检查停止标志）


class NetServer(http.server.ThreadingHTTPServer):
    """带房间表与运行统计的 HTTP 服务器。"""

    daemon_threads = True
    allow_reuse_address = True
    janitor_interval = 300.0        # 每 5 分钟回收一次空闲房间

    def __init__(self, address, handler, manager=None, quiet=True):
        super(NetServer, self).__init__(address, handler)
        self.manager = manager or RoomManager()
        self.quiet = quiet
        self.started_ms = P.now_ms()
        self.connections = 0
        self._conn_lock = threading.Lock()
        self._janitor_stop = threading.Event()
        self._janitor = threading.Thread(target=self._janitor_loop,
                                         name="xiangqi-janitor", daemon=True)
        self._janitor.start()

    def _janitor_loop(self):
        """定期回收长时间无活动的房间（跨国对局也不会把房间表撑爆）。"""
        while not self._janitor_stop.wait(self.janitor_interval):
            try:
                self.manager.cleanup()
            except Exception:  # noqa: BLE001 - 清理失败不影响服务
                pass

    def server_close(self):
        self._janitor_stop.set()
        return super(NetServer, self).server_close()

    def note_connection(self, delta):
        with self._conn_lock:
            self.connections += delta

    def stats(self):
        data = self.manager.stats()
        data.update({"connections": self.connections,
                     "uptime_ms": P.now_ms() - self.started_ms,
                     "protocol": P.PROTOCOL_VERSION})
        return data


class XQHandler(http.server.BaseHTTPRequestHandler):
    """所有路由：静态网页 / REST / SSE / WebSocket。"""

    protocol_version = "HTTP/1.1"
    server_version = "XiangqiLocalNet/1.0"
    sys_version = ""

    # ------------------------------------------------------------------ 基础
    def log_message(self, fmt, *args):  # pragma: no cover - 日志开关
        if not getattr(self.server, "quiet", True):
            http.server.BaseHTTPRequestHandler.log_message(self, fmt, *args)

    def log_error(self, fmt, *args):  # pragma: no cover
        if not getattr(self.server, "quiet", True):
            http.server.BaseHTTPRequestHandler.log_error(self, fmt, *args)

    def handle_one_request(self):
        try:
            http.server.BaseHTTPRequestHandler.handle_one_request(self)
        except ConnectionError:            # 客户端断开/重置/中止，属正常情况
            self.close_connection = True

    def handle(self):
        """统计当前存活连接数（SSE/WS 长连接也计入）。"""
        self.server.note_connection(1)
        try:
            http.server.BaseHTTPRequestHandler.handle(self)
        finally:
            self.server.note_connection(-1)

    def send_error(self, code, message=None, explain=None):
        """把 http.server 默认的 HTML 错误页也换成 JSON。

        客户端约定：所有应答（含 4xx/5xx、未知方法、畸形请求）都是 JSON，
        且错误体形如 ``{"ok": false, "error": ..., "msg": ...}``。
        """
        short, long_message = http.server.BaseHTTPRequestHandler.responses.get(
            code, ("Error", ""))
        payload = {"ok": False,
                   "error": "bad_request" if code < 500 else "internal",
                   "msg": message or short or long_message}
        try:
            body = P.dumps(payload).encode("utf-8")
            self.send_response(code, short)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if getattr(self, "command", None) != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):  # pragma: no cover
            self.close_connection = True

    # ------------------------------------------------------------------ 工具
    def _parse(self):
        parsed = urlparse(self.path)
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        return unquote(parsed.path), query

    def _send_json(self, status, payload, extra_headers=None, close=False):
        body = P.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if close:
            # 长轮询会长时间占住一条连接。HTTP/1.1 keep-alive 下，隧道/反代可能复用它转发
            # 后续请求，那些请求就会被排在挂起的响应之后；显式关闭可让它们另开连接。
            self.send_header("Connection", "close")
            self.close_connection = True
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status, code, message, **extra):
        payload = {"ok": False, "error": code, "msg": message}
        payload.update(extra)
        self._send_json(status, payload)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise P.ProtocolError(P.ERR_BAD_REQUEST, "Content-Length 无效")
        if length < 0:
            raise P.ProtocolError(P.ERR_BAD_REQUEST, "Content-Length 无效")
        if length > P.MAX_BODY_BYTES:
            raise P.ProtocolError(P.ERR_TOO_LARGE, "请求体过大")
        raw = self.rfile.read(length) if length else b"{}"
        return P.load(raw)

    def _serve_static(self, name):
        safe = os.path.basename(name or "index.html")
        path = os.path.join(WEB_DIR, safe)
        if not os.path.isfile(path):
            self._send_error_json(404, P.ERR_NOT_FOUND, "资源不存在：%s" % safe)
            return
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError as error:  # pragma: no cover - 磁盘异常
            self._send_error_json(500, "internal", "读取资源失败：%s" % error)
            return
        extension = os.path.splitext(safe)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", STATIC_TYPES.get(extension,
                                                          "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        # 客户端只有几十 KB，禁掉缓存以免升级后浏览器仍用旧版
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------ 路由
    def do_GET(self):
        path, query = self._parse()
        try:
            if path in ("/", "/index.html"):
                self._serve_static("index.html")
                return
            if path in ("/app.js", "/logic.js", "/style.css", "/favicon.ico"):
                self._serve_static(path.lstrip("/"))
                return
            if path == "/api/health":
                self._send_json(200, {"ok": True, "server_ms": P.now_ms(),
                                      **self.server.stats()})
                return
            if path == "/ws":
                self._route_websocket(query)
                return
            parts = [segment for segment in path.split("/") if segment]
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "rooms":
                room = self.server.manager.get(parts[2])
                self._send_json(200, {"ok": True, "v": P.PROTOCOL_VERSION,
                                      **room.public_summary()})
                return
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "rooms":
                room = self.server.manager.get(parts[2])
                if parts[3] == "events":
                    self._route_long_poll(room, query)
                    return
                if parts[3] == "stream":
                    self._route_stream(room, query)
                    return
            self._send_error_json(404, P.ERR_NOT_FOUND, "未知接口：%s" % path)
        except RoomError as error:
            self._send_error_json(_status_for(error.code), error.code,
                                  error.message,
                                  **({"snapshot": error.snapshot}
                                     if error.snapshot else {}))
        except P.ProtocolError as error:
            self._send_error_json(400, error.code, error.message)
        except ConnectionError:            # pragma: no cover - 客户端提前断开
            self.close_connection = True

    def do_POST(self):
        path, query = self._parse()
        try:
            parts = [segment for segment in path.split("/") if segment]
            if parts == ["api", "rooms"]:
                body = self._read_json()
                self._create_room(body, query)
                return
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "rooms":
                room = self.server.manager.get(parts[2])
                action = parts[3]
                body = self._read_json()
                if action == "join":
                    self._join_room(room, body, query)
                    return
                if action == "resume":
                    self._resume_room(room, body, query)
                    return
                if action == "move":
                    self._move(room, body, query)
                    return
                if action == "resign":
                    self._simple_action(room, body, query, "resign")
                    return
                if action == "new":
                    self._simple_action(room, body, query, "new")
                    return
            self._send_error_json(404, P.ERR_NOT_FOUND, "未知接口：%s" % path)
        except RoomError as error:
            self._send_error_json(_status_for(error.code), error.code,
                                  error.message,
                                  **({"snapshot": error.snapshot}
                                     if error.snapshot else {}))
        except P.ProtocolError as error:
            self._send_error_json(400, error.code, error.message)
        except ConnectionError:            # pragma: no cover - 客户端提前断开
            self.close_connection = True

    # ------------------------------------------------------------------ REST
    def _seat_token(self, room, body, query):
        token = body.get("token") or query.get("token")
        if token is None:
            raise RoomError(P.ERR_FORBIDDEN, "缺少座位令牌 token")
        seat = room.seat_of_token(token)
        if seat is None:
            raise RoomError(P.ERR_FORBIDDEN,
                            "令牌无效：该房间没有对应座位（可尝试用房间号重新加入）")
        return seat

    def _create_room(self, body, query):
        side = body.get("side") or query.get("side")
        if side is not None and side not in P.SIDES:
            raise P.ProtocolError(P.ERR_BAD_REQUEST, "side 只能是 r 或 b")
        name = body.get("name")
        room, chosen, token, events = self.server.manager.create(side=side, name=name)
        self._send_json(200, {
            "ok": True, "v": P.PROTOCOL_VERSION, "room": room.code,
            "side": chosen, "token": token, "seq": room.snapshot()["seq"],
            "events": events, "snapshot": room.snapshot(),
        })

    def _join_room(self, room, body, query):
        side = body.get("side") or query.get("side")
        if side is not None and side not in P.SIDES:
            raise P.ProtocolError(P.ERR_BAD_REQUEST, "side 只能是 r 或 b")
        token = body.get("token") or query.get("token")
        name = body.get("name")
        chosen, token, events = room.join(side=side, name=name, token=token)
        self._send_json(200, {
            "ok": True, "v": P.PROTOCOL_VERSION, "room": room.code,
            "side": chosen, "token": token, "seq": room.snapshot()["seq"],
            "events": events, "snapshot": room.snapshot(),
        })

    def _resume_room(self, room, body, query):
        seat = self._seat_token(room, body, query)
        since = P.require_int(body, "since", minimum=0, default=0)
        room.resume(seat.token, since)
        events = room.events_since(since)
        snapshot = room.snapshot()
        events_payload = events.get("events", [])
        # 契约：同一应答中 events 全部 > since，且不超过 snapshot.seq；
        # 位置过旧时 events_since 返回快照，此时只回快照 + 空 events。
        self._send_json(200, {"ok": True, "v": P.PROTOCOL_VERSION,
                              "room": room.code, "side": seat.side,
                              "token": seat.token, "seq": snapshot["seq"],
                              "since": since, "events": events_payload,
                              "snapshot": snapshot})

    def _move(self, room, body, query):
        seat = self._seat_token(room, body, query)
        iccs = P.require_str(body, "iccs", max_len=P.MAX_ICCS_LEN)
        cid = body.get("cid")
        if cid is not None:
            cid = P.require_str(body, "cid", max_len=P.MAX_CID)
        base_seq = body.get("base_seq")
        if base_seq is not None:
            base_seq = P.require_int(body, "base_seq", minimum=0)
        result = room.apply_move(seat.side, iccs, base_seq=base_seq, cid=cid)
        self._send_json(200, result)

    def _simple_action(self, room, body, query, action):
        seat = self._seat_token(room, body, query)
        result = (room.resign(seat.side) if action == "resign"
                  else room.new_game(seat.side))
        self._send_json(200, result)

    # ------------------------------------------------------------------ SSE
    #
    # 关于"公网/反代下 SSE 卡住"的说明（实战踩坑记录）：
    # 早期实现用 `Connection: close` + 无长度（读到 EOF 为止）来流式输出，本机直连没问题，
    # 但经 Cloudflare 隧道时会**整包缓冲**：本地首帧 0.02s 到达，隧道上 45s 都收不到任何字节。
    # 修法是两点：① 用标准的 `Transfer-Encoding: chunked` 明确分块，代理才会逐块转发；
    # ② 开头先写一段 ~2KB 的注释做"填充"，越过部分代理的攒包阈值。
    # 客户端（EventSource）会忽略注释行，语义完全不变。

    def _sse_raw(self, data):
        """按 chunked 编码写一段 SSE 数据（必须与 Transfer-Encoding 一致）。"""
        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()

    def _route_long_poll(self, room, query):
        seat = room.seat_of_token(query.get("token"))
        if seat is None:
            raise RoomError(P.ERR_FORBIDDEN, "长轮询需要有效的座位令牌")
        since = _query_int(query, "since", 0)
        wait = min(max(_query_int(query, "wait", P.LONG_POLL_SECONDS), 0),
                   P.LONG_POLL_SECONDS)
        payload = room.wait_events(since, timeout=wait)
        self._send_json(200, payload, close=True)

    def _route_stream(self, room, query):
        seat = room.seat_of_token(query.get("token"))
        if seat is None:
            raise RoomError(P.ERR_FORBIDDEN, "SSE 需要有效的座位令牌")
        since = _query_int(query, "since", 0)
        # 浏览器重连 SSE 时会带上 Last-Event-ID（我们每条事件都发了 id），
        # 用它校正 since，断线期间的事件就能精确补齐而不是重发全部。
        last_event_id = self.headers.get("Last-Event-ID")
        if last_event_id:
            try:
                since = max(since, int(str(last_event_id).strip()))
            except (TypeError, ValueError):
                pass
        room.touch(seat.token, True)
        self.close_connection = True          # 由本函数独占连接
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Transfer-Encoding", "chunked")   # 明确分块，反代才会流式转发
            self.send_header("X-Accel-Buffering", "no")        # nginx 不缓冲
            self.end_headers()
            self._sse_raw(b"retry: 3000\n\n")                  # 断线 3 秒后自动重连
            self._sse_raw(b": " + b" " * 2048 + b"\n\n")       # 起始填充，越过代理攒包阈值
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            return
        deadline = time.monotonic() + STREAM_MAX_SECONDS
        try:
            # 首帧：事件名 hello，载荷用 welcome 形状（带快照），客户端可立即渲染；
            # 随后 wait_events 会把 since 之后的事件/快照按批推下去。
            self._sse_write("hello", {"v": P.PROTOCOL_VERSION, "t": "welcome",
                                      "room": room.code, "side": seat.side,
                                      "seq": room.snapshot()["seq"],
                                      "snapshot": room.snapshot(),
                                      "server_ms": P.now_ms()}, since)
            while time.monotonic() < deadline:
                payload = room.wait_events(since, timeout=P.HEARTBEAT_SECONDS)
                if payload.get("t") == "snapshot":
                    snapshot = payload["snapshot"]
                    since = snapshot.get("seq", since)
                    self._sse_write("snapshot", payload, since)
                    continue
                events = payload.get("events") or []
                if events:
                    since = events[-1]["seq"]
                    self._sse_write("events", payload, since)
                else:
                    self._sse_raw(b":hb %d\n\n" % P.now_ms())
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            # 流要结束时（约 30 分钟上限，或客户端断开）再补一个快照，
            # 客户端重连时无论 since 是否落后都能对齐，然后正常关闭连接。
            try:
                snapshot = room.snapshot()
                self._sse_write("snapshot", {"v": P.PROTOCOL_VERSION,
                                             "t": "snapshot",
                                             "snapshot": snapshot},
                                snapshot.get("seq", since))
                self._sse_raw(b"")            # 收尾分块，正常结束 chunked 流
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            room.touch(seat.token, False)
    def _sse_write(self, event_name, payload, event_id):
        frame = "event: %s\nid: %d\ndata: %s\n\n" % (
            event_name, int(event_id), P.dumps(payload))
        self._sse_raw(frame.encode("utf-8"))

    # ------------------------------------------------------------------ WebSocket
    def _route_websocket(self, query):
        if not wsmod.is_websocket_upgrade(self.headers):
            raise P.ProtocolError(P.ERR_BAD_REQUEST, "这不是合法的 WebSocket 升级请求")
        room = self.server.manager.get(query.get("room") or "")
        seat = room.seat_of_token(query.get("token"))
        if seat is None:
            raise RoomError(P.ERR_FORBIDDEN, "WebSocket 需要有效的座位令牌")
        client_key = self.headers.get("Sec-WebSocket-Key")
        since = _query_int(query, "since", 0)

        sock = self.connection
        try:  # 小消息立即发出，跨国链路上少一点延迟
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:  # pragma: no cover
            pass

        connection = wsmod.WebSocket(self.rfile, self.wfile, sock)
        connection.complete_handshake(client_key)
        self.close_connection = True           # 之后由 WS 循环管理连接

        stop = threading.Event()
        out_lock = threading.Lock()
        state = {"token": seat.token, "side": seat.side, "since": since}

        def send(payload):
            with out_lock:
                try:
                    connection.send_text(P.dumps(payload))
                except Exception:  # noqa: BLE001 - 对端已断开
                    stop.set()

        room.touch(seat.token, True)
        send(P.envelope("welcome", room=room.code, side=state["side"],
                        seq=room.snapshot()["seq"], snapshot=room.snapshot(),
                        server_ms=P.now_ms()))

        writer = threading.Thread(target=self._ws_writer,
                                  args=(room, state, stop, send), daemon=True)
        writer.start()
        try:
            self._ws_reader(room, state, stop, send, connection)
        finally:
            stop.set()
            writer.join(timeout=2.0)
            with out_lock:
                try:
                    connection.send_close()
                except Exception:  # noqa: BLE001
                    pass
            room.touch(seat.token, False)

    def _ws_writer(self, room, state, stop, send):
        deadline = time.monotonic() + STREAM_MAX_SECONDS
        while not stop.is_set() and time.monotonic() < deadline:
            payload = room.wait_events(state["since"], timeout=P.HEARTBEAT_SECONDS)
            if stop.is_set():
                return
            if payload.get("t") == "snapshot":
                state["since"] = payload["snapshot"].get("seq", state["since"])
                send(payload)
                continue
            events = payload.get("events") or []
            if events:
                state["since"] = events[-1]["seq"]
                send(payload)
            else:
                send(P.envelope("heartbeat", server_ms=P.now_ms()))
        stop.set()

    def _ws_reader(self, room, state, stop, send, connection):
        while not stop.is_set():
            try:
                opcode, payload = connection.recv(timeout=WS_READ_TIMEOUT)
            except socket.timeout:
                continue
            except wsmod.WSClosed:
                break
            if opcode == 0x8:                     # close
                break
            if opcode not in (0x1, 0x2):
                continue
            try:
                message = P.load(payload)
            except P.ProtocolError as error:
                send(P.error_message(error.code, error.message))
                continue
            try:
                self._ws_dispatch(room, state, message, send)
            except RoomError as error:
                extra = {"snapshot": error.snapshot} if error.snapshot else {}
                send(P.error_message(error.code, error.message, **extra))
            except P.ProtocolError as error:
                send(P.error_message(error.code, error.message))

    def _ws_dispatch(self, room, state, message, send):
        kind = message.get("t")
        if kind == "hello":
            token = message.get("token") or state["token"]
            seat = room.seat_of_token(token)
            if seat is None:
                raise RoomError(P.ERR_FORBIDDEN, "令牌无效")
            state["token"] = seat.token
            state["side"] = seat.side
            since = message.get("since")
            if isinstance(since, int) and not isinstance(since, bool):
                state["since"] = max(0, since)
            room.resume(seat.token, state["since"])
            send(P.envelope("welcome", room=room.code, side=seat.side,
                            seq=room.snapshot()["seq"], snapshot=room.snapshot(),
                            server_ms=P.now_ms()))
            return
        if kind == "sync":
            since = message.get("since")
            if isinstance(since, int) and not isinstance(since, bool):
                state["since"] = max(0, since)
            send(room.events_since(state["since"]))
            return
        if kind == "move":
            iccs = P.require_str(message, "iccs", max_len=P.MAX_ICCS_LEN)
            cid = message.get("cid")
            if cid is not None:
                cid = P.require_str(message, "cid", max_len=P.MAX_CID)
            base_seq = message.get("base_seq")
            if base_seq is not None:
                base_seq = P.require_int(message, "base_seq", minimum=0)
            result = room.apply_move(state["side"], iccs, base_seq=base_seq, cid=cid)
            send(result)
            return
        if kind == "resign":
            send(room.resign(state["side"]))
            return
        if kind == "new":
            send(room.new_game(state["side"]))
            return
        if kind == "ping":
            send(P.envelope("pong", ts=message.get("ts"),
                            server_ms=P.now_ms()))
            return
        raise P.ProtocolError(P.ERR_BAD_REQUEST, "未知消息类型：%r" % kind)


def _query_int(query, key, default):
    raw = query.get(key)
    if raw is None or raw == "":
        return default
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


def _status_for(code):
    return {
        P.ERR_NOT_FOUND: 404,
        P.ERR_ROOM_FULL: 409,
        P.ERR_STALE: 409,
        P.ERR_GAME_OVER: 409,
        P.ERR_NOT_YOUR_TURN: 409,
        P.ERR_ILLEGAL: 409,
        P.ERR_FORBIDDEN: 403,
        P.ERR_TOO_LARGE: 413,
        P.ERR_BAD_REQUEST: 400,
        P.ERR_BAD_ROOM_CODE: 400,
    }.get(code, 400)


class ServerHandle(object):
    """已启动服务器的句柄（供 main.py 与测试使用）。"""

    def __init__(self, server, thread):
        self.server = server
        self.thread = thread

    @property
    def port(self):
        return self.server.server_address[1]

    @property
    def host(self):
        return self.server.server_address[0]

    def url(self, host=None):
        return "http://%s:%d/" % (host or self.host, self.port)

    def stop(self):
        try:
            self.server.shutdown()
        except Exception:  # noqa: BLE001 - 关闭阶段的竞态
            pass
        try:
            self.server.server_close()
        except Exception:  # noqa: BLE001
            pass


def start_server(host=P.DEFAULT_HOST, port=P.DEFAULT_PORT, quiet=True,
                 manager=None):
    """在后台线程启动服务器，立即返回 :class:`ServerHandle`。"""
    server = NetServer((host, port), XQHandler, manager=manager, quiet=quiet)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2},
                              daemon=True, name="xiangqi-net")
    thread.start()
    return ServerHandle(server, thread)


def main(argv=None):
    """命令行入口：``python -m xiangqi.net.server --port 8000``。"""
    import argparse

    parser = argparse.ArgumentParser(
        description="中国象棋联机服务器（标准库，无第三方依赖）")
    parser.add_argument("--host", default=P.DEFAULT_HOST,
                        help="监听地址（默认 127.0.0.1；跨国对战时用 0.0.0.0）")
    parser.add_argument("--port", type=int, default=P.DEFAULT_PORT,
                        help="监听端口（默认 %d）" % P.DEFAULT_PORT)
    parser.add_argument("--verbose", action="store_true", help="打印每个请求")
    args = parser.parse_args(argv)

    handle = start_server(args.host, args.port, quiet=not args.verbose)
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print("=" * 66)
    print("中国象棋联机服务器已启动（Ctrl+C 退出）")
    print("本机地址：%s" % handle.url(shown))
    print("开两个浏览器窗口：一个「创建房间」，另一个输入房间号「加入房间」。")
    print("跨国对战时：--host 0.0.0.0，并建议用 nginx/caddy 反向代理加 HTTPS。")
    print("=" * 66)
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n正在关闭…")
        handle.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
