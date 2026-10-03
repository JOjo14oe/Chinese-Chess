# -*- coding: utf-8 -*-
"""命令行界面测试（重定向标准输出，避免污染测试结果）。"""

import contextlib
import io
import os
import tempfile
import unittest

try:
    from .helpers import board_of
except ImportError:  # 直接运行单文件时
    from helpers import board_of

from xiangqi.cli import CliGame, render_board, render_history
from xiangqi.constants import BLACK, RED, START_FEN
from xiangqi.notation import move_from_iccs


def run(game, command):
    """执行一条命令并吞掉输出，返回打印内容。"""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        keep_going = game.handle(command)
    return keep_going, buffer.getvalue()


class TestCliGame(unittest.TestCase):
    def test_chinese_and_iccs_input(self):
        game = CliGame(mode="human")
        run(game, "炮二平五")
        self.assertEqual(len(game.board.history), 1)
        self.assertEqual(game.move_texts[0][1], "炮二平五")
        self.assertEqual(game.board.history[0].move.iccs(), "h2e2")
        run(game, "h9g7")
        self.assertEqual(len(game.board.history), 2)
        self.assertEqual(game.move_texts[1][1], "马8进7")

    def test_illegal_and_unknown_input(self):
        game = CliGame(mode="human")
        _, out = run(game, "h2e5")          # 炮不能这样走
        self.assertIn("不合法", out)
        self.assertEqual(len(game.board.history), 0)
        _, out = run(game, "乱七八糟")
        self.assertIn("无法识别", out)
        self.assertEqual(len(game.board.history), 0)

    def test_undo(self):
        game = CliGame(mode="human")
        run(game, "h2e2")
        run(game, "h9g7")
        run(game, "undo")
        self.assertEqual(len(game.board.history), 1)
        self.assertEqual(len(game.move_texts), 1)
        self.assertEqual(game.board.side, BLACK)

    def test_undo_returns_to_human_in_ai_mode(self):
        game = CliGame(mode="ai", human_side=RED, level="easy")
        game.board.push(move_from_iccs("h2e2"))
        game.move_texts.append((RED, "炮二平五"))
        game.board.push(move_from_iccs("h9g7"))
        game.move_texts.append((BLACK, "马8进7"))
        run(game, "undo")
        self.assertEqual(game.board.side, RED)
        self.assertEqual(len(game.board.history), 0)

    def test_new_game_resets(self):
        game = CliGame(mode="human")
        run(game, "h2e2")
        run(game, "new")
        self.assertEqual(len(game.board.history), 0)
        self.assertEqual(game.board.to_fen(), START_FEN)

    def test_fen_and_moves_output(self):
        game = CliGame(mode="human")
        run(game, "h2e2")
        _, out = run(game, "fen")
        self.assertIn("rnbakabnr", out)
        _, out = run(game, "moves")
        self.assertIn("炮二平五", out)

    def test_save_and_load_roundtrip(self):
        game = CliGame(mode="human")
        run(game, "h2e2")
        run(game, "h9g7")
        handle, path = tempfile.mkstemp(suffix=".txt")
        os.close(handle)
        try:
            _, out = run(game, "save %s" % path)
            self.assertIn("2 步", out)
            fresh = CliGame(mode="human")
            _, out = run(fresh, "load %s" % path)
            self.assertIn("2 步", out)
            self.assertEqual(fresh.board.to_fen(), game.board.to_fen())
            self.assertEqual([text for _, text in fresh.move_texts],
                             [text for _, text in game.move_texts])
        finally:
            os.remove(path)

    def test_save_load_with_custom_start_fen(self):
        fen = "3k5/9/9/9/9/9/9/9/9/R3K4 w - - 0 1"
        game = CliGame(fen=fen, mode="human")
        run(game, "a0a4")
        handle, path = tempfile.mkstemp(suffix=".txt")
        os.close(handle)
        try:
            run(game, "save %s" % path)
            fresh = CliGame(mode="human")
            run(fresh, "load %s" % path)
            self.assertEqual(fresh.start_fen, fen)
            self.assertEqual(fresh.board.to_fen(), game.board.to_fen())
        finally:
            os.remove(path)

    def test_load_missing_file(self):
        game = CliGame(mode="human")
        _, out = run(game, "load 不存在的文件.txt")
        self.assertIn("文件不存在", out)

    def test_quit_returns_false(self):
        game = CliGame(mode="human")
        keep_going, _ = run(game, "quit")
        self.assertFalse(keep_going)

    def test_ai_replies_after_human_move(self):
        game = CliGame(mode="ai", human_side=RED, level="easy")
        run(game, "h2e2")
        self.assertEqual(len(game.board.history), 2, "电脑应当自动应着")
        self.assertEqual(game.board.side, RED)

    def test_render_helpers(self):
        game = CliGame(mode="human")
        board_text = render_board(game.board)
        self.assertEqual(len(board_text.splitlines()), 12)   # 10 行 + 2 行列标
        self.assertEqual(render_history([]), "（尚无着法）")
        self.assertIn("炮", render_history([(RED, "炮二平五")]))

    def test_game_over_stops_accepting_moves(self):
        game = CliGame(mode="human")
        game.board = board_of({"a9": "R", "e9": "k", "a8": "R", "d0": "K"}, side="b")
        game._check_over()
        self.assertTrue(game.game_over)
        before = len(game.board.history)
        _, out = run(game, "a9a8")
        self.assertIn("对局已结束", out)
        self.assertEqual(len(game.board.history), before)


if __name__ == "__main__":
    unittest.main()
