# -*- coding: utf-8 -*-
"""网页离线规则引擎（``xiangqi/net/web/engine.js``）与 Python 权威引擎的对照测试。

联机版由 Python 服务器判定规则；断网时必须由浏览器里的 ``XQEngine`` 判定，
所以这里把两边的结果**在测试时动态对拍**（不写死基准值，避免两边一起错）：

* perft(1..3)：开局 + 中局/残局/牵制/对脸 等非开局局面，Python ``Board.perft``
  与 JS ``XQEngine.perft`` 逐一对齐；
* 合法着法：``XQEngine.moves`` / ``legalMap`` 与 ``Board.gen_legal_moves`` 完全一致
  （含顺序，顺序影响 legalMap 里提示点的排列）；
* 中文记谱：``XQEngine.chinese`` 与 ``notation.move_to_chinese`` 逐条对齐；
* 走子：``XQEngine.applyMove`` 的新 FEN / 记谱 / 将军 / 结果与 ``Board`` 一致，
  非法着法（送将、对脸、兵后退、塞象眼、蹩马腿、炮无炮架）一律拒绝；
* 胜负：将死 / 困毙 / 三次重复 / 60 回合无吃子与 ``Board.status`` 一致；
* AI：``XQEngine.bestMove`` 必须在时限内返回一步合法着法。

需要本机有 ``node``，否则整类测试自动跳过（与 ``tests/test_web.py`` 同样的门控），
不引入任何 npm 依赖。
"""

import json
import os
import shutil
import subprocess
import unittest

try:
    from .helpers import fen as make_fen
except ImportError:  # 直接运行单文件时
    from helpers import fen as make_fen

from xiangqi.board import Board
from xiangqi.constants import START_FEN
from xiangqi.notation import move_from_iccs, move_to_chinese

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "xiangqi", "net", "web")
NODE = shutil.which("node")

MISSING_NODE = "本机没有 node，跳过 JS 检查"


# ---------------------------------------------------------------------- 局面
def _expand(row):
    """把 FEN 风格的行（``4k4``）展开成点阵行（``....k....``）。"""
    text = ""
    for ch in str(row):
        if ch.isdigit():
            text += "." * int(ch)
        else:
            text += ch
    if len(text) != 9:
        raise ValueError("每行需要 9 列: %r" % (row,))
    return text


def _pos(rows, side="w", halfmove=0):
    """由棋盘文本构造 FEN（rank 9 在上、rank 0 在下）。

    行可以写成 FEN 风格的 ``"4k4"``（数字表示连续空点），也可以写成点阵
    ``"....k...."``；整体可以给 10 行文本，也可以直接给 FEN 的棋盘段
    ``"4k4/9/.../3K5"``。
    """
    if isinstance(rows, (list, tuple)):
        parts = []
        for row in rows:
            parts.extend(str(row).split("/"))
        lines = parts
    else:
        lines = str(rows).strip().strip("[]").split("/")
    return make_fen([_expand(line) for line in lines], side, halfmove)


#: 开局（红先）
START = START_FEN
#: 同一开局但黑先
START_BLACK = ("rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/"
               "RNBAKABNR b - - 0 1")
#: 中局：红车马对黑象，双方都有活动空间
MIDGAME = _pos("4k4/9/4b4/9/9/9/9/4N4/4R4/3K5")
#: 牵制：e4 黑车照将，e1 红车被牵制在 e0 帅前（动 e1 车即送将）
PINNED_ROOK = _pos("4k4/9/9/9/9/4r4/9/9/4R4/4K4")
#: 将帅照面（飞将）：d 线中间无子，走子方已被将军
KINGS_FACING = _pos("3k5/9/9/9/9/9/9/9/9/3K5")
#: 车兵残局：红方有杀着
ENDGAME_MATE = _pos("4k4/1R7/9/9/9/9/9/9/9/R2K5")
#: 炮架：红炮 e0 隔 e4 兵打 e9 黑将
CANNON_SCREEN = _pos("4k4/9/9/9/4P4/9/9/9/9/4C3K")
#: 同上但没有炮架：e0 炮打不到 e9 将
CANNON_SCREEN_NOSCREEN = _pos("4k4/9/9/9/9/9/9/9/9/4C3K")
#: 塞象眼：黑象 c9 的 a7 象眼（b8）被红车占住，e7 畅通
BLOCKED_ELEPHANT = _pos("2b1k4/9/9/9/9/9/9/9/1R7/3K5")
#: 蹩马腿：黑马 b9 的马腿（b8 红兵）被占，只能走 d8
HORSE_LEG = _pos("1n3k3/1P7/9/9/9/9/9/9/9/3K5")
#: 黑方着法测试：黑卒、黑马，红方一马一帅
BLACK_PIECES = _pos("3k5/9/9/9/9/2n6/2p6/9/4N4/3K5", side="b")
#: 双车叠在同一条纵线（中文记谱的 前/后）；红帅在 d0、黑将在 e9，两车都不被牵制
STACKED_ROOKS = _pos("4k4/9/9/9/9/R8/9/9/9/R2K5")
#: 同上但两车在 a3/a2，红帅在 c0：a2 的后车可以直接退回底线（后车退一/后车退二）
STACKED_ROOKS_LOW = _pos("4k4/9/9/9/9/9/R8/R8/9/2K6")
#: 两条纵线上各有两个兵（前/后 称谓冲突时需附纵线号）
STACKED_PAWNS = _pos("3k5/9/9/9/9/9/9/9/2P1P4/2P1P1K2")
#: 六兵叠在 a 线：第 5 个兵是“五兵”（复刻 test_notation 的极端叠子用例）
SIX_PAWN_STACK = _pos("4k4/9/9/9/P8/P8/P8/P8/P8/3K5", side="b")

POSITIONS = (
    ("start", START),
    ("start_black", START_BLACK),
    ("midgame", MIDGAME),
    ("pinned_rook", PINNED_ROOK),
    ("kings_facing", KINGS_FACING),
    ("endgame_mate", ENDGAME_MATE),
    ("cannon_screen", CANNON_SCREEN),
    ("blocked_elephant", BLOCKED_ELEPHANT),
    ("horse_leg", HORSE_LEG),
    ("black_pieces", BLACK_PIECES),
    ("stacked_rooks", STACKED_ROOKS),
    ("stacked_rooks_low", STACKED_ROOKS_LOW),
    ("stacked_pawns", STACKED_PAWNS),
    ("six_pawn_stack", SIX_PAWN_STACK),
)
FEN_BY_KEY = dict(POSITIONS)

#: perft 只挑分支不大的局面，保证 Python 端深度 3 也在几秒内跑完。
PERFT_KEYS = ("start", "start_black", "endgame_mate", "pinned_rook",
              "kings_facing", "stacked_rooks", "cannon_screen",
              "blocked_elephant", "horse_leg", "black_pieces")
PERFT_DEPTHS = (1, 2, 3)
PERFT_CASES = [(key, depth) for key in PERFT_KEYS for depth in PERFT_DEPTHS]

#: 三次重复：双方各来回走两步，八手之后又回到红方走棋的同一局面
REPEAT_FEN = _pos("3k5/9/9/9/9/9/9/9/9/4K4")
REPEAT_HISTORY = ["e0e1", "d9d8", "e1e0", "d8d9",
                  "e0e1", "d9d8", "e1e0", "d8d9"]
#: 困毙：黑将 a9 被 a8 车封住，b9 也在车口上（红帅在 d0，两将不同线）
STALEMATE_FEN = "4k4/3R1R3/9/9/9/9/9/9/9/3K5 b - - 0 1"
#: 被将死：黑将 a9，红车 b8 与 d0 帅
    #: 已被将死：黑将 e9，红车 a9/a8 双车错（黑方无着可走；与 main.py 自检同一局面）
MATE_FEN = "R3k4/R8/9/9/9/9/9/9/9/3K5 b - - 0 1"
#: 60 回合无吃子：半回合数已经 119
NO_CAPTURE_FEN = _pos("3k5/9/9/9/9/9/9/9/9/4K4", halfmove=119)


# ---------------------------------------------------------------------- 桥
def engine_request(request, timeout=240):
    """把请求交给 node 里的 XQEngine，返回解析后的应答。

    用 ``node -e`` 执行，避免在仓库里留下临时文件；脚本里的 ``__dirname``
    由 ``cwd`` 决定，所以 ``require('./engine.js')`` 一定命中网页目录里的引擎。
    """
    if not NODE:
        raise RuntimeError(MISSING_NODE)
    script = (
        "var req = JSON.parse(process.argv[1]);\n"
        "var E = require('./engine.js');\n"
        "var out = {};\n"
        "var i;\n"
        "function list(name) { return Array.isArray(req[name]) ? req[name] : []; }\n"
        "out.perft = {};\n"
        "for (i = 0; i < list('perft').length; i += 1) {\n"
        "  out.perft[req.perft[i].key] = E.perft(req.perft[i].fen, req.perft[i].depth);\n"
        "}\n"
        "out.moves = {};\n"
        "for (i = 0; i < list('moves').length; i += 1) {\n"
        "  out.moves[req.moves[i].key] = E.moves(req.moves[i].fen);\n"
        "}\n"
        "out.legal = {};\n"
        "for (i = 0; i < list('legal').length; i += 1) {\n"
        "  out.legal[req.legal[i].key] = E.legalMap(req.legal[i].fen);\n"
        "}\n"
        "out.chinese = [];\n"
        "for (i = 0; i < list('chinese').length; i += 1) {\n"
        "  out.chinese.push(E.chinese(req.chinese[i].fen, req.chinese[i].iccs));\n"
        "}\n"
        "out.apply = [];\n"
        "for (i = 0; i < list('apply').length; i += 1) {\n"
        "  out.apply.push(E.applyMove(req.apply[i].fen, req.apply[i].iccs));\n"
        "}\n"
        "out.status = [];\n"
        "for (i = 0; i < list('status').length; i += 1) {\n"
        "  out.status.push(E.status(req.status[i].fen, req.status[i].history));\n"
        "}\n"
        "out.best = {};\n"
        "for (i = 0; i < list('best').length; i += 1) {\n"
        "  var item = req.best[i];\n"
        "  var started = Date.now();\n"
        "  var move = E.bestMove(item.fen, item.level, item.seed);\n"
        "  out.best[item.key] = { move: move, ms: Date.now() - started };\n"
        "}\n"
        "process.stdout.write(JSON.stringify(out));\n"
    )
    result = subprocess.run(
        [NODE, "-e", script, json.dumps(request, ensure_ascii=False)],
        cwd=WEB_DIR, capture_output=True, timeout=timeout,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    if result.returncode != 0:
        raise AssertionError("node 执行失败（%d）：\n%s" % (
            result.returncode,
            (result.stdout + result.stderr).decode("utf-8", "replace")))
    return json.loads(result.stdout.decode("utf-8"))


def py_move_iccs(board):
    return [move.iccs() for move in board.gen_legal_moves()]


def perft_request(keys=PERFT_KEYS, depths=PERFT_DEPTHS):
    return [{"key": "%s:%d" % (key, depth), "fen": FEN_BY_KEY[key], "depth": depth}
            for key in keys for depth in depths]


class TestWebEnginePerft(unittest.TestCase):
    """结点计数必须与 Python 完全一致（着法生成是规则正确性的地基）。"""

    @classmethod
    def setUpClass(cls):
        if not NODE:
            raise unittest.SkipTest(MISSING_NODE)
        cls.perft = engine_request({"perft": perft_request()})["perft"]

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_perft_matches_python(self):
        for key, depth in PERFT_CASES:
            fen = FEN_BY_KEY[key]
            expected = Board(fen).perft(depth)
            actual = self.perft["%s:%d" % (key, depth)]
            self.assertEqual(actual, expected,
                             "perft(%d) 不一致：%s\nPython=%d JS=%d\nFEN=%s"
                             % (depth, key, expected, actual, fen))

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_start_position_reference_numbers(self):
        """开局 perft 有公开基准，顺手钉死，避免两边一起错。"""
        self.assertEqual(self.perft["start:1"], 44)
        self.assertEqual(self.perft["start:2"], 1920)
        self.assertEqual(self.perft["start:3"], 79666)


class TestWebEngineMoves(unittest.TestCase):
    """着法列表（含顺序）与 legalMap 形状。"""

    @classmethod
    def setUpClass(cls):
        if not NODE:
            raise unittest.SkipTest(MISSING_NODE)
        payload = engine_request({
            "moves": [{"key": key, "fen": fen} for key, fen in POSITIONS],
            "legal": [{"key": key, "fen": fen} for key, fen in POSITIONS],
        })
        cls.moves = payload["moves"]
        cls.legal = payload["legal"]

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_move_list_matches_python(self):
        total = 0
        for key, fen in POSITIONS:
            expected = py_move_iccs(Board(fen))
            self.assertEqual(sorted(self.moves[key]), sorted(expected),
                             "合法着法集合不一致：%s" % key)
            total += len(expected)
        self.assertGreater(total, 100, "对照样本太少")

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_move_list_order_matches_python(self):
        """顺序也要一致：提示点按生成顺序渲染，顺序不同会让 UI 抖动。"""
        for key, fen in POSITIONS:
            self.assertEqual(self.moves[key], py_move_iccs(Board(fen)),
                             "合法着法顺序不一致：%s" % key)

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_legal_map_matches_python(self):
        for key, fen in POSITIONS:
            legal = {}
            for move in Board(fen).gen_legal_moves():
                legal.setdefault(move.from_iccs, []).append(move.to_iccs)
            self.assertEqual(self.legal[key], legal, "legalMap 不一致：%s" % key)

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_legal_map_shape_is_plain_object(self):
        legal = self.legal["start"]
        self.assertIsInstance(legal, dict)
        for square, targets in legal.items():
            self.assertRegex(square, r"^[a-i][0-9]$")
            self.assertIsInstance(targets, list)
            for target in targets:
                self.assertRegex(target, r"^[a-i][0-9]$")
        self.assertEqual(legal["a0"], ["a1", "a2"])
        # 落点**顺序**不属于协议约定（只保证集合合法），因此按集合比较
        self.assertEqual(sorted(legal["b0"]), sorted(["c2", "a2"]))

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_kings_facing_position_is_check(self):
        """将帅照面时必须判为被将军（照面即违规）。"""
        self.assertTrue(Board(KINGS_FACING).in_check())
        payload = engine_request({"status": [
            {"fen": KINGS_FACING, "history": []}]})
        self.assertTrue(payload["status"][0]["check"])
        self.assertIn("d0e0", py_move_iccs(Board(KINGS_FACING)))
        self.assertNotIn("d0d1", py_move_iccs(Board(KINGS_FACING)))


class TestWebEngineNotation(unittest.TestCase):
    """中文记谱逐条对拍（平/进/退、红/黑、前/后 叠子）。"""

    @classmethod
    def setUpClass(cls):
        if not NODE:
            raise unittest.SkipTest(MISSING_NODE)
        cls.pairs = []
        cls.expected = []
        for key, fen in POSITIONS:
            board = Board(fen)
            for move in board.gen_legal_moves():
                cls.pairs.append({"fen": fen, "iccs": move.iccs()})
                cls.expected.append("%s/%s -> %s"
                                    % (key, move.iccs(),
                                       move_to_chinese(board, move)))
        cls.payload = engine_request({"chinese": cls.pairs})

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_chinese_notation_matches_python(self):
        self.assertGreaterEqual(len(self.pairs), 15, "对照样本太少")
        actual = self.payload["chinese"]
        self.assertEqual(len(actual), len(self.expected))
        for want, got in zip(self.expected, actual):
            self.assertEqual(got, want.split(" -> ")[1],
                             "记谱不一致：%s（JS 得到 %s）" % (want, got))

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_chinese_notation_covers_all_actions_and_stacks(self):
        texts = self.payload["chinese"]
        self.assertTrue(any("平" in text for text in texts), "缺少 平")
        self.assertTrue(any("进" in text for text in texts), "缺少 进")
        self.assertTrue(any("退" in text for text in texts), "缺少 退")
        self.assertTrue(any(text[0] in "前后" for text in texts), "缺少 前/后 叠子")
        for want in ("炮二平五", "马二进三", "车九进一", "马8进7", "炮2平5",
                     "前车进一", "后车进一", "前车退一", "后车退一", "后车平七",
                     "前兵七进一", "前兵五进一"):
            self.assertIn(want, texts, "缺少经典记谱：%s" % want)
        # 叠兵的“数字/前后”称谓（具体是 五兵 还是 前兵，取决于哪个兵能合法横走）
        self.assertTrue(any(text[0] in "前后二三四五" and "兵" in text for text in texts),
                        "缺少叠兵的数字/前后记谱")

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_chinese_notation_for_start_position(self):
        """开局每一手的中文记谱逐个核对（红黑各一份）。"""
        cases = {
            START: {"h2e2": "炮二平五", "h0g2": "马二进三", "b0a2": "马八进九",
                    "c3c4": "兵七进一", "a0a1": "车九进一", "f0e1": "仕四进五",
                    "g0e2": "相三进五", "h2h9": "炮二进七"},
            START_BLACK: {"h9g7": "马8进7", "b7e7": "炮2平5", "c6c5": "卒3进1",
                          "a9a8": "车1进1", "i9i8": "车9进1"},
        }
        request = {"chinese": []}
        for fen, moves in cases.items():
            for iccs in moves:
                request["chinese"].append({"fen": fen, "iccs": iccs})
        actual = engine_request(request)["chinese"]
        expected = []
        for fen, moves in cases.items():
            board = Board(fen)
            for iccs, text in moves.items():
                expected.append((text, move_to_chinese(board, move_from_iccs(iccs))))
        self.assertEqual(len(actual), len(expected))
        for got, (want, check) in zip(actual, expected):
            self.assertEqual(want, check, "测试用例自身写错了：%s" % want)
            self.assertEqual(got, want, "开局记谱不一致：%s" % want)


class TestWebEngineApplyMove(unittest.TestCase):
    """走子：新 FEN / 记谱 / 将军 / 胜负。"""

    @classmethod
    def setUpClass(cls):
        if not NODE:
            raise unittest.SkipTest(MISSING_NODE)
        cls.cases = []
        for key, fen in POSITIONS:
            board = Board(fen)
            for move in board.gen_legal_moves():
                after = Board(fen)
                after.make_move(move)
                cls.cases.append({"key": "%s/%s" % (key, move.iccs()),
                                  "fen": fen, "iccs": move.iccs(),
                                  "board": after})
        cls.payload = engine_request({"apply": [
            {"fen": case["fen"], "iccs": case["iccs"]} for case in cls.cases]})

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_apply_move_matches_python(self):
        self.assertGreater(len(self.cases), 100, "对照样本太少")
        for case, got in zip(self.cases, self.payload["apply"]):
            board = case["board"]
            label = case["key"]
            self.assertTrue(got.get("ok"), "%s 走子被拒：%s" % (label, got))
            self.assertEqual(got["fen"], board.to_fen(), "%s 新 FEN 不一致" % label)
            self.assertEqual(got["side"], board.side, "%s 走子方不一致" % label)
            move = move_from_iccs(case["iccs"])
            self.assertEqual(got["from"], move.from_iccs, label)
            self.assertEqual(got["to"], move.to_iccs, label)
            self.assertEqual(got["chinese"],
                             move_to_chinese(Board(case["fen"]), move),
                             "%s 中文记谱不一致" % label)
            self.assertEqual(got["check"], board.in_check(board.side),
                             "%s 将军标志不一致" % label)
            if board.status()[0]:
                self.assertIsNotNone(got["result"], "%s 应判出结果" % label)
                self.assertEqual(got["result"]["text"], board.result_text(),
                                 "%s 结果文本不一致" % label)
            else:
                self.assertIsNone(got["result"], "%s 不应有结果" % label)

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_capture_and_piece_fields(self):
        got = engine_request({"apply": [
            {"fen": CANNON_SCREEN, "iccs": "e0e9"}]})["apply"][0]
        self.assertTrue(got["ok"])
        self.assertEqual(got["piece"], "C")
        self.assertEqual(got["captured"], "k")
        self.assertEqual(got["chinese"], "炮五进九")
        board = Board(CANNON_SCREEN)
        board.make_move(move_from_iccs("e0e9"))
        self.assertEqual(got["fen"], board.to_fen())
        self.assertEqual(got["result"]["kind"], "checkmate")

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_quiet_move_reports_no_captured(self):
        got = engine_request({"apply": [
            {"fen": START, "iccs": "h2e2"}]})["apply"][0]
        self.assertTrue(got["ok"])
        self.assertEqual(got["captured"], "")
        self.assertEqual(got["piece"], "C")
        self.assertEqual(got["chinese"], "炮二平五")
        self.assertEqual(got["fen"],
                         "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C2C4/9/"
                         "RNBAKABNR b - - 1 1")


class TestWebEngineLegalityEdges(unittest.TestCase):
    """非法着法必须被拒绝（与 Python 的合法着法集合完全同集）。"""

    #: (说明, 局面, ICCS)
    PROBES = (
        ("帅主动走到被照将的格子", PINNED_ROOK, "e0e1"),
        ("被牵制的车离开 e 线（送将）", PINNED_ROOK, "e1a1"),
        ("被牵制的车吃掉照将的车（解将，合法）", PINNED_ROOK, "e1e4"),
        ("照面时红帅让到 e0（离开 d 线，合法）", KINGS_FACING, "d0e0"),
        ("照面时红帅让到 c0（九宫之外，非法）", KINGS_FACING, "d0c0"),
        ("照面时红帅沿 d 线前进（仍照面，非法）", KINGS_FACING, "d0d1"),
        ("炮隔着炮架吃子（e0 炮隔 e4 兵吃 e9 将，合法）",
         CANNON_SCREEN, "e0e9"),
        ("炮无炮架不能吃子（e4 兵走后 e0 炮不能吃 e9 将）",
         CANNON_SCREEN_NOSCREEN, "e0e9"),
        ("炮无炮架时只能平移（e0 炮不能吃 e9 将）",
         CANNON_SCREEN_NOSCREEN, "e0e7"),
        ("兵不能后退", START, "c3c2"),
        ("相被塞象眼（c9 象走 a7，b8 被红车占住）", BLOCKED_ELEPHANT, "c9a7"),
        ("相的另一条斜线畅通（c9 象走 e7，b8 不挡象眼，合法）",
         BLOCKED_ELEPHANT, "c9e7"),
        ("马蹩马腿（b9 马走 a7，b8 有子）", HORSE_LEG, "b9a7"),
        ("马蹩马腿（b9 马走 c7，b8 有子）", HORSE_LEG, "b9c7"),
        ("马腿不被挡时可以走（b9 马走 d8，c9 空）", HORSE_LEG, "b9d8"),
        ("不能吃自己的子", START, "a0a3"),
        ("非本方棋子不能走（黑先时红车 a0 不能动）", START_BLACK, "a0a1"),
        ("黑卒不能后退", BLACK_PIECES, "c6c7"),
        ("起点没有棋子", START, "a5a6"),
    )

    #: 格式非法的 ICCS（与局面无关）
    BAD_ICCS = ("h2e", "z9z8", "", "h2e2e2", "炮二平五")

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_probes_agree_with_python(self):
        legal_by_fen = {}
        for _label, fen, _iccs in self.PROBES:
            legal_by_fen.setdefault(fen, set(py_move_iccs(Board(fen))))
        payload = engine_request({"apply": [
            {"fen": fen, "iccs": iccs} for _label, fen, iccs in self.PROBES]})
        for (label, fen, iccs), got in zip(self.PROBES, payload["apply"]):
            want = iccs in legal_by_fen[fen]
            self.assertEqual(got["ok"], want,
                             "判定与 Python 不符：%s（%s %s -> %s）"
                             % (label, fen, iccs, got))
            if not want:
                self.assertEqual(got["error"], "illegal", label)

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_bad_iccs_rejected(self):
        request = {"apply": [{"fen": START, "iccs": text} for text in self.BAD_ICCS]}
        for text, got in zip(self.BAD_ICCS, engine_request(request)["apply"]):
            self.assertFalse(got["ok"], "%r 不应被接受" % text)
            self.assertEqual(got["error"], "bad_iccs", "%r 应报格式错误" % text)

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_own_king_left_in_check_rejected(self):
        """把照将的车"吃掉"以外的任何送将着法都必须被拒。"""
        board = Board(PINNED_ROOK)
        bad = [iccs for iccs in py_move_iccs(board) if iccs.startswith("e1")]
        self.assertEqual(bad, ["e1e2", "e1e3", "e1e4"],
                         "e1 车只能沿 e 线走（离开 e 线即送将）")
        request = {"apply": [{"fen": PINNED_ROOK, "iccs": "e1b1"},
                             {"fen": PINNED_ROOK, "iccs": "e1a1"},
                             {"fen": PINNED_ROOK, "iccs": "e0e1"}]}
        for got in engine_request(request)["apply"]:
            self.assertFalse(got["ok"])
            self.assertEqual(got["error"], "illegal")

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_illegal_fen_raises(self):
        """FEN 本身非法时 JS 必须报错，而不是给出错误答案。"""
        script = (
            "require('./engine.js');"
            "var bad = ['9/9/9/9/9/9/9/9/9/9 w - - 0 1',"
            "           'kk7/9/9/9/9/9/9/9/9/4K4 w - - 0 1',"
            "           '4k4/9/9/9/9/9/9/9/9/3K5 z - - 0 1'];\n"
            "for (var i = 0; i < bad.length; i += 1) {"
            "  var threw = false;"
            "  try { globalThis.XQEngine.legalMap(bad[i]); } catch (e) { threw = true; }"
            "  if (!threw) process.exit(3 + i);"
            "}\n"
            "process.exit(0);\n"
        )
        result = subprocess.run([NODE, "-e", script], cwd=WEB_DIR,
                                capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0,
                         (result.stdout + result.stderr).decode("utf-8", "replace"))


class TestWebEngineResults(unittest.TestCase):
    """将死 / 困毙 / 三次重复 / 60 回合无吃子。"""

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_checkmate(self):
        board = Board(MATE_FEN)
        over, winner, reason = board.status()
        self.assertTrue(over, "该局面黑方应已被将死")
        self.assertEqual(winner, "r")
        self.assertIn("将死", reason)
        got = engine_request({"status": [
            {"fen": MATE_FEN, "history": []}]})["status"][0]
        self.assertTrue(got["over"])
        self.assertEqual(got["result"]["kind"], "checkmate")
        self.assertEqual(got["result"]["text"], board.result_text())
        self.assertEqual(got["result"]["text"], "红方胜（黑方被将死）")
        # 走子接口对“将死局面”也必须报同样的结果（applyMove 的 result 路径）
        applied = engine_request({"apply": [
            {"fen": MATE_FEN, "iccs": "e9e8"}]})["apply"][0]
        self.assertFalse(applied["ok"], "被将死的一方不可能还有合法着法")

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_stalemate(self):
        board = Board(STALEMATE_FEN)
        over, winner, reason = board.status()
        self.assertTrue(over, "该局面应当已经结束（困毙）")
        self.assertEqual(winner, "r")
        self.assertEqual(reason, "黑方困毙，无着可走")
        got = engine_request({"status": [
            {"fen": STALEMATE_FEN, "history": []}]})["status"][0]
        self.assertTrue(got["over"])
        self.assertEqual(got["result"]["kind"], "stalemate")
        self.assertEqual(got["result"]["text"], board.result_text())
        self.assertEqual(got["result"]["text"], "红方胜（黑方困毙，无着可走）")

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_repetition_is_draw(self):
        board = Board(REPEAT_FEN)
        played = []
        for iccs in REPEAT_HISTORY:
            played.append(board.to_fen())
            board.make_move(move_from_iccs(iccs))
        over, winner, reason = board.status()
        self.assertTrue(over, "同一局面三次重复应当判和")
        self.assertIsNone(winner)
        got = engine_request({"status": [
            {"fen": board.to_fen(), "history": played}]})["status"][0]
        self.assertTrue(got["over"])
        self.assertEqual(got["result"]["kind"], "repetition")
        self.assertEqual(got["result"]["text"], board.result_text())
        self.assertEqual(got["result"]["text"], "和棋（同一局面三次重复，判和）")

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_status_without_history_is_not_repetition(self):
        """历史为空时不能凭空判重复。"""
        got = engine_request({"status": [
            {"fen": REPEAT_FEN, "history": []}]})["status"][0]
        self.assertFalse(got["over"])
        self.assertIsNone(got["result"])
        self.assertEqual(got["side"], "r")

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_no_capture_rule(self):
        board = Board(NO_CAPTURE_FEN)
        self.assertFalse(board.status()[0], "半回合 119 时还不该判和")
        board.make_move(move_from_iccs("e0e1"))
        self.assertTrue(board.status()[0], "半回合达到 120 应判和")
        got = engine_request({"apply": [
            {"fen": NO_CAPTURE_FEN, "iccs": "e0e1"}]})["apply"][0]
        self.assertTrue(got["ok"])
        self.assertEqual(got["result"]["kind"], "no_capture")
        self.assertEqual(got["result"]["text"], board.result_text())
        self.assertEqual(got["result"]["text"], "和棋（连续 60 回合无吃子，判和）")

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_history_accepts_fen_objects(self):
        """history 允许 FEN 字符串数组，也允许 ``{fen}`` 对象数组。"""
        board = Board(REPEAT_FEN)
        played = []
        for iccs in REPEAT_HISTORY:
            played.append({"fen": board.to_fen()})
            board.make_move(move_from_iccs(iccs))
        payload = engine_request({"status": [
            {"fen": board.to_fen(), "history": played}]})
        self.assertEqual(payload["status"][0]["result"]["kind"], "repetition")


class TestWebEngineBestMove(unittest.TestCase):
    """AI 必须返回合法着法，且不超时。"""

    @classmethod
    def setUpClass(cls):
        if not NODE:
            raise unittest.SkipTest(MISSING_NODE)
        cls.start_legal = set(py_move_iccs(Board(START)))
        cls.mid_legal = set(py_move_iccs(Board(MIDGAME)))
        cls.mate_legal = set(py_move_iccs(Board(ENDGAME_MATE)))

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_easy_returns_legal_move_quickly(self):
        payload = engine_request({"best": [
            {"key": "easy_start", "fen": START, "level": "easy", "seed": 11},
            {"key": "easy_mid", "fen": MIDGAME, "level": "easy", "seed": 12},
        ]}, timeout=120)
        for key, legal in (("easy_start", self.start_legal),
                           ("easy_mid", self.mid_legal)):
            item = payload["best"][key]
            self.assertRegex(item["move"], r"^[a-i][0-9][a-i][0-9]$")
            self.assertIn(item["move"], legal,
                          "%s 返回了非法着法：%s" % (key, item["move"]))
            self.assertLess(item["ms"], 5000, "%s 用时过长：%dms" % (key, item["ms"]))

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_normal_and_hard_stay_in_budget(self):
        payload = engine_request({"best": [
            {"key": "normal", "fen": MIDGAME, "level": "normal", "seed": 3},
            {"key": "hard", "fen": ENDGAME_MATE, "level": "hard", "seed": 4},
        ]}, timeout=120)
        self.assertIn(payload["best"]["normal"]["move"], self.mid_legal)
        self.assertIn(payload["best"]["hard"]["move"], self.mate_legal)
        self.assertLess(payload["best"]["normal"]["ms"], 8000)
        self.assertLess(payload["best"]["hard"]["ms"], 15000)

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_best_move_is_deterministic_for_same_seed(self):
        payload = engine_request({"best": [
            {"key": "a", "fen": START, "level": "easy", "seed": 99},
            {"key": "b", "fen": START, "level": "easy", "seed": 99},
        ]}, timeout=120)
        self.assertEqual(payload["best"]["a"]["move"], payload["best"]["b"]["move"])

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_no_legal_move_returns_empty(self):
        """黑方被将死时没有合法着法，bestMove 必须返回空串。"""
        payload = engine_request({"best": [
            {"key": "mate", "fen": MATE_FEN, "level": "easy", "seed": 1},
        ]}, timeout=120)
        self.assertEqual(payload["best"]["mate"]["move"], "")

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_engine_finds_mate_in_one(self):
        """残局里有杀着；hard 至少不能比 easy 更差——这里只要求结果是合法着法。"""
        payload = engine_request({"best": [
            {"key": "mate", "fen": ENDGAME_MATE, "level": "hard", "seed": 5},
        ]}, timeout=120)
        self.assertIn(payload["best"]["mate"]["move"], self.mate_legal)


class TestWebEngineRegressions(unittest.TestCase):
    """两个真实踩过的坑（离线模式实测时发现）。"""

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_distinct_positions_are_not_threefold(self):
        """三个**不同**局面不能被算成三次重复。

        回归：positionKey 曾用 grid 行 join('') 拼键，而空格存的是空串，
        于是 "1c5c1" 与 "2c4c1" 都压成 "cc"，普通开局（炮八平五 炮2平3）
        被误判为“同一局面三次重复，判和”。
        """
        board = Board(START)
        board.make_move(move_from_iccs("b2e2"))
        after_red = board.to_fen()
        board.make_move(move_from_iccs("b7c7"))
        after_black = board.to_fen()
        self.assertNotEqual(after_red, after_black)
        got = engine_request({"status": [
            {"fen": after_black, "history": [START, after_red, after_black]}]})["status"][0]
        self.assertFalse(got["over"], "三个不同局面不应判和")
        self.assertIsNone(got["result"])

    @unittest.skipUnless(NODE, MISSING_NODE)
    def test_apply_move_rejects_illegal_pattern(self):
        """applyMove 必须校验走法本身（不能只查“走完是否被将”）。

        回归：帅 d0→c0 走出了九宫，JS 曾返回 ok:true（Python 判定非法）。
        """
        out_of_palace = _pos("3k5/9/9/9/9/9/9/9/9/3K5")
        self.assertNotIn("d0c0", py_move_iccs(Board(out_of_palace)))
        got = engine_request({"apply": [
            {"fen": out_of_palace, "iccs": "d0c0"},
            {"fen": out_of_palace, "iccs": "d0e0"}]})["apply"]
        self.assertFalse(got[0]["ok"], "帅不能走出九宫")
        self.assertTrue(got[1]["ok"], "帅在九宫内的一步应当合法")


if __name__ == "__main__":
    unittest.main()
