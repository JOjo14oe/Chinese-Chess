# -*- coding: utf-8 -*-
"""房间状态机测试（不涉及 socket）：轮次、幂等、过期重发、重连、事件日志。

这些测试覆盖跨国高延迟场景下最容易出错的语义：
重发同一着法不能走两步棋、基于过期局面的着法必须被拒绝并回快照、
重连要能精确补齐缺失事件、对手上线/掉线不能让在途的着法作废。
"""

import threading
import time
import unittest

from xiangqi.net import protocol as P
from xiangqi.net.rooms import RoomError, RoomManager


class TestRoomBasics(unittest.TestCase):
    def setUp(self):
        self.manager = RoomManager()
        (self.room, self.red_side,
         self.red_token, _) = self.manager.create()

    def test_created_room_starts_with_red_and_snapshot(self):
        self.assertEqual(self.red_side, "r")
        snapshot = self.room.snapshot()
        self.assertEqual(snapshot["side"], "r")
        self.assertEqual(snapshot["moves"], [])
        self.assertEqual(snapshot["seq"], 2)                  # start + join 两个事件
        self.assertEqual([ev["t"] for ev in self.room.events],
                         [P.EV_START, P.EV_JOIN])
        self.assertIn("legal", snapshot)
        self.assertEqual(len(snapshot["legal"]["h2"]), 12)    # 右炮 12 步
        self.assertIsNone(snapshot["result"])

    def test_room_code_lookup_is_case_insensitive(self):
        self.assertIs(self.manager.get(self.room.code.lower()), self.room)

    def test_unknown_room_raises(self):
        with self.assertRaises(RoomError) as ctx:
            self.manager.get("AAAAAA")
        self.assertEqual(ctx.exception.code, P.ERR_NOT_FOUND)

    def test_bad_room_code_raises(self):
        with self.assertRaises(RoomError) as ctx:
            self.manager.get("bad!code")
        self.assertEqual(ctx.exception.code, P.ERR_BAD_ROOM_CODE)

    def test_second_seat_then_room_full(self):
        black_side, black_token, events = self.room.join()
        self.assertEqual(black_side, "b")
        self.assertTrue(black_token)
        self.assertEqual(events[0]["t"], P.EV_JOIN)
        with self.assertRaises(RoomError) as ctx:
            self.room.join()
        self.assertEqual(ctx.exception.code, P.ERR_ROOM_FULL)

    def test_join_can_pick_a_side(self):
        side, _, _ = self.room.join(side="b")
        self.assertEqual(side, "b")
        self.assertEqual(self.room.snapshot()["seats"]["r"]["joined"], True)
        self.assertEqual(self.room.snapshot()["seats"]["b"]["joined"], True)

    def test_join_with_token_reconnects_same_seat(self):
        side, token, _ = self.room.join()
        again_side, again_token, events = self.room.join(token=token)
        self.assertEqual((again_side, again_token), (side, token))
        self.assertTrue(events[0].get("rejoin"))
        self.assertEqual(self.room.seats["b"].resumes, 1)

    def test_presence_updates_connected_flag(self):
        _, token, _ = self.room.join()
        self.room.touch(token, True)
        self.assertTrue(self.room.snapshot()["seats"]["b"]["connected"])
        self.room.touch(token, False)
        self.assertFalse(self.room.snapshot()["seats"]["b"]["connected"])


class TestMoveProtocol(unittest.TestCase):
    def setUp(self):
        self.manager = RoomManager()
        (self.room, _, self.red_token, _) = self.manager.create()
        self.black_side, self.black_token, _ = self.room.join()

    def test_turn_enforcement(self):
        with self.assertRaises(RoomError) as ctx:
            self.room.apply_move("b", "h9g7", base_seq=self.room.snapshot()["seq"])
        self.assertEqual(ctx.exception.code, P.ERR_NOT_YOUR_TURN)

    def test_move_records_event_snapshot_and_notation(self):
        base = self.room.snapshot()["seq"]
        result = self.room.apply_move("r", "h2e2", base_seq=base, cid="c1")
        self.assertTrue(result["ok"])
        event = result["events"][0]
        self.assertEqual(event["t"], P.EV_MOVE)
        self.assertEqual(event["chinese"], "炮二平五")
        self.assertEqual(event["from"], "h2")
        self.assertEqual(event["to"], "e2")
        snapshot = result["snapshot"]
        self.assertEqual(snapshot["side"], "b")
        self.assertEqual(snapshot["moves"][0]["chinese"], "炮二平五")
        self.assertEqual(snapshot["last_move"], {"from": "h2", "to": "e2"})
        self.assertIn("h9", snapshot["legal"])        # 轮到黑方，给黑方合法着法

    def test_illegal_move_rejected_with_snapshot(self):
        base = self.room.snapshot()["seq"]
        with self.assertRaises(RoomError) as ctx:
            self.room.apply_move("r", "h2e5", base_seq=base)
        self.assertEqual(ctx.exception.code, P.ERR_ILLEGAL)
        self.assertIsNotNone(ctx.exception.snapshot)
        self.assertEqual(self.room.snapshot()["moves"], [])

    def test_bad_iccs_format_rejected(self):
        with self.assertRaises(RoomError) as ctx:
            self.room.apply_move("r", "hello", base_seq=self.room.snapshot()["seq"])
        self.assertEqual(ctx.exception.code, P.ERR_ILLEGAL)

    def test_stale_base_seq_rejected(self):
        self.room.apply_move("r", "h2e2", cid="c1")
        with self.assertRaises(RoomError) as ctx:
            self.room.apply_move("b", "h9g7", base_seq=0)     # 基于开局
        self.assertEqual(ctx.exception.code, P.ERR_STALE)
        self.assertIsNotNone(ctx.exception.snapshot)
        self.assertEqual(len(self.room.snapshot()["moves"]), 1)

    def test_duplicate_cid_is_idempotent(self):
        base = self.room.snapshot()["seq"]
        first = self.room.apply_move("r", "h2e2", base_seq=base, cid="same-cid")
        replay = self.room.apply_move("r", "h2e2", base_seq=base, cid="same-cid")
        self.assertTrue(replay.get("replayed"))
        self.assertEqual(replay["seq"], first["seq"])
        self.assertEqual(len(self.room.snapshot()["moves"]), 1)

    def test_presence_event_does_not_invalidate_inflight_move(self):
        """对手上线/掉线不改变棋局，不能让在途的一步棋被判过期。"""
        base = self.room.snapshot()["seq"]
        self.room.touch(self.black_token, False)      # 追加 presence 事件
        self.assertGreater(self.room.snapshot()["seq"], base)
        result = self.room.apply_move("r", "h2e2", base_seq=base)
        self.assertTrue(result["ok"])

    def test_game_over_blocks_further_moves(self):
        self.room.resign("r")
        with self.assertRaises(RoomError) as ctx:
            self.room.apply_move("b", "h9g7")
        self.assertEqual(ctx.exception.code, P.ERR_GAME_OVER)

    def test_resign_result(self):
        result = self.room.resign("r")
        self.assertEqual(result["events"][-1]["t"], P.EV_END)
        self.assertEqual(self.room.snapshot()["result"]["winner"], "b")
        self.assertIn("认输", self.room.snapshot()["result_text"])

    def test_new_game_resets_and_keeps_seq_monotonic(self):
        self.room.apply_move("r", "h2e2")
        before = self.room.snapshot()["seq"]
        self.room.new_game("b")
        snapshot = self.room.snapshot()
        self.assertEqual(snapshot["moves"], [])
        self.assertIsNone(snapshot["result"])
        self.assertGreater(snapshot["seq"], before)
        self.assertEqual(snapshot["side"], "r")

    def test_checkmate_ends_game(self):
        room = RoomManager().create()[0]
        room.board.set_fen("4k4/1R7/9/9/9/9/9/9/9/R2K5 w - - 0 1")
        result = room.apply_move("r", "a0a9")
        self.assertIsNotNone(room.result)
        self.assertEqual(room.result["winner"], "r")
        self.assertEqual(result["events"][-1]["t"], P.EV_END)
        self.assertIn("将死", room.result["reason"])

    def test_engine_move_via_api_is_accepted(self):
        from xiangqi.ai import Engine
        room = RoomManager().create()[0]
        move = Engine("easy", seed=3).choose_move(room.board, "easy")
        result = room.apply_move("r", move.iccs(), cid="engine")
        self.assertTrue(result["ok"])
        self.assertEqual(len(room.snapshot()["moves"]), 1)


class TestEventsAndReconnect(unittest.TestCase):
    def setUp(self):
        self.manager = RoomManager()
        (self.room, _, self.red_token, _) = self.manager.create()
        _, self.black_token, _ = self.room.join()

    def test_events_since_returns_only_new(self):
        self.room.apply_move("r", "h2e2")
        seq = self.room.snapshot()["seq"]
        self.room.apply_move("b", "h9g7")
        payload = self.room.events_since(seq)
        self.assertEqual(payload["t"], "events")
        self.assertTrue(payload["events"])
        self.assertTrue(all(ev["seq"] > seq for ev in payload["events"]))

    def test_stale_since_gets_snapshot(self):
        payload = self.room.events_since(10 ** 6)
        self.assertEqual(payload["t"], "snapshot")
        self.assertEqual(payload["snapshot"]["room"], self.room.code)

    def test_wait_events_returns_immediately_when_pending(self):
        self.room.apply_move("r", "h2e2")
        started = time.monotonic()
        payload = self.room.wait_events(0, timeout=5)
        # 有事件时必须立刻返回（给慢 runner 留足调度余量，不卡 1 秒紧边界）
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(payload["t"], "events")

    def test_wait_events_wakes_up_on_move_from_another_thread(self):
        since = self.room.snapshot()["seq"]
        collected = {}

        def waiter():
            collected["payload"] = self.room.wait_events(since, timeout=10)

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.2)
        self.room.apply_move("r", "h2e2")
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "等待线程没有被唤醒")
        self.assertEqual(collected["payload"]["events"][0]["iccs"], "h2e2")

    def test_wait_events_times_out_quietly(self):
        payload = self.room.wait_events(self.room.snapshot()["seq"], timeout=0.2)
        self.assertEqual(payload["events"], [])
        self.assertTrue(payload.get("timeout"))

    def test_seq_is_strictly_incremental_and_replayable(self):
        """seq 必须严格每事件 +1，且任何 seq 都能通过 since=seq-1 拿到。"""
        room, _, red_token, _ = RoomManager().create()
        _, black_token, _ = room.join()
        room.touch(red_token, True)
        room.apply_move("r", "h2e2")
        room.touch(red_token, False)
        room.apply_move("b", "h9g7")
        payload = room.events_since(0)
        seqs = [ev["seq"] for ev in payload["events"]]
        self.assertEqual(seqs, list(range(1, room.next_seq)),
                         "事件序号必须连续无空洞：%s" % seqs)
        for seq in seqs:
            replay = room.events_since(seq - 1)["events"]
            self.assertTrue(replay, "since=%d 应能拿到 seq=%d" % (seq - 1, seq))
            self.assertEqual(replay[0]["seq"], seq)

    def test_multiple_connections_keep_seat_online(self):
        """同一令牌开两个标签页时，先断开的那个不能把座位标成离线。"""
        token = self.black_token
        self.assertFalse(self.room.snapshot()["seats"]["b"]["connected"])
        self.room.touch(token, True)
        self.room.touch(token, True)
        self.assertTrue(self.room.snapshot()["seats"]["b"]["connected"])
        self.room.touch(token, False)
        self.assertTrue(self.room.snapshot()["seats"]["b"]["connected"],
                        "还有一个连接存活，应保持在线")
        self.room.touch(token, False)
        self.assertFalse(self.room.snapshot()["seats"]["b"]["connected"])
        self.room.touch(token, False)          # 多余的下线不应变成负数
        self.assertEqual(self.room.seats["b"].connections, 0)

    def test_resume_events_are_after_since_and_not_newer_than_snapshot(self):
        self.room.apply_move("r", "h2e2")
        since = self.room.snapshot()["seq"]
        self.room.apply_move("b", "h9g7")
        payload = self.room.events_since(since)
        snapshot = self.room.snapshot()
        self.assertTrue(payload["events"])
        for event in payload["events"]:
            self.assertGreater(event["seq"], since)
            self.assertLessEqual(event["seq"], snapshot["seq"])
        # 位置过旧时：只回快照，不混着回事件
        stale = self.room.events_since(10 ** 6)
        self.assertEqual(stale["t"], "snapshot")
        self.assertNotIn("events", stale)

    def test_public_summary_seq_shares_events_namespace(self):
        summary = self.room.public_summary()
        self.room.apply_move("r", "h2e2")
        self.assertEqual(summary["seq"] + 1, self.room.public_summary()["seq"])
        self.assertEqual(summary["moves"], 0)          # 公开摘要里 moves 是数量
        self.assertEqual(self.room.public_summary()["moves"], 1)

    def test_resume_validates_token(self):
        with self.assertRaises(RoomError) as ctx:
            self.room.resume("bogus-token")
        self.assertEqual(ctx.exception.code, P.ERR_FORBIDDEN)
        self.assertEqual(self.room.resume(self.red_token, 0), "r")

    def test_token_must_match_exactly(self):
        self.assertIsNotNone(self.room.seat_of_token(self.red_token))
        self.assertIsNone(self.room.seat_of_token(self.red_token[:-1]))
        self.assertIsNone(self.room.seat_of_token(self.red_token + "x"))
        self.assertIsNone(self.room.seat_of_token(self.red_token.upper()))
        self.assertIsNone(self.room.seat_of_token(""))
        self.assertIsNone(self.room.seat_of_token(None))

    def test_event_log_is_trimmed_and_old_since_becomes_snapshot(self):
        room, _, token, _ = RoomManager().create()
        for _ in range(P.MAX_EVENTS + 50):
            room.touch(token, True)
        self.assertLessEqual(len(room.events), P.MAX_EVENTS)
        self.assertEqual(room.events_since(0)["t"], "snapshot")

    def test_public_summary_hides_tokens(self):
        summary = self.room.public_summary()
        text = P.dumps(summary)
        self.assertNotIn(self.red_token, text)
        self.assertNotIn(self.black_token, text)
        self.assertEqual(summary["free_sides"], [])


class TestManager(unittest.TestCase):
    def test_create_many_rooms_and_stats(self):
        manager = RoomManager()
        codes = set()
        for _ in range(5):
            room, side, token, _ = manager.create()
            codes.add(room.code)
            self.assertTrue(token)
            self.assertEqual(side, "r")
        self.assertEqual(len(codes), 5)
        self.assertEqual(manager.stats()["rooms"], 5)

    def test_max_rooms_enforced(self):
        manager = RoomManager(max_rooms=2)
        manager.create()
        manager.create()
        with self.assertRaises(RoomError) as ctx:
            manager.create()
        self.assertEqual(ctx.exception.code, P.ERR_TOO_LARGE)

    def test_cleanup_removes_idle_rooms(self):
        manager = RoomManager(ttl_seconds=0)
        room, _, _, _ = manager.create()
        time.sleep(0.01)
        self.assertEqual(manager.cleanup(), 1)
        self.assertFalse(manager.exists(room.code))

    def test_protocol_helpers(self):
        self.assertTrue(P.room_code_valid("A7K2QM"))
        self.assertFalse(P.room_code_valid("A7K2Q"))
        self.assertFalse(P.room_code_valid("A7K2QO"))     # O 不在字母表中
        with self.assertRaises(P.ProtocolError):
            P.load(b"{oops")
        self.assertEqual(P.load(b'{"a":1}')["a"], 1)
        with self.assertRaises(P.ProtocolError):
            P.require_str({}, "x")
        with self.assertRaises(P.ProtocolError):
            P.require_int({"x": "abc"}, "x")
        self.assertEqual(P.dumps({"a": "炮"}), '{"a":"炮"}')
        self.assertNotEqual(P.new_room_code(), P.new_room_code())


if __name__ == "__main__":
    unittest.main()
