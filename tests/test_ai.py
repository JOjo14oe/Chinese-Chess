# -*- coding: utf-8 -*-
"""引擎测试：一步杀、评估对称性、着法合法性与耗时。"""

import time
import unittest

try:
    from .helpers import board_of
except ImportError:  # 直接运行单文件时
    from helpers import board_of
from xiangqi.ai import MATE, Engine, evaluate
from xiangqi.board import Board
from xiangqi.constants import BLACK, RED, START_FEN
from xiangqi.notation import move_from_iccs

MATE_IN_ONE_FEN = "4k4/1R7/9/9/9/9/9/9/9/R2K5 w - - 0 1"
MATED_FEN = "R3k4/R8/9/9/9/9/9/9/9/3K5 b - - 0 1"


class TestEngine(unittest.TestCase):
    def test_finds_mate_in_one(self):
        board = Board(MATE_IN_ONE_FEN)
        engine = Engine("normal", seed=1)
        move = engine.choose_move(board, "normal")
        self.assertIsNotNone(move)
        self.assertGreaterEqual(engine.last_score, MATE - 100,
                                "未识别出一步杀，分值 %d" % engine.last_score)
        board.make_move(move)
        over, winner, _ = board.status()
        self.assertTrue(over, "引擎着法 %s 未能将死" % move.iccs())
        self.assertEqual(winner, RED)

    def test_returns_none_when_mated(self):
        board = Board(MATED_FEN)
        self.assertIsNone(Engine("easy", seed=2).choose_move(board, "easy"))

    def test_evaluate_is_symmetric(self):
        board = Board(START_FEN)
        self.assertEqual(evaluate(board, RED), 0)
        self.assertEqual(evaluate(board, RED), -evaluate(board, BLACK))

    def test_evaluate_prefers_extra_material(self):
        board = board_of({"e0": "K", "d9": "k", "a0": "R"})
        self.assertGreater(evaluate(board, RED), 500)
        self.assertLess(evaluate(board, BLACK), -500)

    def test_easy_returns_legal_move_quickly(self):
        """入门难度必须很快返回**合法**着法。

        上限取自引擎自身配置（时间上限 + 复核余量 + 慢机器的宽限），
        不在 CI 上写死 3 秒这种紧边界——2 核共享 runner 上会偶发超时。
        """
        from xiangqi.ai import LEVELS
        budget = LEVELS["easy"][1] + 3.0          # 1.5s 时限 + 额外宽限
        board = Board(START_FEN)
        start = time.monotonic()
        move = Engine("easy", seed=3).choose_move(board, "easy")
        elapsed = time.monotonic() - start
        self.assertIn(move, list(board.gen_legal_moves()))
        self.assertLess(elapsed, budget,
                        "入门难度耗时过长：%.2fs（上限 %.1fs）" % (elapsed, budget))

    def test_engine_wins_free_rook(self):
        # 双方各一车、子力均势；黑车 a4 无根，红方应能白得一车（甚至直接成杀）
        board = board_of({"e0": "K", "d9": "k", "a0": "R", "a4": "r"})
        engine = Engine("normal", seed=4)
        move = engine.choose_move(board, "normal")
        self.assertIsNotNone(move)
        self.assertIn(move, list(board.gen_legal_moves()))
        self.assertGreaterEqual(engine.last_score, 900,
                                "应看出白得一车（选择 %s，分值 %d）"
                                % (move.iccs(), engine.last_score))
        # 吃车着法（a0a4）与兑车后成杀的着法都应被视为大优
        engine2 = Engine("normal", seed=4)
        score, _, _ = engine2._root(board.copy(), 2)
        self.assertGreaterEqual(score, 900)

    def test_engine_does_not_lose_by_itself(self):
        # 引擎与自身对弈若干步，局面始终合法、不出现照面
        board = Board(START_FEN)
        engines = {"r": Engine("easy", seed=5), "b": Engine("easy", seed=6)}
        for _ in range(6):
            over, _, _ = board.status()
            if over:
                break
            move = engines[board.side].choose_move(board, "easy")
            self.assertIn(move, list(board.gen_legal_moves()))
            board.make_move(move)
            self.assertFalse(board.kings_facing(), "出现将帅照面：%s" % board.to_fen())

    def test_search_does_not_mutate_board(self):
        board = Board(START_FEN)
        before = board.to_fen()
        Engine("easy", seed=7).choose_move(board, "easy")
        self.assertEqual(board.to_fen(), before)
        self.assertEqual(len(board.history), 0)


if __name__ == "__main__":
    unittest.main()
