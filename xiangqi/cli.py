# -*- coding: utf-8 -*-
"""命令行界面（纯本地，无网络）。

用法::

    python main.py --cli                 # 人机对战，执红，普通难度
    python main.py --cli --mode human    # 双人对战
    python main.py --cli --level hard    # 难度：easy/normal/hard/master
    python main.py --cli --side b        # 执黑

着法输入支持两种记谱：

* ICCS 坐标：``h2e2``（列 a-i，行 0-9，0 为红方底线）
* 中文纵线：``炮二平五``、``马8进7``、``前车进一``
"""

import os

from .ai import Engine, LEVEL_NAMES, LEVELS
from .board import Board, IllegalMove
from .constants import (BLACK, FILE_LETTERS, RED, SIDE_NAMES, START_FEN,
                        side_of)
from .notation import move_from_iccs, move_to_chinese, parse_chinese

HELP_TEXT = """\
可用命令：
  <着法>        ICCS 坐标，如 h2e2；或中文记谱，如 炮二平五、马8进7、前车进一
  board         重新显示棋盘
  moves         显示着法记录
  fen           显示当前局面 FEN
  new           重新开局
  undo          悔棋（人机模式下退回到自己走子）
  hint          让引擎给出建议着法
  level <级别>  设置难度：easy / normal / hard / master
  mode ai|human 人机对战 / 双人对战
  side r|b      设置己方（人机模式下执红 / 执黑）
  save <文件>   保存着法记录（ICCS，本地文本文件）
  load <文件>   载入着法记录并重演
  help          显示本帮助
  quit / exit   退出
"""


def render_board(board):
    """把棋盘渲染成文本。"""
    lines = []
    lines.append("     " + "  ".join(letter + " " for letter in FILE_LETTERS))
    for rank in range(9, -1, -1):
        cells = []
        for file in range(9):
            piece = board.grid[rank][file]
            cells.append(piece + " " if piece else "\u00b7 ")
        lines.append(" %d   %s" % (rank, "  ".join(cells)))
    lines.append("     " + "  ".join(letter + " " for letter in FILE_LETTERS))
    return "\n".join(lines)


def render_history(move_texts):
    """着法记录按回合排版。"""
    if not move_texts:
        return "（尚无着法）"
    lines = []
    for index in range(0, len(move_texts), 2):
        number = index // 2 + 1
        left = move_texts[index][1]
        right = move_texts[index + 1][1] if index + 1 < len(move_texts) else ""
        lines.append("%3d. %-10s %s" % (number, left, right))
    return "\n".join(lines)


class CliGame(object):
    """命令行对局。"""

    def __init__(self, fen=START_FEN, human_side=RED, mode="ai", level="normal"):
        self.start_fen = fen
        self.board = Board(fen)
        self.human_side = human_side
        self.mode = mode
        self.level = level if level in LEVELS else "normal"
        self.engine = Engine(self.level)
        self.move_texts = []  # [(阵营, 中文记谱), ...]
        self.game_over = False
        self.over_text = ""

    # ---------------------------------------------------------------- 输出
    def show(self):
        print()
        print(render_board(self.board))
        side = SIDE_NAMES[self.board.side]
        extra = "（将军！）" if self.board.in_check(self.board.side) else ""
        counters = "无吃子半回合：%d" % self.board.halfmove_clock
        print("轮到：%s%s   %s" % (side, extra, counters))
        if self.game_over:
            print("*** %s ***" % self.over_text)
        print()

    # ---------------------------------------------------------------- 工具
    def _parse_move(self, text):
        try:
            move = move_from_iccs(text)
        except ValueError:
            move = None
        if move is not None:
            return move
        return parse_chinese(self.board, text)

    def _is_legal(self, move):
        return any(candidate == move for candidate in self.board.gen_legal_moves())

    def _check_over(self):
        over, winner, reason = self.board.status()
        if over:
            self.game_over = True
            if winner is None:
                self.over_text = "和棋（%s）" % reason
            else:
                self.over_text = "%s胜（%s）" % (SIDE_NAMES[winner], reason)
        return over

    def _play(self, move):
        text = move_to_chinese(self.board, move)
        side = self.board.side
        self.board.make_move(move)
        self.move_texts.append((side, text))
        print("  %s：%s（%s）" % (SIDE_NAMES[side], text, move.iccs()))
        return self._check_over()

    def _ai_turn(self):
        return (self.mode == "ai" and not self.game_over
                and self.board.side != self.human_side)

    def _ai_move(self):
        print("  电脑思考中（难度：%s）…" % LEVEL_NAMES.get(self.level, self.level))
        move = self.engine.choose_move(self.board, self.level)
        if move is None:
            self._check_over()
            return
        self._play(move)

    # ---------------------------------------------------------------- 主循环
    def run(self):
        print("=" * 62)
        print("中国象棋 · 命令行版（纯本地运行，不联网）")
        print("输入 help 查看命令；直接输入着法即可行棋。")
        print("=" * 62)
        while True:
            self.show()
            if self.game_over:
                print("对局结束，输入 new 重开，或 quit 退出。")
            try:
                raw = input("请着法/命令> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见。")
                return
            if not raw:
                continue
            if not self.handle(raw):
                print("再见。")
                return

    def handle(self, raw):
        """处理一条命令；返回 False 表示退出。"""
        command = raw.strip()
        lowered = command.lower()

        if lowered in ("quit", "exit", "q"):
            return False
        if lowered == "help":
            print(HELP_TEXT)
            return True
        if lowered == "board":
            return True
        if lowered == "moves":
            print(render_history(self.move_texts))
            return True
        if lowered == "fen":
            print(self.board.to_fen())
            return True
        if lowered == "new":
            self.start_fen = START_FEN
            self.board = Board(START_FEN)
            self.move_texts = []
            self.game_over = False
            self.over_text = ""
            print("已重新开局。")
            return True
        if lowered == "undo":
            if not self.board.history:
                print("没有可悔的棋。")
                return True
            self.board.pop()
            self.move_texts.pop()
            if self.mode == "ai":
                while self.board.history and self.board.side != self.human_side:
                    self.board.pop()
                    self.move_texts.pop()
            self.game_over = False
            self.over_text = ""
            print("已悔棋。")
            return True
        if lowered == "hint":
            if self.game_over:
                print("对局已结束。")
                return True
            move = self.engine.choose_move(self.board, self.level)
            if move is None:
                print("无着可走。")
            else:
                print("建议：%s（%s）" % (move_to_chinese(self.board, move), move.iccs()))
            return True
        if lowered.startswith("level"):
            parts = command.split()
            if len(parts) > 1:
                level = parts[1].lower()
                if level in LEVELS:
                    self.level = level
                    self.engine = Engine(level)
                    print("难度已设为：%s（最大深度 %d）" % (
                        LEVEL_NAMES[level], LEVELS[level][0]))
                else:
                    print("难度只能是：%s" % " / ".join(LEVELS))
            else:
                print("当前难度：%s" % LEVEL_NAMES.get(self.level, self.level))
            return True
        if lowered.startswith("mode"):
            parts = command.split()
            if len(parts) > 1 and parts[1].lower() in ("ai", "human"):
                self.mode = parts[1].lower()
                print("模式：%s" % ("人机对战" if self.mode == "ai" else "双人对战"))
            else:
                print("用法：mode ai 或 mode human")
            return True
        if lowered.startswith("side"):
            parts = command.split()
            if len(parts) > 1 and parts[1].lower() in ("r", "b", "red", "black"):
                token = parts[1].lower()
                self.human_side = RED if token in ("r", "red") else BLACK
                print("己方：%s" % SIDE_NAMES[self.human_side])
            else:
                print("用法：side r 或 side b")
            return True
        if lowered.startswith("save"):
            parts = command.split()
            if len(parts) < 2:
                print("用法：save <文件>")
                return True
            self._save(parts[1])
            return True
        if lowered.startswith("load"):
            parts = command.split()
            if len(parts) < 2:
                print("用法：load <文件>")
                return True
            self._load(parts[1])
            return True

        # 视为着法
        if self.game_over:
            print("对局已结束，输入 new 重新开局。")
            return True
        move = self._parse_move(command)
        if move is None:
            print("无法识别或不合法的着法：%s（输入 help 查看用法）" % command)
            return True
        if not self._is_legal(move):
            print("该着法不合法：%s" % command)
            return True
        self._play(move)
        while self._ai_turn():
            self._ai_move()
        return True

    # ---------------------------------------------------------------- 存读档
    def _save(self, path):
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("# 中国象棋着法记录（ICCS，本地文本文件）\n")
                handle.write("# 起始 FEN: %s\n" % self.start_fen)
                for record in self.board.history:
                    handle.write("%s\n" % record.move.iccs())
        except OSError as error:
            print("保存失败：%s" % error)
            return
        print("已保存到 %s（%d 步）" % (path, len(self.board.history)))

    def _load(self, path):
        if not os.path.exists(path):
            print("文件不存在：%s" % path)
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = [line.strip() for line in handle]
        except OSError as error:
            print("读取失败：%s" % error)
            return

        start_fen = self.start_fen
        move_lines = []
        for line in lines:
            if not line:
                continue
            if line.startswith("#"):
                marker = "# 起始 FEN:"
                if line.startswith(marker):
                    candidate = line[len(marker):].strip()
                    try:
                        Board(candidate)
                        start_fen = candidate
                    except ValueError as error:
                        print("文件中的起始 FEN 无效（%s），改用默认开局。" % error)
                continue
            move_lines.append(line)

        board = Board(start_fen)
        texts = []
        for line in move_lines:
            try:
                move = move_from_iccs(line)
            except ValueError:
                print("忽略无法解析的行：%s" % line)
                continue
            if not any(candidate == move for candidate in board.gen_legal_moves()):
                print("发现不合法着法，停止重演：%s" % line)
                break
            texts.append((board.side, move_to_chinese(board, move)))
            board.make_move(move)
        self.start_fen = start_fen
        self.board = board
        self.move_texts = texts
        self.game_over = False
        self.over_text = ""
        self._check_over()
        print("已载入 %s（%d 步）" % (path, len(texts)))


def main(argv=None):
    """命令行入口。"""
    import argparse

    parser = argparse.ArgumentParser(description="中国象棋命令行版（本地离线）")
    parser.add_argument("--fen", default=START_FEN, help="起始局面 FEN")
    parser.add_argument("--mode", choices=["ai", "human"], default="ai",
                        help="ai=人机对战，human=双人对战")
    parser.add_argument("--side", choices=["r", "b"], default="r",
                        help="人机模式下己方执子")
    parser.add_argument("--level", choices=sorted(LEVELS), default="normal",
                        help="AI 难度")
    args = parser.parse_args(argv)

    game = CliGame(fen=args.fen,
                   human_side=RED if args.side == "r" else BLACK,
                   mode=args.mode, level=args.level)
    game.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
