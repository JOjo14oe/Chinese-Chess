# -*- coding: utf-8 -*-
"""记谱测试：ICCS 坐标记谱与中文纵线记谱的解析、生成与往返一致性。"""

import random
import unittest

try:
    from .helpers import board_of
except ImportError:  # 直接运行单文件时
    from helpers import board_of
from xiangqi.board import Board
from xiangqi.constants import START_FEN
from xiangqi.notation import (move_from_iccs, move_to_chinese, parse_chinese)

BLACK_TO_MOVE_FEN = ("rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/"
                     "RNBAKABNR b - - 0 1")


class TestIccs(unittest.TestCase):
    def test_roundtrip(self):
        move = move_from_iccs("h2e2")
        self.assertEqual((move.fr, move.fc, move.tr, move.tc), (2, 7, 2, 4))
        self.assertEqual(move.iccs(), "h2e2")
        self.assertEqual(move.from_iccs, "h2")
        self.assertEqual(move.to_iccs, "e2")

    def test_lenient_input(self):
        self.assertEqual(move_from_iccs("H2-E2").iccs(), "h2e2")
        self.assertEqual(move_from_iccs(" a0 a1 ").iccs(), "a0a1")

    def test_invalid(self):
        for text in ("z9x9", "h2", "hello", ""):
            with self.assertRaises(ValueError):
                move_from_iccs(text)


class TestChineseNotation(unittest.TestCase):
    def test_red_opening_moves(self):
        board = Board(START_FEN)
        cases = {
            "炮二平五": "h2e2",
            "炮八平五": "b2e2",
            "炮二进七": "h2h9",
            "马二进三": "h0g2",
            "马八进七": "b0c2",
            "兵七进一": "c3c4",
            "车九进一": "a0a1",
            "仕四进五": "f0e1",
            "相三进五": "g0e2",
        }
        for text, iccs in cases.items():
            parsed = parse_chinese(board, text)
            self.assertIsNotNone(parsed, "无法解析 %s" % text)
            self.assertEqual(parsed.iccs(), iccs, "%s 解析错误" % text)

    def test_black_opening_moves(self):
        board = Board(BLACK_TO_MOVE_FEN)
        cases = {
            "马8进7": "h9g7",
            "炮2平5": "b7e7",
            "卒3进1": "c6c5",
            "车1进1": "a9a8",
        }
        for text, iccs in cases.items():
            parsed = parse_chinese(board, text)
            self.assertIsNotNone(parsed, "无法解析 %s" % text)
            self.assertEqual(parsed.iccs(), iccs, "%s 解析错误" % text)

    def test_generate_red_and_black(self):
        board = Board(START_FEN)
        self.assertEqual(move_to_chinese(board, move_from_iccs("h2e2")), "炮二平五")
        self.assertEqual(move_to_chinese(board, move_from_iccs("h0g2")), "马二进三")
        self.assertEqual(move_to_chinese(board, move_from_iccs("b0a2")), "马八进九")
        black = Board(BLACK_TO_MOVE_FEN)
        self.assertEqual(move_to_chinese(black, move_from_iccs("h9g7")), "马8进7")
        self.assertEqual(move_to_chinese(black, move_from_iccs("b7e7")), "炮2平5")

    def test_stacked_pieces_use_front_back(self):
        board = board_of({"e0": "K", "d9": "k", "a0": "R", "a5": "R"})
        self.assertEqual(move_to_chinese(board, move_from_iccs("a5a6")), "前车进一")
        self.assertEqual(move_to_chinese(board, move_from_iccs("a0a1")), "后车进一")
        self.assertEqual(move_to_chinese(board, move_from_iccs("a5a2")), "前车退三")
        self.assertEqual(parse_chinese(board, "前车进一").iccs(), "a5a6")
        # 后车 a0 只能进（不能后退），退一不存在
        self.assertIsNone(parse_chinese(board, "后车退一"))

    def test_ambiguous_stacks_include_file_number(self):
        # c 线与 e 线各有两兵，前/后 称谓冲突 -> 记谱需附纵线号
        board = board_of({"e0": "K", "d9": "k",
                          "c2": "P", "c4": "P", "e2": "P", "e4": "P"})
        text = move_to_chinese(board, move_from_iccs("c4c5"))
        self.assertEqual(text, "前兵七进一")
        self.assertEqual(parse_chinese(board, text).iccs(), "c4c5")

    def test_invalid_notation(self):
        board = Board(START_FEN)
        for text in ("炮二平十", "车五进一", "飞象", "", "马二进"):
            self.assertIsNone(parse_chinese(board, text))

    def test_roundtrip_initial_position(self):
        self._assert_roundtrip(Board(START_FEN))

    def test_roundtrip_stacked_rooks(self):
        self._assert_roundtrip(board_of({"e0": "K", "d9": "k", "a0": "R", "a5": "R"}))

    def test_roundtrip_stacked_pawns(self):
        self._assert_roundtrip(board_of({"e0": "K", "d9": "k",
                                         "c2": "P", "c4": "P", "e2": "P", "e4": "P"}))

    def test_six_piece_stack_roundtrip(self):
        """同线 6 个同类子（实战不可能，最多 5 兵）时记谱生成/解析仍须自洽。"""
        board = board_of({"a4": "P", "a5": "P", "a6": "P", "a7": "P",
                          "a8": "P", "a9": "P", "d3": "K", "e9": "k"})
        text = move_to_chinese(board, move_from_iccs("a5b5"))
        self.assertEqual(text, "五兵平八")
        self.assertEqual(parse_chinese(board, text).iccs(), "a5b5")
        self._assert_roundtrip(board)

    def test_roundtrip_random_midgame(self):
        rng = random.Random(20240607)
        board = Board(START_FEN)
        for _ in range(40):
            legal = list(board.gen_legal_moves())
            if not legal:
                break
            board.push(rng.choice(legal))
        self._assert_roundtrip(board)

    def _assert_roundtrip(self, board):
        for move in board.gen_legal_moves():
            text = move_to_chinese(board, move)
            parsed = parse_chinese(board, text)
            self.assertIsNotNone(parsed, "无法解析 %s（%s）" % (text, move.iccs()))
            self.assertEqual(parsed, move,
                             "记谱往返不一致：%s -> %s / %s"
                             % (text, parsed.iccs(), move.iccs()))


if __name__ == "__main__":
    unittest.main()
