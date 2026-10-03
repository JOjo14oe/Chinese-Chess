# -*- coding: utf-8 -*-
"""图形界面冒烟测试（需要图形环境）。

默认跳过；设置 ``XQ_GUI_TEST=1`` 后运行：会创建窗口（随即隐藏）、绘制棋盘、
模拟鼠标点击行棋、悔棋，并验证后台搜索线程能正常落子。
"""

import gc
import os
import time
import unittest

from xiangqi.constants import BLACK, RED

ENABLED = os.environ.get("XQ_GUI_TEST") == "1"


class _FakeEvent(object):
    def __init__(self, x, y):
        self.x = x
        self.y = y


@unittest.skipUnless(ENABLED, "设置 XQ_GUI_TEST=1 运行图形界面测试")
class TestGuiSmoke(unittest.TestCase):
    def setUp(self):
        try:
            import tkinter
            probe = tkinter.Tk()
            probe.withdraw()
            probe.destroy()
        except Exception as error:  # noqa: BLE001
            self.skipTest("无图形环境：%s" % error)

    def tearDown(self):
        gc.collect()

    def _make_app(self, **kwargs):
        from xiangqi.gui import XiangqiApp
        app = XiangqiApp(**kwargs)
        app.withdraw()
        app.update()
        return app

    def test_draw_and_click_move(self):
        app = self._make_app(mode="human")
        try:
            self.assertGreater(len(app.canvas.find_all()), 30)
            # 红车 a0 -> a1
            app._on_click(_FakeEvent(*app._xy(0, 0)))
            self.assertEqual(app.selected, (0, 0))
            self.assertIn((1, 0), [m.to_square for m in app.legal_targets])
            app._on_click(_FakeEvent(*app._xy(1, 0)))
            app.update()
            self.assertEqual(len(app.move_texts), 1)
            self.assertEqual(app.move_texts[0][1], "车九进一")
            self.assertEqual(app.board.grid[1][0], "R")
            # 悔棋
            app._undo()
            app.update()
            self.assertEqual(len(app.board.history), 0)
            self.assertEqual(len(app.move_texts), 0)
            self.assertIsNone(app.board.grid[1][0])
            # 非法点击不应落子：点黑卒后点己方空格
            app._on_click(_FakeEvent(*app._xy(6, 0)))
            self.assertIsNone(app.selected)
        finally:
            app._on_close()

    def test_flip_and_new_game(self):
        app = self._make_app(mode="human")
        try:
            app._flip()
            app.update()
            self.assertTrue(app.flipped)
            app._new_game()
            app.update()
            self.assertEqual(app.board.to_fen().split()[0].count("/"), 9)
        finally:
            app._on_close()

    def test_check_indicator_renders(self):
        from xiangqi.gui import COLOR_CHECK
        app = self._make_app(mode="human",
                             fen="R3k4/R8/9/9/9/9/9/9/9/3K5 b - - 0 1")
        try:
            app._refresh()
            app.update()
            outlines = []
            for item in app.canvas.find_all():
                try:
                    outlines.append(app.canvas.itemcget(item, "outline"))
                except Exception:  # noqa: BLE001 - 文本等图元没有 outline 选项
                    continue
            self.assertIn(COLOR_CHECK, outlines, "被将军时应画出红圈提示")
        finally:
            app._on_close()

    def test_ai_thread_plays_a_move(self):
        app = self._make_app(mode="ai", human_side=BLACK, level="easy")
        try:
            self.assertEqual(app.board.side, RED)   # 电脑执红先行
            app._maybe_ai_move()
            deadline = time.monotonic() + 15
            while app._ai_busy and time.monotonic() < deadline:
                app.update()
                time.sleep(0.05)
            app.update()
            self.assertFalse(app._ai_busy, "后台搜索未在 15 秒内完成")
            self.assertEqual(len(app.board.history), 1, "电脑没有落子")
            self.assertEqual(len(app.move_texts), 1)
        finally:
            app._on_close()

    def test_hint_marks_a_move(self):
        app = self._make_app(mode="human")
        try:
            app._hint()
            deadline = time.monotonic() + 15
            while app._ai_busy and time.monotonic() < deadline:
                app.update()
                time.sleep(0.05)
            app.update()
            self.assertIsNotNone(app.hint_move)
            self.assertIn(app.hint_move, list(app.board.gen_legal_moves()))
        finally:
            app._on_close()


if __name__ == "__main__":
    unittest.main()
