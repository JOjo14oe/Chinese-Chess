# -*- coding: utf-8 -*-
"""联机对战子包：本地服务器 + 浏览器客户端 + 事件协议。

本子包是整个项目中**唯一**使用网络模块（``socket`` / ``http.server``）的地方；
``board`` / ``ai`` / ``cli`` / ``gui`` 等核心模块仍然完全不联网。
所有服务器端代码仅依赖 Python 标准库，不需要 pip 安装任何东西。

``protocol``（协议）与 ``rooms``（权威棋局状态机）不碰 socket，可以单独测试；
``server`` 才负责 HTTP/SSE/WebSocket 传输，需要时才导入。
"""

from .protocol import DEFAULT_HOST, DEFAULT_PORT, PROTOCOL_VERSION

__all__ = ["PROTOCOL_VERSION", "DEFAULT_HOST", "DEFAULT_PORT"]
