# -*- coding: utf-8 -*-
"""房间与对局的权威状态机（服务器权威，跨国高延迟下可安全重发/重连）。

设计要点
--------
* 每个房间持有一副 :class:`xiangqi.board.Board`，所有规则判定都在这里完成。
* 房间内的每一次状态变化都追加为一个**事件**，带全局递增 ``seq``；客户端只记住
  自己的 ``since``，重连时精确补齐，不会漏步也不会重开一局。
* 走子请求必须带 ``base_seq``（乐观并发）与 ``cid``（幂等键）：前者过期即返回 ``stale``
  与完整快照，后者重复则直接返回上次结果——两者共同保证“高延迟链路重发不走出两步棋”。
* 事件保留 ``MAX_EVENTS`` 条；客户端索要的事件超出窗口时回完整快照。
"""

import copy
import hmac
import threading
import time

from ..board import Board, IllegalMove
from ..constants import RED, SIDE_NAMES, START_FEN, FILE_LETTERS, other
from ..notation import move_from_iccs, move_to_chinese
from . import protocol as P


class RoomError(Exception):
    """房间层错误，可安全返回给客户端。"""

    def __init__(self, code, message, snapshot=None):
        super(RoomError, self).__init__(message)
        self.code = code
        self.message = message
        self.snapshot = snapshot


def _square_name(rank, file):
    return "%s%d" % (FILE_LETTERS[file], rank)


class Seat(object):
    """一个座位（红方或黑方）。``token`` 是重连凭据。

    ``connections`` 记录该座位当前**存活的流连接数**（WebSocket / SSE），
    因此同一个令牌在多标签页打开不会互相把对方“踢下线”：只有最后一个连接
    断开时，座位上才会显示为离线。
    """

    __slots__ = ("side", "token", "name", "connections", "last_seen_ms", "resumes")

    def __init__(self, side, token, name=None):
        self.side = side
        self.token = token
        self.name = name or SIDE_NAMES[side]
        self.connections = 0
        self.last_seen_ms = P.now_ms()
        self.resumes = 0

    @property
    def connected(self):
        return self.connections > 0

    @connected.setter
    def connected(self, value):
        """兼容旧写法：置 True/False 等价于 1/0 个连接。"""
        self.connections = 1 if value else 0

    def public(self):
        return {"joined": True, "connected": self.connected, "name": self.name,
                "resumes": self.resumes}


class Room(object):
    """一个对局房间。所有公开方法都在内部锁保护下执行。"""

    def __init__(self, code):
        self.code = code
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self.created_ms = P.now_ms()
        self.updated_ms = self.created_ms
        self.board = Board(START_FEN)
        self.seats = {"r": None, "b": None}
        self.events = []
        self.next_seq = 1
        #: 最后一次**改变棋局**的事件的 seq（入座/在线状态不算），走子请求的 base_seq
        #: 与它比较：这样对手上线/掉线不会让正在途中的一步棋被判为过期。
        self.game_seq = 0
        self.move_log = []
        self.result = None
        self._cid_cache = {}
        self._cid_order = []
        with self._cv:                     # notify 需要在锁内
            self._append_locked({"t": P.EV_START, "ms": self.created_ms}, game=True)

    # ------------------------------------------------------------------ 内部
    def _append_locked(self, event, game=False):
        event = dict(event)
        event["seq"] = self.next_seq
        event.setdefault("ms", P.now_ms())
        self.events.append(event)
        self.next_seq += 1
        self.updated_ms = event["ms"]
        if game:
            self.game_seq = event["seq"]
        if len(self.events) > P.MAX_EVENTS:
            self.events = self.events[-P.MAX_EVENTS:]
        self._cv.notify_all()
        return event

    def _first_kept_seq_locked(self):
        return self.events[0]["seq"] if self.events else self.next_seq

    def _events_since_locked(self, since):
        """返回 ``(events, stale)``；``stale`` 表示客户端位置已丢失，应改发快照。"""
        last_seq = self.next_seq - 1
        if since is None:
            return [], True
        if since < self._first_kept_seq_locked() - 1 or since > last_seq:
            return [], True
        return [ev for ev in self.events if ev["seq"] > since], False

    def legal_map_locked(self):
        """当前走子方的合法着法：``{"b0": ["a2", "c2"], ...}``（供网页端画提示点）。"""
        legal = {}
        for move in self.board.gen_legal_moves():
            legal.setdefault(_square_name(move.fr, move.fc), []).append(
                _square_name(move.tr, move.tc))
        return legal

    def _snapshot_locked(self):
        board = self.board
        last_move = None
        if self.move_log:
            last = self.move_log[-1]
            last_move = {"from": last["from"], "to": last["to"]}
        return {
            "v": P.PROTOCOL_VERSION,
            "room": self.code,
            "seq": self.next_seq - 1,
            "game_seq": self.game_seq,
            "fen": board.to_fen(),
            "side": board.side,
            "turn_name": SIDE_NAMES[board.side],
            "check": board.in_check(board.side) and self.result is None,
            "legal": self.legal_map_locked(),
            "seats": {side: (seat.public() if seat else {"joined": False,
                                                         "connected": False})
                      for side, seat in self.seats.items()},
            "moves": [{"seq": item["seq"], "side": item["side"],
                       "iccs": item["iccs"], "chinese": item["chinese"]}
                      for item in self.move_log],
            "last_move": last_move,
            "result": self.result,
            "result_text": ((self.result.get("text") or board.result_text())
                            if self.result else ""),
            "started_ms": self.created_ms,
            "updated_ms": self.updated_ms,
            "server_ms": P.now_ms(),
        }

    # ------------------------------------------------------------------ 查询
    def snapshot(self):
        with self._cv:
            return self._snapshot_locked()

    def events_since(self, since):
        with self._cv:
            events, stale = self._events_since_locked(since)
            if stale:
                return {"v": P.PROTOCOL_VERSION, "t": "snapshot",
                        "snapshot": self._snapshot_locked()}
            return {"v": P.PROTOCOL_VERSION, "t": "events", "events": events,
                    "seq": self.next_seq - 1}

    def wait_events(self, since, timeout=P.LONG_POLL_SECONDS):
        """等到有 ``since`` 之后的新事件（或超时）；位置过旧则回快照。"""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cv:
            while True:
                events, stale = self._events_since_locked(since)
                if stale:
                    return {"v": P.PROTOCOL_VERSION, "t": "snapshot",
                            "snapshot": self._snapshot_locked()}
                if events:
                    return {"v": P.PROTOCOL_VERSION, "t": "events",
                            "events": events, "seq": self.next_seq - 1}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"v": P.PROTOCOL_VERSION, "t": "events", "events": [],
                            "seq": self.next_seq - 1, "timeout": True}
                self._cv.wait(remaining)

    def seat_of_token(self, token):
        """按令牌找到座位；找不到返回 ``None``（用常量时间比较，避免逐字符试探）。"""
        if not token or not isinstance(token, str):
            return None
        with self._cv:
            for seat in self.seats.values():
                if seat and hmac.compare_digest(seat.token, token):
                    return seat
            return None

    # ------------------------------------------------------------------ 入座
    def _free_side_locked(self, preferred=None):
        order = [preferred] if preferred in P.SIDES else list(P.SIDES)
        for side in order:
            if self.seats.get(side) is None:
                return side
        return None

    def join(self, side=None, name=None, token=None):
        """入座或凭令牌重新入座；返回 ``(side, token, events)``。"""
        with self._cv:
            seat = self.seat_of_token(token) if token else None
            if seat is not None:                      # 重连：沿用原座位与令牌
                seat.resumes += 1
                seat.last_seen_ms = P.now_ms()
                event = self._append_locked({"t": P.EV_JOIN, "side": seat.side,
                                             "rejoin": True, "name": seat.name})
                return seat.side, seat.token, [event]

            side = self._free_side_locked(side)
            if side is None:
                raise RoomError(P.ERR_ROOM_FULL, "房间已满（红黑两方都已入座）")
            token = P.new_token()
            seat = Seat(side, token, name=name)
            self.seats[side] = seat
            event = self._append_locked({"t": P.EV_JOIN, "side": side,
                                         "rejoin": False, "name": seat.name})
            return side, token, [event]

    def resume(self, token, since=None):
        """重连入口：校验令牌并返回缺失事件（位置过旧则回快照）。"""
        seat = self.seat_of_token(token)
        if seat is None:
            raise RoomError(P.ERR_FORBIDDEN, "令牌无效，无法恢复到该座位")
        with self._cv:
            seat.resumes += 1
            seat.last_seen_ms = P.now_ms()
            self._cv.notify_all()
            return seat.side

    def touch(self, token, connected, name=None):
        """登记一条流连接的上线/下线。

        同一座位允许同时存在多条连接（例如同一令牌开了两个标签页），
        只有最后一条连接断开时座位才会显示为离线。
        """
        seat = self.seat_of_token(token)
        if seat is None:
            return
        with self._cv:
            if connected:
                seat.connections += 1
            else:
                seat.connections = max(0, seat.connections - 1)
            seat.last_seen_ms = P.now_ms()
            if name:
                seat.name = name
            self._append_locked({"t": "presence", "side": seat.side,
                                 "connected": seat.connected})

    # ------------------------------------------------------------------ 行棋
    def _cache_cid_locked(self, cid, response):
        if not cid:
            return
        if cid not in self._cid_cache:
            self._cid_order.append(cid)
        self._cid_cache[cid] = copy.deepcopy(response)
        while len(self._cid_order) > 512:
            self._cid_cache.pop(self._cid_order.pop(0), None)

    def apply_move(self, side, iccs, base_seq=None, cid=None):
        """执行一步棋；返回给客户端的响应（含新事件）。"""
        with self._cv:
            if cid and cid in self._cid_cache:          # 幂等：重发不再走棋
                cached = copy.deepcopy(self._cid_cache[cid])
                cached["replayed"] = True
                return cached
            if self.result is not None:
                raise RoomError(P.ERR_GAME_OVER, "对局已结束，请开始新的一局",
                                snapshot=self._snapshot_locked())
            if self.board.side != side:
                raise RoomError(P.ERR_NOT_YOUR_TURN, "还没轮到你走棋",
                                snapshot=self._snapshot_locked())
            if base_seq is not None and not (self.game_seq <= base_seq
                                             <= self.next_seq - 1):
                raise RoomError(P.ERR_STALE, "局面已变化，请按最新局面重新走棋",
                                snapshot=self._snapshot_locked())
            try:
                move = move_from_iccs(iccs)
            except ValueError:
                raise RoomError(P.ERR_ILLEGAL, "着法格式不正确：%s" % iccs)
            try:
                chinese = move_to_chinese(self.board, move)   # 必须走子前取
            except ValueError:
                raise RoomError(P.ERR_ILLEGAL, "无法解析该着法的记谱")
            try:
                self.board.make_move(move)
            except IllegalMove as error:
                raise RoomError(P.ERR_ILLEGAL, "不合法的着法：%s（%s）"
                                % (iccs, error), snapshot=self._snapshot_locked())

            self.move_log.append({
                "seq": self.next_seq, "side": side, "iccs": move.iccs(),
                "chinese": chinese, "from": _square_name(move.fr, move.fc),
                "to": _square_name(move.tr, move.tc),
            })
            produced = [self._append_locked({
                "t": P.EV_MOVE, "side": side, "iccs": move.iccs(),
                "chinese": chinese, "fen": self.board.to_fen(),
                "check": self.board.in_check(self.board.side),
                "from": _square_name(move.fr, move.fc),
                "to": _square_name(move.tr, move.tc),
            }, game=True)]

            over, winner, reason = self.board.status()
            if over:
                self.result = {"winner": winner, "reason": reason,
                               "text": self.board.result_text()}
                produced.append(self._append_locked({
                    "t": P.EV_END, "winner": winner, "reason": reason,
                    "result_text": self.board.result_text(),
                }))

            response = {"v": P.PROTOCOL_VERSION, "t": "ack", "ok": True,
                        "seq": self.next_seq - 1, "events": produced,
                        "snapshot": self._snapshot_locked()}
            self._cache_cid_locked(cid, response)
            return response

    # ------------------------------------------------------------------ 其他
    def resign(self, side):
        with self._cv:
            if self.result is not None:
                raise RoomError(P.ERR_GAME_OVER, "对局已结束",
                                snapshot=self._snapshot_locked())
            self.result = {"winner": other(side), "reason": "%s认输" % SIDE_NAMES[side],
                           "text": "%s胜（%s认输）" % (SIDE_NAMES[other(side)],
                                                      SIDE_NAMES[side])}
            events = [self._append_locked({"t": P.EV_RESIGN, "side": side}),
                      self._append_locked({"t": P.EV_END,
                                           "winner": self.result["winner"],
                                           "reason": self.result["reason"],
                                           "result_text": self.result["text"]})]
            return {"v": P.PROTOCOL_VERSION, "t": "ack", "ok": True,
                    "seq": self.next_seq - 1, "events": events,
                    "snapshot": self._snapshot_locked()}

    def new_game(self, side):
        with self._cv:
            self.board = Board(START_FEN)
            self.move_log = []
            self.result = None
            self._cid_cache.clear()
            self._cid_order = []
            event = self._append_locked({"t": P.EV_NEW, "side": side}, game=True)
            return {"v": P.PROTOCOL_VERSION, "t": "ack", "ok": True,
                    "seq": self.next_seq - 1, "events": [event],
                    "snapshot": self._snapshot_locked()}

    def public_summary(self):
        """不含任何秘密的公开摘要（可安全暴露给未入座者）。"""
        with self._cv:
            return {
                "v": P.PROTOCOL_VERSION, "room": self.code,
                "seq": self.next_seq - 1,
                "seats": {side: ({"joined": True, "connected": seat.connected,
                                  "name": seat.name} if seat
                                 else {"joined": False, "connected": False})
                          for side, seat in self.seats.items()},
                "free_sides": [side for side in P.SIDES if self.seats[side] is None],
                "side": self.board.side,
                "fen": self.board.to_fen(),
                "moves": len(self.move_log),
                "result": self.result,
                "updated_ms": self.updated_ms,
            }

    def last_activity_ms(self):
        with self._cv:
            return self.updated_ms


class RoomManager(object):
    """房间表（内存态；服务器重启后对局结束，这是刻意的取舍）。"""

    def __init__(self, max_rooms=P.MAX_ROOMS, ttl_seconds=P.ROOM_TTL_SECONDS):
        self._lock = threading.RLock()
        self._rooms = {}
        self.max_rooms = max_rooms
        self.ttl_seconds = ttl_seconds

    def create(self, side=None, name=None):
        with self._lock:
            if len(self._rooms) >= self.max_rooms:
                self.cleanup()
            if len(self._rooms) >= self.max_rooms:
                raise RoomError(P.ERR_TOO_LARGE, "服务器房间数已达上限，请稍后再试")
            code = P.new_room_code()
            while code in self._rooms:
                code = P.new_room_code()
            room = Room(code)                       # 先建房间（含 start 事件）
            self._rooms[code] = room
        chosen, token, events = room.join(side=side, name=name)
        return room, chosen, token, events

    def get(self, code):
        try:
            code = P.normalize_room_code(code)
        except P.ProtocolError as error:
            raise RoomError(error.code, error.message)
        with self._lock:
            room = self._rooms.get(code)
        if room is None:
            raise RoomError(P.ERR_NOT_FOUND, "房间不存在或已回收：%s" % code)
        return room

    def exists(self, code):
        with self._lock:
            return code in self._rooms

    def cleanup(self):
        """回收长时间无活动的房间，返回回收数量。"""
        deadline = time.time() * 1000 - self.ttl_seconds * 1000
        removed = 0
        with self._lock:
            for code in list(self._rooms):
                if self._rooms[code].last_activity_ms() < deadline:
                    del self._rooms[code]
                    removed += 1
        return removed

    def stats(self):
        with self._lock:
            rooms = list(self._rooms.values())
        return {
            "rooms": len(rooms),
            "games_over": sum(1 for room in rooms if room.result is not None),
            "players_online": sum(
                1 for room in rooms for seat in room.seats.values()
                if seat is not None and seat.connected),
        }


__all__ = ["Room", "Seat", "RoomError", "RoomManager"]
