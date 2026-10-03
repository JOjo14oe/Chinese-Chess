# -*- coding: utf-8 -*-
"""联机对战的通信协议定义（与传输方式无关）。

同一套 JSON 消息同时用于三种传输，客户端可在它们之间无缝切换而不断局：

* **WebSocket**：``GET /ws``（低延迟双向，首选）
* **SSE**：``GET /api/rooms/{code}/stream``（长连接推送）+ ``POST`` 走子（兜底）
* **长轮询**：``GET /api/rooms/{code}/events``（最后兜底）

跨国/高延迟设计的要点
--------------------
1. **服务器权威**：所有规则判定在 Python 服务器上用既有引擎完成，客户端不实现规则。
2. **序号（seq）单调递增**：房间内的每一次状态变化都是一个事件，带全局递增 ``seq``；
   客户端只需记住 ``since``，重连时即可精确补齐缺失事件，不必重开一局。
3. **乐观并发 + 幂等**：每个走子请求携带 ``base_seq``（它所基于的局面）与客户端生成的
   ``cid``；``base_seq`` 不匹配返回 ``stale`` 并附完整快照，``cid`` 重复则直接返回上次结果。
   这样在高延迟链路上重发请求不会走出两步棋。
4. **快照兜底**：若客户端索要的事件已超出保留窗口，服务器回一个完整快照。
5. **小载荷**：JSON 采用紧凑分隔符、中文不转义；一局棋的全部事件都很小。
"""

import json
import re
import secrets
import time

PROTOCOL_VERSION = 1
DEFAULT_PORT = 8000
DEFAULT_HOST = "127.0.0.1"

# 房间号：去掉 0/O/1/I 等易混字符，便于跨语言口述
ROOM_CODE_ALPHABET = "ACDEFGHJKLMNPQRSTUVWXYZ23456789"
ROOM_CODE_LEN = 6
ROOM_CODE_RE = re.compile(r"^[%s]{%d}$" % (ROOM_CODE_ALPHABET, ROOM_CODE_LEN))

SIDES = ("r", "b")

# ------------------------------------------------------------------ 事件类型
EV_START = "start"      # 房间创建 / 新开一局
EV_JOIN = "join"        # 某方入座或重新入座
EV_MOVE = "move"        # 走子
EV_RESIGN = "resign"    # 认输
EV_END = "end"          # 对局结束（将死/困毙/认输/和棋）
EV_NEW = "new"          # 重开一局

# ------------------------------------------------------------------ 错误码
ERR_BAD_REQUEST = "bad_request"
ERR_NOT_FOUND = "room_not_found"
ERR_ROOM_FULL = "room_full"
ERR_FORBIDDEN = "forbidden"
ERR_NOT_YOUR_TURN = "not_your_turn"
ERR_ILLEGAL = "illegal_move"
ERR_STALE = "stale"
ERR_GAME_OVER = "game_over"
ERR_TOO_LARGE = "too_large"
ERR_BAD_ROOM_CODE = "bad_room_code"

# ------------------------------------------------------------------ 上限
MAX_BODY_BYTES = 16 * 1024          # 单个请求体上限
MAX_EVENTS = 4096                   # 每房间保留的事件条数
MAX_ROOMS = 500                     # 同时存在的房间数
MAX_CID = 64
MAX_ICCS_LEN = 8
ROOM_TTL_SECONDS = 6 * 3600         # 空闲房间回收
LONG_POLL_SECONDS = 25              # 长轮询最长挂起时间
HEARTBEAT_SECONDS = 15              # SSE/WS 心跳间隔


class ProtocolError(Exception):
    """协议层错误（可安全返回给客户端）。"""

    def __init__(self, code, message):
        super(ProtocolError, self).__init__(message)
        self.code = code
        self.message = message


def now_ms():
    """服务器单调递增的毫秒时间戳（仅供展示，不参与规则判定）。"""
    return int(time.time() * 1000)


def new_room_code():
    """生成随机房间号。"""
    return "".join(secrets.choice(ROOM_CODE_ALPHABET) for _ in range(ROOM_CODE_LEN))


def new_token():
    """生成座位令牌（重连凭据）。"""
    return secrets.token_urlsafe(24)


def room_code_valid(code):
    return bool(code) and bool(ROOM_CODE_RE.match(str(code).upper()))


def normalize_room_code(code):
    code = str(code or "").strip().upper()
    if not room_code_valid(code):
        raise ProtocolError(ERR_BAD_ROOM_CODE, "房间号格式不正确")
    return code


def dumps(obj):
    """紧凑 JSON（中文不转义），减少跨国链路上的字节数。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def load(raw):
    """解析客户端 JSON；非法输入抛 :class:`ProtocolError`。"""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ProtocolError(ERR_BAD_REQUEST, "请求体不是合法 UTF-8")
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise ProtocolError(ERR_BAD_REQUEST, "请求体不是合法 JSON")
    if not isinstance(data, dict):
        raise ProtocolError(ERR_BAD_REQUEST, "请求体必须是 JSON 对象")
    return data


def require_str(data, key, max_len=256, allow_empty=False):
    value = data.get(key)
    if value is None:
        raise ProtocolError(ERR_BAD_REQUEST, "缺少字段 %s" % key)
    if not isinstance(value, str):
        raise ProtocolError(ERR_BAD_REQUEST, "字段 %s 必须是字符串" % key)
    value = value.strip()
    if not value and not allow_empty:
        raise ProtocolError(ERR_BAD_REQUEST, "字段 %s 不能为空" % key)
    if len(value) > max_len:
        raise ProtocolError(ERR_TOO_LARGE, "字段 %s 过长" % key)
    return value


def require_int(data, key, minimum=None, maximum=None, default=None):
    value = data.get(key, default)
    if value is None:
        raise ProtocolError(ERR_BAD_REQUEST, "缺少字段 %s" % key)
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ProtocolError(ERR_BAD_REQUEST, "字段 %s 必须是整数" % key)
    if minimum is not None and value < minimum:
        raise ProtocolError(ERR_BAD_REQUEST, "字段 %s 太小" % key)
    if maximum is not None and value > maximum:
        raise ProtocolError(ERR_BAD_REQUEST, "字段 %s 太大" % key)
    return value


def envelope(event_type, **fields):
    """构造一条服务器消息（统一带协议版本）。"""
    message = {"v": PROTOCOL_VERSION, "t": event_type}
    message.update(fields)
    return message


def error_message(code, message, **extra):
    payload = envelope("error", code=code, msg=message)
    payload.update(extra)
    return payload


__all__ = [
    "PROTOCOL_VERSION", "DEFAULT_PORT", "DEFAULT_HOST", "SIDES",
    "ROOM_CODE_ALPHABET", "ROOM_CODE_LEN", "EV_START", "EV_JOIN", "EV_MOVE",
    "EV_RESIGN", "EV_END", "EV_NEW", "ERR_BAD_REQUEST", "ERR_NOT_FOUND",
    "ERR_ROOM_FULL", "ERR_FORBIDDEN", "ERR_NOT_YOUR_TURN", "ERR_ILLEGAL",
    "ERR_STALE", "ERR_GAME_OVER", "ERR_TOO_LARGE", "ERR_BAD_ROOM_CODE",
    "MAX_BODY_BYTES", "MAX_EVENTS", "MAX_ROOMS", "MAX_CID", "MAX_ICCS_LEN",
    "ROOM_TTL_SECONDS", "LONG_POLL_SECONDS", "HEARTBEAT_SECONDS",
    "ProtocolError", "now_ms", "new_room_code", "new_token", "room_code_valid",
    "normalize_room_code", "dumps", "load", "require_str", "require_int",
    "envelope", "error_message",
]
