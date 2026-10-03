# -*- coding: utf-8 -*-
"""测试公共工具（不参与测试收集）。"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

EMPTY = "." * 9


def fen(rows, side="w", halfmove=0):
    """由 10 行棋盘文本构造 FEN（第 1 行是 rank 9，'.' 表示空点）。"""
    if len(rows) != 10:
        raise ValueError("需要 10 行")
    encoded = []
    for row in rows:
        if len(row) != 9:
            raise ValueError("每行需要 9 列: %r" % row)
        text = ""
        empty = 0
        for ch in row:
            if ch == ".":
                empty += 1
                continue
            if empty:
                text += str(empty)
                empty = 0
            text += ch
        if empty:
            text += str(empty)
        encoded.append(text)
    return "%s %s - - %d 1" % ("/".join(encoded), side, halfmove)


def square(text):
    """``'e2'`` -> ``(rank, file)``。"""
    from xiangqi.constants import FILE_LETTERS
    return int(text[1]), FILE_LETTERS.index(text[0])


def board_of(pieces, side="w", halfmove=0, fen_string=None):
    """由 ``{'e0': 'K', 'd9': 'k', ...}`` 构造棋盘；也可直接给 FEN。"""
    from xiangqi.board import Board
    if fen_string is not None:
        return Board(fen_string)
    grid = [["."] * 9 for _ in range(10)]
    for name, piece in pieces.items():
        rank, file = square(name)
        grid[9 - rank][file] = piece
    return Board(fen(["".join(row) for row in grid], side, halfmove))


def targets(board, name):
    """该格棋子的合法终点集合（ICCS 终点字符串）。"""
    return {move.to_iccs for move in board.legal_moves_from(*square(name))}


def moves(board, name):
    """该格棋子的合法着法列表。"""
    return board.legal_moves_from(*square(name))
