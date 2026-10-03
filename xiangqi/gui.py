# -*- coding: utf-8 -*-
"""tkinter 图形界面（纯本地，无任何网络访问）。

启动::

    python main.py            # 图形界面
    python main.py --side b   # 执黑，棋盘自动翻转

界面功能：鼠标点击行棋、合法着法提示、悔棋、提示（引擎建议）、认输、
着法记录（中文记谱）、FEN 复制、双人/人机模式与四档难度、棋盘翻转。
"""

import queue
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk

from .ai import LEVEL_NAMES, Engine
from .board import Board, IllegalMove
from .constants import BLACK, K, PIECE_NAMES, RED, SIDE_NAMES, START_FEN, in_board
from .notation import move_to_chinese

# ------------------------------------------------------------------ 尺寸与配色
CELL = 62
MARGIN = 46
BOARD_W = CELL * 8
BOARD_H = CELL * 9
CANVAS_W = BOARD_W + MARGIN * 2
CANVAS_H = BOARD_H + MARGIN * 2
PIECE_R = 25

COLOR_BG = "#E8C88C"
COLOR_LINE = "#6B4A20"
COLOR_RED = "#B32E24"
COLOR_BLACK = "#1B2430"
COLOR_PIECE_BG = "#FCF3DE"
COLOR_SELECT = "#1F6FB2"
COLOR_TARGET = "#2E9E5B"
COLOR_LAST = "#E08A1E"
COLOR_CHECK = "#D32F2F"
COLOR_HINT = "#7B1FA2"

FONT_CANDIDATES = ("Microsoft YaHei UI", "Microsoft YaHei", "SimHei", "KaiTi",
                   "SimSun", "Noto Sans CJK SC", "PingFang SC", "Arial Unicode MS")

MODE_LABELS = {
    "人人对战": ("human", None),
    "人机对战（执红）": ("ai", RED),
    "人机对战（执黑）": ("ai", BLACK),
}


def pick_font(root, size=11, weight="normal"):
    """挑选一个可用的中文字体。"""
    try:
        families = set(tkfont.families(root))
    except tk.TclError:  # pragma: no cover
        families = set()
    for name in FONT_CANDIDATES:
        if name in families:
            return (name, size, weight)
    return ("TkDefaultFont", size, weight)


class XiangqiApp(tk.Tk):
    """中国象棋图形界面主窗口。"""

    def __init__(self, fen=START_FEN, human_side=RED, level="normal", mode="ai"):
        super().__init__()
        self.title("中国象棋 · 本地离线版")
        self.configure(bg="#2F2A24")
        self.resizable(False, False)

        self.board = Board(fen)
        self.human_side = human_side
        self.mode = mode
        self.level = level
        self.flipped = (mode == "ai" and human_side == BLACK)

        self.selected = None
        self.legal_targets = []
        self.last_move = None
        self.hint_move = None
        self.move_texts = []          # [(阵营, 中文记谱), ...]
        self.game_over = False
        self.over_text = ""

        self._ai_busy = False
        self._queue = queue.Queue()
        self._closing = False
        self._after_ids = set()

        self.font_small = pick_font(self, 11)
        self.font_piece = pick_font(self, 20, "bold")
        self.font_status = pick_font(self, 12, "bold")
        self.font_river = pick_font(self, 17, "bold")

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh()
        self._later(200, self._maybe_ai_move)

    # ---------------------------------------------------------------- 定时回调
    def _later(self, delay, callback):
        """登记一个延迟回调，窗口关闭时统一取消（避免销毁后触发 Tcl 报错）。"""
        if self._closing:
            return None

        def run():
            self._after_ids.discard(handle)
            if not self._closing:
                callback()

        handle = self.after(delay, run)
        self._after_ids.add(handle)
        return handle

    # ---------------------------------------------------------------- 界面构建
    def _build_ui(self):
        top = tk.Frame(self, bg="#2F2A24")
        top.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(8, 4))

        def button(text, command, width=6):
            return tk.Button(top, text=text, command=command, width=width,
                             font=self.font_small, bg="#EFE6D6",
                             activebackground="#D8C9B0", relief=tk.RAISED)

        button("新局", self._new_game).pack(side=tk.LEFT, padx=2)
        button("悔棋", self._undo).pack(side=tk.LEFT, padx=2)
        button("提示", self._hint).pack(side=tk.LEFT, padx=2)
        button("认输", self._resign).pack(side=tk.LEFT, padx=2)
        button("翻转", self._flip).pack(side=tk.LEFT, padx=2)

        tk.Label(top, text="模式", font=self.font_small, bg="#2F2A24",
                 fg="#F0E6D2").pack(side=tk.LEFT, padx=(14, 2))
        self.mode_var = tk.StringVar(
            value="人人对战" if self.mode == "human"
            else ("人机对战（执红）" if self.human_side == RED else "人机对战（执黑）"))
        mode_box = ttk.Combobox(top, textvariable=self.mode_var, width=15,
                                state="readonly", values=list(MODE_LABELS))
        mode_box.pack(side=tk.LEFT)
        mode_box.bind("<<ComboboxSelected>>", self._on_mode_change)

        tk.Label(top, text="难度", font=self.font_small, bg="#2F2A24",
                 fg="#F0E6D2").pack(side=tk.LEFT, padx=(10, 2))
        self.level_var = tk.StringVar(value=LEVEL_NAMES.get(self.level, "普通"))
        level_box = ttk.Combobox(top, textvariable=self.level_var, width=6,
                                 state="readonly", values=list(LEVEL_NAMES.values()))
        level_box.pack(side=tk.LEFT)
        level_box.bind("<<ComboboxSelected>>", self._on_level_change)

        body = tk.Frame(self, bg="#2F2A24")
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))

        self.canvas = tk.Canvas(body, width=CANVAS_W, height=CANVAS_H,
                               bg=COLOR_BG, highlightthickness=0)
        self.canvas.pack(side=tk.LEFT)
        self.canvas.bind("<Button-1>", self._on_click)

        panel = tk.Frame(body, bg="#2F2A24", width=250)
        panel.pack(side=tk.LEFT, fill=tk.Y, padx=(10, 0))
        panel.pack_propagate(False)

        self.status_var = tk.StringVar(value="轮到红方")
        self.status_label = tk.Label(panel, textvariable=self.status_var,
                                     font=self.font_status, bg="#2F2A24",
                                     fg="#FFD54F", wraplength=240,
                                     justify=tk.LEFT, anchor="w")
        self.status_label.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))

        self.result_var = tk.StringVar(value="")
        tk.Label(panel, textvariable=self.result_var, font=self.font_small,
                 bg="#2F2A24", fg="#8BC34A", wraplength=240, justify=tk.LEFT,
                 anchor="w").pack(side=tk.TOP, fill=tk.X, pady=(0, 6))

        tk.Label(panel, text="着法记录", font=self.font_small, bg="#2F2A24",
                 fg="#F0E6D2", anchor="w").pack(side=tk.TOP, fill=tk.X)
        list_frame = tk.Frame(panel, bg="#2F2A24")
        list_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(2, 6))
        scrollbar = tk.Scrollbar(list_frame, orient=tk.VERTICAL)
        self.move_list = tk.Listbox(list_frame, font=self.font_small, width=34,
                                    height=18, yscrollcommand=scrollbar.set,
                                    bg="#FBF6EA", selectbackground="#BBDEFB")
        scrollbar.config(command=self.move_list.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.move_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        fen_frame = tk.Frame(panel, bg="#2F2A24")
        fen_frame.pack(side=tk.BOTTOM, fill=tk.X)
        tk.Button(fen_frame, text="复制当前 FEN", font=self.font_small,
                  command=self._copy_fen).pack(side=tk.BOTTOM, fill=tk.X, pady=2)
        self.fen_var = tk.StringVar(value="")
        tk.Entry(fen_frame, textvariable=self.fen_var, font=self.font_small,
                 state="readonly").pack(side=tk.BOTTOM, fill=tk.X)

    # ---------------------------------------------------------------- 坐标换算
    def _xy(self, rank, file):
        if self.flipped:
            return (MARGIN + (8 - file) * CELL, MARGIN + rank * CELL)
        return (MARGIN + file * CELL, MARGIN + (9 - rank) * CELL)

    def _square_from_xy(self, x, y):
        if self.flipped:
            file = 8 - round((x - MARGIN) / CELL)
            rank = round((y - MARGIN) / CELL)
        else:
            file = round((x - MARGIN) / CELL)
            rank = 9 - round((y - MARGIN) / CELL)
        if not in_board(rank, file):
            return None
        px, py = self._xy(rank, file)
        if abs(px - x) > CELL * 0.55 or abs(py - y) > CELL * 0.55:
            return None
        return rank, file

    # ---------------------------------------------------------------- 绘制
    def _draw(self):
        canvas = self.canvas
        canvas.delete("all")
        canvas.create_rectangle(0, 0, CANVAS_W, CANVAS_H, fill=COLOR_BG,
                                outline=COLOR_BG)
        canvas.create_rectangle(MARGIN - 16, MARGIN - 16,
                                MARGIN + BOARD_W + 16, MARGIN + BOARD_H + 16,
                                outline=COLOR_LINE, width=3)

        for rank in range(10):
            _, y = self._xy(rank, 0)
            canvas.create_line(MARGIN, y, MARGIN + BOARD_W, y, fill=COLOR_LINE)
        for file in range(9):
            x = MARGIN + file * CELL
            if self.flipped:
                x = MARGIN + (8 - file) * CELL
            if file in (0, 8):
                canvas.create_line(x, MARGIN, x, MARGIN + BOARD_H, fill=COLOR_LINE)
            else:
                # 河界处断开（rank 5 与 rank 4 之间）
                canvas.create_line(x, MARGIN, x, MARGIN + 4 * CELL, fill=COLOR_LINE)
                canvas.create_line(x, MARGIN + 5 * CELL, x, MARGIN + BOARD_H,
                                   fill=COLOR_LINE)

        # 九宫斜线
        for base in (0, 7):
            for f1, f2 in ((3, 5), (5, 3)):
                x1, y1 = self._xy(base, f1)
                x2, y2 = self._xy(base + 2, f2)
                canvas.create_line(x1, y1, x2, y2, fill=COLOR_LINE)

        # 兵/炮位小十字
        for rank, file in [(3, 0), (3, 2), (3, 4), (3, 6), (3, 8),
                           (6, 0), (6, 2), (6, 4), (6, 6), (6, 8),
                           (2, 1), (2, 7), (7, 1), (7, 7)]:
            self._draw_mark(rank, file)

        # 楚河汉界
        mid_x = MARGIN + BOARD_W / 2
        mid_y = MARGIN + 4.5 * CELL
        canvas.create_text(mid_x - BOARD_W / 4, mid_y, text="楚 河",
                           font=self.font_river, fill=COLOR_LINE)
        canvas.create_text(mid_x + BOARD_W / 4, mid_y, text="汉 界",
                           font=self.font_river, fill=COLOR_LINE)

        self._draw_marks()
        self._draw_pieces()

    def _draw_mark(self, rank, file):
        """棋子起始位的装饰小十字。"""
        x, y = self._xy(rank, file)
        gap = 5
        span = 9
        for dx in (-1, 1):
            for dy in (-1, 1):
                sx = x + dx * gap
                sy = y + dy * gap
                if not (MARGIN <= sx <= MARGIN + BOARD_W
                        and MARGIN <= sy <= MARGIN + BOARD_H):
                    continue
                self.canvas.create_line(sx, sy, sx + dx * span, sy,
                                        fill=COLOR_LINE)
                self.canvas.create_line(sx, sy, sx, sy + dy * span,
                                        fill=COLOR_LINE)

    def _draw_marks(self):
        """上一步、选中框、可走点、将军提示、建议着法。"""
        canvas = self.canvas
        if self.last_move is not None:
            for rank, file in (self.last_move.from_square, self.last_move.to_square):
                x, y = self._xy(rank, file)
                canvas.create_rectangle(x - 26, y - 26, x + 26, y + 26,
                                        outline=COLOR_LAST, width=2)
        if self.selected is not None:
            x, y = self._xy(*self.selected)
            canvas.create_oval(x - 28, y - 28, x + 28, y + 28,
                               outline=COLOR_SELECT, width=3)
        for move in self.legal_targets:
            rank, file = move.to_square
            x, y = self._xy(rank, file)
            if self.board.grid[rank][file] is None:
                canvas.create_oval(x - 7, y - 7, x + 7, y + 7,
                                   fill=COLOR_TARGET, outline="")
            else:
                canvas.create_oval(x - 27, y - 27, x + 27, y + 27,
                                   outline=COLOR_TARGET, width=3)
        if self.hint_move is not None:
            x1, y1 = self._xy(*self.hint_move.from_square)
            x2, y2 = self._xy(*self.hint_move.to_square)
            canvas.create_line(x1, y1, x2, y2, fill=COLOR_HINT, width=3,
                               dash=(6, 4))
        if not self.game_over and self.board.in_check(self.board.side):
            position = self.board.king_pos(self.board.side)
            if position:
                x, y = self._xy(*position)
                canvas.create_oval(x - 28, y - 28, x + 28, y + 28,
                                   outline=COLOR_CHECK, width=4)

    def _draw_pieces(self):
        canvas = self.canvas
        for rank in range(10):
            for file in range(9):
                piece = self.board.grid[rank][file]
                if piece is None:
                    continue
                x, y = self._xy(rank, file)
                side = RED if piece.isupper() else BLACK
                color = COLOR_RED if side == RED else COLOR_BLACK
                canvas.create_oval(x - PIECE_R, y - PIECE_R,
                                   x + PIECE_R, y + PIECE_R,
                                   fill=COLOR_PIECE_BG, outline=color, width=2)
                canvas.create_oval(x - PIECE_R + 4, y - PIECE_R + 4,
                                   x + PIECE_R - 4, y + PIECE_R - 4,
                                   outline=color, width=1)
                canvas.create_text(x, y, text=PIECE_NAMES[side][piece.upper()],
                                   font=self.font_piece, fill=color)

    # ---------------------------------------------------------------- 状态刷新
    def _refresh(self):
        self._draw()
        self.fen_var.set(self.board.to_fen())
        self._refresh_move_list()
        self._refresh_status()

    def _refresh_move_list(self):
        self.move_list.delete(0, tk.END)
        for index in range(0, len(self.move_texts), 2):
            number = index // 2 + 1
            left = self.move_texts[index][1]
            right = self.move_texts[index + 1][1] if index + 1 < len(self.move_texts) else ""
            self.move_list.insert(tk.END, "%3d. %-8s %s" % (number, left, right))
        if self.move_texts:
            self.move_list.see(tk.END)

    def _refresh_status(self):
        self.result_var.set(self.over_text)
        if self.game_over:
            self.status_var.set("对局结束")
            return
        side = self.board.side
        if self.mode == "ai" and side != self.human_side:
            self.status_var.set("电脑思考中…（%s）" % LEVEL_NAMES.get(self.level, self.level))
            return
        text = "轮到%s" % SIDE_NAMES[side]
        if self.board.in_check(side):
            text += "（被将军！）"
        if self.mode == "ai":
            text += "｜你执%s" % SIDE_NAMES[self.human_side]
        self.status_var.set(text)

    # ---------------------------------------------------------------- 交互
    def _human_turn(self):
        if self.game_over or self._ai_busy:
            return False
        if self.mode == "ai" and self.board.side != self.human_side:
            return False
        return True

    def _on_click(self, event):
        if not self._human_turn():
            return
        square = self._square_from_xy(event.x, event.y)
        if square is None:
            return
        rank, file = square
        piece = self.board.grid[rank][file]
        if self.selected is not None:
            for move in self.legal_targets:
                if move.to_square == (rank, file):
                    self._play(move)
                    return
        if piece is not None and ((piece.isupper() and self.board.side == RED)
                                  or (piece.islower() and self.board.side == BLACK)):
            self.selected = (rank, file)
            self.legal_targets = self.board.legal_moves_from(rank, file)
        else:
            self.selected = None
            self.legal_targets = []
        self._draw()

    def _play(self, move):
        board = self.board
        try:
            text = move_to_chinese(board, move)
        except ValueError:  # pragma: no cover - 保险
            text = move.iccs()
        side = board.side
        try:
            board.make_move(move)
        except IllegalMove as error:
            messagebox.showerror("非法着法", str(error), parent=self)
            return
        self.move_texts.append((side, text))
        self.last_move = move
        self.selected = None
        self.legal_targets = []
        self.hint_move = None

        over, winner, reason = board.status()
        if over:
            self.game_over = True
            if winner is None:
                self.over_text = "和棋（%s）" % reason
            else:
                self.over_text = "%s胜（%s）" % (SIDE_NAMES[winner], reason)
        self._refresh()
        if over:
            self._later(80, self._show_result)
            return
        self._later(120, self._maybe_ai_move)

    def _show_result(self):
        messagebox.showinfo("对局结束", self.over_text, parent=self)

    def _maybe_ai_move(self):
        if (self._closing or self.game_over or self.mode != "ai"
                or self.board.side == self.human_side or self._ai_busy):
            return
        self._refresh_status()
        self._spawn_search("ai", self.level)

    def _spawn_search(self, tag, level):
        if self._ai_busy:
            return
        self._ai_busy = True
        snapshot = self.board.copy()
        seed = time.time_ns() & 0xFFFFFFFF
        output = self._queue  # 只把队列传进线程，避免线程持有窗口对象

        def work():
            engine = Engine(level, seed=seed)
            output.put((tag, engine.choose_move(snapshot, level)))

        threading.Thread(target=work, daemon=True).start()
        self._later(60, self._poll_search)

    def _poll_search(self):
        if self._closing:
            return
        try:
            tag, move = self._queue.get_nowait()
        except queue.Empty:
            self._later(60, self._poll_search)
            return
        self._ai_busy = False
        if tag == "ai":
            if move is not None and not self.game_over:
                self._play(move)
            else:
                self._refresh()
        else:  # 提示
            self._refresh()
            if move is not None and not self.game_over:
                self.hint_move = move
                try:
                    text = move_to_chinese(self.board, move)
                except ValueError:  # pragma: no cover
                    text = move.iccs()
                self.status_var.set("建议着法：%s（%s）" % (text, move.iccs()))
                self._draw()

    # ---------------------------------------------------------------- 按钮
    def _new_game(self):
        if self._ai_busy:
            return
        self.board = Board(START_FEN)
        self.move_texts = []
        self.selected = None
        self.legal_targets = []
        self.last_move = None
        self.hint_move = None
        self.game_over = False
        self.over_text = ""
        self.flipped = (self.mode == "ai" and self.human_side == BLACK)
        self._refresh()
        self._later(120, self._maybe_ai_move)

    def _undo(self):
        if self._ai_busy or not self.board.history:
            return
        self.board.pop()
        self.move_texts.pop()
        if self.mode == "ai":
            while self.board.history and self.board.side != self.human_side:
                self.board.pop()
                self.move_texts.pop()
        self.game_over = False
        self.over_text = ""
        self.selected = None
        self.legal_targets = []
        self.hint_move = None
        self.last_move = (self.board.history[-1].move if self.board.history else None)
        self._refresh()

    def _hint(self):
        if self._ai_busy or self.game_over:
            return
        level = self.level if self.level in ("normal", "hard", "master") else "normal"
        self.status_var.set("正在计算建议着法…")
        self._spawn_search("hint", level)

    def _resign(self):
        if self.game_over:
            return
        loser = self.human_side if self.mode == "ai" else self.board.side
        winner = BLACK if loser == RED else RED
        self.game_over = True
        self.over_text = "%s认输，%s胜" % (SIDE_NAMES[loser], SIDE_NAMES[winner])
        self._refresh()

    def _flip(self):
        self.flipped = not self.flipped
        self._draw()

    def _copy_fen(self):
        self.clipboard_clear()
        self.clipboard_append(self.board.to_fen())
        self.status_var.set("FEN 已复制到剪贴板")

    def _on_mode_change(self, _event=None):
        mode, side = MODE_LABELS[self.mode_var.get()]
        self.mode = mode
        if side is not None:
            self.human_side = side
        self.flipped = (self.mode == "ai" and self.human_side == BLACK)
        self._refresh()
        self._later(120, self._maybe_ai_move)

    def _on_level_change(self, _event=None):
        for key, label in LEVEL_NAMES.items():
            if label == self.level_var.get():
                self.level = key
                break
        self._refresh_status()

    def _on_close(self):
        self._closing = True
        for handle in list(self._after_ids):
            try:
                self.after_cancel(handle)
            except tk.TclError:  # pragma: no cover - 窗口已销毁
                pass
        self._after_ids.clear()
        self.destroy()


def main(argv=None):
    """图形界面入口。"""
    import argparse

    parser = argparse.ArgumentParser(description="中国象棋图形界面（本地离线）")
    parser.add_argument("--fen", default=START_FEN, help="起始局面 FEN")
    parser.add_argument("--side", choices=["r", "b"], default="r",
                        help="人机模式下己方执子")
    parser.add_argument("--level", choices=sorted(LEVEL_NAMES), default="normal",
                        help="AI 难度")
    parser.add_argument("--mode", choices=["ai", "human"], default="ai",
                        help="ai=人机对战，human=双人对战")
    args = parser.parse_args(argv)

    app = XiangqiApp(fen=args.fen,
                     human_side=RED if args.side == "r" else BLACK,
                     level=args.level, mode=args.mode)
    app.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
