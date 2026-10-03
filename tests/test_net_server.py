# -*- coding: utf-8 -*-
"""联机服务器端到端测试：真实 TCP + 真实 HTTP/SSE/WebSocket。

测试自己手写 WebSocket 客户端组帧/解帧（只用 ``struct``/``os``/``base64``），
不使用被测模块的编码器，因此对服务端帧实现是独立预言机。

覆盖跨国高延迟最关键的几条链路：两个座位各自的传输、断线重连补发、
重发同一着法的幂等、基于过期局面的拒绝与快照、以及三种传输下的同一套事件。
"""

import base64
import hashlib
import http.client
import json
import os
import socket
import struct
import threading
import time
import unittest

from xiangqi.net import protocol as P
from xiangqi.net.server import start_server
from xiangqi.net.ws import WS_GUID

OP_TEXT = 0x1
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


# ------------------------------------------------------------------ HTTP 工具
class Client(object):
    """极简 HTTP 客户端（每次新建连接，避免 keep-alive 状态互相干扰）。"""

    def __init__(self, host, port):
        self.host = host
        self.port = port

    def request(self, method, path, body=None, timeout=15):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            headers = {"Accept": "application/json"}
            payload = None
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                data = raw
            return response.status, data, dict(response.getheaders())
        finally:
            conn.close()

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, body=None, **kwargs):
        return self.request("POST", path, body=body or {}, **kwargs)


# ------------------------------------------------------- 独立手写 WS 客户端
def ws_client_frame(opcode, payload=b"", fin=True, mask=None):
    payload = bytes(payload)
    if mask is None:
        mask = os.urandom(4)
    head = bytearray()
    head.append((0x80 if fin else 0) | (opcode & 0x0F))
    length = len(payload)
    flag = 0x80 if mask else 0
    if length < 126:
        head.append(flag | length)
    elif length <= 0xFFFF:
        head.append(flag | 126)
        head.extend(struct.pack(">H", length))
    else:
        head.append(flag | 127)
        head.extend(struct.pack(">Q", length))
    if mask:
        head.extend(mask)
        body = bytes(byte ^ mask[index & 3] for index, byte in enumerate(payload))
    else:
        body = payload
    return bytes(head) + body


def read_exact(stream, count):
    chunks = []
    while count > 0:
        chunk = stream.read(count)
        if not chunk:
            raise EOFError("连接提前结束")
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def read_server_frame(stream):
    first, second = read_exact(stream, 2)
    opcode = first & 0x0F
    fin = bool(first & 0x80)
    if second & 0x80:
        raise AssertionError("服务端帧不得带掩码")
    length = second & 0x7F
    if length == 126:
        length = struct.unpack(">H", read_exact(stream, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", read_exact(stream, 8))[0]
    return fin, opcode, (read_exact(stream, length) if length else b"")


class WsSession(object):
    """一个独立的 WebSocket 客户端会话。"""

    def __init__(self, host, port, path):
        self.sock = socket.create_connection((host, port), timeout=5)
        self.key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n"
                   % (path, host, port, self.key))
        self.sock.sendall(request.encode("ascii"))
        self.stream = self.sock.makefile("rb")
        lines = []
        while True:
            line = self.stream.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
            lines.append(line)
        self.raw_head = b"".join(lines).decode("latin-1")
        self.status_line = self.raw_head.split("\r\n")[0]
        self.headers = {}
        for line in self.raw_head.split("\r\n")[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                self.headers[name.strip().lower()] = value.strip()

    @property
    def upgraded(self):
        return "101" in self.status_line

    def send_json(self, payload):
        self.sock.sendall(ws_client_frame(OP_TEXT,
                                          P.dumps(payload).encode("utf-8")))

    def recv_json(self, timeout=6.0):
        """读下一条文本消息（自动跳过 ping，并回 pong）。"""
        self.sock.settimeout(timeout)
        while True:
            fin, opcode, payload = read_server_frame(self.stream)
            if opcode == OP_PING:
                self.sock.sendall(ws_client_frame(OP_PONG, payload))
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                raise EOFError("服务端关闭连接")
            if not fin:
                raise AssertionError("文本消息不应分片")
            return json.loads(payload.decode("utf-8"))

    def wait_for(self, wanted, timeout=6.0):
        """持续读取直到出现指定 ``t`` 类型的消息。"""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("等待 %r 超时" % wanted)
            try:
                message = self.recv_json(timeout=max(0.2, remaining))
            except TimeoutError:
                continue
            if message.get("t") == wanted:
                return message

    def close(self):
        try:
            self.sock.sendall(ws_client_frame(OP_CLOSE, struct.pack(">H", 1000)))
        except OSError:
            pass
        try:
            self.stream.close()
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class ServerCase(unittest.TestCase):
    """公共装置：一个监听随机端口的真实服务器。"""

    @classmethod
    def setUpClass(cls):
        cls.handle = start_server("127.0.0.1", 0, quiet=True)
        cls.host = cls.handle.host
        cls.port = cls.handle.port
        cls.client = Client(cls.host, cls.port)

    @classmethod
    def tearDownClass(cls):
        cls.handle.stop()

    # ------------------------------------------------------------- 便捷方法
    def new_room(self, side=None):
        status, data, _ = self.client.post("/api/rooms", {"side": side} if side else {})
        self.assertEqual(status, 200, data)
        return data

    def join_room(self, code, side=None):
        body = {"side": side} if side else {}
        status, data, _ = self.client.post("/api/rooms/%s/join" % code, body)
        self.assertEqual(status, 200, data)
        return data

    def move(self, code, token, iccs, base_seq=None, cid=None):
        body = {"token": token, "iccs": iccs}
        if base_seq is not None:
            body["base_seq"] = base_seq
        if cid is not None:
            body["cid"] = cid
        return self.client.post("/api/rooms/%s/move" % code, body)


# ------------------------------------------------------------------ 静态资源
class TestStaticAndHealth(ServerCase):
    def test_index_html_served(self):
        status, data, headers = self.client.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        text = data.decode("utf-8")
        self.assertIn("中国象棋", text)
        self.assertIn("app.js", text)
        self.assertIn("logic.js", text)

    def test_client_assets_served_without_external_urls(self):
        for path, expected in (("/app.js", "javascript"),
                               ("/logic.js", "javascript"),
                               ("/style.css", "css")):
            status, data, headers = self.client.get(path)
            self.assertEqual(status, 200, path)
            self.assertIn(expected, headers.get("Content-Type", ""), path)
            text = data.decode("utf-8")
            self.assertNotIn("http://", text, path)
            self.assertNotIn("https://", text, path)
            self.assertNotIn("//cdn", text, path)

    def test_unknown_asset_is_404_json(self):
        status, data, _ = self.client.get("/nope.js")
        self.assertEqual(status, 404)
        self.assertFalse(data["ok"])

    def test_health(self):
        status, data, _ = self.client.get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn("rooms", data)
        self.assertEqual(data["protocol"], P.PROTOCOL_VERSION)


# ------------------------------------------------------------------ 房间/走子
class TestRoomOverHttp(ServerCase):
    def test_create_join_and_room_full(self):
        created = self.new_room()
        self.assertEqual(created["side"], "r")
        self.assertTrue(created["token"])
        self.assertEqual(created["snapshot"]["moves"], [])
        code = created["room"]

        joined = self.join_room(code, "b")
        self.assertEqual(joined["side"], "b")

        status, data, _ = self.client.post("/api/rooms/%s/join" % code, {})
        self.assertEqual(status, 409)
        self.assertEqual(data["error"], P.ERR_ROOM_FULL)

    def test_public_summary_has_no_tokens(self):
        created = self.new_room()
        code, token = created["room"], created["token"]
        joined = self.join_room(code)
        status, data, _ = self.client.get("/api/rooms/%s" % code)
        self.assertEqual(status, 200)
        text = json.dumps(data)
        self.assertNotIn(token, text)
        self.assertNotIn(joined["token"], text)
        self.assertEqual(data["free_sides"], [])
        self.assertEqual(data["seq"], created["snapshot"]["seq"] + 1)

    def test_turn_order_and_illegal_move(self):
        created = self.new_room()
        code, red = created["room"], created["token"]
        black = self.join_room(code)["token"]
        base = created["snapshot"]["seq"]

        status, data, _ = self.move(code, black, "h9g7", base_seq=base)
        self.assertEqual(status, 409)
        self.assertEqual(data["error"], P.ERR_NOT_YOUR_TURN)

        status, data, _ = self.move(code, red, "h2e5", base_seq=base)
        self.assertEqual(status, 409)
        self.assertEqual(data["error"], P.ERR_ILLEGAL)
        self.assertIn("snapshot", data)

        status, data, _ = self.move(code, red, "h2e2", base_seq=base, cid="m1")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["events"][0]["chinese"], "炮二平五")
        self.assertEqual(data["snapshot"]["side"], "b")

    def test_idempotent_cid_and_stale_base_seq(self):
        created = self.new_room()
        code, red = created["room"], created["token"]
        black = self.join_room(code, "b")["token"]
        base = created["snapshot"]["seq"]

        status, first, _ = self.move(code, red, "h2e2", base_seq=base, cid="dup-1")
        self.assertEqual(status, 200)
        status, again, _ = self.move(code, red, "h2e2", base_seq=base, cid="dup-1")
        self.assertEqual(status, 200)
        self.assertTrue(again.get("replayed"))
        self.assertEqual(again["seq"], first["seq"])

        # 轮到黑方，但请求基于开局：必须判定过期并回快照
        status, data, _ = self.move(code, black, "h9g7", base_seq=0, cid="dup-2")
        self.assertEqual(status, 409)
        self.assertEqual(data["error"], P.ERR_STALE)
        self.assertEqual(len(data["snapshot"]["moves"]), 1)

    def test_missing_or_bad_token_is_forbidden(self):
        created = self.new_room()
        code = created["room"]
        status, data, _ = self.client.post("/api/rooms/%s/move" % code,
                                           {"iccs": "h2e2"})
        self.assertEqual(status, 403)
        self.assertEqual(data["error"], P.ERR_FORBIDDEN)
        status, data, _ = self.client.post("/api/rooms/%s/move" % code,
                                           {"token": "nope", "iccs": "h2e2"})
        self.assertEqual(status, 403)

    def test_unknown_room_and_bad_code(self):
        status, data, _ = self.client.get("/api/rooms/ZZZZZZ")
        self.assertEqual(status, 404)
        self.assertEqual(data["error"], P.ERR_NOT_FOUND)
        status, data, _ = self.client.get("/api/rooms/not-a-code")
        self.assertEqual(status, 400)

    def test_resign_and_new_game(self):
        created = self.new_room()
        code, red = created["room"], created["token"]
        black = self.join_room(code)["token"]

        status, data, _ = self.client.post("/api/rooms/%s/resign" % code,
                                           {"token": red})
        self.assertEqual(status, 200)
        self.assertEqual(data["snapshot"]["result"]["winner"], "b")

        status, data, _ = self.move(code, black, "h9g7")
        self.assertEqual(status, 409)
        self.assertEqual(data["error"], P.ERR_GAME_OVER)

        status, data, _ = self.client.post("/api/rooms/%s/new" % code,
                                           {"token": black})
        self.assertEqual(status, 200)
        self.assertIsNone(data["snapshot"]["result"])
        self.assertEqual(data["snapshot"]["moves"], [])

    def test_resume_replays_missed_events(self):
        created = self.new_room()
        code, red = created["room"], created["token"]
        black = self.join_room(code)["token"]
        self.move(code, red, "h2e2")
        seq = self.client.get("/api/rooms/%s" % code)[1]["seq"]
        self.move(code, black, "h9g7")

        status, data, _ = self.client.post("/api/rooms/%s/resume" % code,
                                           {"token": red, "since": created["snapshot"]["seq"]})
        self.assertEqual(status, 200)
        self.assertTrue(any(ev["t"] == P.EV_MOVE for ev in data["events"]))
        self.assertGreaterEqual(data["snapshot"]["seq"], seq + 1)
        self.assertEqual(len(data["snapshot"]["moves"]), 2)

        status, data, _ = self.client.post("/api/rooms/%s/resume" % code,
                                           {"token": "bogus", "since": 0})
        self.assertEqual(status, 403)

    def test_resume_contract_and_too_old_position(self):
        """契约：events 一律 > since 且 <= snapshot.seq；位置过旧只回快照。"""
        created = self.new_room()
        code, red = created["room"], created["token"]
        black = self.join_room(code, "b")["token"]
        since = self.client.get("/api/rooms/%s" % code)[1]["seq"]
        self.move(code, red, "h2e2")
        self.move(code, black, "h9g7")

        status, data, _ = self.client.post("/api/rooms/%s/resume" % code,
                                           {"token": red, "since": since})
        self.assertEqual(status, 200)
        self.assertEqual(data["side"], "r")
        self.assertTrue(data["events"])
        for event in data["events"]:
            self.assertGreater(event["seq"], since)
            self.assertLessEqual(event["seq"], data["snapshot"]["seq"])
        self.assertEqual(len(data["snapshot"]["moves"]), 2)

        status, data, _ = self.client.post("/api/rooms/%s/resume" % code,
                                           {"token": red, "since": 10 ** 6})
        self.assertEqual(status, 200)
        self.assertEqual(data["events"], [], "位置过旧时不应混着回事件")
        self.assertEqual(len(data["snapshot"]["moves"]), 2)

    def test_long_poll_wait_is_optional(self):
        created = self.new_room()
        code, red = created["room"], created["token"]
        self.move(code, red, "h2e2")
        status, data, _ = self.client.get(
            "/api/rooms/%s/events?token=%s&since=0" % (code, red))     # 不带 wait
        self.assertEqual(status, 200)
        self.assertEqual(data["t"], "events")
        status, data, _ = self.client.get(
            "/api/rooms/%s/events?token=%s&since=0&wait=abc" % (code, red))
        self.assertEqual(status, 200, "非数字 wait 应被容忍")

    def test_unknown_method_returns_json(self):
        """http.server 默认的 HTML 错误页也必须被换成 JSON。"""
        status, data, headers = self.client.request("PUT", "/api/rooms", {})
        self.assertEqual(status, 501)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertFalse(data["ok"])
        self.assertIn("error", data)

    def test_body_size_and_json_errors(self):
        status, data, _ = self.client.post("/api/rooms", {"side": "x"})
        self.assertEqual(status, 400)
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            conn.request("POST", "/api/rooms", body=b"{not json",
                         headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
        finally:
            conn.close()


# ------------------------------------------------------------------ 长轮询/SSE
class TestLongPollAndSse(ServerCase):
    def test_long_poll_returns_pending_events_immediately(self):
        created = self.new_room()
        code, red = created["room"], created["token"]
        self.move(code, red, "h2e2")
        status, data, _ = self.client.get(
            "/api/rooms/%s/events?token=%s&since=0&wait=5" % (code, red))
        self.assertEqual(status, 200)
        self.assertEqual(data["t"], "events")
        self.assertTrue(any(ev.get("iccs") == "h2e2" for ev in data["events"]))

    def test_long_poll_wakes_on_opponent_move(self):
        created = self.new_room()
        code, red = created["room"], created["token"]
        black = self.join_room(code)["token"]
        # 入座本身也会产生事件，因此从“当前”序号开始等，才能真正验证被唤醒
        since = self.client.get("/api/rooms/%s" % code)[1]["seq"]
        result = {}

        def poll():
            result["value"] = self.client.get(
                "/api/rooms/%s/events?token=%s&since=%d&wait=10" % (code, red, since))

        thread = threading.Thread(target=poll)
        thread.start()
        time.sleep(0.3)
        self.move(code, red, "h2e2")
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive(), "长轮询没有被及时唤醒")
        status, data, _ = result["value"]
        self.assertEqual(status, 200)
        self.assertTrue(any(ev.get("iccs") == "h2e2" for ev in data["events"]))

    def test_sse_honours_last_event_id_on_reconnect(self):
        """SSE 断线重连时浏览器会带 Last-Event-ID，服务器应用它校正 since。"""
        created = self.new_room()
        code, red = created["room"], created["token"]
        self.move(code, red, "h2e2")
        current = self.client.get("/api/rooms/%s" % code)[1]["seq"]

        conn = http.client.HTTPConnection(self.host, self.port, timeout=15)
        try:
            # 查询串故意给 since=0（陈旧），但 Last-Event-ID 是准确的
            conn.request("GET", "/api/rooms/%s/stream?token=%s&since=0" % (code, red),
                         headers={"Last-Event-ID": str(current)})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.readline().decode().strip(), "retry: 3000")
            hello = self._read_sse_event(response)
            self.assertEqual(hello["t"], "welcome")
            self.assertIn("snapshot", hello)

            # 连接本身就会产生新事件（在线状态），再加入黑方制造更多事件
            self.join_room(code, "b")
            batch = self._read_sse_event(response, want_type="events")
            seqs = [ev["seq"] for ev in batch["events"]]
            self.assertTrue(seqs, "应当收到新事件")
            self.assertGreater(min(seqs), current,
                               "Last-Event-ID 之后的事件不应重发：%s" % seqs)
        finally:
            conn.close()

    def test_long_poll_requires_token(self):
        created = self.new_room()
        code = created["room"]
        status, data, _ = self.client.get("/api/rooms/%s/events?since=0&wait=0" % code)
        self.assertEqual(status, 403)

    def test_sse_stream_pushes_move(self):
        created = self.new_room()
        code, red = created["room"], created["token"]
        black = self.join_room(code)["token"]
        since = self.client.get("/api/rooms/%s" % code)[1]["seq"]

        conn = http.client.HTTPConnection(self.host, self.port, timeout=15)
        try:
            conn.request("GET", "/api/rooms/%s/stream?token=%s&since=%d"
                         % (code, red, since))
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("text/event-stream", response.getheader("Content-Type", ""))
            self.assertEqual(response.getheader("X-Accel-Buffering"), "no")

            hello = self._read_sse_event(response)
            self.assertEqual(hello["t"], "welcome")     # 首帧 event: hello，载荷是 welcome
            self.assertIn("snapshot", hello)

            # 连接建立本身会产生事件（补齐批次），因此持续读到“对手着法”为止
            self.move(code, red, "h2e2")
            found = False
            deadline = time.monotonic() + 10
            while not found and time.monotonic() < deadline:
                batch = self._read_sse_event(
                    response, want_type="events",
                    timeout=max(0.5, deadline - time.monotonic()))
                found = any(ev.get("iccs") == "h2e2" for ev in batch["events"])
            self.assertTrue(found, "SSE 未推送着法")
        finally:
            conn.close()

    @staticmethod
    def _read_sse_event(response, want_type=None, timeout=10):
        deadline = time.monotonic() + timeout
        event_type = None
        while time.monotonic() < deadline:
            line = response.readline()
            if not line:
                raise AssertionError("SSE 流被过早关闭")
            text = line.decode("utf-8").rstrip("\n")
            if text.startswith(":"):
                continue                      # 心跳注释
            if text.startswith("event:"):
                event_type = text.split(":", 1)[1].strip()
                continue
            if text.startswith("data:"):
                if want_type is None or event_type == want_type:
                    return json.loads(text.split(":", 1)[1].strip())
                event_type = None
        raise AssertionError("等待 SSE 事件超时")


# ------------------------------------------------------------------ WebSocket
class TestWebSocket(ServerCase):
    def _pair(self):
        created = self.new_room()
        code, red = created["room"], created["token"]
        joined = self.join_room(code, "b")
        black = joined["token"]
        since = created["snapshot"]["seq"]
        red_ws = WsSession(self.host, self.port,
                           "/ws?room=%s&token=%s&since=%d" % (code, red, since))
        self.addCleanup(red_ws.close)
        black_ws = WsSession(self.host, self.port,
                             "/ws?room=%s&token=%s&since=%d" % (code, black, since))
        self.addCleanup(black_ws.close)
        return code, red, red_ws, black, black_ws

    def test_handshake_and_welcome(self):
        created = self.new_room()
        session = WsSession(self.host, self.port,
                            "/ws?room=%s&token=%s&since=0"
                            % (created["room"], created["token"]))
        self.addCleanup(session.close)
        self.assertTrue(session.upgraded, session.status_line)
        expected = base64.b64encode(
            hashlib.sha1((session.key + WS_GUID).encode("ascii")).digest()).decode("ascii")
        self.assertEqual(session.headers.get("sec-websocket-accept"), expected)
        self.assertEqual(session.headers.get("sec-websocket-version"), "13")
        welcome = session.recv_json()
        self.assertEqual(welcome["t"], "welcome")
        self.assertEqual(welcome["side"], "r")
        self.assertEqual(welcome["snapshot"]["side"], "r")

    def test_bad_token_is_rejected_before_upgrade(self):
        created = self.new_room()
        session = WsSession(self.host, self.port,
                            "/ws?room=%s&token=bogus&since=0" % created["room"])
        self.addCleanup(session.close)
        self.assertFalse(session.upgraded)
        self.assertIn("403", session.status_line)

    def test_missing_upgrade_headers_is_400(self):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            conn.request("GET", "/ws")
            self.assertEqual(conn.getresponse().status, 400)
        finally:
            conn.close()

    @staticmethod
    def _collect_until(session, predicate, timeout=8.0):
        """持续读取消息直到条件成立，返回读到的全部消息（真实客户端也必须这样处理补齐批次）。"""
        messages = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = session.recv_json(timeout=max(0.2, deadline - time.monotonic()))
            messages.append(message)
            if predicate(message):
                return messages
        raise AssertionError("等待条件超时")

    def test_two_players_play_over_websocket(self):
        code, red, red_ws, black, black_ws = self._pair()
        welcome_red = red_ws.recv_json()
        welcome_black = black_ws.recv_json()
        self.assertEqual(welcome_red["side"], "r")
        self.assertEqual(welcome_black["side"], "b")
        base = welcome_red["snapshot"]["seq"]

        red_ws.send_json({"t": "move", "iccs": "h2e2", "base_seq": base,
                          "cid": "ws-1"})

        def has_move(message, iccs):
            return any(ev.get("iccs") == iccs for ev in message.get("events", []))

        red_messages = self._collect_until(
            red_ws, lambda m: m.get("t") == "ack" and has_move(m, "h2e2"))
        ack = [m for m in red_messages if m.get("t") == "ack"][0]
        self.assertEqual(ack["events"][0]["chinese"], "炮二平五")

        # 对手也必须收到这一步（走子方自己也收到，客户端按 seq 去重）
        self._collect_until(
            black_ws, lambda m: m.get("t") == "events" and has_move(m, "h2e2"))

        # 轮到黑方，用 WS 走一步
        black_ws.send_json({"t": "move", "iccs": "h9g7",
                            "base_seq": ack["seq"], "cid": "ws-2"})
        black_messages = self._collect_until(
            black_ws, lambda m: m.get("t") == "ack" and has_move(m, "h9g7"))
        ack2 = [m for m in black_messages if m.get("t") == "ack"][0]
        self.assertEqual(ack2["snapshot"]["side"], "r")
        self.assertEqual(len(ack2["snapshot"]["moves"]), 2)

        # ping/pong 与同步请求
        red_ws.send_json({"t": "ping", "ts": 12345})
        pong = red_ws.wait_for("pong")
        self.assertEqual(pong["ts"], 12345)

        # sync：位置仍保留时补事件（唯一同时含两着法的批次就是补发应答），
        # 位置过旧时整包快照
        red_ws.send_json({"t": "sync", "since": 0})
        catchup = self._collect_until(
            red_ws,
            lambda m: m.get("t") == "events"
            and {"h2e2", "h9g7"} <= {ev.get("iccs") for ev in m.get("events", [])})[-1]
        self.assertGreaterEqual(
            sum(1 for ev in catchup["events"] if ev.get("t") == P.EV_MOVE), 2)

        red_ws.send_json({"t": "sync", "since": 10 ** 6})
        sync = red_ws.wait_for("snapshot")
        self.assertEqual(len(sync["snapshot"]["moves"]), 2)

    def test_websocket_rejects_stale_and_duplicate(self):
        code, red, red_ws, black, black_ws = self._pair()
        base = red_ws.recv_json()["snapshot"]["seq"]
        black_ws.recv_json()

        red_ws.send_json({"t": "move", "iccs": "h2e2", "base_seq": base,
                          "cid": "dup"})
        first = red_ws.wait_for("ack")
        red_ws.send_json({"t": "move", "iccs": "h2e2", "base_seq": base,
                          "cid": "dup"})
        again = red_ws.wait_for("ack")
        self.assertTrue(again.get("replayed"))
        self.assertEqual(again["seq"], first["seq"])

        red_ws.send_json({"t": "move", "iccs": "h2e2", "base_seq": 0})
        error = red_ws.wait_for("error")
        self.assertEqual(error["code"], P.ERR_NOT_YOUR_TURN)

        # 轮到黑方，但请求基于开局：必须判定过期
        black_ws.send_json({"t": "move", "iccs": "h9g7", "base_seq": 0})
        stale = black_ws.wait_for("error")
        self.assertEqual(stale["code"], P.ERR_STALE)
        self.assertEqual(len(stale["snapshot"]["moves"]), 1)

    def test_websocket_move_and_resign_flow(self):
        code, red, red_ws, black, black_ws = self._pair()
        red_ws.recv_json()
        black_ws.recv_json()
        red_ws.send_json({"t": "resign"})
        ack = red_ws.wait_for("ack")
        self.assertEqual(ack["snapshot"]["result"]["winner"], "b")
        black_ws.send_json({"t": "move", "iccs": "h9g7"})
        error = black_ws.wait_for("error")
        self.assertEqual(error["code"], P.ERR_GAME_OVER)

        black_ws.send_json({"t": "new"})
        again = black_ws.wait_for("ack")
        self.assertEqual(again["snapshot"]["moves"], [])

    def test_unknown_websocket_message_type(self):
        created = self.new_room()
        session = WsSession(self.host, self.port,
                            "/ws?room=%s&token=%s&since=0"
                            % (created["room"], created["token"]))
        self.addCleanup(session.close)
        session.recv_json()
        session.send_json({"t": "explode"})
        error = session.wait_for("error")
        self.assertEqual(error["code"], P.ERR_BAD_REQUEST)

    def test_events_survive_a_reconnect_with_since(self):
        """模拟跨国链路掉线：重连时带 since，补齐断线期间的着法。"""
        code, red, red_ws, black, black_ws = self._pair()
        base = red_ws.recv_json()["snapshot"]["seq"]
        black_ws.recv_json()
        red_ws.send_json({"t": "move", "iccs": "h2e2", "base_seq": base,
                          "cid": "r-1"})
        red_ws.wait_for("ack")
        black_ws.wait_for("events")
        black_ws.close()                      # 黑方掉线

        # 红方（轮到黑方，但红方可以继续观察）——黑方回来时用 since 补发
        red_ws.send_json({"t": "ping", "ts": 1})
        red_ws.wait_for("pong")

        resumed = WsSession(self.host, self.port,
                            "/ws?room=%s&token=%s&since=0" % (code, black))
        self.addCleanup(resumed.close)
        welcome = resumed.recv_json()
        self.assertEqual(welcome["side"], "b")
        self.assertEqual(len(welcome["snapshot"]["moves"]), 1)
        self.assertEqual(welcome["snapshot"]["moves"][0]["iccs"], "h2e2")


if __name__ == "__main__":
    unittest.main()
