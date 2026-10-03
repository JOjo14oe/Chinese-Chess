# -*- coding: utf-8 -*-
"""行棋规则测试：各棋子走法、蹩马腿/塞象眼/炮架、九宫、过河、飞将、胜负判定。"""

import unittest

try:
    from .helpers import board_of, targets
except ImportError:  # 直接运行单文件时
    from helpers import board_of, targets
from xiangqi.board import Board, IllegalMove
from xiangqi.constants import BLACK, Move, RED, START_FEN
from xiangqi.notation import move_from_iccs


class TestInitialPosition(unittest.TestCase):
    def test_counts(self):
        board = Board(START_FEN)
        self.assertEqual(len(board.gen_pseudo_moves()), 44)
        self.assertEqual(len(list(board.gen_legal_moves())), 44)
        self.assertFalse(board.in_check(RED))
        self.assertFalse(board.in_check(BLACK))

    def test_cannon_moves(self):
        board = Board(START_FEN)
        # 右炮：纵线 4 格 + 隔黑炮吃 h9 黑马 + 横线 5 格 + i2 + h1
        self.assertEqual(targets(board, "h2"),
                         {"h3", "h4", "h5", "h6", "h9",
                          "g2", "f2", "e2", "d2", "c2", "i2", "h1"})

    def test_rook_and_horse_moves(self):
        board = Board(START_FEN)
        self.assertEqual(targets(board, "a0"), {"a1", "a2"})      # 车被己方兵挡住
        self.assertEqual(targets(board, "b0"), {"a2", "c2"})      # 马腿被 c0 相挡住 d1
        self.assertEqual(targets(board, "h0"), {"g2", "i2"})
        self.assertEqual(targets(board, "c0"), {"a2", "e2"})      # 相
        self.assertEqual(targets(board, "d0"), {"e1"})            # 仕
        self.assertEqual(targets(board, "e0"), {"e1"})            # 帅（d0/f0 有仕）
        self.assertEqual(targets(board, "a3"), {"a4"})            # 兵


class TestHorse(unittest.TestCase):
    def test_leg_blocking(self):
        board = board_of({"e0": "K", "d9": "k", "b2": "N"})
        self.assertEqual(targets(board, "b2"),
                         {"a0", "c0", "a4", "c4", "d1", "d3"})
        blocked = board_of({"e0": "K", "d9": "k", "b2": "N", "b3": "P"})
        self.assertEqual(targets(blocked, "b2"), {"a0", "c0", "d1", "d3"})


class TestElephant(unittest.TestCase):
    def test_eye_and_river(self):
        board = board_of({"e0": "K", "d9": "k", "c0": "B"})
        self.assertEqual(targets(board, "c0"), {"a2", "e2"})
        blocked = board_of({"e0": "K", "d9": "k", "c0": "B", "b1": "P"})
        self.assertEqual(targets(blocked, "c0"), {"e2"})          # 塞象眼
        across = board_of({"e0": "K", "d9": "k", "e4": "B"})
        self.assertEqual(targets(across, "e4"), {"c2", "g2"})     # 不得过河


class TestCannon(unittest.TestCase):
    # 红帅置于 f0：既避免与黑将 d9 照面，也不会让 e2 的炮成为“被牵制”的子
    BASE = {"f0": "K", "d9": "k", "e2": "C"}

    def test_screen_required(self):
        with_screen = board_of(dict(self.BASE, e5="P", e9="r"))
        self.assertIn("e9", targets(with_screen, "e2"))

        no_screen = board_of(dict(self.BASE, e9="r"))
        self.assertNotIn("e9", targets(no_screen, "e2"))
        self.assertIn("e3", targets(no_screen, "e2"))             # 可平移
        self.assertIn("h2", targets(no_screen, "e2"))

    def test_two_screens_cannot_reach(self):
        board = board_of(dict(self.BASE, e5="P", e7="p", e9="r"))
        moved = targets(board, "e2")
        self.assertIn("e7", moved)      # 隔 e5 吃 e7 的黑卒
        self.assertNotIn("e9", moved)   # 有两个子挡着，吃不到 e9 黑车


class TestPalaceGuards(unittest.TestCase):
    def test_advisor(self):
        board = board_of({"e0": "K", "d9": "k", "d0": "A"})
        self.assertEqual(targets(board, "d0"), {"e1"})            # c1 出九宫

    def test_king(self):
        # 黑将放 a9：避免与红帅同线照面影响走法统计
        board = board_of({"e0": "K", "a9": "k"})
        self.assertEqual(targets(board, "e0"), {"d0", "e1", "f0"})
        upper = board_of({"e2": "K", "a9": "k"})
        self.assertEqual(targets(upper, "e2"), {"e1", "d2", "f2"})  # e3 出九宫

    def test_king_cannot_face_enemy_king(self):
        # 黑将在 d9 时，红帅走到 d0 会照面，故 d0 非法
        board = board_of({"e0": "K", "d9": "k"})
        self.assertEqual(targets(board, "e0"), {"e1", "f0"})


class TestPawn(unittest.TestCase):
    def test_before_and_after_river(self):
        before = board_of({"e0": "K", "a9": "k", "c4": "P"})
        self.assertEqual(targets(before, "c4"), {"c5"})
        after = board_of({"e0": "K", "a9": "k", "c5": "P"})
        self.assertEqual(targets(after, "c5"), {"c6", "b5", "d5"})
        black_before = board_of({"e0": "K", "a9": "k", "c6": "p"}, side="b")
        self.assertEqual(targets(black_before, "c6"), {"c5"})
        black_after = board_of({"e0": "K", "a9": "k", "c4": "p"}, side="b")
        self.assertEqual(targets(black_after, "c4"), {"c3", "b4", "d4"})

    def test_never_moves_backward(self):
        board = board_of({"e0": "K", "a9": "k", "c5": "P"})
        self.assertNotIn("c4", targets(board, "c5"))
        board = board_of({"e0": "K", "a9": "k", "c4": "p"}, side="b")
        self.assertNotIn("c5", targets(board, "c4"))


class TestFlyingGeneral(unittest.TestCase):
    def test_facing_detected(self):
        self.assertTrue(board_of({"e0": "K", "e9": "k"}).kings_facing())
        self.assertFalse(board_of({"e0": "K", "e9": "k", "e5": "P"}).kings_facing())
        self.assertFalse(board_of({"e0": "K", "d9": "k"}).kings_facing())

    def test_move_exposing_facing_is_illegal(self):
        board = board_of({"e0": "K", "e9": "k", "e5": "P"})
        self.assertEqual(targets(board, "e5"), {"e6"})   # b5/d5 会让将帅照面

    def test_blocker_cannot_leave_file(self):
        board = board_of({"e0": "K", "e9": "k", "e5": "R"})
        for move in board.legal_moves_from(5, 4):
            self.assertEqual(move.tc, 4, "车离开 e 线应属非法：%s" % move.iccs())


class TestCheckDetection(unittest.TestCase):
    def test_cannon_check(self):
        board = board_of({"e0": "K", "d9": "k", "e9": "c", "e5": "P"})
        self.assertTrue(board.in_check(RED))

    def test_horse_check_and_leg(self):
        board = board_of({"e0": "K", "d9": "k", "d2": "n"})
        self.assertTrue(board.in_check(RED))
        blocked = board_of({"e0": "K", "d9": "k", "d2": "n", "d1": "P"})
        self.assertFalse(blocked.in_check(RED))

    def test_rook_check(self):
        board = board_of({"e0": "K", "d9": "k", "e7": "r"})
        self.assertTrue(board.in_check(RED))
        blocked = board_of({"e0": "K", "d9": "k", "e7": "r", "e4": "R"})
        self.assertFalse(blocked.in_check(RED))

    def test_pawn_check(self):
        board = board_of({"d9": "k", "d4": "K", "d5": "p"})
        self.assertTrue(board.in_check(RED))

    def test_pinned_piece(self):
        board = board_of({"e0": "K", "d9": "k", "e2": "R", "e9": "r"})
        for move in board.legal_moves_from(2, 4):
            self.assertEqual(move.fc, 4, "被牵制的车不能离开 e 线：%s" % move.iccs())


class TestGameEnd(unittest.TestCase):
    def test_checkmate(self):
        board = board_of({"a9": "R", "e9": "k", "a8": "R", "d0": "K"}, side="b")
        self.assertTrue(board.in_check(BLACK))
        over, winner, reason = board.status()
        self.assertTrue(over)
        self.assertEqual(winner, RED)
        self.assertIn("将死", reason)

    def test_stalemate_is_loss(self):
        board = board_of({"e9": "k", "d8": "R", "f8": "R", "d0": "K"}, side="b")
        self.assertFalse(board.in_check(BLACK))
        self.assertFalse(board.has_legal_moves(BLACK))
        over, winner, reason = board.status()
        self.assertTrue(over)
        self.assertEqual(winner, RED)
        self.assertIn("困毙", reason)

    def test_check_but_not_mate(self):
        board = board_of({"a9": "R", "e9": "k", "d0": "K"}, side="b")
        over, winner, _ = board.status()
        self.assertFalse(over)
        self.assertIsNone(winner)

    def test_result_text(self):
        board = board_of({"a9": "R", "e9": "k", "a8": "R", "d0": "K"}, side="b")
        self.assertIn("红方胜", board.result_text())
        self.assertEqual(Board(START_FEN).result_text(), "")


class TestRepetitionAndDraw(unittest.TestCase):
    SHUFFLE = ("a2a3", "i7i6", "a3a2", "i6i7")

    def _shuffle_board(self, halfmove=0):
        return board_of({"e9": "k", "i7": "r", "a2": "R", "d0": "K"},
                        side="w", halfmove=halfmove)

    def test_first_repetition_not_over(self):
        board = self._shuffle_board()
        for text in self.SHUFFLE:
            board.make_move(move_from_iccs(text))
        self.assertEqual(board.repetition_count(), 2)
        self.assertFalse(board.status()[0])

    def test_threefold_is_draw(self):
        board = self._shuffle_board()
        for _ in range(2):
            for text in self.SHUFFLE:
                board.make_move(move_from_iccs(text))
        over, winner, reason = board.status()
        self.assertTrue(over)
        self.assertIsNone(winner)
        self.assertIn("重复", reason)

    def test_natural_limit_draw(self):
        board = self._shuffle_board(halfmove=119)
        board.make_move(move_from_iccs("a2a3"))
        over, winner, reason = board.status()
        self.assertTrue(over)
        self.assertIsNone(winner)
        self.assertIn("无吃子", reason)

    def test_capture_resets_counter(self):
        board = board_of({"e0": "K", "d9": "k", "a2": "R", "a7": "r"})
        board.make_move(move_from_iccs("a2a3"))
        self.assertEqual(board.halfmove_clock, 1)
        board.make_move(move_from_iccs("a7a3"))   # 黑车吃红车
        self.assertEqual(board.halfmove_clock, 0)

    def test_perpetual_check_detected_with_push(self):
        """用 push() 走棋（不记录 gave_check）时也必须能判出长将。"""
        board = board_of({"a0": "K", "e9": "k", "e4": "R"}, side="b")
        for _ in range(2):
            for text in ("e9d9", "e4d4", "d9e9", "d4e4"):
                board.push(move_from_iccs(text))
        self.assertEqual(board.repetition_count(), 3)
        over, winner, reason = board.status()
        self.assertTrue(over, "三次重复应结束对局")
        self.assertEqual(winner, BLACK)
        self.assertIn("长将", reason)

    def test_no_false_perpetual_check(self):
        """双方都不将军的重复局面应判和，不能误判长将。"""
        board = self._shuffle_board()
        for _ in range(2):
            for text in self.SHUFFLE:
                board.push(move_from_iccs(text))
        over, winner, reason = board.status()
        self.assertTrue(over)
        self.assertIsNone(winner)
        self.assertIn("重复", reason)


class TestBoardState(unittest.TestCase):
    def test_fen_roundtrip(self):
        self.assertEqual(Board(START_FEN).to_fen(), START_FEN)
        custom = board_of({"e0": "K", "d9": "k", "b2": "N"}).to_fen()
        self.assertEqual(Board(custom).to_fen(), custom)

    def test_set_fen_rejects_without_corrupting_state(self):
        """非法 FEN 必须被拒绝，且不得留下“新棋盘 + 旧散列”的混合状态。"""
        cases = [
            ("", "空 FEN"),
            ("9/9/9/9/9/9/9/9/9", "行数不足"),
            ("9/9/9/9/9/9/9/9/9/9/9", "行数过多"),
            ("9/9/9/9/9/9/9/9/9/4K4K", "某行过宽"),
            ("9/9/9/9/9/9/9/9/9/x", "未知棋子"),
            ("4k4/9/9/9/9/9/9/9/9/3K5 z - - 0 1", "走子方无效"),
            ("4k4/9/9/9/9/9/9/9/9/3K5 w - - x 1", "回合计数无效"),
            ("4k4/9/9/9/9/9/9/9/9/3K5 w - - 0 y", "回合计无效"),
        ]
        for fen, label in cases:
            board = Board(START_FEN)
            before = (board.to_fen(), board.hash, board.side,
                      len(board.history), len(board._keys))
            with self.assertRaises(ValueError, msg="应拒绝：" + label):
                board.set_fen(fen)
            after = (board.to_fen(), board.hash, board.side,
                     len(board.history), len(board._keys))
            self.assertEqual(before, after, "载入失败后局面被破坏：%s" % label)
            self.assertEqual(board.hash, board._compute_hash())

    def test_push_pop_restores_everything(self):
        board = Board(START_FEN)
        before_fen = board.to_fen()
        before_hash = board.hash
        board.push(move_from_iccs("h2e2"))
        self.assertNotEqual(board.to_fen(), before_fen)
        board.pop()
        self.assertEqual(board.to_fen(), before_fen)
        self.assertEqual(board.hash, before_hash)
        self.assertEqual(board.side, RED)

    def test_make_move_rejects_illegal(self):
        board = Board(START_FEN)
        with self.assertRaises(IllegalMove):
            board.make_move(Move(0, 0, 5, 0))          # 车不能飞过河吃子
        with self.assertRaises(IllegalMove):
            board.make_move(Move(3, 0, 3, 1))          # 兵不能横走（未过河）
        self.assertEqual(board.history, [])

    def test_make_move_records_check_flag(self):
        board = board_of({"e0": "K", "d9": "k", "a8": "R"})
        record = board.make_move(move_from_iccs("a8a9"))
        self.assertTrue(record.gave_check)
        board2 = board_of({"e0": "K", "d9": "k", "a8": "R"})
        record2 = board2.make_move(move_from_iccs("a8b8"))
        self.assertFalse(record2.gave_check)

    def test_copy_is_independent(self):
        board = Board(START_FEN)
        clone = board.copy()
        clone.push(move_from_iccs("h2e2"))
        self.assertNotEqual(board.to_fen(), clone.to_fen())
        self.assertEqual(board.side, RED)
        self.assertEqual(len(board.history), 0)

    def test_set_fen_requires_exactly_one_king_each(self):
        """缺将/多将的 FEN 属于结构性非法，必须拒绝。"""
        cases = [
            ("4k4/9/9/9/9/9/9/9/9/9 w - - 0 1", "缺少红帅"),
            ("9/9/9/9/9/9/9/9/9/3K5 w - - 0 1", "缺少黑将"),
            ("4k4/9/9/9/9/9/9/9/9/3KK4 w - - 0 1", "红方两个帅"),
            ("4kk4/9/9/9/9/9/9/9/9/3K5 w - - 0 1", "黑方两个将"),
        ]
        for fen, label in cases:
            with self.assertRaises(ValueError, msg="应拒绝：" + label):
                Board(fen)

    def test_missing_king_counts_as_check_defensively(self):
        """将/帅缺失时 in_check 返回 True（防御性分支，正常局面不会出现）。"""
        board = Board(START_FEN)
        rank, file = board.king_pos(RED)
        board.grid[rank][file] = None
        self.assertTrue(board.in_check(RED))


if __name__ == "__main__":
    unittest.main()
