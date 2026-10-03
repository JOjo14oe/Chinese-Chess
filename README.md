# 中国象棋 · 本地实现（Xiangqi）

[![CI](https://github.com/JOjo14oe/Chinese-Chess/actions/workflows/ci.yml/badge.svg)](https://github.com/JOjo14oe/Chinese-Chess/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![License](https://img.shields.io/badge/License-MIT-green)

一个用 **纯 Python 标准库** 实现的中国象棋（象棋）程序：完整的行棋规则引擎、
中文记谱、本地 AI 引擎、命令行 + 图形（tkinter）两套界面，以及**同一台机器上开两个
浏览器窗口对战**的联机服务器（也可放到公网跨国对弈）。

> **没有第三方依赖**：不需要 pip install 任何东西；单机模式（GUI/CLI/AI）完全不联网；
> 联机模式只是自己监听一个端口，由 Python 标准库的 `http.server` + 手写 WebSocket 提供服务，
> **服务进程本身从不主动对外发起请求**；不需要账号、不依赖云。
> 只有你在公网暴露时（§11），才由可选的隧道工具 `cloudflared` 主动连到 Cloudflare 边缘。
> `tests/test_offline.py` 会静态扫描源码：核心模块不得出现任何网络导入，
> 联机层只能使用标准库，网页客户端不得引用任何外部资源。

---

## 1. 功能特性

| 模块 | 内容 |
|---|---|
| 规则引擎 | 七种棋子的完整走法、蹩马腿、塞象眼、炮架、九宫限制、相/象不过河、兵/卒过河横走、飞将照面、将军/应将、将死、困毙、三次重复、60 回合无吃子和棋 |
| 记谱 | ICCS 坐标记谱（`h2e2`）与中文纵线记谱（`炮二平五`）双向互转，支持 `前/后/中` 消歧 |
| 本地 AI | Alpha-Beta 负极大值搜索 + 吃子延伸（quiescence）+ 子力/位置评估，四档难度、迭代加深、时间上限 |
| 命令行界面 | 文本棋盘、中文记谱提示、悔棋、提示、难度/模式切换、着法记录、存/读档（本地文本文件） |
| 图形界面 | tkinter 绘制棋盘（楚河汉界、九宫斜线、兵炮位标记）、点击行棋、合法着法提示、悔棋、提示、认输、翻转、着法列表、FEN 复制 |
| 联机对战 | 房间号入座、浏览器棋盘、WebSocket（SSE / 长轮询自动降级）、断线重连补发、幂等重发、认输/新局；为跨国高延迟链路设计（见 §10） |
| 单机 / 联机合一 | **同一套网页前端**同时提供两种模式：单机（人机 / 同屏双人，规则引擎与 AI 全在浏览器内，直接双击 `xiangqi/net/web/index.html` 也能玩）与联机（房间对战，服务器权威判定） |
| 一键联机 | 本地离线版直接开联机：`python main.py --serve --tunnel`，或图形界面点「联机对战」——自动准备并校验 cloudflared、拉起隧道、给出可发给朋友的公网地址 |
| 公网/跨国部署 | 一键脚本 `tools\serve-public.cmd`：自动准备 cloudflared（校验 Cloudflare 签名）→ 启动本机服务 → 建立隧道 → 打印给国外朋友用的 https 地址；也支持局域网直连（见 §11 与 [docs/公网对战部署.md](<docs/公网对战部署.md>)） |
| 测试 | 291 项测试（规则/记谱/引擎/命令行/图形界面/房间状态机/WebSocket/端到端/网页客户端/依赖边界/随机不变量），含公开 perft 基准比对 |

## 2. 运行环境与依赖（自包含说明）

**从 GitHub 克隆下来就能跑，不需要我（作者）的任何环境**。依赖只有下面这些：

| 依赖 | 是否必需 | 说明 |
|---|---|---|
| **Python 3.9+** | ✅ 必需 | 只用标准库；开发与 CI 验证 3.9 / 3.12 / 3.13 |
| tkinter | 仅图形界面 | Windows/macOS 官方 Python 自带；Debian 系 `apt install python3-tk`。缺失时 `main.py` 自动回退命令行 |
| 浏览器 | 仅联机对战 | Chrome / Edge / Firefox 等，前端由本仓库自带、零构建 |
| Node.js | 可选 | 只有 `tests/test_web.py` 里的 JS 语法/逻辑断言需要；没有则自动跳过 |
| cloudflared | 仅公网暴露 | `tools/serve-public.*` 会自动下载并校验（Windows 校验 Authenticode 签名），不提交进仓库 |
| **第三方 Python 包 / pip install** | ❌ 不需要 | 一个都不需要（`tests/test_offline.py` 会扫描源码强制这一点） |
| **数据库 / 账号 / API Key / 云服务** | ❌ 不需要 | 房间与对局只在内存里；单机模式全程不联网 |
| Docker / npm / 构建步骤 | ❌ 不需要 | 纯 Python + 原生网页 |

**仓库里没有什么**（已在 `.gitignore` 中排除）：`tools/bin/`（下载来的 cloudflared 二进制）、
`tools/public-url.txt`（隧道临时地址）、`__pycache__/`、任何对局数据。
所以克隆后体积很小，第一次公网部署时脚本会按需下载隧道工具。

```bat
:: 克隆后三步
python main.py --selftest      :: 1) 自检：perft 基准 + 规则 + 记谱 + 引擎（约 1 秒）
python -m unittest discover -s tests -t .    :: 2) 291 项测试
python main.py                 :: 3) 玩：图形界面（对 AI）／--cli 命令行／--serve 浏览器对战
```

> Windows 上若控制台中文乱码，先 `set PYTHONIOENCODING=utf-8`（CI 里已设置）。
> GitHub Actions（[.github/workflows/ci.yml](<.github/workflows/ci.yml>)）会在
> ubuntu + windows、Python 3.9/3.12/3.13 上跑同一套测试与自检，另有 perft(4) 深度校验，
> 用来证明“干净机器上也能跑通”。

## 3. 快速开始

在本目录（即 `main.py` 所在目录）执行：

```bat
python main.py                 :: 图形界面（默认），人机对战执红
python main.py --side b        :: 图形界面，执黑（棋盘自动翻转）
python main.py --level hard    :: 难度：easy / normal / hard / master
python main.py --cli           :: 命令行界面
python main.py --cli --mode human --level normal
python main.py --serve         :: 联机服务器：开两个浏览器窗口对战（默认 127.0.0.1:8000）
python main.py --serve --tunnel        :: 联机 + 自动拉起公网隧道，打印可发给朋友的 https 地址
python main.py --serve --tunnel --open :: 同上，并自动打开浏览器
python main.py --selftest      :: 自检：着法生成基准 + 规则检查，不进入游戏
python main.py --selftest --deep   :: 自检并包含 perft(4)（约 40 秒）
```

也可以直接运行模块入口：

```bat
python -m xiangqi.cli          :: 命令行
python -m xiangqi.gui          :: 图形界面
python -m xiangqi.net.server   :: 联机服务器（等价于 main.py --serve）
```

### 3.1 命令行操作

直接输入着法即可行棋，支持两种记谱：

```
炮二平五          # 中文纵线记谱（红方纵线由右至左为一~九）
马8进7            # 黑方纵线由右至左为 1~9
前车进一          # 同一纵线有两个同类子时用 前/后/中 区分
h2e2              # ICCS 坐标：列 a-i，行 0-9，0 为红方底线
```

其他命令：`board`（显示棋盘）、`moves`（着法记录）、`fen`、`new`、`undo`、
`hint`（引擎建议）、`level easy|normal|hard|master`、`mode ai|human`、
`side r|b`、`save <文件>`、`load <文件>`、`help`、`quit`。

### 3.2 图形界面操作

* **走棋**：点自己的棋子 → 蓝色圈选中并显示全部合法落点（绿点/绿圈）→ 点目标点落子
* **按钮**：`新局`、`悔棋`（人机模式下退回到自己走子的局面）、`提示`（本地引擎给出建议并用紫色虚线标出）、`认输`、`翻转`
* **模式**：人人对战 / 人机对战（执红）/ 人机对战（执黑）
* **难度**：入门（深度 2，约 1.5 秒）/ 普通（深度 4，约 3 秒）/ 较难（深度 6，约 5 秒）/ 大师（深度 8，约 12 秒）
* 右侧显示当前轮到谁、是否被将军、对局结果、中文记谱着法列表，并可复制当前局面 FEN
* 被将军时，九宫内的将/帅会被红圈标出

> AI 为纯 Python 实现，`较难`/`大师` 档每步可能需要数秒；搜索在后台线程进行，界面不会卡死。

## 4. 已实现的规则

坐标约定：`board[rank][file]`，`rank 0` 为红方底线（下方），`rank 9` 为黑方底线；
`file 0` 为 a 列（红方视角最左，即红方九路）；红方向 rank 增大方向前进；
河界在 `rank 4` 与 `rank 5` 之间；红方九宫 `rank 0-2 × file 3-5`，黑方九宫 `rank 7-9 × file 3-5`。

| 棋子 | 走法 |
|---|---|
| 帅 / 将 K | 九宫内一步直行；**双方将帅不得在同一纵线上照面**（照面视为被将军） |
| 仕 / 士 A | 九宫内一步斜行 |
| 相 / 象 B | 斜走两格，**象眼**被占则不能走，**不得过河** |
| 马 N | 走“日”字，**马腿**（先直行的那一格）被占则不能走 |
| 车 R | 直线任意格，不得越子 |
| 炮 C | 不吃子时同车；**吃子必须恰好隔一个棋子**（炮架），隔 0 个或 2 个都不能吃 |
| 兵 / 卒 P | 向前一步；**过河后可左右一步**；永远不能后退 |

胜负与和棋：

* 任何使己方将/帅被将军（含造成照面）的着法都是**非法**着法，程序不会生成也不会接受；
* 轮到走棋一方**无着可走即判负**：被将军称为 **将死**，未被将军称为 **困毙**（象棋规则中困毙同样判负，不是和棋）；
* **同一局面（含走子方）出现三次**：若其中单方连续将军则为 **长将判负**，否则判和；
* **连续 60 回合（120 个半回合）无吃子**判和。

## 5. 目录结构

```
xiangqi/
├── main.py                 # 统一入口（GUI / CLI / 联机服务器 / 自检）
├── README.md
├── xiangqi/
│   ├── __init__.py
│   ├── constants.py        # 坐标约定、棋子编码、方向表、中文名称
│   ├── board.py            # 棋盘状态、着法生成、合法性、将军/将死/困毙/重复判定、FEN
│   ├── notation.py         # ICCS 与中文纵线记谱互转
│   ├── ai.py               # Alpha-Beta 引擎（评估 + 搜索 + 难度）
│   ├── cli.py              # 命令行界面
│   ├── gui.py              # tkinter 图形界面
│   └── net/                # 联机层（唯一允许用 socket 的地方，仍只用标准库）
│       ├── protocol.py     # 消息/事件/错误码/上限（与传输无关）
│       ├── rooms.py        # 权威棋局状态机：座位、事件日志、幂等、重连
│       ├── ws.py           # RFC 6455 WebSocket 服务端（手写帧编解码）
│       ├── server.py       # http.server 路由：静态资源 / REST / SSE / WebSocket
│       ├── tunnel.py       # 一键联机：自动准备 cloudflared + 拉起隧道 + 解析公网地址
│       └── web/            # 浏览器客户端（零依赖、零构建、无外部资源）
│           ├── index.html
│           ├── app.js      # 画棋盘、点击行棋、三传输降级、重连
│           ├── logic.js    # 纯逻辑（坐标、事件归并、可选中判断）便于测试
│           ├── engine.js   # 浏览器内规则引擎 + 中文记谱 + 简易 AI（单机模式用，与 Python 逐条对拍）
│           └── style.css
├── tools/                  # 部署工具（不属于业务代码）
│   ├── serve-public.cmd    # Windows：双击即可（启动服务 + Cloudflare 隧道 + 打印公网地址）
│   ├── serve-public.ps1    # 上面那个 cmd 的实现（自动下载并校验 cloudflared 签名）
│   ├── serve-public.sh     # Linux / macOS / WSL 的等价脚本
│   └── bin/                # 按需下载的 cloudflared（.gitignore 已排除，不进仓库）
├── docs/
│   └── 公网对战部署.md      # 跨国暴露：一键脚本、实测数据、安全清单、故障排查、验收清单
├── .github/workflows/ci.yml # ubuntu+windows × Python 3.9/3.12/3.13，另含 perft(4) 深度校验
├── .gitignore
├── LICENSE                 # MIT（请把 <YOUR NAME> 换成你的名字/ID）
└── tests/
    ├── helpers.py            # 测试工具（由棋子布局构造局面）
    ├── test_board.py         # 行棋规则、胜负判定、重复与自然限着
    ├── test_notation.py      # 记谱解析/生成与往返一致性
    ├── test_ai.py            # 引擎：一步杀、评估对称性、着法合法性
    ├── test_cli.py           # 命令行：着法输入、悔棋、存/读档、终局处理
    ├── test_perft.py         # perft 基准比对
    ├── test_invariants.py    # 随机对局不变量（散列/FEN/push-pop 可逆）
    ├── test_net_rooms.py     # 联机房间状态机（轮次/幂等/过期/重连/事件日志）
    ├── test_net_ws.py        # RFC 6455 帧与握手（客户端组帧手写作预言机）
    ├── test_net_server.py    # 端到端：REST / 长轮询 / SSE / WebSocket / 重连
    ├── test_web.py           # 网页客户端：HTML/JS 集成检查 +（有 node 时）JS 语法与逻辑断言
    ├── test_offline.py       # 依赖边界自证：核心离线、联机层仅标准库、网页零外部资源
    └── test_gui.py           # 图形界面冒烟测试（默认跳过）
```

## 6. 测试与验证

```bat
:: 全部测试（默认跳过 perft(4) 与图形界面测试）
python -m unittest discover -s tests -t . -v

:: 打开图形界面冒烟测试（PowerShell 语法；cmd 用 set XQ_GUI_TEST=1）
$env:XQ_GUI_TEST='1'; python -m unittest discover -s tests -t . -v

:: 打开 perft(4)（约 40 秒）
$env:XQ_PERFT4='1'; python -m unittest discover -s tests -t . -p "test_perft.py" -v
```

**着法生成基准（perft）**：以公开的中国象棋 perft 数据严格校验走子规则，
`tests/test_perft.py` 与 `python main.py --selftest` 都会比对：

| 深度 | 基准值 | 本程序实测 |
|---|---|---|
| perft(1) | 44 | **44** ✅ |
| perft(2) | 1920 | **1920** ✅ |
| perft(3) | 79666 | **79666** ✅ |
| perft(4) | 3290240 | **3290240** ✅（约 42 秒） |

当前状态：**242 项测试全部通过**（`Ran 242 tests ... OK`）。其中 5 项图形界面冒烟测试需要
`XQ_GUI_TEST=1`，`perft(4)` 需要 `XQ_PERFT4=1`，网页的 JS 语法/逻辑断言需要本机有 `node`
（没有则自动跳过），未开启时自动跳过。
联机相关共 158 项：房间状态机 37、RFC 6455 / WebSocket 73、端到端（真实 TCP）29、
网页客户端 10、依赖边界 9。

### 6.1 开发期的独立交叉验证

除上述自带测试外，本项目在开发期还用**另写的一套独立规则实现**（不复用本程序任何代码，
含独立的着法生成、被攻击格、飞将照面与合法性判定）作为“裁判”做对抗性交叉验证，
结论为 **PASS-WITH-CONCERNS：任何合法局面下均未发现规则缺陷**。核心证据：

* **着法生成比对**：随机对局局面 1050 个、随机合理摆子局面 2500 个，另加 891 + 423 个局面，
  共 **4864 个局面**，本程序生成的合法着法集合与独立实现**完全一致（0 处差异）**——
  既没有多出非法着法，也没有遗漏合法着法；
* **46 项手工推导的走法几何测试**全部通过（九宫、象眼与不过河、蹩马腿四个方向、车不越子、
  炮恰好一个炮架（0/1/2 个炮架、友方炮架、贴身吃子）、兵过河前后、飞将牵制等）；
* **记谱**：标准开局着法与 `前/后/中`、双线同称谓消歧全部正确；在 23118 着与 26787 着
  （含 1200 个刻意堆子的模糊局面）上“生成→解析”恒等，**0 失败**；
* **FEN**：159 个随机局面往返完全一致（棋盘/走子方/计数器/散列/文本）；40 个由 FEN 载入的
  局面与实走走出的同一局面行为一致（合法着法集合、胜负判定、perft(2) 全同）；
* **push/pop**：1200 次以上单步与嵌套循环，棋盘、走子方、计数器、散列、重复局面计数完全还原；
* **引擎**：独立构造的一步杀在入门/普通/较难三档均走出杀着；四档从开局都返回合法着法；
  每次 `choose_move` 前后调用方棋盘**逐位相同**；easy 档 240 个半回合 + 中局 120 个半回合 +
  60 个半回合自我对弈，每一步都由独立实现对账，**全部合法**、无自杀、无照面、无返回 None；
* **非法输入**：8 种畸形 FEN、越界着法、非法着法、空历史悔棋均被正确拒绝，且失败后原局面
  （含散列与重复局面表）保持不变。

该轮对抗验证共提出 3 个非规则问题，均已在当前版本处理：

| # | 问题 | 处理 |
|---|---|---|
| 1 | 载入畸形 FEN 时“先改棋盘、后校验”，失败后留下新棋盘 + 旧散列，重复局面判定会永久失真 | 改为**全部校验通过后一次性提交**，并加入回归测试 `test_set_fen_rejects_without_corrupting_state` |
| 2 | 同一纵线 ≥6 个同类子时记谱会生成自己解析不了的着法（前缀缺“五”） | 前缀类别扩展到 `前/中/后/二~八`，并加入回归测试 |
| 3 | FEN 未校验将/帅数量：无帅局面会被当成“红方被将死”，双帅局面第二个帅对将军判定不可见 | 载入时要求**每方恰有一个将/帅**，否则拒绝；见 §7 |

另有 1 项文档事项一并修复：**长将判定原先只在使用 `make_move()` 时生效**（`push()` 不记录
将军标记），现在 `status()` 会在需要时回放补齐该标记，两种走子方式结果一致。

## 7. 规则取舍与已知限制（如实说明）

* **长捉、长兑等复杂判负条款未实现**：程序实现了“三次重复局面”判定，并在此之上实现
  单方连续将军（长将）判负；中国象棋竞赛规则中关于长捉、长兑、一将一捉等的细则没有实现，
  这类局面会按“判和”处理。长将判定与走子方式无关（`make_move()` 与 `push()` 结果一致）。
* **FEN 会做基本结构校验**：每方必须恰有一个将/帅，走子方与回合计数必须合法，否则拒绝载入
  且不改变原局面；但**不强制将/帅位于九宫内**，以便摆出用于测试/讲棋的构造局面。
* **自然限着按 60 回合无吃子判和**：未实现部分规则中“兵/卒未过河”等的附加条件。
* **AI 为纯 Python 实现**，棋力约相当于入门到业余水平；有子力/位置评估、吃子延伸，
  但没有置换表、开局库、残局库。
* **图形界面（tkinter）为单机界面**，本身不含联机功能；联机走浏览器客户端（见 §10）。
* 图形界面使用 tkinter；若环境中缺少 tkinter，`main.py` 会自动回退到命令行界面。
* **联机房间保存在内存中**：服务器重启即结束所有对局；空闲房间 6 小时后回收。

## 8. 依赖边界与离线保证

`tests/test_offline.py` 会静态扫描源码，强制以下三条边界：

* **单机核心完全离线**：`board` / `notation` / `ai` / `cli` / `gui` 以及 `main.py`（模块级）
  不出现 `socket`、`ssl`、`urllib`、`http`、`webbrowser` 等任何网络导入——单人下棋、
  AI 对战、图形界面在断网环境下功能完整；
* **联机层只用标准库**：`xiangqi.net` 是唯一允许使用 `socket` / `http.server` 的模块，
  且只允许标准库导入（禁止 `requests` / `flask` / `websockets` / `aiohttp` 等第三方包）；
* **网页客户端自包含**：`xiangqi/net/web/` 下不得出现 `http://` / `https://` / CDN 引用，
  页面只加载同源资源，可完全离线加载。

联机模式只是本机（或你自己的服务器）监听一个端口，**服务进程本身从不主动对外发起请求**
（公网暴露见 §11：那是可选的 `cloudflared` 部署工具在向外连 Cloudflare），
不需要账号、不需要 API Key、不依赖云；房间号与座位令牌就是全部凭据。

## 9. 许可

个人学习用途，可自由修改使用。

---

## 10. 联机对战（同一台机器两个浏览器窗口 / 跨国）

### 10.1 启动

```bat
python main.py --serve                      :: http://127.0.0.1:8000
python main.py --serve --port 9000          :: 换端口
python main.py --serve --host 0.0.0.0       :: 允许局域网/公网访问（跨国对战时用）
python main.py --serve --verbose            :: 打印每个请求（排障用）
```

然后：

1. 浏览器打开 **http://127.0.0.1:8000/** → 窗口 A 点「创建房间」，得到 6 位房间号（如 `A7K2QM`）；
2. 再开一个窗口（或另一台机器/另一个国家）打开同一地址 → 输入房间号点「加入房间」；
   也可以直接把「复制房间链接」得到的 `…/?room=A7K2QM` 发给对方，打开即自动入座；
3. 红方先行，点自己的棋子 → 绿色圆点显示合法落点（由服务器下发）→ 点落点走子。

刷新页面、断网重连都会**自动恢复同一座位与同一棋局**（房间号 + 座位令牌存在浏览器
`localStorage` 中）；对手上线/掉线只影响在线状态，不会让在途的一步棋作废。

### 10.2 架构

```
  浏览器 A（红）┐                                  ┌── 静态资源：index.html / app.js / logic.js / style.css
  浏览器 B（黑）┼── WebSocket ──┐                  │                （零依赖、零构建、无 CDN）
  浏览器 A/B   ┼── SSE ────────┼──→ xiangqi.net.server ──┤
               └── 长轮询+POST ─┘   （标准库 http.server）│
                                                       └── xiangqi.net.rooms ──→ board.py（既有规则引擎）
                                                           权威棋局状态机 + 事件日志
```

* **服务器权威**：所有规则判定都在服务器上复用既有引擎完成，客户端**不实现任何规则**，
  只做画棋盘与事件归并；合法着法由服务器随局面一起下发（`snapshot.legal`）。
* **协议与传输解耦**：同一套 JSON 事件在 WebSocket / SSE / 长轮询上完全一致，
  换传输不断局（`xiangqi/net/protocol.py`）。

### 10.3 接口一览

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/` `/app.js` `/logic.js` `/style.css` | 网页客户端（同源、无外部资源） |
| GET | `/api/health` | 服务器状态（房间数、在线玩家、连接数） |
| POST | `/api/rooms` | 创建房间，返回 `{room, side, token, snapshot}` |
| POST | `/api/rooms/{code}/join` | 入座（带 `token` 则为重连该座位） |
| POST | `/api/rooms/{code}/resume` | 校验令牌并补齐 `since` 之后的事件 |
| POST | `/api/rooms/{code}/move` | 走子：`{token, iccs, base_seq, cid}` |
| POST | `/api/rooms/{code}/resign` / `/new` | 认输 / 重开一局 |
| GET | `/api/rooms/{code}` | 公开摘要（不含任何令牌） |
| GET | `/api/rooms/{code}/events?since&wait` | 长轮询取事件（兜底传输） |
| GET | `/api/rooms/{code}/stream?since&token` | SSE 推送（`X-Accel-Buffering: no` + 心跳） |
| GET | `/ws?room&token&since` | WebSocket（首选传输，握手由 `xiangqi/net/ws.py` 手写实现） |

服务器 → 客户端的消息：`welcome` / `events` / `snapshot` / `ack` / `error` / `pong` / `heartbeat`；
事件类型：`start` / `join` / `presence` / `move` / `resign` / `end` / `new`，
每条都带全局递增的 `seq`。

### 10.3.1 协议契约（逐条写死，客户端可直接依赖）

1. **增量与快照的关系**：`/resume`、`/events`、SSE 批量中的 `events[i].seq` 一定满足
   `since < seq <= snapshot.seq`；**位置过旧**（`since` 早于保留窗口，或大于服务器当前最新
   `seq`）时**只回 `snapshot`、`events` 为空**，绝不混着回。客户端应「先按序合并 events，
   再用 snapshot 覆盖派生状态」。
2. **`seq` 严格每事件 +1、不跳号**：`start` / `join` / `presence` / `move` / `end` / `resign` /
   `new` 都占一个 seq 并作为事件下发，没有“隐形序号”。只要该 seq 仍在保留窗口内
   （每房间 4096 条），`GET /events?since=seq-1` 一定能拿到它，否则回 `snapshot`。
   因此客户端可以放心地把「最后一个事件的 seq」同时当作下次的 `since` 与 `base_seq`。
3. **SSE 首帧**：连接后立刻发 `event: hello`，其 data 是 welcome 形状
   `{"v":1,"t":"welcome","room","side","seq","snapshot"}`（可直接用于首屏渲染）；
   之后按批发 `event: events` / `event: snapshot`，每条都带 `id: <seq>`（浏览器重连时
   会用 `Last-Event-ID` 请求头发回来，服务器据此校正 `since`）；连接开始时另发
   `retry: 3000`，`:hb` 注释心跳每 15 秒一次。
4. **长轮询**：`wait` 可缺省、可为非数字（默认/上限均为 25 秒）；超时返回
   `{"v":1,"t":"events","events":[],"seq":N,"timeout":true}`（`timeout` 仅供调试，客户端可忽略）。
5. **所有应答都是 JSON**：包括 4xx/5xx、未知方法（501）、畸形请求——`http.server` 默认的
   HTML 错误页已被替换。错误体统一为
   `{"ok":false,"error":"<code>","msg":"<中文说明>"}`，`error` ∈
   `room_not_found / bad_room_code / room_full / forbidden / not_your_turn / illegal_move / stale / game_over / too_large / bad_request`。
6. **SSE 单连接最长 30 分钟**：到期前会补发一个 `event: snapshot`，然后**正常关闭**连接；
   浏览器 EventSource 自动重连并带上 `Last-Event-ID`，对局不受影响。
7. **`GET /api/rooms/{code}`** 的 `seq` 与 `/events?since=` 是同一命名空间（可用于对齐），
   其中 `moves` 是**着法数量**而非数组；要着法明细请用 `snapshot.moves`。
8. **同一令牌可以同时开多个标签页**（不做互踢）：服务器按该座位的**存活连接数**统计在线，
   只有最后一条连接断开才算离线；多标签页会各自收到事件流。

### 10.4 跨国（高延迟/易丢包）下为什么不会出错

| 机制 | 作用 |
|---|---|
| **事件序号 `seq`** | 每次状态变化都是一个带序号的不可变事件；客户端只记住 `since`，重连时精确补齐缺失事件；若位置已超出保留窗口（每房间 4096 条），服务器回一整个快照 |
| **幂等键 `cid`** | 每个走子请求带客户端生成的唯一 id，重复发送（超时重发）只执行一次，直接返回上次结果 |
| **乐观并发 `base_seq`** | 请求声明“我基于哪个局面”。注意它只与**改变棋局**的事件比较（`game_seq`），因此对手上线/掉线这类事件不会让在途的一步棋被判过期；真的过期则返回 `stale` + 最新快照 |
| **传输自动降级** | WebSocket → SSE → 长轮询；降级单向，避免来回抖动；换传输不丢局 |
| **心跳与自动重连** | WS 15 秒应用心跳、SSE 心跳注释行；客户端 0.5s→8s 指数退避 + 抖动重连 |
| **无实时钟** | 不做走子限时（服务器不按时间判负），链路慢只会让对手多等一会儿 |
| **反代友好** | SSE 显式 `X-Accel-Buffering: no`，单进程无需粘性会话；TCP_NODELAY 降低小消息延迟 |
| **小载荷** | 紧凑 JSON（中文不转义）、单文件客户端（约 100 KB），弱网加载也不慢 |

### 10.5 公网部署与安全

**最省事的方式是隧道，而不是开门给别人直连**（一键脚本见 §11）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\serve-public.ps1
# 自动：准备并校验 cloudflared → 确保本机服务在跑 → 建隧道 → 打印 https 公网地址
```

* **默认只监听 `127.0.0.1`**：隧道模式下公网入口只有 Cloudflare 一条，本机不额外暴露端口。
  只有「局域网直连」才需要 `--host 0.0.0.0` + 防火墙放行（脚本 `-Lan` 已处理）。
* **HTTPS 由 Cloudflare 提供**（`https://*.trycloudflare.com`，浏览器侧自动）：
  房间号 + 座位令牌就是全部凭据，**公网务必走 HTTPS**（wss/EventSource 会自动用 https）。
* 若自己套 nginx / caddy 反代：需放行 WebSocket 的 `Upgrade` / `Connection` 头、
  关闭响应缓冲（服务器已发 `X-Accel-Buffering: no` 并用 chunked 流式输出）、放宽读超时。
* **快速隧道的已知限制（实测）**：SSE 响应体会被整包缓冲（不实时）；WebSocket 与长轮询正常，
  客户端会自动降级，所以不影响下棋。需要稳定地址/正常 SSE 请用命名隧道 + 自有域名。
* 资源上限（防误用）：同时最多 500 个房间、请求体 ≤ 16 KB、WebSocket 单帧 ≤ 1 MiB、
  每房间保留 4096 条事件、SSE/WS 单连接最长 30 分钟（到期客户端自动重连，不影响对局）。
* 房间与令牌只存在内存里：**服务器重启 = 所有对局结束**；空闲 6 小时的房间自动回收。
* 本实现刻意**没有**观战、聊天、悔棋、行棋计时——只有两个座位与断线重连，边界更小、更好审计；
  房间满时第三人会被明确拒绝。

### 10.6 联机层与客户端测试（共 158 项）

| 测试文件 | 数量 | 覆盖 |
|---|---|---|
| `tests/test_net_rooms.py` | 37 | 轮次、非法着法、`cid` 幂等、`base_seq` 过期、presence 不影响在途着法、`seq` 严格连续且可重放、多连接在线状态、重连补齐、令牌精确匹配与不外泄、事件裁剪、房间上限与回收 |
| `tests/test_net_ws.py` | 73 | RFC 6455 握手（含官方样例向量）、掩码、粘包/半包、分片、126/127 长度、ping/pong、close、超时后连接可继续用、并发发送不交错——客户端组帧全部手写作为预言机 |
| `tests/test_net_server.py` | 29 | 真实 TCP 端到端：静态资源与健康检查、开房/入座/满员、轮次与非法着法、幂等与过期、令牌校验、认输/新局、`/resume` 契约、长轮询被唤醒与 `wait` 容错、SSE 推送与 `Last-Event-ID` 重连、JSON 错误体、WebSocket 双人对局与重连 |
| `tests/test_web.py` | 10 | HTML 元素 id 与 app.js 引用一一对应、只用 logic.js 导出的符号、接口与协议字段齐全、无内联事件处理器与外部资源；有 `node` 时另做 `node --check` 与 18 项纯逻辑断言（坐标换算、事件归并幂等/乱序容错、快照应用、`canSelect`、`legalFrom`、ICCS 四字符校验） |
| `tests/test_offline.py` | 9 | 核心模块离线、联机层仅标准库、网页零外部资源、`--serve` 入口不会在导入时监听端口 |
| `tests/test_web_engine.py` | 30 | 浏览器内引擎 vs Python 逐条对拍：perft(1-3)、中文记谱（平/进/退、前后叠子）、合法性边界（蹩马腿/塞象眼/牵制/照面）、将死/困毙/三次重复/60 回合、AI 合法性；含两个真实踩坑的回归 |
| `tests/test_tunnel.py` | 13 | 一键联机（不联网）：公网地址解析、cloudflared 查找/复用、假 cloudflared 进程编排、入口与图形界面接线 |

另外用一个**独立的 Node 脚本**验证了网页端纯逻辑模块 `logic.js`（坐标换算、棋子名、
`applyEvents` 幂等与乱序容错、`applySnapshot`、`canSelect`、`legalFrom`），21 项断言全部通过。

---

## 11. 公网 / 跨国对战（只做部署，不改业务代码）

完整说明见 [docs/公网对战部署.md](<docs/公网对战部署.md>)，含实测数据、安全清单、故障排查与验收清单。

### 11.1 一键暴露到公网（Cloudflare 隧道）

```
Windows：双击  tools\serve-public.cmd
Linux/macOS/WSL：bash tools/serve-public.sh
```

它做的事：准备 `cloudflared`（没有就下载；Windows 会**校验 Authenticode 签名必须是
Cloudflare, Inc.**，Linux/macOS 打印 sha256 供核对）→ 确保本机 `python main.py --serve` 在跑 →
建立隧道 → 打印形如 `https://xxx-xxx-xxx.trycloudflare.com/` 的公网地址 →
退出时关闭隧道（并清理它启动的服务）。地址同时写入 `tools/public-url.txt`。

> 这两个脚本**只做部署**，不碰任何业务代码；`tools/bin/cloudflared` 不进仓库、按需下载。
> 下载源顺序：官方直链 → GitHub API 兜底（API 未认证有速率限制），也支持手工指定直链
> （PowerShell 参数 `-CloudflaredUrl`）。

### 11.2 和朋友开始对局

1. 你打开公网地址 → 点「创建房间」→ 得到 6 位房间号；
2. 把地址或 `…/?room=XXXXXX` 链接发给国外朋友 → 他打开即入座（或手动输入房间号）；
3. 红方先行，双方实时看到落子；他刷新页面会**恢复同一座位与棋局**，断网重连会自动补齐。

### 11.3 本机实测（2026-10-03，cloudflared 2026.9.3）

* 隧道：QUIC 连到 Cloudflare 边缘 `lax07`，连通性 precheck 全 PASS；
* **外部网络可达性**：从本机之外的网络抓取公网地址，首页 **HTTP 200**（完整中文界面）、
  `/api/health` **HTTP 200 JSON** —— 即“国外朋友能打开”这一环已打通；
* **WebSocket 经隧道正常**：`101 Switching Protocols` + 握手校验 + 双方实时收着法；
* **长轮询经隧道正常**：对手走子后 **2.61 s** 唤醒；
* `cid` 幂等、`base_seq` 过期（`409 stale` + 快照）经公网仍然成立；
* ⚠️ **SSE 会被 Cloudflare 快速隧道整包缓冲**（用最小裸 socket 复现确认与业务代码无关）——
  浏览器默认走 WebSocket，所以不影响下棋；WS 被封时会等约 60 s 自动降级到长轮询。

> 本机网络注意：**github.com 直连被重置**（`api.github.com` 正常），所以脚本走 GitHub API 取资产地址下载；
> 若哪天 UDP 7844 被封，给 cloudflared 加 `--protocol http2` 回退 TCP 443。
