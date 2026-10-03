# -*- coding: utf-8 -*-
"""中国象棋基础常量、坐标约定与棋子工具函数。

坐标约定（全项目统一）
----------------------
``board[rank][file]``

* ``rank 0`` = 红方底线（棋盘最下方，红方视角）；``rank 9`` = 黑方底线
* ``file 0`` = a 列（红方视角最左，即红方九路）；``file 8`` = i 列（红方一路）
* 红方向 rank 增大方向前进，黑方向 rank 减小方向前进
* 河界位于 ``rank 4`` 与 ``rank 5`` 之间
* 红方九宫：``rank 0-2, file 3-5``；黑方九宫：``rank 7-9, file 3-5``
* 红兵起始于 ``rank 3``，黑卒起始于 ``rank 6``

棋子编码使用通用字母，大写为红方，小写为黑方：
``K`` 帅/将，``A`` 仕/士，``B`` 相/象，``N`` 马，``R`` 车，``C`` 炮，``P`` 兵/卒。
坐标记谱使用 ICCS 形式，例如 ``h2e2``（红方右炮平中）。
"""

from collections import namedtuple

# ------------------------------------------------------------------ 阵营、棋子
RED = "r"
BLACK = "b"
SIDES = (RED, BLACK)

K = "K"  # 帅 / 将
A = "A"  # 仕 / 士
B = "B"  # 相 / 象
N = "N"  # 马
R = "R"  # 车
C = "C"  # 炮
P = "P"  # 兵 / 卒
PIECE_TYPES = (K, A, B, N, R, C, P)

START_FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"

SIDE_NAMES = {RED: "红方", BLACK: "黑方"}

PIECE_NAMES = {
    RED: {K: "帅", A: "仕", B: "相", N: "马", R: "车", C: "炮", P: "兵"},
    BLACK: {K: "将", A: "士", B: "象", N: "马", R: "车", C: "炮", P: "卒"},
}

FILE_LETTERS = "abcdefghi"
CN_DIGITS = "一二三四五六七八九"
ARABIC_DIGITS = "123456789"

# 中文棋子名 -> [(阵营, 棋子类型), ...]；车/马/炮 两方同名，需结合行棋方消歧
CN_NAME_TO_SIDE_TYPE = {}
for _side in SIDES:
    for _ptype, _name in PIECE_NAMES[_side].items():
        CN_NAME_TO_SIDE_TYPE.setdefault(_name, []).append((_side, _ptype))
del _side, _ptype, _name

# ------------------------------------------------------------------ 方向常量
ORTHO = ((1, 0), (-1, 0), (0, 1), (0, -1))
DIAG = ((1, 1), (1, -1), (-1, 1), (-1, -1))
ELEPHANT_MOVES = ((2, 2), (2, -2), (-2, 2), (-2, -2))
HORSE_MOVES = ((2, 1), (2, -1), (-2, 1), (-2, -1),
               (1, 2), (1, -2), (-1, 2), (-1, -2))


class Move(namedtuple("Move", "fr fc tr tc")):
    """一步着法：起点 ``(fr, fc)`` -> 终点 ``(tr, tc)``（rank, file）。"""

    __slots__ = ()

    @property
    def from_square(self):
        return (self.fr, self.fc)

    @property
    def to_square(self):
        return (self.tr, self.tc)

    @property
    def from_iccs(self):
        return "%s%d" % (FILE_LETTERS[self.fc], self.fr)

    @property
    def to_iccs(self):
        return "%s%d" % (FILE_LETTERS[self.tc], self.tr)

    def iccs(self):
        """返回 ICCS 坐标记谱，如 ``h2e2``。"""
        return self.from_iccs + self.to_iccs

    def __str__(self):  # pragma: no cover - 便于调试
        return self.iccs()


# ------------------------------------------------------------------ 工具函数
def other(side):
    """返回另一方。"""
    return BLACK if side == RED else RED


def side_of(piece):
    """棋子所属阵营（大写为红方）。"""
    return RED if piece.isupper() else BLACK


def type_of(piece):
    """棋子类型字母（大写）。"""
    return piece.upper()


def make_piece(side, ptype):
    """由阵营与类型生成棋子编码。"""
    return ptype.upper() if side == RED else ptype.lower()


def in_board(rank, file):
    """坐标是否在棋盘内。"""
    return 0 <= rank < 10 and 0 <= file < 9


def in_palace(side, rank, file):
    """坐标是否在指定方的九宫内。"""
    if not (3 <= file <= 5):
        return False
    if side == RED:
        return 0 <= rank <= 2
    return 7 <= rank <= 9


def own_half(side, rank, file):
    """坐标是否在指定方的半场（相/象 不得过河）。"""
    if side == RED:
        return rank <= 4
    return rank >= 5


def crossed_river(side, rank):
    """该阵营的兵/卒位于此 rank 时是否已过河。"""
    if side == RED:
        return rank >= 5
    return rank <= 4


def horse_leg(dr, dc):
    """马从起点向 ``(dr, dc)`` 方向走时的马腿偏移。"""
    if abs(dr) == 2:
        return (dr // 2, 0)
    return (0, dc // 2)


def forward_of(side):
    """该阵营前进方向（rank 增量）。"""
    return 1 if side == RED else -1
