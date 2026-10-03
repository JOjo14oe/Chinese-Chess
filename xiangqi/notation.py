# -*- coding: utf-8 -*-
"""坐标记谱（ICCS）与中文纵线记谱的互转。

* ICCS：``file`` 用字母 a-i，``rank`` 用数字 0-9；如 ``h2e2``。
* 中文纵线记谱：红方纵线由右至左为一~九，黑方纵线由右至左为 1~9；
  着法写作 ``炮二平五``、``马8进7``、``前车进一``。
  同一纵线上有多个同类子时用 前/中/后 区分；若不同纵线上存在同称谓的棋子，
  则在棋子名后附上该子所在纵线号消歧（如 ``前兵九进一``）。
"""

import re

from .constants import (ARABIC_DIGITS, CN_DIGITS, CN_NAME_TO_SIDE_TYPE, C,
                        FILE_LETTERS, K, Move, P, PIECE_NAMES, R, RED,
                        make_piece, side_of, type_of)

# ------------------------------------------------------------------ ICCS 记谱
_ICCS_RE = re.compile(r"^([a-i])(\d)([a-i])(\d)$")


def move_to_iccs(move):
    """着法 -> ICCS 字符串。"""
    return move.iccs()


def move_from_iccs(text):
    """ICCS 字符串 -> 着法；非法输入抛 ``ValueError``。"""
    s = re.sub(r"[\s\-_,]+", "", str(text)).lower()
    m = _ICCS_RE.match(s)
    if not m:
        raise ValueError("无法识别的坐标记谱: %r" % (text,))
    fc = FILE_LETTERS.index(m.group(1))
    fr = int(m.group(2))
    tc = FILE_LETTERS.index(m.group(3))
    tr = int(m.group(4))
    return Move(fr, fc, tr, tc)


# ------------------------------------------------------------------ 中文记谱
def _file_number(side, file_index):
    """file 索引 -> 该方纵线编号（红方由右至左 1-9）。"""
    if side == RED:
        return 9 - file_index
    return file_index + 1


def _file_from_number(side, number):
    """该方纵线编号 -> file 索引。"""
    if side == RED:
        return 9 - number
    return number - 1


def _numeral(side, number):
    """纵线/步数编号 -> 显示用字符。"""
    if side == RED:
        return CN_DIGITS[number - 1]
    return ARABIC_DIGITS[number - 1]


def _numeral_value(ch):
    """中文或阿拉伯数字字符 -> 1-9；失败返回 None。"""
    if ch in CN_DIGITS:
        return CN_DIGITS.index(ch) + 1
    if ch in ARABIC_DIGITS:
        return int(ch)
    return None


def _stack_label(index, count):
    """同一纵线上第 ``index`` 个同类子的称谓（前/中/后/二/三…）。"""
    if count == 2:
        return "前" if index == 0 else "后"
    if count == 3:
        return ("前", "中", "后")[index]
    labels = ["前"] + [CN_DIGITS[i] for i in range(1, count - 1)] + ["后"]
    return labels[index]


def _stacked_lines(board, piece, side):
    """返回 {file: [rank, ...]}，仅包含同类棋子数 >= 2 的纵线，按前->后排序。"""
    lines = {}
    for f in range(9):
        ranks = [r for r in range(10) if board.grid[r][f] == piece]
        if len(ranks) >= 2:
            lines[f] = sorted(ranks, reverse=(side == RED))
    return lines


def move_to_chinese(board, move):
    """把 ``move`` 转成中文纵线记谱（以走子前的局面 ``board`` 为准）。"""
    fr, fc, tr, tc = move
    piece = board.grid[fr][fc]
    if piece is None:
        raise ValueError("起点 %s 上没有棋子" % move.from_iccs)
    side = side_of(piece)
    ptype = type_of(piece)
    name = PIECE_NAMES[side][ptype]
    forward = 1 if side == RED else -1

    lines = _stacked_lines(board, piece, side)
    if fc in lines:
        ordered = lines[fc]
        label = _stack_label(ordered.index(fr), len(ordered))
        head = label + name
        # 其它纵线上是否存在同样称谓的同类子；若有则附纵线号消歧
        clashes = 0
        for ranks in lines.values():
            labels = [_stack_label(i, len(ranks)) for i in range(len(ranks))]
            if label in labels:
                clashes += 1
        if clashes > 1:
            head += _numeral(side, _file_number(side, fc))
    else:
        head = name + _numeral(side, _file_number(side, fc))

    if tr == fr:
        return head + "平" + _numeral(side, _file_number(side, tc))
    action = "进" if (tr - fr) * forward > 0 else "退"
    if ptype in (K, R, C, P):
        return head + action + _numeral(side, abs(tr - fr))
    return head + action + _numeral(side, _file_number(side, tc))


_NAME_CHARS = "".join(CN_NAME_TO_SIDE_TYPE.keys())
_DIGIT_CHARS = CN_DIGITS + ARABIC_DIGITS
# 前缀含 前/中/后 与同线多子时的序号（二~八；一 由“前”表示）
_CHINESE_RE = re.compile(
    r"^(?P<pre>[前后中二三四五六七八])?(?P<name>[%s])(?P<filenum>[%s])?(?P<act>[进退平])(?P<target>[%s])$"
    % (_NAME_CHARS, _DIGIT_CHARS, _DIGIT_CHARS))


def parse_chinese(board, text):
    """解析中文纵线记谱；成功返回 ``Move``，无法解析或非法返回 ``None``。"""
    s = re.sub(r"\s+", "", str(text or ""))
    m = _CHINESE_RE.match(s)
    if not m:
        return None

    options = CN_NAME_TO_SIDE_TYPE.get(m.group("name"), [])
    if not options:
        return None
    if len(options) > 1:  # 车/马/炮：结合行棋方消歧
        options = [o for o in options if o[0] == board.side]
    if not options:
        return None
    side, ptype = options[0]
    if side != board.side:
        return None

    target = _numeral_value(m.group("target"))
    if target is None:
        return None
    pre = m.group("pre")
    filenum_txt = m.group("filenum")
    act = m.group("act")
    piece = make_piece(side, ptype)
    positions = [(r, f) for r in range(10) for f in range(9)
                 if board.grid[r][f] == piece]
    if not positions:
        return None

    if pre:
        candidates = []
        for f in range(9):
            ranks = sorted([r for (r, ff) in positions if ff == f],
                           reverse=(side == RED))
            n = len(ranks)
            if n < 2:
                continue
            idx = None
            for i in range(n):
                if _stack_label(i, n) == pre:
                    idx = i
                    break
            if idx is None:
                continue
            candidates.append((ranks[idx], f))
        if filenum_txt:
            want = _file_from_number(side, _numeral_value(filenum_txt))
            candidates = [c for c in candidates if c[1] == want]
    elif filenum_txt:
        want = _file_from_number(side, _numeral_value(filenum_txt))
        candidates = [(r, f) for (r, f) in positions if f == want]
    else:
        return None

    if len(candidates) != 1:
        return None
    fr, fc = candidates[0]

    forward = 1 if side == RED else -1
    matched = []
    for mv in board.legal_moves_from(fr, fc):
        if act == "平":
            if mv.tr == fr and mv.tc == _file_from_number(side, target):
                matched.append(mv)
        elif ptype in (K, R, C, P):
            delta = target * (forward if act == "进" else -forward)
            if mv.tr == fr + delta and mv.tc == fc:
                matched.append(mv)
        else:  # 马/仕/相：目标为纵线号
            if mv.tc == _file_from_number(side, target):
                going_forward = (mv.tr - fr) * forward > 0
                if (act == "进") == going_forward:
                    matched.append(mv)
    if len(matched) != 1:
        return None
    return matched[0]


def describe_move(board, move):
    """返回 ``'炮二平五 (h2e2)'`` 形式的可读描述。"""
    return "%s (%s)" % (move_to_chinese(board, move), move.iccs())
