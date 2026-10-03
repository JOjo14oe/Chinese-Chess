# -*- coding: utf-8 -*-
"""着法生成基准测试（perft）。

基准值来自公开的中国象棋 perft 数据，用于严格校验走子规则：

    perft(1) = 44
    perft(2) = 1920
    perft(3) = 79666
    perft(4) = 3290240

depth 4 约需 40 秒，默认跳过；设置环境变量 ``XQ_PERFT4=1`` 后运行。
"""

import os
import unittest

try:
    from .helpers import board_of
except ImportError:  # 直接运行单文件时
    from helpers import board_of
from xiangqi.board import Board
from xiangqi.constants import START_FEN

REFERENCE = {1: 44, 2: 1920, 3: 79666, 4: 3290240}


class TestPerft(unittest.TestCase):
    def test_initial_position(self):
        for depth in (1, 2, 3):
            count = Board(START_FEN).perft(depth)
            self.assertEqual(count, REFERENCE[depth],
                             "perft(%d) 应为 %d，实际 %d" % (depth, REFERENCE[depth], count))

    @unittest.skipUnless(os.environ.get("XQ_PERFT4") == "1",
                         "设置 XQ_PERFT4=1 运行 perft(4)（约 40 秒）")
    def test_depth_four(self):
        self.assertEqual(Board(START_FEN).perft(4), REFERENCE[4])

    def test_known_midgame_position(self):
        # 双车杀局：红方 29 步合法着法，且存在杀着（a0a9 / b8f8）
        board = Board("4k4/1R7/9/9/9/9/9/9/9/R2K5 w - - 0 1")
        legal = list(board.gen_legal_moves())
        self.assertGreaterEqual(len(legal), 20)
        mates = 0
        for move in legal:
            board.push(move)
            if not board.has_legal_moves() and board.in_check(board.side):
                mates += 1
            board.pop()
        self.assertGreaterEqual(mates, 1, "该局面应存在杀着")

    def test_lone_king_moves(self):
        board = board_of({"e0": "K", "a9": "k"})
        self.assertEqual(len(list(board.gen_legal_moves())), 3)


if __name__ == "__main__":
    unittest.main()
