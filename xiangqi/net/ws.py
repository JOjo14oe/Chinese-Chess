# -*- coding: utf-8 -*-
"""RFC 6455 服务端 WebSocket 实现（纯标准库，无第三方依赖）。

设计要点
--------
1. **服务端视角**：只解析“客户端发来”的帧，因此收到的数据帧必须带掩码，
   否则按协议错误处理；发出的帧一律不掩码（RFC 6455 §5.1）。
2. **增量解码**：:class:`FrameDecoder` 可以处理粘包（一次 feed 收到多帧）、
   半包（一帧分多次 feed）与分片（FIN=0 后接 CONTINUATION），
   控制帧（close/ping/pong）不入分片队列，立即返回。
3. **适配 ``http.server``**：:class:`WebSocket` 直接使用 ``BaseHTTPRequestHandler``
   已经预读过的 ``rfile`` / ``wfile`` / ``connection``，
   握手完成后调用方只需反复 ``recv()``。
4. **发送串行化**：同一连接可能有两个线程在发（心跳线程 + 业务线程），
   ``send_*`` 全部在同一把锁内完成组帧与写盘，避免帧字节交错。
"""

import base64
import hashlib
import socket
import struct
import threading

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# ------------------------------------------------------------------ 帧类型
OP_CONT = 0x0       # 分片续帧
OP_TEXT = 0x1       # 文本
OP_BINARY = 0x2     # 二进制
OP_CLOSE = 0x8      # 关闭
OP_PING = 0x9       # 心跳请求
OP_PONG = 0xA       # 心跳应答

OPCODES = (OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG)

DEFAULT_MAX_PAYLOAD = 1 << 20       # 单条消息上限 1 MiB
_READ_CHUNK = 65536                 # 单次 recv 最大读取字节数

# 关闭状态码（供调用方构造 send_close）
CLOSE_NORMAL = 1000
CLOSE_PROTOCOL_ERROR = 1002
CLOSE_TOO_LARGE = 1009


def accept_key(client_key):
    """Sec-WebSocket-Key -> Sec-WebSocket-Accept（RFC 6455 §4.2.2）。"""
    digest = hashlib.sha1(
        (str(client_key or "") + WS_GUID).encode("ascii", "ignore")).digest()
    return base64.b64encode(digest).decode("ascii")


def header_values(headers, name):
    """取出某个头部的所有取值（大小写不敏感）。

    兼容 ``email.message.Message``（http.server 的 ``handler.headers``）
    与普通 ``dict``。
    """
    values = []
    wanted = name.lower()
    get_all = getattr(headers, "get_all", None)
    if get_all is not None:
        for value in get_all(name) or []:
            values.append(value)
    elif hasattr(headers, "items"):
        for key, value in headers.items():
            if str(key).lower() == wanted:
                values.append(value)
    return [str(value) for value in values if value is not None]


def header_value(headers, name):
    """取某个头部的第一个取值，没有则返回空串。"""
    values = header_values(headers, name)
    return values[0] if values else ""


def _has_token(value, token):
    """判断逗号分隔的头域里是否含某个 token（大小写不敏感）。"""
    for part in value.split(","):
        if part.strip().lower() == token:
            return True
    return False


def is_websocket_upgrade(headers):
    """headers 是 email.message.Message（http.server 的 handler.headers）。

    要求：Connection 含 'upgrade'（大小写不敏感）、Upgrade == 'websocket'、
    Sec-WebSocket-Version == '13'、存在非空 Sec-WebSocket-Key。
    """
    if headers is None:
        return False
    connection = header_values(headers, "Connection")
    if not any(_has_token(value, "upgrade") for value in connection):
        return False
    if header_value(headers, "Upgrade").strip().lower() != "websocket":
        return False
    if header_value(headers, "Sec-WebSocket-Version").strip() != "13":
        return False
    key = header_value(headers, "Sec-WebSocket-Key").strip()
    return bool(key)


class WSClosed(Exception):
    """对端关闭 / EOF / 协议错误。"""


def _validate_length(length, max_payload):
    """校验声明长度：非负、不超过上限。"""
    if length < 0:
        raise ValueError("长度域为负：%d" % length)
    if length > max_payload:
        raise ValueError("载荷 %d 字节超过上限 %d" % (length, max_payload))


def _masked(payload, mask):
    """按 4 字节掩码做异或，返回新字节串（RFC 6455 §5.3）。"""
    if not payload:
        return b""
    key = mask
    return bytes(byte ^ key[index & 3]
                 for index, byte in enumerate(payload))


class FrameDecoder:
    """增量解码器：处理粘包、半包、分片、掩码、控制帧。

    只能由服务端视角使用：来自客户端的帧必须带掩码，否则视为协议错误。

    使用方式::

        decoder = FrameDecoder()
        for opcode, payload in decoder.feed(chunk):
            handle(opcode, payload)

    协议错误一律抛 :class:`ValueError`；数据不足时静默等待下一次 feed。
    """

    def __init__(self, max_payload=DEFAULT_MAX_PAYLOAD):
        if max_payload <= 0:
            raise ValueError("max_payload 必须为正数")
        self.max_payload = int(max_payload)
        self._buffer = bytearray()
        # 分片消息：首帧 opcode 与已聚合载荷
        self._frag_opcode = None
        self._frag_payload = bytearray()

    @property
    def buffered(self):
        """尚未凑成完整帧的字节数。"""
        return len(self._buffer)

    @property
    def fragmented(self):
        """是否正处于分片消息中。"""
        return self._frag_opcode is not None

    def feed(self, data):
        """喂入新收到的字节，返回本次凑齐的**完整消息**列表。"""
        if data:
            self._buffer.extend(data)
        messages = []
        while True:
            frame = self._take_frame()
            if frame is None:
                break
            fin, opcode, payload = frame
            message = self._assemble(fin, opcode, payload)
            if message is not None:
                messages.append(message)
        return messages

    # ------------------------------------------------------------- 内部实现
    def _take_frame(self):
        """尝试取出一帧，返回 (fin, opcode, payload)，数据不足返回 None。"""
        buffer = self._buffer
        if len(buffer) < 2:
            return None
        byte0 = buffer[0]
        byte1 = buffer[1]
        fin = bool(byte0 & 0x80)
        if byte0 & 0x70:
            raise ValueError("RSV 位必须为 0")
        opcode = byte0 & 0x0F
        if opcode not in OPCODES:
            raise ValueError("未知 opcode：0x%X" % opcode)
        masked = bool(byte1 & 0x80)
        if not masked:
            raise ValueError("客户端发来的帧必须带掩码")
        length = byte1 & 0x7F
        offset = 2
        if length == 126:
            if len(buffer) < offset + 2:
                return None
            length = struct.unpack(">H", bytes(buffer[offset:offset + 2]))[0]
            offset += 2
            if length < 126:
                raise ValueError("126 扩展长度编码非最短形式：%d" % length)
        elif length == 127:
            if len(buffer) < offset + 8:
                return None
            length = struct.unpack(">Q", bytes(buffer[offset:offset + 8]))[0]
            offset += 8
            if length & (1 << 63):
                raise ValueError("64 位长度最高位必须为 0")
            if length <= 0xFFFF:
                raise ValueError("127 扩展长度编码非最短形式：%d" % length)
        _validate_length(length, self.max_payload)
        if len(buffer) < offset + 4:
            return None
        mask = bytes(buffer[offset:offset + 4])
        offset += 4
        if len(buffer) < offset + length:
            return None
        payload = _masked(bytes(buffer[offset:offset + length]), mask)
        del buffer[:offset + length]
        if opcode >= OP_CLOSE:
            # 控制帧不得分片，且载荷 <= 125 字节
            if not fin:
                raise ValueError("控制帧不得分片")
            if length > 125:
                raise ValueError("控制帧载荷不得超过 125 字节：%d" % length)
        return fin, opcode, payload

    def _assemble(self, fin, opcode, payload):
        """把一帧并入分片状态，返回可交付的消息或 None。"""
        if opcode == OP_CONT:
            if self._frag_opcode is None:
                raise ValueError("没有起始帧的续帧")
            self._frag_payload.extend(payload)
            if fin:
                message = (self._frag_opcode, bytes(self._frag_payload))
                self._frag_opcode = None
                self._frag_payload = bytearray()
                return message
            return None
        if opcode >= OP_CLOSE:
            return opcode, payload           # 控制帧立即交付
        if self._frag_opcode is not None:
            raise ValueError("上一条分片消息尚未结束")
        if fin:
            return opcode, payload
        self._frag_opcode = opcode
        self._frag_payload = bytearray(payload)
        return None


class WebSocket:
    """服务端 WebSocket 连接。

    ``rfile`` / ``wfile`` / ``sock`` 来自 ``BaseHTTPRequestHandler``；
    若拿不到底层 socket（例如自建的读写包装），可传 ``sock=None``，
    此时 ``timeout`` 参数不生效，全部读取按阻塞处理。
    """

    def __init__(self, rfile, wfile, sock, max_payload=DEFAULT_MAX_PAYLOAD):
        if max_payload <= 0:
            raise ValueError("max_payload 必须为正数")
        self.rfile = rfile
        self.wfile = wfile
        self.sock = sock
        self.max_payload = int(max_payload)
        self.decoder = FrameDecoder(max_payload)
        self._send_lock = threading.Lock()
        self._closed = False
        self._close_sent = False
        self._pending = []

    # ------------------------------------------------------------- 属性
    @property
    def closed(self):
        """连接是否已关闭（收到/发出 close，或出现致命错误）。"""
        return self._closed

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # ------------------------------------------------------------- 握手
    def complete_handshake(self, client_key):
        """向 wfile 写 101 升级响应并 flush。"""
        lines = [
            "HTTP/1.1 101 Switching Protocols",
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Accept: %s" % accept_key(client_key),
            "Sec-WebSocket-Version: 13",
            "",
            "",
        ]
        self._write("\r\n".join(lines).encode("ascii"))

    # ------------------------------------------------------------- 发送
    def send_text(self, text):
        """发送文本帧（服务端帧不掩码）。"""
        self._send_frame(OP_TEXT, str(text).encode("utf-8"))

    def send_binary(self, payload):
        """发送二进制帧。"""
        self._send_frame(OP_BINARY, bytes(payload))

    def send_ping(self, payload=b""):
        """发送 ping 帧（载荷 <= 125 字节）。"""
        self._send_frame(OP_PING, bytes(payload))

    def send_pong(self, payload=b""):
        """发送 pong 帧（载荷 <= 125 字节）。"""
        self._send_frame(OP_PONG, bytes(payload))

    def send_close(self, code=1000, reason=""):
        """发送 close 帧，并把连接标记为已关闭。"""
        body = struct.pack(">H", int(code) & 0xFFFF)
        if reason:
            body += str(reason).encode("utf-8")
        if len(body) > 125:
            body = body[:125]
        try:
            self._send_frame(OP_CLOSE, body)
        except WSClosed:
            pass
        self._closed = True

    # ------------------------------------------------------------- 接收
    def recv(self, timeout=None):
        """读取下一条完整消息，返回 (opcode, payload_bytes)。

        - ``timeout`` 为 None 时阻塞；否则先 ``sock.settimeout(timeout)``
        - 读超时：把 ``socket.timeout`` 原样抛给调用方（用于心跳）
        - 收到 ping(0x9)：自动回 pong(0xA)，继续读下一条（不返回给调用方）
        - 收到 pong(0xA)：忽略，继续读
        - 收到 close(0x8)：回一个 close 帧，标记关闭，返回 (0x8, payload)
        - 对端 EOF、socket 错误或协议错误：抛 :class:`WSClosed`
        - 文本消息按原始 bytes 返回，不做解码
        """
        if self._closed and not self._pending:
            self._closed = True
            raise WSClosed("连接已关闭")
        previous = self._apply_timeout(timeout)
        try:
            return self._recv_loop()
        finally:
            self._restore_timeout(previous)

    # ------------------------------------------------------------- 内部实现
    def _apply_timeout(self, timeout):
        if self.sock is None:
            return None
        previous = self.sock.gettimeout()
        if timeout is not None:
            self.sock.settimeout(timeout)
        return previous

    def _restore_timeout(self, previous):
        if self.sock is None or previous is None:
            return
        try:
            self.sock.settimeout(previous)
        except OSError:
            pass

    def _recv_loop(self):
        while True:
            if self._pending:
                opcode, payload = self._pending.pop(0)
            else:
                opened = self._read_more()
                if not opened:
                    self._fail("对端已关闭连接")
                continue
            if opcode == OP_PING:
                self.send_pong(payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self._reply_close(payload)
                self._closed = True
                return opcode, payload
            return opcode, payload

    def _read_more(self):
        """从 rfile/sock 再读一批数据，返回是否读到了新字节。

        注意：``http.server`` 的 ``rfile`` 是 ``BufferedReader``，它的 ``read(n)``
        会一直阻塞到凑满 ``n`` 字节或遇到 EOF —— 对“一个小帧就停住等回应”的
        WebSocket 场景是致命的。因此优先用 ``read1``：它能先把缓冲区里已有的字节
        交出（升级请求与第一个帧在同一个 TCP 段到达时不会丢帧），没有缓冲数据时
        就返回一次系统调用读到的量。优先顺序为 ``recv`` -> ``read1`` -> ``read``。
        """
        source = self.rfile if self.rfile is not None else self.sock
        # 原始 socket 必须用 recv：socket.read(n) 会一直等到凑满 n 字节。
        read = getattr(source, "recv", None)
        if read is None:
            read = getattr(source, "read1", None)
        if read is None:
            read = getattr(source, "read", None)
        if read is None:
            raise WSClosed("没有可读的数据源")
        try:
            chunk = read(_READ_CHUNK)
        except socket.timeout:
            raise
        except (OSError, ValueError) as exc:
            self._fail("读取失败：%s" % exc)
        if not chunk:
            return False
        try:
            messages = self.decoder.feed(chunk)
        except ValueError as exc:
            self._fail("协议错误：%s" % exc)
        self._pending.extend(messages)
        return True

    def _reply_close(self, payload):
        """回一个 close 帧（只回一次）。"""
        if self._close_sent:
            return
        code = 1000
        if len(payload) >= 2:
            code = struct.unpack(">H", payload[:2])[0]
        try:
            self._send_frame(OP_CLOSE, struct.pack(">H", code))
        except WSClosed:
            pass

    def _send_frame(self, opcode, payload):
        """组帧并写出（同一把锁内完成，避免多线程交错）。"""
        payload = bytes(payload)
        if len(payload) > self.max_payload:
            raise ValueError("载荷 %d 字节超过上限 %d"
                             % (len(payload), self.max_payload))
        if opcode >= OP_CLOSE and len(payload) > 125:
            raise ValueError("控制帧载荷不得超过 125 字节")
        if self._closed:
            raise WSClosed("连接已关闭")
        frame = self._build_frame(opcode, payload)
        with self._send_lock:
            self._write(frame)

    def _build_frame(self, opcode, payload):
        """构造服务端帧（不掩码）。"""
        length = len(payload)
        header = bytearray()
        header.append(0x80 | (opcode & 0x0F))
        if length < 126:
            header.append(length)
        elif length <= 0xFFFF:
            header.append(126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(127)
            header.extend(struct.pack(">Q", length))
        return bytes(header) + payload

    def _write(self, data):
        target = self.wfile
        write = getattr(target, "write", None)
        try:
            if write is not None:
                write(data)
                flush = getattr(target, "flush", None)
                if flush is not None:
                    flush()
                return
            if self.sock is not None:
                # 兜底：允许直接把裸 socket 当作 wfile 传入
                self.sock.sendall(data)
                return
            raise WSClosed("没有可写的数据源")
        except (socket.timeout, WSClosed):
            raise
        except (OSError, ValueError) as exc:
            self._fail("写入失败：%s" % exc)

    def _fail(self, message):
        """记录致命错误并抛出 WSClosed。"""
        self._closed = True
        raise WSClosed(message)

    def close(self):
        """释放底层 socket（幂等，不抛异常）。"""
        self._closed = True
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass


__all__ = [
    "WS_GUID", "OP_CONT", "OP_TEXT", "OP_BINARY", "OP_CLOSE", "OP_PING",
    "OP_PONG", "DEFAULT_MAX_PAYLOAD", "CLOSE_NORMAL", "CLOSE_PROTOCOL_ERROR",
    "CLOSE_TOO_LARGE", "accept_key", "is_websocket_upgrade", "WSClosed",
    "FrameDecoder", "WebSocket",
]
