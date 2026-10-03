# -*- coding: utf-8 -*-
"""本地搜索引擎：Alpha-Beta 负极大值搜索 + 子力/位置评估 + 吃子延伸。

完全离线、无第三方依赖。搜索在 :class:`xiangqi.board.Board` 的副本上进行
（``push``/``pop`` 原地走子），支持迭代加深与时间上限。
"""

import random
import time

from .constants import (A, B, BLACK, C, K, N, P, R, RED, SIDE_NAMES, other,
                        side_of, type_of)

MATE = 100000  # 将死分值

# 子力价值
PIECE_VALUE = {K: 60000, R: 1000, C: 500, N: 450, B: 250, A: 250, P: 100}

# 位置价值表（以红方视角，rank 0 为红方底线；黑方按 9-rank 镜像）
PAWN_PST = [
    [0, 0, 0, 0, 0, 0, 0, 0, 0],
    [0, 0, 0, 0, 0, 0, 0, 0, 0],
    [0, 0, 0, 0, 0, 0, 0, 0, 0],
    [6, 8, 10, 14, 16, 14, 10, 8, 6],
    [12, 16, 20, 26, 30, 26, 20, 16, 12],
    [22, 28, 36, 44, 50, 44, 36, 28, 22],
    [32, 40, 50, 60, 66, 60, 50, 40, 32],
    [42, 52, 62, 74, 82, 74, 62, 52, 42],
    [52, 62, 74, 86, 94, 86, 74, 62, 52],
    [40, 50, 60, 70, 76, 70, 60, 50, 40],
]

HORSE_PST = [
    [0, -4, 0, 0, 0, 0, 0, -4, 0],
    [0, 2, 4, 4, 2, 4, 4, 2, 0],
    [4, 6, 10, 12, 10, 12, 10, 6, 4],
    [6, 10, 14, 16, 16, 16, 14, 10, 6],
    [8, 12, 16, 20, 20, 20, 16, 12, 8],
    [8, 12, 16, 20, 20, 20, 16, 12, 8],
    [6, 10, 14, 18, 18, 18, 14, 10, 6],
    [4, 8, 12, 14, 16, 14, 12, 8, 4],
    [2, 4, 8, 10, 10, 10, 8, 4, 2],
    [0, 2, 4, 6, 6, 6, 4, 2, 0],
]

CANNON_PST = [
    [0, 0, 2, 4, 6, 4, 2, 0, 0],
    [0, 2, 4, 6, 6, 6, 4, 2, 0],
    [2, 4, 8, 10, 12, 10, 8, 4, 2],
    [2, 6, 10, 14, 16, 14, 10, 6, 2],
    [4, 8, 12, 16, 18, 16, 12, 8, 4],
    [4, 8, 12, 16, 18, 16, 12, 8, 4],
    [2, 6, 10, 14, 16, 14, 10, 6, 2],
    [0, 4, 8, 10, 12, 10, 8, 4, 0],
    [0, 2, 4, 6, 6, 6, 4, 2, 0],
    [0, 0, 2, 4, 4, 4, 2, 0, 0],
]

ROOK_PST = [
    [6, 8, 10, 14, 16, 14, 10, 8, 6],
    [8, 10, 12, 16, 18, 16, 12, 10, 8],
    [8, 12, 14, 18, 20, 18, 14, 12, 8],
    [10, 14, 16, 20, 22, 20, 16, 14, 10],
    [12, 16, 18, 22, 24, 22, 18, 16, 12],
    [12, 16, 18, 22, 24, 22, 18, 16, 12],
    [10, 14, 16, 20, 22, 20, 16, 14, 10],
    [8, 12, 14, 18, 20, 18, 14, 12, 8],
    [8, 10, 12, 16, 18, 16, 12, 10, 8],
    [6, 8, 10, 14, 16, 14, 10, 8, 6],
]

PST = {P: PAWN_PST, N: HORSE_PST, C: CANNON_PST, R: ROOK_PST}

# 难度：最大深度、时间上限（秒）、随机化分值（在最优着法附近随机挑选）
LEVELS = {
    "easy": (2, 1.5, 120),
    "normal": (4, 3.0, 40),
    "hard": (6, 5.0, 0),
    "master": (8, 12.0, 0),
}

LEVEL_NAMES = {
    "easy": "入门",
    "normal": "普通",
    "hard": "较难",
    "master": "大师",
}


class SearchTimeout(Exception):
    """搜索超出时间上限（内部使用）。"""


def evaluate(board, side):
    """从 ``side`` 视角评估局面（正数表示占优）。"""
    score = 0
    grid = board.grid
    for rank in range(10):
        row = grid[rank]
        for file in range(9):
            piece = row[file]
            if piece is None:
                continue
            ptype = type_of(piece)
            value = PIECE_VALUE[ptype]
            table = PST.get(ptype)
            if table is not None:
                value += table[rank][file] if piece.isupper() else table[9 - rank][file]
            if piece.isupper():
                score += value
            else:
                score -= value
    return score if side == RED else -score


class Engine(object):
    """本地搜索引擎。

    :param level: ``easy`` / ``normal`` / ``hard`` / ``master``
    :param seed:  随机种子（低难度随机化用）
    """

    def __init__(self, level="normal", seed=None):
        self.level = level if level in LEVELS else "normal"
        self._rng = random.Random(seed)
        self.nodes = 0
        self._deadline = float("inf")
        self.last_depth = 0
        self.last_score = 0

    # ---------------------------------------------------------------- 对外接口
    def choose_move(self, board, level=None, randomness=None):
        """为 ``board`` 当前走子方选择一步棋；无合法着法返回 ``None``。"""
        level = level or self.level
        max_depth, time_limit, default_random = LEVELS.get(level, LEVELS["normal"])
        if randomness is None:
            randomness = default_random

        work = board.copy()
        self.nodes = 0
        self._deadline = time.monotonic() + time_limit
        best_move = None
        best_score = -MATE * 2
        best_scores = []

        for depth in range(1, max_depth + 1):
            try:
                score, move, scored = self._root(work, depth)
            except SearchTimeout:
                break
            if move is not None:
                best_move, best_score, best_scores = move, score, scored
                self.last_depth = depth
                self.last_score = score
            if best_score >= MATE - 100:  # 已找到杀棋
                break

        if best_scores and randomness:
            # 传入独立副本，避免上一轮迭代因超时中断留下任何残留状态
            best_move = self._pick_random(work.copy(), best_move, best_score,
                                          best_scores, randomness)
        return best_move

    def _pick_random(self, board, best_move, best_score, scored, randomness):
        """在接近最优的着法中随机挑选（低难度），并用满窗口复核其真实分值。

        Alpha-Beta 返回的非最优着法分值只是“上界”，直接据此随机可能挑到明显
        吃亏的着法，因此这里对候选着法逐一做满窗口重搜。
        """
        pool = [move for move, score in scored if score >= best_score - randomness]
        if len(pool) <= 1:
            return best_move
        depth = max(0, self.last_depth - 1)
        self.nodes = 0
        # 复核最多再多花约 1 秒，保证低难度下不会思考过久
        self._deadline = max(self._deadline, time.monotonic() + 1.0)
        exact = []
        for move in pool:
            board.push(move)
            try:
                value = -self._negamax(board, depth, -MATE * 2, MATE * 2, 1)
            except SearchTimeout:
                return best_move
            finally:
                board.pop()
            exact.append((move, value))
        top = max(value for _, value in exact)
        final = [move for move, value in exact if value >= top - randomness]
        return self._rng.choice(final) if final else best_move

    # ---------------------------------------------------------------- 搜索主体
    def _root(self, board, depth):
        moves = self._order(board, list(board.gen_legal_moves()))
        best_move = None
        best_score = -MATE * 2
        alpha, beta = -MATE * 2, MATE * 2
        scored = []
        for move in moves:
            board.push(move)
            try:
                score = -self._negamax(board, depth - 1, -beta, -alpha, 1)
            finally:
                board.pop()
            scored.append((move, score))
            if score > best_score:
                best_score, best_move = score, move
            if best_score > alpha:
                alpha = best_score
        return best_score, best_move, scored

    def _negamax(self, board, depth, alpha, beta, ply):
        self.nodes += 1
        if (self.nodes & 1023) == 0 and time.monotonic() > self._deadline:
            raise SearchTimeout()

        moves = list(board.gen_legal_moves())
        if not moves:
            return -MATE + ply  # 被将死或困毙均判负

        if depth <= 0:
            return self._quiesce(board, alpha, beta, ply, 0, moves)

        moves = self._order(board, moves)
        best = -MATE * 2
        for move in moves:
            board.push(move)
            try:
                score = -self._negamax(board, depth - 1, -beta, -alpha, ply + 1)
            finally:
                board.pop()
            if score > best:
                best = score
            if best > alpha:
                alpha = best
            if alpha >= beta:
                break
        return best

    def _quiesce(self, board, alpha, beta, ply, qdepth, moves=None):
        """吃子延伸，缓解水平线效应。返回实际搜索到的最优分值。"""
        self.nodes += 1
        if (self.nodes & 1023) == 0 and time.monotonic() > self._deadline:
            raise SearchTimeout()

        if moves is None:
            moves = list(board.gen_legal_moves())
        stand = evaluate(board, board.side)
        if qdepth >= 4:
            return stand
        if stand >= beta:
            return stand

        best = stand
        if stand > alpha:
            alpha = stand

        captures = [move for move in moves
                    if board.grid[move.tr][move.tc] is not None]
        for move in self._order(board, captures):
            board.push(move)
            try:
                score = -self._quiesce(board, -beta, -alpha, ply + 1, qdepth + 1)
            finally:
                board.pop()
            if score > best:
                best = score
            if best > alpha:
                alpha = best
            if alpha >= beta:
                break
        return best

    @staticmethod
    def _order(board, moves):
        """着法排序：吃子优先，按 MVV-LVA 启发。"""
        grid = board.grid

        def key(move):
            victim = grid[move.tr][move.tc]
            if victim is not None:
                attacker = grid[move.fr][move.fc]
                return (2, PIECE_VALUE[type_of(victim)] * 10
                        - PIECE_VALUE[type_of(attacker)])
            return (1, 0)

        return sorted(moves, key=key, reverse=True)


def best_move(board, level="normal", seed=None):
    """便捷函数：直接返回引擎推荐着法。"""
    return Engine(level, seed=seed).choose_move(board)


__all__ = ["Engine", "evaluate", "best_move", "LEVELS", "LEVEL_NAMES",
           "PIECE_VALUE", "MATE", "SearchTimeout", "SIDE_NAMES", "other",
           "side_of", "BLACK", "RED"]
