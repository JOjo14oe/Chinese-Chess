# -*- coding: utf-8 -*-
"""随机对局不变量测试。

用固定种子随机走棋，每一步都检查以下不变量：

* 增量维护的 Zobrist 散列与整盘重算结果一致；
* 合法着法执行后不会出现将帅照面；
* FEN 导出→载入后局面与散列完全一致；
* ``push`` / ``pop`` 是严格可逆的（局面、走子方、计数器、散列全部还原）。
"""

import random
import unittest

from xiangqi.board import Board
from xiangqi.constants import START_FEN


class TestRandomPlayInvariants(unittest.TestCase):
    def test_invariants_over_random_games(self):
        rng = random.Random(20240607)
        plies = 0
        for _ in range(3):
            board = Board(START_FEN)
            for _ in range(100):
                legal = list(board.gen_legal_moves())
                if not legal:
                    break
                move = rng.choice(legal)
                fen_before = board.to_fen()
                hash_before = board.hash
                history_before = len(board.history)

                board.push(move)
                self.assertEqual(board.hash, board._compute_hash(),
                                 "增量散列不一致：%s" % board.to_fen())
                self.assertFalse(board.kings_facing(),
                                 "合法着法后出现照面：%s" % move.iccs())
                reloaded = Board(board.to_fen())
                self.assertEqual(reloaded.to_fen(), board.to_fen())
                self.assertEqual(reloaded.hash, board.hash)

                board.pop()
                self.assertEqual(board.to_fen(), fen_before)
                self.assertEqual(board.hash, hash_before)
                self.assertEqual(len(board.history), history_before)

                board.push(move)   # 还原后继续对局
                plies += 1
                over, winner, _ = board.status()
                if over:
                    self.assertTrue(winner is None or winner in ("r", "b"))
                    break
        self.assertGreater(plies, 150, "随机对局步数过少：%d" % plies)

    def test_repetition_counter_matches_history(self):
        rng = random.Random(99)
        board = Board(START_FEN)
        for _ in range(40):
            legal = list(board.gen_legal_moves())
            if not legal:
                break
            board.make_move(rng.choice(legal))
            self.assertGreaterEqual(board.repetition_count(), 1)
            self.assertEqual(board._keys[-1], board.hash)
            self.assertEqual(len(board._keys), len(board.history) + 1)
            self.assertEqual(sum(board._key_counts.values()), len(board._keys))


if __name__ == "__main__":
    unittest.main()
