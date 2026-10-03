# -*- coding: utf-8 -*-
"""网页客户端测试。

分两层：

1. **静态集成检查**（纯 Python，总是运行）：HTML 与 JS 之间的元素 id 必须一一对应、
   app.js 只使用 logic.js 真正导出的符号、接口与协议字段齐全、不得有内联事件处理器或外部资源；
2. **JS 语法与纯逻辑断言**（需要本机有 ``node``，否则自动跳过）：
   ``node --check`` 两个文件，并用 ``node -e`` 独立断言坐标换算、事件归并的幂等与乱序容错、
   快照应用、可选中判断与合法落点。这样网页逻辑的正确性不需要人工开浏览器就能回归。
"""

import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "xiangqi", "net", "web")
NODE = shutil.which("node")

REQUIRED_ASSETS = ("index.html", "app.js", "logic.js", "style.css")


def read(name):
    with open(os.path.join(WEB_DIR, name), "r", encoding="utf-8") as handle:
        return handle.read()


def run_node(args, timeout=30):
    """在 web 目录下执行 node，返回 (returncode, stdout+stderr)。"""
    result = subprocess.run(
        [NODE] + args, cwd=WEB_DIR, capture_output=True, timeout=timeout,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    return result.returncode, (result.stdout + result.stderr).decode("utf-8", "replace")


class TestWebAssets(unittest.TestCase):
    def test_all_assets_present(self):
        for name in REQUIRED_ASSETS:
            self.assertTrue(os.path.isfile(os.path.join(WEB_DIR, name)), name)

    def test_no_external_resources(self):
        for name in REQUIRED_ASSETS:
            text = read(name)
            for needle in ("http://", "https://", "//cdn", "//unpkg",
                           "@import", "import("):
                self.assertNotIn(needle, text,
                                 "%s 出现了外部资源引用：%s" % (name, needle))

    def test_html_only_local_assets_and_no_inline_handlers(self):
        html = read("index.html")
        for asset in ("app.js", "logic.js", "style.css"):
            self.assertIn(asset, html)
        self.assertIsNone(re.search(r"<[^>]+\son(click|load|error)\s*=", html, re.I),
                          "index.html 不应使用内联事件处理器")


class TestWebIntegration(unittest.TestCase):
    """不依赖浏览器的静态集成检查（最容易出错的地方）。"""

    def setUp(self):
        self.html = read("index.html")
        self.app = read("app.js")
        self.logic = read("logic.js")

    def test_every_referenced_element_id_exists(self):
        ids = set(re.findall(r'id="([^"]+)"', self.html))
        self.assertTrue(ids, "index.html 里没有任何元素 id")
        referenced = set()
        for pattern in (r'getElementById\(\s*[\'"]([^\'"]+)[\'"]\s*\)',
                        r'querySelector\(\s*[\'"]#([^\'"]+)[\'"]\s*\)',
                        r'\$\(\s*[\'"]([^\'"#][^\'"]*)[\'"]\s*\)'):
            referenced |= set(re.findall(pattern, self.app))
        self.assertTrue(referenced, "app.js 没有按 id 取元素？")
        self.assertEqual(sorted(referenced - ids), [],
                         "app.js 引用了 HTML 中不存在的 id")

    def test_app_uses_only_exported_logic_symbols(self):
        exported = set(re.findall(r"^\s*XQ\.([A-Za-z_$][\w$]*)\s*=", self.logic, re.M))
        self.assertTrue(exported, "logic.js 没有导出任何符号")
        used = set(re.findall(r"\bXQ\.([A-Za-z_$][\w$]*)", self.app))
        self.assertEqual(sorted(used - exported), [],
                         "app.js 使用了 logic.js 未导出的符号")

    def test_protocol_endpoints_and_fields_are_used(self):
        for pattern, label in (
                (r"['\"`]?/api/rooms['\"`]?", "创建房间"),
                (r"/join", "入座"),
                (r"/resume", "恢复座位"),
                (r"/events\?since=", "长轮询"),
                (r"/stream\?since=", "SSE"),
                (r"['\"]/ws['\"]", "WebSocket"),
                (r"/move", "走子"),
                (r"/resign", "认输"),
                (r"/new", "新局")):
            self.assertRegex(self.app, pattern, "app.js 未调用接口：%s" % label)
        for field in ("base_seq", "cid", "token", "since", "iccs"):
            self.assertIn(field, self.app, "app.js 未使用协议字段：%s" % field)

    def test_server_message_types_are_handled(self):
        for kind in ("welcome", "events", "snapshot", "ack", "error"):
            self.assertIn("'%s'" % kind, self.app,
                          "app.js 未处理服务器消息类型：%s" % kind)

    def test_localstorage_keys(self):
        for key in ("xq.room", "xq.token", "xq.seq"):
            self.assertIn(key, self.app)

    def test_board_size_respects_viewport_height(self):
        """棋盘尺寸必须同时受视口**高度**约束。

        棋盘是 10 格宽 × 11 格高（8×9 格 + 上下各一格边距）。只按容器宽度算格子大小的话，
        宽窗口会算出 858px 高的棋盘，笔记本视口（约 700~800px）就得滚动才能看到最下一排
        —— 这是实际收到过的反馈，所以在此立个回归守卫。
        """
        self.assertIn("innerHeight", self.app, "app.js 未把视口高度纳入棋盘尺寸计算")
        self.assertIn("availableH", self.app, "app.js 缺少高度预算变量")
        self.assertRegex(self.app, r"Math\.min\(\s*available\s*/\s*10\s*,\s*availableH\s*/\s*11\s*\)",
                         "格子大小应取“宽度允许”和“高度允许”的较小值")

    def test_board_shell_aspect_matches_canvas(self):
        """木框的比例必须与画布一致（10 格宽 : 11 格高），否则底部会被 overflow 裁掉。"""
        css = read("style.css")
        shell = re.search(r"\.board-shell\s*\{(.*?)\}", css, re.S)
        self.assertIsNotNone(shell, "style.css 里找不到 .board-shell 规则")
        self.assertRegex(shell.group(1), r"aspect-ratio:\s*10\s*/\s*11",
                         ".board-shell 的 aspect-ratio 必须是 10/11（与画布一致）")


class TestWebLogicWithNode(unittest.TestCase):
    """需要 node；缺失时自动跳过（不引入任何 npm 依赖）。"""

    @unittest.skipUnless(NODE, "本机没有 node，跳过 JS 检查")
    def test_js_syntax(self):
        for name in ("logic.js", "app.js"):
            code, output = run_node(["--check", name])
            self.assertEqual(code, 0, "%s 语法错误：%s" % (name, output))

    @unittest.skipUnless(NODE, "本机没有 node，跳过 JS 检查")
    def test_logic_module_behaviour(self):
        script = r"""
require('./logic.js');
var XQ = globalThis.XQ;
var out = [];
function ok(name, cond) { out.push((cond ? 'ok ' : 'FAIL ') + name); if (!cond) process.exitCode = 1; }
ok('parseIccs', JSON.stringify(XQ.parseIccs('h2e2')) === JSON.stringify({fr:2,fc:7,tr:2,tc:4}));
ok('toIccs', XQ.toIccs(0, 0) === 'a0');
ok('squareName', XQ.squareName(1, 4) === 'e1');
ok('pieceName K', XQ.pieceName('K') === '帅');
ok('pieceName p', XQ.pieceName('p') === '卒');
ok('sideName', XQ.sideName('r') === '红方');
var snap = { v:1, room:'A7K2QM', seq:2, game_seq:1,
  fen:'rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1',
  side:'r', turn_name:'红方', check:false,
  legal:{ h2:['e2','g2'], b0:['a2','c2'] },
  seats:{ r:{joined:true,connected:true}, b:{joined:true,connected:true} },
  moves:[], last_move:null, result:null, result_text:'', server_ms:1 };
var st = XQ.applySnapshot({ v:1, seq:0, moves:[], legal:{}, fen:'', side:'b' }, snap);
ok('applySnapshot side', st.side === 'r');
ok('applySnapshot seq', st.seq === 2);
ok('applySnapshot legal', !!(st.legal && st.legal.h2));
var ev = { seq:3, t:'move', side:'r', iccs:'h2e2', chinese:'炮二平五', fen:'x', check:false, from:'h2', to:'e2' };
var once = XQ.applyEvents(st, [ev]);
ok('applyEvents move', once.moves.length === 1 && once.seq === 3);
var twice = XQ.applyEvents(once, [ev]);
ok('applyEvents idempotent', twice.moves.length === 1 && twice.seq === 3);
var stale = XQ.applyEvents(once, [{ seq:2, t:'presence', side:'b' }]);
ok('applyEvents ignores stale', stale.moves.length === 1 && stale.seq === 3);
var ended = XQ.applyEvents(once, [{ seq:4, t:'end', winner:'r', reason:'黑方被将死', result_text:'红方胜' }]);
ok('applyEvents result', !!(ended.result && ended.result.winner === 'r'));
var fresh = XQ.applyEvents(ended, [{ seq:5, t:'new', side:'b' }]);
ok('applyEvents new', fresh.moves.length === 0 && !fresh.result);
ok('legalFrom', JSON.stringify(XQ.legalFrom(st, 'h2')) === JSON.stringify(['e2','g2']));
ok('canSelect my turn', XQ.canSelect(st, 'h2', 'r') === true);
ok('canSelect wrong side', XQ.canSelect(st, 'h9', 'r') === false);
ok('canSelect not my turn', XQ.canSelect(st, 'h9', 'b') === false);
// 对手还没入座（等待阶段）时不允许选中棋子
var waiting = XQ.applySnapshot(st, { v:1, seq:2, side:'r', legal:{h2:['e2']},
  seats:{ r:{joined:true,connected:true}, b:{joined:false,connected:false} },
  moves:[], result:null, check:false, fen: snap.fen });
ok('canSelect waiting', XQ.canSelect(waiting, 'h2', 'r') === false);
// 走子必须发完整的四字符 ICCS（历史上曾只发目标格）
ok('move iccs is 4 chars', /^[a-i][0-9][a-i][0-9]$/.test(XQ.iccsOf(XQ.parseIccs('h2e2'))));
console.log(out.join('\n'));
"""
        code, output = run_node(["-e", script])
        self.assertEqual(code, 0, "logic.js 断言失败：\n%s" % output)
        self.assertIn("ok applyEvents idempotent", output)
        self.assertNotIn("FAIL", output)


if __name__ == "__main__":
    unittest.main()
