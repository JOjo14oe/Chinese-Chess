#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""中国象棋 · 本地离线版 —— 统一入口。

用法::

    python main.py                # 图形界面（tkinter）
    python main.py --side b       # 图形界面，人机对战执黑
    python main.py --cli          # 命令行界面
    python main.py --cli --mode human --level hard
    python main.py --serve        # 联机对战服务器（开两个浏览器窗口对下）
    python main.py --serve --tunnel        # 联机 + 自动拉起公网隧道（发链接给国外朋友）
    python main.py --serve --tunnel --open # 同上，并自动打开浏览器
    python main.py --serve --host 0.0.0.0 --port 8000    # 允许跨国/局域网访问
    python main.py --selftest     # 自检（着法生成基准 + 规则检查），不进入游戏

运行环境：Python 3.8+，仅标准库。网页前端是**同一个页面**：
单机（人机/双人，纯浏览器内运行、不联网）与联机（房间对战）两种模式共用它。
``--serve`` 只监听端口供浏览器连接；``--tunnel`` 才会去下载/运行 cloudflared。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PERFT_REFERENCE = {1: 44, 2: 1920, 3: 79666, 4: 3290240}

MATE_FEN = "R3k4/R8/9/9/9/9/9/9/9/3K5 b - - 0 1"          # 黑方被将死
STALEMATE_FEN = "4k4/3R1R3/9/9/9/9/9/9/9/3K5 b - - 0 1"   # 黑方困毙
MATE_IN_ONE_FEN = "4k4/R8/9/9/9/9/9/9/9/R2K5 w - - 0 1"   # 红方一步杀


def run_selftest(deep=False):
    """自检；返回进程退出码。"""
    from xiangqi.ai import Engine, MATE
    from xiangqi.board import Board
    from xiangqi.constants import RED, START_FEN
    from xiangqi.notation import move_to_chinese, parse_chinese

    failures = []

    def check(name, condition, detail=""):
        status = "OK  " if condition else "FAIL"
        if not condition:
            failures.append(name)
        print("[%s] %s%s" % (status, name, ("  <- " + detail) if detail else ""))

    # 1. 着法生成基准（perft）：与公开基准值逐一比对
    depths = [1, 2, 3] + ([4] if deep else [])
    for depth in depths:
        got = Board(START_FEN).perft(depth)
        expected = PERFT_REFERENCE[depth]
        check("perft(%d) = %d" % (depth, expected), got == expected,
              "实际 %d" % got)

    # 2. 开局着法数量
    board = Board(START_FEN)
    count = len(list(board.gen_legal_moves()))
    check("开局合法着法 = 44", count == 44, "实际 %d" % count)

    # 3. FEN 往返
    check("FEN 往返一致", Board(START_FEN).to_fen() == START_FEN)

    # 4. 将死 / 困毙
    over, winner, reason = Board(MATE_FEN).status()
    check("将死判定（红胜）", over and winner == RED and "将死" in reason, reason)
    over, winner, reason = Board(STALEMATE_FEN).status()
    check("困毙判定（红胜）", over and winner == RED and "困毙" in reason, reason)

    # 5. 中文记谱往返（开局全部合法着法）
    board = Board(START_FEN)
    bad = []
    for move in board.gen_legal_moves():
        text = move_to_chinese(board, move)
        if parse_chinese(board, text) != move:
            bad.append("%s->%s" % (text, move.iccs()))
    check("中文记谱往返（开局 %d 步）" % count, not bad, ", ".join(bad[:5]))

    # 6. 引擎能找到一步杀
    engine = Engine("normal")
    board = Board(MATE_IN_ONE_FEN)
    move = engine.choose_move(board, "normal")
    if move is None:
        check("引擎一步杀", False, "未返回着法")
    else:
        text = move_to_chinese(board, move)
        board.make_move(move)
        over, winner, _ = board.status()
        check("引擎一步杀（%s）" % text, over and winner == RED)
        check("引擎分值达到杀棋", engine.last_score >= MATE - 100,
              "分值 %d" % engine.last_score)

    print()
    if failures:
        print("自检未通过：%d 项失败 -> %s" % (len(failures), ", ".join(failures)))
        return 1
    print("自检全部通过。")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="中国象棋（本地离线，不访问网络）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cli", action="store_true", help="使用命令行界面")
    parser.add_argument("--gui", action="store_true", help="使用图形界面（默认）")
    parser.add_argument("--serve", action="store_true",
                        help="启动联机服务器（浏览器对战，可开两个窗口）")
    parser.add_argument("--tunnel", action="store_true",
                        help="联机 + 公网隧道：自动准备 cloudflared、拉起隧道并打印公网地址")
    parser.add_argument("--open", action="store_true",
                        help="配合 --tunnel：就绪后自动用默认浏览器打开页面")
    parser.add_argument("--protocol", choices=["quic", "http2"], default=None,
                        help="隧道协议（默认让 cloudflared 自己选；UDP 被封时用 http2）")
    parser.add_argument("--host", default=None,
                        help="联机服务器监听地址（默认 127.0.0.1，跨国对战用 0.0.0.0）")
    parser.add_argument("--port", type=int, default=None,
                        help="联机服务器端口（默认 8000）")
    parser.add_argument("--selftest", action="store_true", help="运行自检后退出")
    parser.add_argument("--deep", action="store_true",
                        help="自检时包含 perft(4)（较慢，约 40 秒）")
    parser.add_argument("--fen", default=None, help="起始局面 FEN")
    parser.add_argument("--mode", choices=["ai", "human"], default="ai",
                        help="ai=人机对战，human=双人对战")
    parser.add_argument("--side", choices=["r", "b"], default="r",
                        help="人机模式下己方执子")
    parser.add_argument("--level", choices=["easy", "normal", "hard", "master"],
                        default="normal", help="AI 难度")
    parser.add_argument("--verbose", action="store_true",
                        help="联机服务器打印每个请求")
    args = parser.parse_args(argv)

    if args.selftest:
        return run_selftest(deep=args.deep)

    if args.tunnel:
        from xiangqi.net.tunnel import serve_public
        return serve_public(host=args.host, port=args.port, verbose=args.verbose,
                            open_browser=args.open, protocol=args.protocol)

    if args.serve:
        from xiangqi.net import protocol as net_protocol
        from xiangqi.net.server import main as serve_main
        serve_args = ["--host", args.host or net_protocol.DEFAULT_HOST,
                      "--port", str(args.port or net_protocol.DEFAULT_PORT)]
        if args.verbose:
            serve_args.append("--verbose")
        return serve_main(serve_args)

    common = []
    if args.fen:
        common += ["--fen", args.fen]
    common += ["--mode", args.mode, "--side", args.side, "--level", args.level]

    if args.cli:
        from xiangqi.cli import main as cli_main
        return cli_main(common)

    try:
        from xiangqi.gui import main as gui_main
    except ImportError as error:  # pragma: no cover
        print("无法加载图形界面（%s），改用命令行界面。" % error)
        from xiangqi.cli import main as cli_main
        return cli_main(common)
    return gui_main(common)


if __name__ == "__main__":
    raise SystemExit(main())
