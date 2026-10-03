# -*- coding: utf-8 -*-
"""中国象棋 · 本地离线实现。

纯 Python 标准库实现（仅使用 ``random``/``re``/``tkinter`` 等本地模块），
**不访问任何网络**。包含：

* :mod:`xiangqi.board` —— 完整行棋规则、着法生成、将军/将死/困毙/重复判定
* :mod:`xiangqi.notation` —— ICCS 坐标记谱与中文纵线记谱互转
* :mod:`xiangqi.ai` —— 本地 Alpha-Beta 引擎（四档难度）
* :mod:`xiangqi.cli` —— 命令行界面
* :mod:`xiangqi.gui` —— tkinter 图形界面
"""

from .ai import Engine
from .board import Board, IllegalMove
from .constants import BLACK, RED, START_FEN, Move

__version__ = "1.0.0"
__all__ = ["Board", "IllegalMove", "Engine", "Move", "RED", "BLACK", "START_FEN"]
