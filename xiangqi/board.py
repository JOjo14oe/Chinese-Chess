# -*- coding: utf-8 -*-
"""棋盘状态、着法生成、合法性判定与胜负判定——中国象棋（象棋）完整行棋规则。

实现的规则
----------
* 帅/将：九宫内一步直行；双方将帅不得在同一纵线上照面（“飞将”照面视为被将军）
* 仕/士：九宫内一步斜行
* 相/象：斜走两格（象眼不得有子），不得过河
* 马：走“日”字，马腿（先直行方向的一格）不得有子
* 车：直线任意距离，不得越子
* 炮：不吃子时同车；吃子必须恰好隔一个棋子（炮架）
* 兵/卒：向前一步；过河后可以左右一步；永远不能后退
* 不得走出使己方将帅被将军（含照面）的着法
* 轮到走棋一方无着可走即负：被将军为“将死”，未被将军为“困毙”
* 长将判负、三次重复局面判和、连续 60 回合无吃子判和

坐标约定见 :mod:`xiangqi.constants`。
"""

import random

from .constants import (A, B, BLACK, C, DIAG, ELEPHANT_MOVES, HORSE_MOVES, K,
                        N, ORTHO, P, PIECE_TYPES, R, RED, SIDE_NAMES, SIDES,
                        START_FEN, Move, crossed_river, forward_of,
                        horse_leg, in_board, in_palace, make_piece, other,
                        own_half, side_of, type_of)

# ------------------------------------------------------------------ Zobrist 散列
def _build_zobrist():
    rng = random.Random(0x5EED2024)
    table = {}
    for ptype in PIECE_TYPES:
        for side in SIDES:
            piece = make_piece(side, ptype)
            for rank in range(10):
                for file in range(9):
                    table[(piece, rank, file)] = rng.getrandbits(64)
    return table, rng.getrandbits(64)


ZOBRIST, ZOBRIST_SIDE = _build_zobrist()

NATURAL_LIMIT_PLIES = 120  # 60 回合（120 个半回合）无吃子判和


class IllegalMove(ValueError):
    """非法的着法。"""


class _Record(object):
    """一步棋的完整回滚信息。"""

    __slots__ = ("move", "piece", "captured", "halfmove_clock",
                 "fullmove_number", "side", "prev_hash", "gave_check")

    def __init__(self, move, piece, captured, halfmove_clock,
                 fullmove_number, side, prev_hash, gave_check=None):
        self.move = move
        self.piece = piece
        self.captured = captured
        self.halfmove_clock = halfmove_clock
        self.fullmove_number = fullmove_number
        self.side = side
        self.prev_hash = prev_hash
        self.gave_check = gave_check

    def __repr__(self):  # pragma: no cover - 调试用
        return "<Record %s %s%s>" % (
            self.move, self.piece, "" if self.captured is None else "-" + self.captured)


class Board(object):
    """中国象棋棋盘。

    典型用法::

        board = Board()
        board.make_move(move_from_iccs('h2e2'))
        for move in board.gen_legal_moves():
            ...
    """

    def __init__(self, fen=START_FEN):
        self.reset(fen)

    # -------------------------------------------------------------- 初始化
    def reset(self, fen=START_FEN):
        """按 FEN 重置棋盘（默认开局）。"""
        self.set_fen(fen)

    def set_fen(self, fen):
        """载入 FEN 局面。"""
        parts = str(fen).strip().split()
        if not parts:
            raise ValueError("FEN 不能为空")
        rows = parts[0].split("/")
        if len(rows) != 10:
            raise ValueError("FEN 应有 10 行，实际 %d 行" % len(rows))
        grid = [[None] * 9 for _ in range(10)]
        for index, row in enumerate(rows):  # FEN 第 1 行是黑方底线（rank 9）
            rank = 9 - index
            file = 0
            for ch in row:
                if ch.isdigit():
                    file += int(ch)
                else:
                    if type_of(ch) not in PIECE_TYPES:
                        raise ValueError("FEN 中出现未知棋子 %r" % ch)
                    if not in_board(rank, file):
                        raise ValueError("FEN 第 %d 行棋子越界" % (index + 1))
                    grid[rank][file] = ch
                    file += 1
            if file != 9:
                raise ValueError("FEN 第 %d 行宽度不是 9 格" % (index + 1))

        side = RED
        if len(parts) > 1:
            token = parts[1].lower()
            if token in ("w", "r"):
                side = RED
            elif token == "b":
                side = BLACK
            else:
                raise ValueError("FEN 走子方无效: %r" % parts[1])
        try:
            halfmove_clock = int(parts[4]) if len(parts) > 4 else 0
            fullmove_number = int(parts[5]) if len(parts) > 5 else 1
        except ValueError:
            raise ValueError("FEN 回合计数无效: %r" % " ".join(parts[4:6]))
        if halfmove_clock < 0 or fullmove_number < 1:
            raise ValueError("FEN 回合计数越界: %s %s"
                             % (halfmove_clock, fullmove_number))
        # 每方必须恰有一个将/帅；否则 king_pos/in_check/胜负判定都会失真
        red_kings = sum(row.count(K) for row in grid)
        black_kings = sum(row.count(K.lower()) for row in grid)
        if red_kings != 1 or black_kings != 1:
            raise ValueError("FEN 必须各有一个将/帅（红 %d 个，黑 %d 个）"
                             % (red_kings, black_kings))

        # 全部校验通过后才提交，保证载入失败时原局面（含散列）保持不变
        self.grid = grid
        self.side = side
        self.halfmove_clock = halfmove_clock
        self.fullmove_number = fullmove_number
        self.history = []
        self._keys = []
        self._key_counts = {}
        self.hash = self._compute_hash()
        self._push_key(self.hash)

    def copy(self):
        """轻量副本（不含历史），供搜索/多线程使用。"""
        clone = Board.__new__(Board)
        clone.grid = [row[:] for row in self.grid]
        clone.side = self.side
        clone.halfmove_clock = self.halfmove_clock
        clone.fullmove_number = self.fullmove_number
        clone.hash = self.hash
        clone.history = []
        clone._keys = [self.hash]
        clone._key_counts = {self.hash: 1}
        return clone

    # -------------------------------------------------------------- 序列化
    def to_fen(self):
        """导出 FEN。"""
        rows = []
        for rank in range(9, -1, -1):
            row = ""
            empty = 0
            for file in range(9):
                piece = self.grid[rank][file]
                if piece is None:
                    empty += 1
                    continue
                if empty:
                    row += str(empty)
                    empty = 0
                row += piece
            if empty:
                row += str(empty)
            rows.append(row)
        side = "w" if self.side == RED else "b"
        return "%s %s - - %d %d" % ("/".join(rows), side,
                                    self.halfmove_clock, self.fullmove_number)

    # -------------------------------------------------------------- 散列/重复
    def _compute_hash(self):
        value = 0
        for rank in range(10):
            row = self.grid[rank]
            for file in range(9):
                piece = row[file]
                if piece is not None:
                    value ^= ZOBRIST[(piece, rank, file)]
        if self.side == BLACK:
            value ^= ZOBRIST_SIDE
        return value

    def _push_key(self, key):
        self._keys.append(key)
        self._key_counts[key] = self._key_counts.get(key, 0) + 1

    def _pop_key(self, key):
        self._keys.pop()
        count = self._key_counts[key] - 1
        if count <= 0:
            del self._key_counts[key]
        else:
            self._key_counts[key] = count

    def repetition_count(self):
        """当前局面（含走子方）在本局中出现过的次数。"""
        return self._key_counts.get(self.hash, 0)

    # -------------------------------------------------------------- 查询
    def piece_at(self, rank, file):
        if not in_board(rank, file):
            return None
        return self.grid[rank][file]

    def king_pos(self, side):
        """返回该方将/帅位置 ``(rank, file)``，不存在时返回 ``None``。"""
        target = make_piece(side, K)
        for rank in range(10):
            row = self.grid[rank]
            for file in range(9):
                if row[file] == target:
                    return (rank, file)
        return None

    def pieces_of(self, side):
        """返回 ``[(rank, file, piece), ...]``。"""
        result = []
        for rank in range(10):
            row = self.grid[rank]
            for file in range(9):
                piece = row[file]
                if piece is not None and side_of(piece) == side:
                    result.append((rank, file, piece))
        return result

    # -------------------------------------------------------------- 走子
    def push(self, move):
        """执行一步棋（不做合法性校验），返回可回滚的记录。"""
        fr, fc, tr, tc = move
        if not in_board(fr, fc) or not in_board(tr, tc):
            raise IllegalMove("着法越界: %s" % (move,))
        grid = self.grid
        piece = grid[fr][fc]
        if piece is None:
            raise IllegalMove("起点 %s 没有棋子" % move.from_iccs)
        captured = grid[tr][tc]

        value = self.hash
        value ^= ZOBRIST[(piece, fr, fc)]
        if captured is not None:
            value ^= ZOBRIST[(captured, tr, tc)]
        grid[fr][fc] = None
        grid[tr][tc] = piece
        value ^= ZOBRIST[(piece, tr, tc)]
        value ^= ZOBRIST_SIDE

        record = _Record(move, piece, captured, self.halfmove_clock,
                         self.fullmove_number, self.side, self.hash)
        self.history.append(record)
        if captured is not None:
            self.halfmove_clock = 0
        else:
            self.halfmove_clock += 1
        if self.side == BLACK:
            self.fullmove_number += 1
        self.side = other(self.side)
        self.hash = value
        self._push_key(value)
        return record

    def pop(self):
        """撤销最后一步棋，返回被撤销的记录。"""
        if not self.history:
            raise IllegalMove("没有可撤销的着法")
        record = self.history.pop()
        self._pop_key(self.hash)
        fr, fc, tr, tc = record.move
        self.grid[fr][fc] = record.piece
        self.grid[tr][tc] = record.captured
        self.side = record.side
        self.halfmove_clock = record.halfmove_clock
        self.fullmove_number = record.fullmove_number
        self.hash = record.prev_hash
        return record

    def make_move(self, move):
        """走一步合法棋；非法时抛 :class:`IllegalMove`。"""
        if not any(candidate == move for candidate in self.gen_legal_moves()):
            raise IllegalMove("非法着法: %s" % (move,))
        record = self.push(move)
        record.gave_check = self.in_check(self.side)  # 走完后轮到对方
        return record

    # -------------------------------------------------------------- 着法生成
    def gen_pseudo_moves(self, side=None, square=None):
        """生成伪合法着法（未剔除送将/照面）。``square`` 限定起点。"""
        if side is None:
            side = self.side
        grid = self.grid
        moves = []
        for rank in range(10):
            row = grid[rank]
            for file in range(9):
                piece = row[file]
                if piece is None or side_of(piece) != side:
                    continue
                if square is not None and (rank, file) != square:
                    continue
                ptype = type_of(piece)

                if ptype == K:
                    for dr, df in ORTHO:
                        nr, nf = rank + dr, file + df
                        if in_palace(side, nr, nf) and not self._own(nr, nf, side):
                            moves.append(Move(rank, file, nr, nf))

                elif ptype == A:
                    for dr, df in DIAG:
                        nr, nf = rank + dr, file + df
                        if in_palace(side, nr, nf) and not self._own(nr, nf, side):
                            moves.append(Move(rank, file, nr, nf))

                elif ptype == B:
                    for dr, df in ELEPHANT_MOVES:
                        nr, nf = rank + dr, file + df
                        if not in_board(nr, nf) or not own_half(side, nr, nf):
                            continue
                        if grid[rank + dr // 2][file + df // 2] is not None:
                            continue  # 塞象眼
                        if not self._own(nr, nf, side):
                            moves.append(Move(rank, file, nr, nf))

                elif ptype == N:
                    for dr, df in HORSE_MOVES:
                        nr, nf = rank + dr, file + df
                        if not in_board(nr, nf):
                            continue
                        lr, lf = horse_leg(dr, df)
                        if grid[rank + lr][file + lf] is not None:
                            continue  # 蹩马腿
                        if not self._own(nr, nf, side):
                            moves.append(Move(rank, file, nr, nf))

                elif ptype == R:
                    for dr, df in ORTHO:
                        nr, nf = rank + dr, file + df
                        while in_board(nr, nf):
                            occupant = grid[nr][nf]
                            if occupant is None:
                                moves.append(Move(rank, file, nr, nf))
                            else:
                                if side_of(occupant) != side:
                                    moves.append(Move(rank, file, nr, nf))
                                break
                            nr += dr
                            nf += df

                elif ptype == C:
                    for dr, df in ORTHO:
                        nr, nf = rank + dr, file + df
                        while in_board(nr, nf) and grid[nr][nf] is None:
                            moves.append(Move(rank, file, nr, nf))
                            nr += dr
                            nf += df
                        if not in_board(nr, nf):
                            continue
                        # 此处 grid[nr][nf] 为炮架，越过它寻找第一个棋子
                        nr += dr
                        nf += df
                        while in_board(nr, nf):
                            occupant = grid[nr][nf]
                            if occupant is not None:
                                if side_of(occupant) != side:
                                    moves.append(Move(rank, file, nr, nf))
                                break
                            nr += dr
                            nf += df

                elif ptype == P:
                    forward = forward_of(side)
                    nr, nf = rank + forward, file
                    if in_board(nr, nf) and not self._own(nr, nf, side):
                        moves.append(Move(rank, file, nr, nf))
                    if crossed_river(side, rank):
                        for df in (-1, 1):
                            nf2 = file + df
                            if in_board(rank, nf2) and not self._own(rank, nf2, side):
                                moves.append(Move(rank, file, rank, nf2))
        return moves

    def _own(self, rank, file, side):
        piece = self.grid[rank][file]
        return piece is not None and side_of(piece) == side

    def gen_legal_moves(self, side=None):
        """生成全部合法着法（已剔除导致己方被将军或照面的着法）。"""
        if side is None:
            side = self.side
        for move in self.gen_pseudo_moves(side):
            self.push(move)
            legal = not self.in_check(side)
            self.pop()
            if legal:
                yield move

    def legal_moves_from(self, rank, file):
        """返回起点 ``(rank, file)`` 上棋子的全部合法着法。"""
        side = self.side
        piece = self.piece_at(rank, file)
        if piece is None or side_of(piece) != side:
            return []
        result = []
        for move in self.gen_pseudo_moves(side, (rank, file)):
            self.push(move)
            legal = not self.in_check(side)
            self.pop()
            if legal:
                result.append(move)
        return result

    def has_legal_moves(self, side=None):
        """该方是否还有合法着法。"""
        for _ in self.gen_legal_moves(side):
            return True
        return False

    # -------------------------------------------------------------- 将军判定
    def kings_facing(self):
        """双方将帅是否在同一纵线上照面（中间无子）。"""
        red_king = self.king_pos(RED)
        black_king = self.king_pos(BLACK)
        if red_king is None or black_king is None:
            return False
        if red_king[1] != black_king[1]:
            return False
        file = red_king[1]
        low, high = sorted((red_king[0], black_king[0]))
        for rank in range(low + 1, high):
            if self.grid[rank][file] is not None:
                return False
        return True

    def is_attacked(self, rank, file, by_side):
        """``by_side`` 方是否有棋子攻击 ``(rank, file)`` 格。"""
        grid = self.grid

        # 兵/卒：向前攻击；过河后可横向攻击
        pawn = make_piece(by_side, P)
        pawn_rank = rank - forward_of(by_side)
        if in_board(pawn_rank, file) and grid[pawn_rank][file] == pawn:
            return True
        if crossed_river(by_side, rank):
            for df in (-1, 1):
                nf = file + df
                if in_board(rank, nf) and grid[rank][nf] == pawn:
                    return True

        # 马（检查马腿）
        horse = make_piece(by_side, N)
        for dr, df in HORSE_MOVES:
            hr, hf = rank + dr, file + df
            if not in_board(hr, hf) or grid[hr][hf] != horse:
                continue
            lr, lf = horse_leg(-dr, -df)
            if grid[hr + lr][hf + lf] is None:
                return True

        # 相/象（检查象眼与河界）
        elephant = make_piece(by_side, B)
        if own_half(by_side, rank, file):
            for dr, df in ELEPHANT_MOVES:
                er, ef = rank + dr, file + df
                if not in_board(er, ef) or grid[er][ef] != elephant:
                    continue
                if grid[(er + rank) // 2][(ef + file) // 2] is None:
                    return True

        # 仕/士
        advisor = make_piece(by_side, A)
        if in_palace(by_side, rank, file):
            for dr, df in DIAG:
                ar, af = rank + dr, file + df
                if in_board(ar, af) and grid[ar][af] == advisor:
                    return True

        # 车、炮
        rook = make_piece(by_side, R)
        cannon = make_piece(by_side, C)
        king = make_piece(by_side, K)
        for dr, df in ORTHO:
            nr, nf = rank + dr, file + df
            screen = False
            while in_board(nr, nf):
                occupant = grid[nr][nf]
                if occupant is not None:
                    if not screen:
                        if occupant == rook:
                            return True
                        screen = True
                    else:
                        if occupant == cannon:
                            return True
                        break
                nr += dr
                nf += df

        # 将/帅：贴身一步；以及对将/帅的飞将照面
        for dr, df in ORTHO:
            nr, nf = rank + dr, file + df
            if in_board(nr, nf) and grid[nr][nf] == king:
                return True
        target = grid[rank][file]
        if target is not None and type_of(target) == K:
            for dr, df in ORTHO:
                nr, nf = rank + dr, file + df
                while in_board(nr, nf):
                    occupant = grid[nr][nf]
                    if occupant is not None:
                        if occupant == king:
                            return True
                        break
                    nr += dr
                    nf += df
        return False

    def in_check(self, side=None):
        """该方是否被将军（含将帅照面）。"""
        if side is None:
            side = self.side
        position = self.king_pos(side)
        if position is None:
            return True
        return (self.is_attacked(position[0], position[1], other(side))
                or self.kings_facing())

    # -------------------------------------------------------------- 胜负判定
    def _segment_gave_checks(self, index):
        """返回自 ``index`` 起每步是否造成将军。

        ``make_move`` 会直接记录该标记；若局面是用 ``push`` 走出来的（搜索或第三方
        调用），则通过回放补齐并写回记录，使长将判定与走子方式无关。
        """
        segment = self.history[index:]
        if all(record.gave_check is not None for record in segment):
            return [bool(record.gave_check) for record in segment]

        moves = [record.move for record in segment]
        for _ in segment:
            self.pop()
        flags = []
        pushed = 0
        try:
            for move in moves:
                self.push(move)
                pushed += 1
                flags.append(self.in_check(self.side))
        finally:
            for _ in range(pushed):
                self.pop()
            for move in moves:
                self.push(move)
        for offset, flag in enumerate(flags):
            self.history[index + offset].gave_check = flag
        return flags

    def _perpetual_checker(self):
        """若当前重复局面源于单方连续将军，返回该方；否则返回 ``None``。"""
        key = self.hash
        index = None
        for i in range(len(self._keys) - 2, -1, -1):
            if self._keys[i] == key:
                index = i
                break
        if index is None:
            return None
        segment = self.history[index:]
        if not segment:
            return None
        flags = self._segment_gave_checks(index)
        guilty = []
        for side in SIDES:
            marks = [flag for record, flag in zip(segment, flags)
                     if record.side == side]
            if marks and all(marks):
                guilty.append(side)
        if len(guilty) == 1:
            return guilty[0]
        return None

    def status(self, check_moves=True):
        """返回 ``(是否结束, 获胜方或 None, 原因文本)``。"""
        side = self.side
        if check_moves and not self.has_legal_moves(side):
            if self.in_check(side):
                return True, other(side), "%s被将死" % SIDE_NAMES[side]
            return True, other(side), "%s困毙，无着可走" % SIDE_NAMES[side]
        if self.repetition_count() >= 3:
            guilty = self._perpetual_checker()
            if guilty is not None:
                return True, other(guilty), "%s长将判负" % SIDE_NAMES[guilty]
            return True, None, "同一局面三次重复，判和"
        if self.halfmove_clock >= NATURAL_LIMIT_PLIES:
            return True, None, "连续 60 回合无吃子，判和"
        return False, None, ""

    def is_game_over(self):
        return self.status()[0]

    def result_text(self):
        """返回结果描述；未结束返回 ``''``。"""
        over, winner, reason = self.status()
        if not over:
            return ""
        if winner is None:
            return "和棋（%s）" % reason
        return "%s胜（%s）" % (SIDE_NAMES[winner], reason)

    # -------------------------------------------------------------- 调试/测试
    def perft(self, depth):
        """结点计数（用于验证着法生成正确性）。"""
        if depth <= 0:
            return 1
        total = 0
        for move in self.gen_legal_moves(self.side):
            self.push(move)
            total += self.perft(depth - 1)
            self.pop()
        return total

    def __str__(self):  # pragma: no cover - 调试用
        lines = []
        for rank in range(9, -1, -1):
            cells = []
            for file in range(9):
                piece = self.grid[rank][file]
                cells.append(piece if piece else ".")
            lines.append("%d %s" % (rank, " ".join(cells)))
        lines.append("  " + " ".join("abcdefghi"))
        return "\n".join(lines)
