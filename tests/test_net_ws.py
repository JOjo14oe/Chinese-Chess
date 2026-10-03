# -*- coding: utf-8 -*-
"""WebSocket 服务端实现测试（RFC 6455）。

客户端侧的组帧/解帧**独立手写**（只用 ``struct``），
不使用被测模块的编码器，保证测试是真正的“预言机”。
"""

import base64
import email.parser
import hashlib
import http.server
import os
import socket
import socketserver
import struct
import threading
import time
import unittest

from xiangqi.net import ws

WS_PATH = "/ws"

# ------------------------------------------------------------------ 客户端预言机
_FIN = 0x80
_OP_CONT = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


def client_frame(opcode, payload=b"", fin=True, mask=None, rsv=0,
                 force_length=None):
    """从零手写一帧客户端数据（默认带随机掩码），只依赖 struct/os。"""
    payload = bytes(payload)
    if mask is None:
        mask = os.urandom(4)
    elif isinstance(mask, str):
        mask = mask.encode("ascii")
    masked = bool(mask)
    length = len(payload) if force_length is None else force_length
    head = bytearray()
    head.append((_FIN if fin else 0) | ((rsv & 0x7) << 4) | (opcode & 0x0F))
    flag = 0x80 if masked else 0x00
    if length < 126:
        head.append(flag | length)
    elif length <= 0xFFFF:
        head.append(flag | 126)
        head.extend(struct.pack(">H", length))
    else:
        head.append(flag | 127)
        head.extend(struct.pack(">Q", length))
    if masked:
        head.extend(mask)
        body = bytes(byte ^ mask[index & 3]
                     for index, byte in enumerate(payload))
    else:
        body = payload
    return bytes(head) + body


def masked_text(text):
    """把字符串编码后打成掩码文本帧。"""
    return client_frame(_OP_TEXT, text.encode("utf-8"))


def client_pong(payload=b""):
    """掩码 pong 帧（用于回答服务端心跳）。"""
    return client_frame(_OP_PONG, payload)


def read_exact(stream, count):
    """从流里精确读取 count 字节；EOF 抛 EOFError。"""
    chunks = []
    remaining = count
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError("连接提前结束，还差 %d 字节" % remaining)
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_server_frame(stream):
    """解析一帧服务端发来的帧，返回 (fin, opcode, payload)。"""
    first, second = read_exact(stream, 2)
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    if second & 0x80:
        raise AssertionError("服务端帧不得带掩码")
    length = second & 0x7F
    if length == 126:
        length = struct.unpack(">H", read_exact(stream, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", read_exact(stream, 8))[0]
    return fin, opcode, (read_exact(stream, length) if length else b"")


def parse_http_response(raw):
    """把 101 响应解析成 (状态行, 头部字典)，头部名小写。"""
    head = raw.partition(b"\r\n\r\n")[0]
    lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    return lines[0], headers


def message_from_raw(headers_text):
    """用 email.parser 把原始 HTTP 请求文本解析成 HTTPMessage。

    ``email`` 会把请求行当成“Unix From 行”丢掉，因此这里显式滤掉它，
    其余头部交给 ``email.parser`` 真正解析。
    """
    lines = headers_text.split("\r\n")
    if lines and lines[0].startswith("GET "):
        lines = lines[1:]
    return email.parser.Parser().parsestr("\r\n".join(lines))


def masked_accept(key):
    """独立算出期望的 Sec-WebSocket-Accept（预言机）。"""
    digest = hashlib.sha1((key + ws.WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


class _MemoryWriter:
    """收集写入字节的假 wfile。"""

    def __init__(self):
        self.data = bytearray()
        self.flushes = 0

    def write(self, data):
        self.data.extend(data)

    def flush(self):
        self.flushes += 1


class _Reader:
    """把已读到的字节包成一个小文件对象，只实现 read()。"""

    def __init__(self, data):
        self._data = data

    def read(self, count):
        chunk, self._data = self._data[:count], self._data[count:]
        return chunk


# ------------------------------------------------------------------ 握手计算
class TestAcceptKey(unittest.TestCase):
    def test_rfc6455_vector(self):
        self.assertEqual(ws.accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
                         "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_guid_constant(self):
        self.assertEqual(ws.WS_GUID, "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")

    def test_matches_independent_sha1(self):
        for key in ("x3JJHMbDL1EzLkh9GBhXDw==", "abc", "AAAA"):
            self.assertEqual(ws.accept_key(key), masked_accept(key))

    def test_is_deterministic_and_key_sensitive(self):
        self.assertEqual(ws.accept_key("abc"), ws.accept_key("abc"))
        self.assertNotEqual(ws.accept_key("abc"), ws.accept_key("abd"))

    def test_matches_real_browser_handshake_vector(self):
        self.assertEqual(ws.accept_key("x3JJHMbDL1EzLkh9GBhXDw=="),
                         "HSmrc0sMlYUkAGmm5OPpG2HaGWk=")


# ------------------------------------------------------------------ 升级判定
WELL_FORMED_ITEMS = [
    ("Host", "127.0.0.1:8000"),
    ("Upgrade", "websocket"),
    ("Connection", "Upgrade"),
    ("Sec-WebSocket-Key", "dGhlIHNhbXBsZSBub25jZQ=="),
    ("Sec-WebSocket-Version", "13"),
]


def headers_of(items):
    """按 (名, 值) 列表构造 HTTPMessage（http.server 的 handler.headers 类型）。"""
    message = email.message.Message()
    for name, value in items:
        message[name] = value
    return message


def without(name):
    """去掉某个头部后的头部集合。"""
    return headers_of([(key, value) for key, value in WELL_FORMED_ITEMS
                       if key.lower() != name.lower()])


def replacing(name, value):
    """替换某个头部的值（或新增）后的头部集合。"""
    items = [(key, val) for key, val in WELL_FORMED_ITEMS
             if key.lower() != name.lower()]
    items.append((name, value))
    return headers_of(items)


WELL_FORMED = ("GET /ws HTTP/1.1\r\n"
               "Host: 127.0.0.1:8000\r\n"
               "Upgrade: websocket\r\n"
               "Connection: Upgrade\r\n"
               "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
               "Sec-WebSocket-Version: 13\r\n"
               "\r\n")


class TestIsWebSocketUpgrade(unittest.TestCase):
    def test_well_formed(self):
        headers = headers_of(WELL_FORMED_ITEMS)
        self.assertIsInstance(headers, email.message.Message)
        self.assertEqual(headers.get("Upgrade"), "websocket")
        self.assertTrue(ws.is_websocket_upgrade(headers))

    def test_real_request_bytes_parsed_by_email(self):
        """真实请求文本经 email.parser 解析后同样判定为升级请求。"""
        headers = message_from_raw(WELL_FORMED)
        self.assertEqual(headers.get("Connection"), "Upgrade")
        self.assertEqual(headers.get("Sec-WebSocket-Version"), "13")
        self.assertTrue(ws.is_websocket_upgrade(headers))

    def test_case_insensitive_header_names_and_values(self):
        headers = headers_of([
            ("Host", "127.0.0.1"),
            ("UPGRADE", "WebSocket"),
            ("connection", "upgrade"),
            ("sec-websocket-key", "dGhlIHNhbXBsZSBub25jZQ=="),
            ("SEC-WEBSOCKET-VERSION", "13"),
        ])
        self.assertTrue(ws.is_websocket_upgrade(headers))

    def test_connection_with_multiple_tokens(self):
        headers = replacing("Connection", "keep-alive, UPGRADE")
        self.assertTrue(ws.is_websocket_upgrade(headers))
        self.assertTrue(ws.is_websocket_upgrade(
            replacing("Connection", "Upgrade, keep-alive")))

    def test_connection_token_is_not_substring_match(self):
        self.assertFalse(ws.is_websocket_upgrade(
            replacing("Connection", "upgraded")))

    def test_missing_or_wrong_pieces(self):
        cases = [
            ("缺 Connection", without("Connection")),
            ("缺 Upgrade", without("Upgrade")),
            ("Upgrade 值不对", replacing("Upgrade", "h2c")),
            ("Upgrade 为空", replacing("Upgrade", "")),
            ("缺 Version", without("Sec-WebSocket-Version")),
            ("Version 不是 13", replacing("Sec-WebSocket-Version", "8")),
            ("Version 带引号",
             replacing("Sec-WebSocket-Version", '"13"')),
            ("缺 Key", without("Sec-WebSocket-Key")),
            ("Key 为空", replacing("Sec-WebSocket-Key", "")),
            ("Key 只有空白", replacing("Sec-WebSocket-Key", "   ")),
        ]
        for label, headers in cases:
            self.assertFalse(ws.is_websocket_upgrade(headers),
                             "应判定为非升级请求：%s" % label)

    def test_plain_get_is_not_upgrade(self):
        raw = "GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: */*\r\n\r\n"
        self.assertFalse(ws.is_websocket_upgrade(message_from_raw(raw)))

    def test_none_headers(self):
        self.assertFalse(ws.is_websocket_upgrade(None))


# ------------------------------------------------------------------ 增量解码
class TestFrameDecoder(unittest.TestCase):
    def test_single_masked_text_frame(self):
        decoder = ws.FrameDecoder()
        self.assertEqual(decoder.feed(client_frame(_OP_TEXT, b"hello")),
                         [(_OP_TEXT, b"hello")])
        self.assertEqual(decoder.buffered, 0)

    def test_empty_payload(self):
        decoder = ws.FrameDecoder()
        self.assertEqual(decoder.feed(client_frame(_OP_TEXT, b"")),
                         [(_OP_TEXT, b"")])

    def test_two_frames_glued_in_one_feed(self):
        decoder = ws.FrameDecoder()
        blob = client_frame(_OP_TEXT, b"one") + client_frame(_OP_TEXT, b"two")
        self.assertEqual(decoder.feed(blob),
                         [(_OP_TEXT, b"one"), (_OP_TEXT, b"two")])
        self.assertEqual(decoder.buffered, 0)

    def test_glued_pair_with_binary_second_frame(self):
        decoder = ws.FrameDecoder()
        blob = client_frame(_OP_TEXT, b"one") + client_frame(_OP_BINARY, b"\x00\xff")
        self.assertEqual(decoder.feed(blob),
                         [(_OP_TEXT, b"one"), (_OP_BINARY, b"\x00\xff")])

    def test_frame_split_across_three_feeds(self):
        decoder = ws.FrameDecoder()
        payload = "中文分片".encode("utf-8")
        frame = client_frame(_OP_TEXT, payload)
        third = len(frame) // 3
        self.assertEqual(decoder.feed(frame[:third]), [])
        self.assertGreater(decoder.buffered, 0)
        self.assertEqual(decoder.feed(frame[third:2 * third]), [])
        self.assertEqual(decoder.feed(frame[2 * third:]),
                         [(_OP_TEXT, payload)])
        self.assertEqual(decoder.buffered, 0)

    def test_byte_by_byte_feeding(self):
        decoder = ws.FrameDecoder()
        frame = client_frame(_OP_BINARY, b"abc")
        messages = []
        for index in range(len(frame)):
            messages.extend(decoder.feed(frame[index:index + 1]))
        self.assertEqual(messages, [(_OP_BINARY, b"abc")])

    def test_126_length_path(self):
        decoder = ws.FrameDecoder()
        payload = bytes(bytearray(range(200)))
        frame = client_frame(_OP_BINARY, payload)
        self.assertEqual(frame[1] & 0x7F, 126)
        self.assertEqual(decoder.feed(frame), [(_OP_BINARY, payload)])

    def test_127_length_path(self):
        decoder = ws.FrameDecoder()
        payload = bytes(bytearray(index % 251 for index in range(70000)))
        frame = client_frame(_OP_BINARY, payload)
        self.assertEqual(frame[1] & 0x7F, 127)
        self.assertEqual(decoder.feed(frame), [(_OP_BINARY, payload)])

    def test_length_encoding_boundaries(self):
        for size in (0, 1, 125, 126, 65535, 65536):
            decoder = ws.FrameDecoder()
            payload = b"z" * size
            self.assertEqual(decoder.feed(client_frame(_OP_TEXT, payload)),
                             [(_OP_TEXT, payload)], "长度 %d 解码失败" % size)

    def test_masked_binary_with_extreme_masks(self):
        for mask in (b"\x00\x00\x00\x00", b"\xff\xff\xff\xff",
                     b"\x01\x02\x03\x04"):
            decoder = ws.FrameDecoder()
            self.assertEqual(
                decoder.feed(client_frame(_OP_BINARY, b"\x00\xff\x10", mask=mask)),
                [(_OP_BINARY, b"\x00\xff\x10")])

    def test_fragmented_text_aggregated_into_one_message(self):
        decoder = ws.FrameDecoder()
        first = client_frame(_OP_TEXT, b"Hel", fin=False)
        second = client_frame(_OP_CONT, b"lo ", fin=False)
        third = client_frame(_OP_CONT, "世界".encode("utf-8"), fin=True)
        self.assertEqual(decoder.feed(first), [])
        self.assertTrue(decoder.fragmented)
        self.assertEqual(decoder.feed(second), [])
        self.assertEqual(decoder.feed(third),
                         [(_OP_TEXT, b"Hello " + "世界".encode("utf-8"))])
        self.assertFalse(decoder.fragmented)
        self.assertEqual(decoder.buffered, 0)

    def test_fragmented_binary_keeps_first_opcode(self):
        decoder = ws.FrameDecoder()
        blob = (client_frame(_OP_BINARY, b"\x01\x02", fin=False) +
                client_frame(_OP_CONT, b"\x03", fin=True))
        self.assertEqual(decoder.feed(blob), [(_OP_BINARY, b"\x01\x02\x03")])

    def test_fragmented_message_split_across_feeds(self):
        decoder = ws.FrameDecoder()
        first = client_frame(_OP_TEXT, b"a", fin=False)
        rest = client_frame(_OP_CONT, b"b", fin=True)
        self.assertEqual(decoder.feed(first[:3]), [])
        self.assertEqual(decoder.feed(first[3:] + rest[:2]), [])
        self.assertEqual(decoder.feed(rest[2:]), [(_OP_TEXT, b"ab")])

    def test_control_frames_returned_immediately(self):
        decoder = ws.FrameDecoder()
        blob = (client_frame(_OP_PING, b"hi") +
                client_frame(_OP_PONG, b"") +
                client_frame(_OP_CLOSE, struct.pack(">H", 1000)))
        self.assertEqual(decoder.feed(blob),
                         [(_OP_PING, b"hi"), (_OP_PONG, b""),
                          (_OP_CLOSE, struct.pack(">H", 1000))])

    def test_control_frame_interleaved_in_fragmented_message(self):
        decoder = ws.FrameDecoder()
        blob = (client_frame(_OP_TEXT, b"a", fin=False) +
                client_frame(_OP_PING, b"beat") +
                client_frame(_OP_CONT, b"b", fin=True))
        self.assertEqual(decoder.feed(blob),
                         [(_OP_PING, b"beat"), (_OP_TEXT, b"ab")])

    def test_unmasked_client_frame_rejected(self):
        decoder = ws.FrameDecoder()
        with self.assertRaises(ValueError):
            decoder.feed(client_frame(_OP_TEXT, b"hi", mask=b""))

    def test_oversized_payload_rejected(self):
        decoder = ws.FrameDecoder(max_payload=1024)
        with self.assertRaises(ValueError):
            decoder.feed(client_frame(_OP_BINARY, b"x" * 2048))

    def test_oversized_length_rejected_before_body_arrives(self):
        """长度域已声明超限时不必等载荷到齐就报错。"""
        decoder = ws.FrameDecoder(max_payload=1024)
        frame = client_frame(_OP_BINARY, b"x" * 2048)
        with self.assertRaises(ValueError):
            decoder.feed(frame[:4])

    def test_oversized_payload_within_limit_is_accepted(self):
        decoder = ws.FrameDecoder(max_payload=1024)
        payload = b"y" * 1024
        self.assertEqual(decoder.feed(client_frame(_OP_BINARY, payload)),
                         [(_OP_BINARY, payload)])

    def test_rsv_bits_rejected(self):
        for rsv in (0x1, 0x2, 0x4):
            decoder = ws.FrameDecoder()
            with self.assertRaises(ValueError, msg="RSV=0x%X 应被拒绝" % rsv):
                decoder.feed(client_frame(_OP_TEXT, b"hi", rsv=rsv))

    def test_control_frame_payload_over_125_rejected(self):
        decoder = ws.FrameDecoder()
        with self.assertRaises(ValueError):
            decoder.feed(client_frame(_OP_PING, b"p" * 126))
        decoder = ws.FrameDecoder()
        self.assertEqual(decoder.feed(client_frame(_OP_PING, b"p" * 125)),
                         [(_OP_PING, b"p" * 125)])

    def test_fragmented_control_frame_rejected(self):
        decoder = ws.FrameDecoder()
        with self.assertRaises(ValueError):
            decoder.feed(client_frame(_OP_PING, b"p", fin=False))

    def test_unknown_opcode_rejected(self):
        decoder = ws.FrameDecoder()
        with self.assertRaises(ValueError):
            decoder.feed(client_frame(0x3, b"hi"))

    def test_continuation_without_start_frame_rejected(self):
        decoder = ws.FrameDecoder()
        with self.assertRaises(ValueError):
            decoder.feed(client_frame(_OP_CONT, b"hi"))

    def test_non_minimal_126_length_encoding_rejected(self):
        decoder = ws.FrameDecoder()
        frame = client_frame(_OP_TEXT, b"hi")
        forged = (bytes([frame[0], 0x80 | 126]) + struct.pack(">H", 2) +
                  frame[2:])
        with self.assertRaises(ValueError):
            decoder.feed(forged)

    def test_high_bit_of_64bit_length_must_be_zero(self):
        decoder = ws.FrameDecoder()
        forged = (bytes([0x82, 0x80 | 127]) +
                  struct.pack(">Q", 1 << 63) + b"mask")
        with self.assertRaises(ValueError):
            decoder.feed(forged)

    def test_invalid_max_payload_rejected(self):
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                ws.FrameDecoder(max_payload=bad)


# ------------------------------------------------------------------ 发送组帧
class TestServerFrames(unittest.TestCase):
    def make(self, max_payload=ws.DEFAULT_MAX_PAYLOAD):
        writer = _MemoryWriter()
        return ws.WebSocket(None, writer, None, max_payload), writer

    def test_handshake_response(self):
        conn, writer = self.make()
        conn.complete_handshake("dGhlIHNhbXBsZSBub25jZQ==")
        status, headers = parse_http_response(bytes(writer.data))
        self.assertEqual(status, "HTTP/1.1 101 Switching Protocols")
        self.assertEqual(headers.get("sec-websocket-accept"),
                         "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")
        self.assertEqual(headers.get("sec-websocket-version"), "13")
        self.assertEqual(headers.get("upgrade", "").lower(), "websocket")
        self.assertIn("upgrade", headers.get("connection", "").lower())
        self.assertEqual(writer.flushes, 1)

    def test_send_text_frame_layout(self):
        conn, writer = self.make()
        conn.send_text("hi")
        self.assertEqual(bytes(writer.data), b"\x81\x02hi")

    def test_send_text_utf8_bytes(self):
        conn, writer = self.make()
        conn.send_text("红")
        self.assertEqual(bytes(writer.data), b"\x81\x03" + "红".encode("utf-8"))

    def test_send_binary_length_paths(self):
        for size, marker in ((125, 125), (126, 126), (65535, 126),
                             (65536, 127), (70000, 127)):
            conn, writer = self.make()
            conn.send_binary(b"b" * size)
            raw = bytes(writer.data)
            self.assertEqual(raw[0], 0x82)
            self.assertEqual(raw[1] & 0x7F, marker, "长度 %d" % size)
            self.assertEqual(read_server_frame(_Reader(raw)),
                             (True, _OP_BINARY, b"b" * size))

    def test_send_ping_and_pong(self):
        conn, writer = self.make()
        conn.send_ping(b"hb")
        self.assertEqual(bytes(writer.data), b"\x89\x02hb")
        conn.send_pong(b"hb")
        self.assertEqual(bytes(writer.data), b"\x89\x02hb\x8a\x02hb")

    def test_send_close_payload_and_state(self):
        conn, writer = self.make()
        conn.send_close(1000, "bye")
        self.assertEqual(bytes(writer.data),
                         b"\x88\x05" + struct.pack(">H", 1000) + b"bye")
        self.assertTrue(conn.closed)
        with self.assertRaises(ws.WSClosed):
            conn.send_text("after close")

    def test_send_close_empty_reason(self):
        conn, writer = self.make()
        conn.send_close()
        self.assertEqual(bytes(writer.data),
                         b"\x88\x02" + struct.pack(">H", 1000))

    def test_oversized_send_rejected(self):
        conn, writer = self.make(max_payload=16)
        with self.assertRaises(ValueError):
            conn.send_text("x" * 17)
        with self.assertRaises(ValueError):
            conn.send_binary(b"x" * 17)
        self.assertEqual(bytes(writer.data), b"")
        self.assertFalse(conn.closed)

    def test_oversized_control_send_rejected(self):
        conn, _writer = self.make()
        with self.assertRaises(ValueError):
            conn.send_ping(b"p" * 126)

    def test_closed_property_initially_false(self):
        conn, _writer = self.make()
        self.assertFalse(conn.closed)


# ------------------------------------------------------------------ 端到端
class _EchoHandler(http.server.BaseHTTPRequestHandler):
    """真实 http.server 处理器：升级为 WebSocket 后做一轮完整交互。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if not ws.is_websocket_upgrade(self.headers):
            self.send_error(400, "expected websocket upgrade")
            return
        conn = ws.WebSocket(self.rfile, self.wfile, self.connection)
        conn.complete_handshake(self.headers.get("Sec-WebSocket-Key"))
        self.close_connection = True
        conn.send_ping(b"server-beat")      # 服务器主动心跳
        try:
            while True:
                opcode, payload = conn.recv()
                if opcode == ws.OP_TEXT:
                    conn.send_text("echo:" + payload.decode("utf-8"))
                elif opcode == ws.OP_CLOSE:
                    break
                else:
                    break
        except ws.WSClosed:
            pass


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class TestEndToEnd(unittest.TestCase):
    """真实 http.server + 真实 TCP 连接（127.0.0.1:0）。"""

    @classmethod
    def setUpClass(cls):
        cls.server = _Server(("127.0.0.1", 0), _EchoHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      kwargs={"poll_interval": 0.05})
        cls.thread.daemon = True
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def connect(self, key="dGhlIHNhbXBsZSBub25jZQ=="):
        """连上真实服务器并完成握手，返回 (socket, 读流, 101 原始响应)。"""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        sock.settimeout(5)
        request = ("GET %s HTTP/1.1\r\n"
                   "Host: 127.0.0.1:%d\r\n"
                   "Upgrade: websocket\r\n"
                   "Connection: Upgrade\r\n"
                   "Sec-WebSocket-Key: %s\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n"
                   % (WS_PATH, self.port, key))
        sock.sendall(request.encode("ascii"))
        stream = sock.makefile("rb")
        self.addCleanup(stream.close)
        self.addCleanup(sock.close)
        lines = []
        while True:
            line = stream.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
            lines.append(line)
        return sock, stream, b"".join(lines) + b"\r\n"

    def test_handshake_response_headers(self):
        _sock, _stream, raw = self.connect()
        status, headers = parse_http_response(raw)
        self.assertEqual(status, "HTTP/1.1 101 Switching Protocols")
        self.assertEqual(headers.get("sec-websocket-accept"),
                         "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")
        self.assertEqual(headers.get("sec-websocket-accept"),
                         masked_accept("dGhlIHNhbXBsZSBub25jZQ=="))
        self.assertEqual(headers.get("sec-websocket-version"), "13")
        self.assertEqual(headers.get("upgrade", "").lower(), "websocket")
        self.assertIn("upgrade", headers.get("connection", "").lower())

    def test_text_both_ways_with_masked_client_frames(self):
        sock, stream, _raw = self.connect()
        # 服务器握手后立即发一个心跳 ping，先读出来并回 pong
        fin, opcode, payload = read_server_frame(stream)
        self.assertTrue(fin)
        self.assertEqual((opcode, payload), (_OP_PING, b"server-beat"))
        sock.sendall(client_pong(b"server-beat"))
        # 客户端 -> 服务端（手写掩码帧）-> 服务端回显
        sock.sendall(masked_text("红方炮二平五"))
        self.assertEqual(read_server_frame(stream),
                         (True, _OP_TEXT, "echo:红方炮二平五".encode("utf-8")))
        sock.sendall(masked_text("第二步"))
        self.assertEqual(read_server_frame(stream),
                         (True, _OP_TEXT, b"echo:\xe7\xac\xac\xe4\xba\x8c\xe6\xad\xa5"))

    def test_fragmented_client_message_is_echoed_whole(self):
        sock, stream, _raw = self.connect()
        fin, opcode, _payload = read_server_frame(stream)   # 服务器心跳 ping
        self.assertEqual((fin, opcode), (True, _OP_PING))
        sock.sendall(client_frame(_OP_TEXT, "分".encode("utf-8"), fin=False) +
                     client_frame(_OP_CONT, "片".encode("utf-8"), fin=True))
        self.assertEqual(read_server_frame(stream),
                         (True, _OP_TEXT, "echo:分片".encode("utf-8")))

    def test_auto_pong_for_client_ping(self):
        sock, stream, _raw = self.connect()
        read_server_frame(stream)           # 服务器心跳 ping
        sock.sendall(client_frame(_OP_PING, b"beat"))
        self.assertEqual(read_server_frame(stream), (True, _OP_PONG, b"beat"))

    def test_close_handshake_from_client(self):
        sock, stream, _raw = self.connect()
        read_server_frame(stream)           # 服务器心跳 ping
        sock.sendall(client_frame(_OP_CLOSE, struct.pack(">H", 1000) + b"bye"))
        fin, opcode, payload = read_server_frame(stream)
        self.assertTrue(fin)
        self.assertEqual(opcode, _OP_CLOSE)
        self.assertGreaterEqual(len(payload), 2)
        self.assertEqual(struct.unpack(">H", payload[:2])[0], 1000)

    def test_unmasked_client_frame_drops_connection(self):
        sock, stream, _raw = self.connect()
        read_server_frame(stream)           # 服务器心跳 ping
        sock.sendall(client_frame(_OP_TEXT, b"bad", mask=b""))
        self.assertEqual(stream.read(1), b"")   # 协议错误后服务器断开


class TestSocketPairRoundTrip(unittest.TestCase):
    """不经 http.server，直接用 socket 对驱动 WebSocket 验证 recv/send 语义。"""

    def setUp(self):
        self.server_sock, self.client_sock = socket.socketpair()
        self.server_sock.settimeout(5)
        self.client_sock.settimeout(5)
        self.addCleanup(self.server_sock.close)
        self.addCleanup(self.client_sock.close)
        self.memory_writer = _MemoryWriter()
        # 服务端写往客户端必须真的发到 socket 上，测试才能从另一端读到
        self.server_writer = self.server_sock.makefile("wb", 0)
        self.addCleanup(self.server_writer.close)
        self.writer = self.server_writer
        self.conn = ws.WebSocket(self.server_sock, self.server_writer,
                                 self.server_sock)
        self.stream = self.client_sock.makefile("rb")
        self.addCleanup(self.stream.close)

    def test_recv_text_returns_raw_bytes(self):
        self.client_sock.sendall(masked_text("你好"))
        self.assertEqual(self.conn.recv(timeout=2),
                         (_OP_TEXT, "你好".encode("utf-8")))

    def test_recv_ignores_pong_frames(self):
        self.client_sock.sendall(client_frame(_OP_PONG, b"ignored") +
                                 masked_text("real"))
        self.assertEqual(self.conn.recv(timeout=2), (_OP_TEXT, b"real"))

    def test_recv_auto_pong_for_ping(self):
        self.client_sock.sendall(client_frame(_OP_PING, b"p1") +
                                 masked_text("after-ping"))
        self.assertEqual(self.conn.recv(timeout=2), (_OP_TEXT, b"after-ping"))
        self.assertEqual(read_server_frame(self.stream),
                         (True, _OP_PONG, b"p1"))

    def test_recv_handles_glued_frames(self):
        self.client_sock.sendall(masked_text("a") +
                                 client_frame(_OP_BINARY, b"\x01\x02"))
        self.assertEqual(self.conn.recv(timeout=2), (_OP_TEXT, b"a"))
        self.assertEqual(self.conn.recv(timeout=2), (_OP_BINARY, b"\x01\x02"))

    def test_recv_handles_split_frame(self):
        frame = masked_text("split-me")
        self.client_sock.sendall(frame[:4])
        self.client_sock.sendall(frame[4:])
        self.assertEqual(self.conn.recv(timeout=2), (_OP_TEXT, b"split-me"))

    def test_recv_close_marks_closed_and_replies(self):
        self.client_sock.sendall(client_frame(_OP_CLOSE,
                                              struct.pack(">H", 1001) + b"x"))
        opcode, payload = self.conn.recv(timeout=2)
        self.assertEqual(opcode, _OP_CLOSE)
        self.assertEqual(struct.unpack(">H", payload[:2])[0], 1001)
        self.assertTrue(self.conn.closed)
        self.assertEqual(read_server_frame(self.stream)[1], _OP_CLOSE)
        with self.assertRaises(ws.WSClosed):
            self.conn.recv(timeout=2)

    def test_recv_timeout_is_raised_and_connection_survives(self):
        with self.assertRaises(socket.timeout):
            self.conn.recv(timeout=0.2)
        self.assertFalse(self.conn.closed)
        with self.assertRaises(socket.timeout):
            self.conn.recv(timeout=0.2)
        self.client_sock.sendall(masked_text("late"))
        self.assertEqual(self.conn.recv(timeout=5), (_OP_TEXT, b"late"))

    def test_recv_multiple_messages_from_one_batch(self):
        """一次读到的粘包消息要逐条返回，不能因缓冲区被覆盖而丢失。"""
        self.client_sock.sendall(masked_text("first") + masked_text("second"))
        self.assertEqual(self.conn.recv(timeout=5), (_OP_TEXT, b"first"))
        self.assertEqual(self.conn.recv(timeout=5), (_OP_TEXT, b"second"))

    def test_recv_eof_raises_wsclosed(self):
        # makefile 会复制 fd：必须 shutdown 让对端立刻看到 FIN
        # （只 close 底层 socket 也可能因为流仍持有 fd 而看不到 EOF）
        self.client_sock.shutdown(socket.SHUT_RDWR)
        with self.assertRaises(ws.WSClosed):
            self.conn.recv(timeout=2)
        self.assertTrue(self.conn.closed)

    def test_recv_protocol_error_raises_wsclosed(self):
        self.client_sock.sendall(client_frame(_OP_TEXT, b"x", mask=b""))
        with self.assertRaises(ws.WSClosed):
            self.conn.recv(timeout=2)

    def test_send_text_reaches_client(self):
        self.conn.send_text("红")
        self.assertEqual(read_server_frame(self.stream),
                         (True, _OP_TEXT, "红".encode("utf-8")))

    def test_send_ping_reaches_client(self):
        self.conn.send_ping(b"hb")
        self.assertEqual(read_server_frame(self.stream), (True, _OP_PING, b"hb"))

    def test_send_close_reaches_client(self):
        self.conn.send_close(1000, "bye")
        self.assertEqual(read_server_frame(self.stream),
                         (True, _OP_CLOSE, struct.pack(">H", 1000) + b"bye"))

    def test_handshake_written_to_stream_is_readable(self):
        # 裸 socket 直接当 wfile：模块内部退回 sendall，同样应把 101 写出去
        conn = ws.WebSocket(self.server_sock, self.server_sock,
                            self.server_sock)
        conn.complete_handshake("dGhlIHNhbXBsZSBub25jZQ==")
        lines = []
        while True:
            line = self.stream.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
            lines.append(line)
        status, headers = parse_http_response(b"".join(lines) + b"\r\n")
        self.assertEqual(status, "HTTP/1.1 101 Switching Protocols")
        self.assertEqual(headers["sec-websocket-accept"],
                         "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")
        self.assertEqual(headers["sec-websocket-version"], "13")

    def test_recv_oversized_send_rejected(self):
        conn = ws.WebSocket(self.server_sock, self.writer, self.server_sock,
                            max_payload=8)
        with self.assertRaises(ValueError):
            conn.send_text("123456789")

    def test_concurrent_sends_do_not_interleave(self):
        """两个线程同时发消息，客户端必须能逐帧正确解析（发送已串行化）。

        读线程与发送线程**并发**运行，避免 socket 缓冲区被写满导致发送侧超时
        （CI 的 2 核机器上，原先“先发完再读”的写法会稳定超时）；超时值也给足，
        这个测试关心的是帧不交错，而不是速度。
        """
        payload = b"m" * 64
        count = 10
        errors = []
        received = []
        self.server_sock.settimeout(30)
        self.client_sock.settimeout(30)

        def blast():
            try:
                for _ in range(count):
                    self.conn.send_binary(payload)
            except Exception as exc:                # pragma: no cover
                errors.append(exc)

        def drain():
            try:
                for _ in range(count * 2):
                    received.append(read_server_frame(self.stream))
            except Exception as exc:                # pragma: no cover
                errors.append(exc)

        reader = threading.Thread(target=drain)
        senders = [threading.Thread(target=blast) for _ in range(2)]
        reader.start()
        for thread in senders:
            thread.start()
        for thread in senders:
            thread.join(timeout=60)
        reader.join(timeout=60)
        self.assertFalse(reader.is_alive(), "读线程未在超时内收完全部帧")
        self.assertEqual(errors, [])
        self.assertEqual(len(received), count * 2)
        for frame in received:
            self.assertEqual(frame, (True, _OP_BINARY, payload))

    def test_concurrent_sends_through_threaded_writer(self):
        """假 wfile 故意慢写：若不加锁，帧字节必然交错。"""

        class SlowWriter:
            def __init__(self):
                self.data = bytearray()
                self.lock = threading.Lock()

            def write(self, block):
                time.sleep(0.001)
                with self.lock:
                    self.data.extend(block)

            def flush(self):
                pass

        writer = SlowWriter()
        conn = ws.WebSocket(None, writer, None)
        count = 30

        def blast():
            for _ in range(count):
                conn.send_binary(b"z" * 200)

        threads = [threading.Thread(target=blast) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        reader = _Reader(bytes(writer.data))
        for _ in range(count * 2):
            self.assertEqual(read_server_frame(reader),
                             (True, _OP_BINARY, b"z" * 200))


if __name__ == "__main__":
    unittest.main()
