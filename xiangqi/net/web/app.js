/* 中国象棋 · 联机对战网页版 —— 界面与网络
 * ---------------------------------------------------------------------------
 * 零依赖、零构建：只用浏览器原生 API（Canvas 2D / WebSocket / EventSource /
 * fetch / ResizeObserver / localStorage）。所有请求都是同源请求，页面不加载任何
 * 外部字体、图片、脚本或样式。
 *
 * 三层结构：
 *   1. 棋盘渲染（Canvas）：与 tkinter 桌面版同一套视觉语言，棋子/线条/标记同色；
 *   2. 状态：本地状态是服务器快照 + 事件（XQ.applyEvents 幂等合并）的投影，
 *      客户端不实现任何规则判定；
 *   3. 传输：WebSocket -> SSE -> 长轮询，逐级降级（降级是单向的，避免来回抖动），
 *      每次(重)连都带上已知 seq，服务器会补齐缺失事件或直接下发快照。
 * ---------------------------------------------------------------------------
 */
(function () {
  'use strict';

  var XQ = globalThis.XQ;
  if (!XQ) {
    document.body.innerHTML = '<p style="color:#f3b6b3;padding:24px">页面脚本不完整：logic.js 未加载。</p>';
    return;
  }

  /* ====================================================================== */
  /* 第一节 · 工具                                                           */
  /* ====================================================================== */

  var $ = function (id) { return document.getElementById(id); };

  /** localStorage 在某些隐私模式下会抛异常，统一包一层。 */
  var store = {
    get: function (key) {
      try { return localStorage.getItem(key); } catch (error) { return null; }
    },
    set: function (key, value) {
      try { localStorage.setItem(key, value); } catch (error) { /* 忽略：不影响对局 */ }
    },
    del: function (key) {
      try { localStorage.removeItem(key); } catch (error) { /* 忽略 */ }
    }
  };

  var storageKeys = {
    room: 'xq.room',
    token: function (code) { return 'xq.token.' + code; },
    seq: function (code) { return 'xq.seq.' + code; }
  };

  /** UUID（幂等键 cid）：优先原生，退化实现保证任何浏览器都能用。 */
  function newCid() {
    try {
      if (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function') {
        return globalThis.crypto.randomUUID();
      }
      if (globalThis.crypto && typeof globalThis.crypto.getRandomValues === 'function') {
        var bytes = new Uint8Array(16);
        globalThis.crypto.getRandomValues(bytes);
        bytes[6] = (bytes[6] & 0x0f) | 0x40;
        bytes[8] = (bytes[8] & 0x3f) | 0x80;
        var hex = '';
        for (var i = 0; i < 16; i += 1) hex += (bytes[i] + 0x100).toString(16).slice(1);
        return hex.slice(0, 8) + '-' + hex.slice(8, 12) + '-' + hex.slice(12, 16) + '-'
             + hex.slice(16, 20) + '-' + hex.slice(20);
      }
    } catch (error) { /* 落到下面的兜底 */ }
    return 'c' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 12);
  }

  function clamp(value, min, max) { return value < min ? min : (value > max ? max : value); }

  /** 带超时的 fetch（AbortController；不支持时退化为普通 fetch）。 */
  function fetchWithTimeout(url, options, ms) {
    var opts = Object.assign({}, options || {});
    opts.cache = 'no-store';
    var controller = null;
    var timer = null;
    if (typeof AbortController === 'function') {
      controller = new AbortController();
      opts.signal = controller.signal;
      timer = setTimeout(function () { controller.abort(); }, ms);
    }
    return fetch(url, opts).then(function (response) {
      if (timer) clearTimeout(timer);
      return response;
    }, function (error) {
      if (timer) clearTimeout(timer);
      throw error;
    });
  }

  /** 把 fetch 应答规整为 {status, ok, body}；任何异常都变成 status = 0。 */
  async function readJson(response) {
    var text = '';
    try {
      text = await response.text();
    } catch (error) {
      return { status: response.status, ok: false, body: { ok: false, error: 'bad_response', msg: '无法读取服务器应答' } };
    }
    var body = null;
    if (text) {
      try { body = JSON.parse(text); } catch (error) { body = null; }
    }
    if (!body || typeof body !== 'object') {
      return {
        status: response.status,
        ok: false,
        body: { ok: false, error: 'bad_response', msg: '服务器返回了非 JSON 内容（HTTP ' + response.status + '）' }
      };
    }
    return { status: response.status, ok: response.ok, body: body };
  }

  async function postJson(url, payload, timeoutMs) {
    try {
      var response = await fetchWithTimeout(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload || {})
      }, timeoutMs || 12000);
      return await readJson(response);
    } catch (error) {
      return { status: 0, ok: false, body: { ok: false, error: 'network', msg: '无法连接服务器（' + describeError(error) + '）' } };
    }
  }

  function describeError(error) {
    if (!error) return '未知错误';
    if (error.name === 'AbortError') return '请求超时';
    return error.message || String(error);
  }

  function roomUrl(code) {
    return '/api/rooms/' + encodeURIComponent(code);
  }

  function toast(text, kind) {
    var el = $('toast');
    if (!el) return;
    el.textContent = text;
    el.className = 'toast show' + (kind ? ' is-' + kind : '');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(function () { el.className = 'toast'; }, kind === 'error' ? 5200 : 2600);
  }

  /* ====================================================================== */
  /* 第二节 · 全局状态                                                       */
  /* ====================================================================== */

  var PALETTE = {
    board: '#E8C88C',
    line: '#6B4A20',
    red: '#B32E24',
    black: '#1B2430',
    pieceFill: '#FCF3DE',
    select: '#1F6FB2',
    target: '#2E9E5B',
    last: '#E08A1E',
    check: '#D32F2F'
  };

  /** 棋子起始位的装饰小十字（与桌面版 gui.py 同一份坐标表）。 */
  var POSITION_MARKS = [
    [3, 0], [3, 2], [3, 4], [3, 6], [3, 8],
    [6, 0], [6, 2], [6, 4], [6, 6], [6, 8],
    [2, 1], [2, 7], [7, 1], [7, 7]
  ];

  var TRANSPORT_LABEL = { ws: 'WebSocket', sse: 'SSE', poll: '长轮询', none: '未连接' };
  var CONN_PHASE = { idle: '未连接', connecting: '连接中…', live: '已连接', retry: '连接中断，重试中…' };

  var state = {
    app: null,             // 当前 XQ 状态（服务器快照 + 事件）
    room: '',
    token: '',
    mySide: '',
    flipped: false,
    selected: null,
    targets: [],
    lastEventText: '',
    pending: null,         // {cid, baseSeq} 正在等待服务器裁决的着法
    boardSeq: -1,
    conn: { phase: 'idle', mode: 'none', attempt: 0 },
    timer: null,           // 回退重连定时器
    ping: null,            // WebSocket 心跳定时器
    pollAbort: false,
    stream: null,
    streamWatchdog: null,
    socket: null,
    staleNotice: '',
    serverOffset: 0
  };

  var dom = {};

  /* ====================================================================== */
  /* 第三节 · 棋盘渲染                                                       */
  /* ====================================================================== */

  var board = {
    canvas: null,
    ctx: null,
    cssW: 700,
    cssH: 760,
    cell: 68,
    margin: 68,
    dpr: 1,
    fontRiver: 'bold 24px sans-serif',
    scheme: 'light'
  };

  function boardScale() {
    var cell = board.cell;
    var margin = board.margin;
    return {
      cell: cell,
      margin: margin,
      x: function (file) { return margin + file * cell; },
      y: function (rank) { return margin + (9 - rank) * cell; }
    };
  }

  function setupCanvas() {
    var canvas = dom.board;
    var ctx = canvas.getContext('2d');
    board.canvas = canvas;
    board.ctx = ctx;

    if (typeof ResizeObserver === 'function') {
      var observer = new ResizeObserver(function () { scheduleLayout(); });
      observer.observe(dom.boardShell);
    }
    // 壳子的尺寸只随宽度变化；窗口**高度**变化时也要重算（格子大小现在受视口高度约束）
    window.addEventListener('resize', scheduleLayout);
    window.addEventListener('orientationchange', function () { setTimeout(scheduleLayout, 120); });
    if (window.matchMedia) {
      var dark = window.matchMedia('(prefers-color-scheme: dark)');
      var onChange = function (event) { board.scheme = event.matches ? 'dark' : 'light'; scheduleLayout(); };
      board.scheme = dark.matches ? 'dark' : 'light';
      if (typeof dark.addEventListener === 'function') dark.addEventListener('change', onChange);
      else if (typeof dark.addListener === 'function') dark.addListener(onChange);
    }
    scheduleLayout();
  }

  var layoutHandle = 0;
  function scheduleLayout() {
    if (layoutHandle) return;
    layoutHandle = requestAnimationFrame(function () {
      layoutHandle = 0;
      layoutCanvas();
    });
  }

  /* 棋盘格子尺寸的上下限与视口占用。
     CELL_MAX 曾是 78（棋盘 780×858），在大窗口上几乎占满整屏、也容易顶到视口边缘
     （实际收到的反馈），因此下调到 64，并只使用高度预算的 92% 留出呼吸空间。 */
  var CELL_MIN = 21;          // 再小棋子上的字就看不清了
  var CELL_MAX = 64;          // 单格最大 CSS 像素
  var VIEWPORT_FILL = 0.92;   // 高度预算里实际只用 92%
  /* 比例对齐参考图（用户给的棋盘样式图）：
     木框只有半格宽（原来是整整一格，盘面显得被木框挤小），棋子直径约 0.92 格。
     于是画布宽 = 8 + 2*0.5 = 9 格，高 = 9 + 2*0.5 = 10 格。 */
  var MARGIN_RATIO = 0.5;     // 木框宽度（格）
  var PIECE_RATIO = 0.46;     // 棋子半径（格）

  /**
   * 计算并锁定画布尺寸（含 devicePixelRatio），保证高分屏不糊。
   *
   * 关键约束：画布在页面上的**实际显示尺寸必须等于**它的 CSS 尺寸、也等于
   * 我们用来做命中判定的逻辑尺寸。做法是：
   *   * 用 shell 的实际内容宽高（getBoundingClientRect）算格子大小，而不是写死系数；
   *   * 给画布设定精确的 style 宽高 —— 它是块级元素，除非浏览器被迫缩放，
   *     否则一定按此尺寸渲染；
   *   * 命中判定的坐标换算（hitTest）会再乘一次 client/逻辑尺寸的比例，
   *     万一将来有人给它加了 max-width、transform 或缩放的父元素，也不会点错格。
   */
  function layoutCanvas() {
    var canvas = board.canvas;
    var ctx = board.ctx;
    if (!canvas || !ctx) return;

    var shellRect = dom.boardShell.getBoundingClientRect();
    /* 宽度预算取**外层容器**（.board-wrap）的宽度：木框在下面会被设成和画布一样大，
       若拿木框自己的宽度当预算就会越算越小（自我反馈）。 */
    var wrap = dom.boardShell.parentElement || dom.boardShell;
    var available = wrap.getBoundingClientRect().width || shellRect.width;
    if (available < 10) {            // 面板被隐藏（起始屏）时稍后重试
      setTimeout(scheduleLayout, 200);
      return;
    }
    /* 高度预算：视口高度 - 棋盘上方已占高度（顶栏/间距）- 下方提示语 - 余量。
       棋盘是 10 格宽 × 11 格高，只按宽度算的话，1400px 宽的窗口会得到 858px 高的
       棋盘，笔记本视口（约 700~800px）就得滚动才能看到最下一排（实测踩过）。 */
    var hintH = dom.boardHint ? dom.boardHint.getBoundingClientRect().height : 18;
    var viewportH = window.innerHeight || document.documentElement.clientHeight || 720;
    var availableH = Math.max(10 * CELL_MIN, viewportH
      - Math.max(shellRect.top, 0) - Math.max(hintH, 18) - 26);

    /* 画布尺寸 = 棋盘 8×9 格 + 上下左右各半格木框（MARGIN_RATIO）。
       格子大小取“宽度允许”和“高度允许”两者中的较小值，并受 CELL_MIN/CELL_MAX 约束。 */
    var cell = clamp(
      Math.floor(Math.min(available / (8 + 2 * MARGIN_RATIO),
                          (availableH * VIEWPORT_FILL) / (9 + 2 * MARGIN_RATIO)) * 2) / 2,
      CELL_MIN, CELL_MAX);
    var margin = Math.max(12, Math.round(cell * MARGIN_RATIO));
    while (cell > CELL_MIN && cell * 8 + margin * 2 > available) {
      cell -= 0.5;
      margin = Math.max(12, Math.round(cell * MARGIN_RATIO));
    }
    var cssW = cell * 8 + margin * 2;
    var cssH = cell * 9 + margin * 2;
    var dpr = Math.max(1, Math.min(3, window.devicePixelRatio || 1));

    board.cell = cell;
    board.margin = margin;
    board.cssW = cssW;
    board.cssH = cssH;

    var pixelW = Math.round(cssW * dpr);
    var pixelH = Math.round(cssH * dpr);
    if (canvas.width !== pixelW || canvas.height !== pixelH || board.dpr !== dpr) {
      canvas.width = pixelW;
      canvas.height = pixelH;
    }
    canvas.style.width = cssW + 'px';
    canvas.style.height = cssH + 'px';
    /* 木框（外壳）紧贴画布：否则它会按 CSS 的 aspect-ratio 比画布更大，
       底部露出一条空白木纹、并让画布被 overflow:hidden 裁掉几个像素。 */
    dom.boardShell.style.width = cssW + 'px';
    dom.boardShell.style.height = cssH + 'px';
    board.dpr = dpr;

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.scale(dpr, dpr);
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';

    drawBoard();
  }

  function drawBoard() {
    var ctx = board.ctx;
    var canvas = board.canvas;
    if (!ctx) return;
    var unit = board.cell / 68;
    var half = board.cell / 2;
    var flipped = state.flipped;

    function px(rank, file) {
      var file2 = flipped ? 8 - file : file;
      var rank2 = flipped ? 9 - rank : rank;
      return { x: (board.margin + file2 * board.cell) * board.dpr,
               y: (board.margin + (9 - rank2) * board.cell) * board.dpr };
    }
    function pt(x, y) { return { x: x * board.dpr, y: y * board.dpr }; }
    function line(p1, p2) {
      ctx.beginPath();
      ctx.moveTo(p1.x, p1.y);
      ctx.lineTo(p2.x, p2.y);
      ctx.stroke();
    }
    function oval(p, radius, fill, stroke, width) {
      ctx.beginPath();
      ctx.arc(p.x, p.y, radius * board.dpr, 0, Math.PI * 2);
      if (fill) { ctx.fillStyle = fill; ctx.fill(); }
      if (stroke) { ctx.lineWidth = (width || 1) * board.dpr; ctx.strokeStyle = stroke; ctx.stroke(); }
    }

    /* 底色（画满整个画布，避免边缘露白） */
    ctx.fillStyle = PALETTE.board;
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    /* 木板纹理：横竖两组极淡的暖色条，纯本地绘制，不依赖图片 */
    if (board.scheme === 'dark') {
      ctx.fillStyle = 'rgba(120, 82, 32, 0.05)';
      for (var band = 0; band < 14; band += 1) {
        ctx.fillRect(0, band * board.cssH / 14 * board.dpr, canvas.width, 5 * board.dpr);
      }
    }

    var margin = board.margin;
    var x0 = margin;
    var x1 = margin + 8 * board.cell;
    var yTop = margin;
    var yBottom = margin + 9 * board.cell;

    /* 双线外框 */
    ctx.strokeStyle = PALETTE.line;
    ctx.lineWidth = 1 * board.dpr;
    ctx.strokeRect((x0 - 9 * unit) * board.dpr, (yTop - 9 * unit) * board.dpr,
                   (8 * board.cell + 18 * unit) * board.dpr, (9 * board.cell + 18 * unit) * board.dpr);
    ctx.lineWidth = 2.2 * board.dpr;
    ctx.strokeRect((x0 - 3.5 * unit) * board.dpr, (yTop - 3.5 * unit) * board.dpr,
                   (8 * board.cell + 7 * unit) * board.dpr, (9 * board.cell + 7 * unit) * board.dpr);

    /* 横线（10 条，通长） */
    ctx.lineWidth = 1.1 * board.dpr;
    ctx.strokeStyle = PALETTE.line;
    for (var rank = 0; rank < 10; rank += 1) {
      var a = px(rank, 0);
      var b = px(rank, 8);
      line(a, b);
    }

    /* 竖线（9 条，中间 7 条在楚河汉界处断开） */
    for (var file = 0; file < 9; file += 1) {
      if (file === 0 || file === 8) {
        line(px(0, file), px(9, file));
      } else {
        line(px(5, file), px(9, file));   // 黑方半场（rank 5..9）
        line(px(0, file), px(4, file));   // 红方半场（rank 0..4）
      }
    }

    /* 九宫斜线 */
    var palace = [[0, 3, 2, 5], [0, 5, 2, 3], [7, 3, 9, 5], [7, 5, 9, 3]];
    for (var p = 0; p < palace.length; p += 1) {
      line(px(palace[p][0], palace[p][1]), px(palace[p][2], palace[p][3]));
    }

    /* 兵位、炮位的小十字 */
    for (var m = 0; m < POSITION_MARKS.length; m += 1) {
      drawCross(ctx, px(POSITION_MARKS[m][0], POSITION_MARKS[m][1]), unit, board.dpr,
                x0, x1, yTop, yBottom);
    }

    /* 楚河 · 汉界 */
    var riverY = (margin + 4.5 * board.cell) * board.dpr;
    var fc = 'bold ' + Math.max(14, Math.round(board.cell * 0.42)) + 'px ' + riverFont();
    ctx.font = fc;
    ctx.fillStyle = PALETTE.line;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    var riverX = [margin + 2 * board.cell, margin + 6 * board.cell];
    for (var r = 0; r < 2; r += 1) {
      ctx.fillText(r === 0 ? '楚 河' : '汉 界', riverX[r] * board.dpr, Math.round(riverY));
    }

    drawMarkers(px, oval, line, unit, half);
    drawPieces(px, oval, unit);
  }

  function riverFont() {
    return 'system-ui, -apple-system, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei UI", "Microsoft YaHei", sans-serif';
  }

  /** 棋子起始位的小十字：靠近棋盘外沿的一侧按桌面版规则省略。 */
  function drawCross(ctx, center, unit, dpr, x0, x1, yTop, yBottom) {
    var gap = 5.4 * unit * dpr;
    var span = 6.6 * unit * dpr;
    var left = x0 * dpr, right = x1 * dpr, top = yTop * dpr, bottom = yBottom * dpr;
    var eps = 0.5 * dpr;
    ctx.strokeStyle = PALETTE.line;
    ctx.lineWidth = 1.1 * dpr;
    var sides = [[-1, -1], [1, -1], [-1, 1], [1, 1]];
    for (var i = 0; i < sides.length; i += 1) {
      var dx = sides[i][0];
      var dy = sides[i][1];
      var sx = center.x + dx * gap;
      var sy = center.y + dy * gap;
      if (sx < left - eps || sx > right + eps) continue;
      if (sy < top - eps || sy > bottom + eps) continue;
      if (sx + dx * span <= right + eps && sx + dx * span >= left - eps) {
        ctx.beginPath();
        ctx.moveTo(sx, sy);
        ctx.lineTo(sx + dx * span, sy);
        ctx.stroke();
      }
      if (sy + dy * span <= bottom + eps && sy + dy * span >= top - eps) {
        ctx.beginPath();
        ctx.moveTo(sx, sy);
        ctx.lineTo(sx, sy + dy * span);
        ctx.stroke();
      }
    }
  }

  /** 上一步 / 选中 / 合法落点 / 将军标记。 */
  function drawMarkers(px, oval, line, unit, half) {
    var ctx = board.ctx;
    var dpr = board.dpr;
    var api = state.app;

    /* 上一步：起终点描边方框（空心，任何棋子下面都看得见） */
    if (api && api.lastMove) {
      var ends = [api.lastMove.from, api.lastMove.to];
      ctx.strokeStyle = PALETTE.last;
      ctx.lineWidth = 2 * dpr;
      for (var i = 0; i < ends.length; i += 1) {
        var parsed = XQ.parseSquare(ends[i]);
        if (!parsed) continue;
        var mark = px(parsed.rank, parsed.file);
        var size = half * 0.86 * dpr;
        ctx.strokeRect(mark.x - size, mark.y - size, size * 2, size * 2);
      }
    }

    /* 将军：被将一方的将/帅套红圈 */
    if (api && api.check && !XQ.isOver(api)) {
      var king = findKing(api.fen, api.side);
      if (king) {
        var kp = px(king.rank, king.file);
        oval(kp, half * 0.95, null, PALETTE.check, 3);
      }
    }

    /* 选中环 */
    if (state.selected) {
      var sp = px(state.selected.rank, state.selected.file);
      oval(sp, half * 0.98, null, PALETTE.select, 3);
    }

    /* 合法落点：空点画实心圆点，有子画圆圈 */
    for (var t = 0; t < state.targets.length; t += 1) {
      var target = XQ.parseSquare(state.targets[t]);
      if (!target) continue;
      var tp = px(target.rank, target.file);
      var occupied = !!XQ.pieceAt(api ? api.fen : '', target.rank, target.file);
      if (occupied) oval(tp, half * 0.95, null, PALETTE.target, 3);
      else oval(tp, half * 0.22, PALETTE.target, null, 0);
    }
  }

  /** 在 FEN 里找某方的将/帅（客户端只做定位，不做规则判定）。 */
  function findKing(fen, side) {
    var wanted = side === 'r' ? 'K' : 'k';
    var grid = XQ.parseFenBoard(fen);
    for (var rank = 0; rank < 10; rank += 1) {
      for (var file = 0; file < 9; file += 1) {
        if (grid[rank][file] === wanted) return { rank: rank, file: file };
      }
    }
    return null;
  }

  function drawPieces(px, oval, unit) {
    var api = state.app;
    if (!api) return;
    var ctx = board.ctx;
    var dpr = board.dpr;
    var grid = XQ.parseFenBoard(api.fen);
    var radius = board.cell * PIECE_RATIO;
    var font = 'bold ' + Math.max(13, Math.round(board.cell * 0.55)) + 'px ' + riverFont();

    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.font = font;

    for (var rank = 0; rank < 10; rank += 1) {
      for (var file = 0; file < 9; file += 1) {
        var piece = grid[rank][file];
        if (!piece) continue;
        var side = XQ.pieceSide(piece);
        var color = side === 'r' ? PALETTE.red : PALETTE.black;
        var center = px(rank, file);

        /* 棋子本体：木底 + 深色外圈 + 内圈 + 汉字 */
        oval(center, radius, PALETTE.pieceFill, color, 2.2);
        oval(center, radius - Math.max(2, 3 * unit), null, color, 1.1);
        ctx.fillStyle = color;
        ctx.fillText(XQ.pieceName(piece), Math.round(center.x), Math.round(center.y) + 0.5 * dpr);
      }
    }
  }

  /* ====================================================================== */
  /* 第四节 · 交互                                                           */
  /* ====================================================================== */

  function myTurn() {
    return XQ.isMyTurn(state.app, state.mySide);
  }

  /** 画布坐标 -> 棋盘坐标（含翻转），点得离交点太远则忽略。
   *  与 drawBoard 的 px() 严格互逆：
   *    未翻转：x = margin + file*cell          y = margin + (9-rank)*cell
   *    已翻转：x = margin + (8-file)*cell      y = margin + rank*cell
   *  另外把“客户端像素”换算到“逻辑像素”：正常情况下两者相同，
   *  但若画布被 CSS 缩放（曾经的坑：容器 max-width 小于画布宽度时浏览器会缩放），
   *  这一步能保证点击仍然落在正确的交叉点上。
   */
  function hitTest(clientX, clientY) {
    var rect = board.canvas.getBoundingClientRect();
    var scaleX = board.cssW / Math.max(1, rect.width);
    var scaleY = board.cssH / Math.max(1, rect.height);
    var dx = (clientX - rect.left) * scaleX - board.margin;
    var dy = (clientY - rect.top) * scaleY - board.margin;
    var col = Math.round(dx / board.cell);      // 屏幕上的列号 0..8
    var row = Math.round(dy / board.cell);      // 屏幕上的行号 0..9（0 = 最上面）
    var rank;
    var file;
    if (state.flipped) {
      rank = row;
      file = 8 - col;
    } else {
      rank = 9 - row;
      file = col;
    }
    if (rank < 0 || rank > 9 || file < 0 || file > 8) return null;
    var snappedX = (state.flipped ? 8 - file : file) * board.cell;
    var snappedY = (state.flipped ? rank : 9 - rank) * board.cell;
    if (Math.abs(snappedX - dx) > board.cell * 0.55) return null;
    if (Math.abs(snappedY - dy) > board.cell * 0.55) return null;
    return { rank: rank, file: file };
  }

  function handlePick(square) {
    var name = XQ.squareName(square.rank, square.file);
    var api = state.app;
    if (!api) return;

    /* 先判断“落子”：只要该点是当前选中子的合法落点就直接走 */
    if (state.selected && state.targets.indexOf(name) >= 0) {
      var iccs = XQ.toIccs(state.selected.rank, state.selected.file)
        + XQ.toIccs(square.rank, square.file);
      sendMove(iccs);
      return;
    }
    if (XQ.canSelect(api, name, state.mySide)) {
      if (state.selected && state.selected.rank === square.rank && state.selected.file === square.file) {
        state.selected = null;
        state.targets = [];
      } else {
        state.selected = square;
        state.targets = XQ.legalFrom(api, name);
      }
      render();
      return;
    }
    /* 点空白或对方棋子：取消选择 */
    if (state.selected) {
      state.selected = null;
      state.targets = [];
      render();
    }
  }

  function onBoardClick(event) {
    if (!state.app) return;
    var square = hitTest(event.clientX, event.clientY);
    if (square) handlePick(square);
  }

  function onBoardMove(event) {
    if (!state.app) return;
    var square = hitTest(event.clientX, event.clientY);
    var pickable = false;
    if (square) {
      var name = XQ.squareName(square.rank, square.file);
      pickable = XQ.canSelect(state.app, name, state.mySide)
        || state.targets.indexOf(name) >= 0;
    }
    board.canvas.classList.toggle('can-pick', pickable);
  }

  /* ====================================================================== */
  /* 第五节 · 本地状态 -> 界面                                               */
  /* ====================================================================== */

  function setNotice(text, kind) {
    dom.alert.hidden = !text;
    dom.alert.textContent = text || '';
    dom.alert.className = 'alert alert-' + (kind || 'info');
  }

  function seatInfo(side) {
    var seats = (state.app && state.app.seats) || {};
    return seats[side] || { joined: false, connected: false, name: '' };
  }

  function paintSeat(el, side, roleLabel, mine) {
    var info = seatInfo(side);
    var nameEl = el.querySelector('.seat-name');
    var sideEl = el.querySelector('.seat-side');
    var tagEl = el.querySelector('.seat-tag');
    var phase = XQ.phaseOf(state.app);

    var cls = 'seat';
    if (mine) cls += ' seat-self';
    if (info.joined) cls += info.connected ? ' seat-on' : ' seat-off';
    else if (side === state.mySide) cls += ' seat-on';
    else cls += ' seat-empty';
    el.className = cls;

    sideEl.textContent = XQ.sideName(side);
    if (info.joined) {
      nameEl.textContent = (mine ? '你（' + XQ.sideName(side) + '）' : (info.name || XQ.sideName(side) + '棋手'))
        + (info.connected ? ' · 在线' : ' · 离线');
    } else if (side === state.mySide) {
      nameEl.textContent = '你（' + XQ.sideName(side) + '）';
    } else {
      nameEl.textContent = phase === 'over' ? '空位' : '空位（等待加入）';
    }
    tagEl.textContent = roleLabel;
  }

  function renderSeats() {
    var mine = state.mySide || 'r';
    var other = XQ.other(mine) || 'b';
    dom.seatTopSide = other;
    paintSeat(dom.seatTop, other, state.mySide ? '对手' : '黑方', false);
    paintSeat(dom.seatSelf, mine, state.mySide ? '你' : '红方', !!state.mySide);
  }

  function renderStatus() {
    var api = state.app;
    var phase = XQ.phaseOf(api);
    var line = dom.statusLine;
    var text = '';
    var cls = 'status-line';

    if (phase === 'over' && api.result) {
      text = api.result.text || '对局结束';
      cls += ' is-over';
    } else if (phase === 'waiting') {
      text = '等待对手加入房间…（把房间链接发给好友）';
    } else if (myTurn()) {
      text = '轮到你走棋（' + XQ.sideName(state.mySide) + '）';
      if (XQ.isCheckOn(api, state.mySide)) { text += ' · 将军！'; cls += ' is-check'; }
    } else if (state.mySide) {
      text = '等待' + XQ.sideName(api.side) + '走棋…';
      if (XQ.isCheckOn(api, api.side)) { text += ' · 将军！'; cls += ' is-check'; }
    } else {
      text = '观战中 · 轮到' + XQ.sideName(api.side) + '走棋';
    }
    if (state.pending) text = '已发送着法，等待服务器裁决…';

    line.textContent = text;
    line.className = cls;
    dom.boardHint.textContent = phase === 'waiting'
      ? '棋盘已就绪：等待对手入座后即可走棋。' + (state.mySide ? '' : '')
      : (state.mySide
          ? '点击自己的棋子选中，再点绿点落子；点空白处取消。'
          : '观战：你未占用座位。');
  }

  function renderResult() {
    var api = state.app;
    if (!api || !api.result) {
      dom.result.hidden = true;
      return;
    }
    var result = api.result;
    var text = result.text || '';
    if (!text) {
      text = result.winner ? (XQ.sideName(result.winner) + '胜')
                           : '和棋';
      if (result.reason) text += '（' + result.reason + '）';
    }
    var cls = 'result';
    if (!result.winner) cls += ' is-draw';
    else if (state.mySide && result.winner !== state.mySide) cls += ' is-loss';
    dom.result.className = cls;
    dom.result.hidden = false;
    dom.result.textContent = text;
  }

  function renderMoveList() {
    var api = state.app;
    var rounds = XQ.moveRounds(api.moves);
    var html = '';
    for (var i = 0; i < rounds.length; i += 1) {
      var row = rounds[i];
      var last = (i === rounds.length - 1);
      var checkRed = last && api.check && api.side === 'b' ? ' is-check' : '';
      var checkBlack = last && api.check && api.side === 'r' ? ' is-check' : '';
      html += '<div class="mv-round">'
        + '<span class="mv-no">' + row.round + '.</span>'
        + '<span class="mv-red' + checkRed + '">' + escapeHtml(row.red) + '</span>'
        + '<span class="mv-black' + checkBlack + '">' + escapeHtml(row.black) + '</span>'
        + '</div>';
    }
    if (!rounds.length) {
      html += '<div class="mv-empty">' + (XQ.phaseOf(api) === 'waiting' ? '等待对手入座…' : '还没有着法。') + '</div>';
    }
    /* 事件日志（入座、认输、新局等）附在后面，方便理解棋局进程 */
    var log = (api.log || []).slice(-8);
    for (var j = 0; j < log.length; j += 1) {
      var entry = log[j];
      if (entry.type === 'move') continue;
      html += '<div class="mv-note">' + escapeHtml(entry.text) + '</div>';
    }
    dom.movelist.innerHTML = html;
    dom.movelist.scrollTop = dom.movelist.scrollHeight;
    dom.moveCount.textContent = api.moves.length + ' 手';
  }

  function escapeHtml(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function renderOverlay() {
    var api = state.app;
    var phase = XQ.phaseOf(api);
    var show = false;
    var text = '';
    if (phase === 'waiting') {
      show = true;
      text = '等待对手加入…' + (state.mySide ? '（你执' + XQ.sideName(state.mySide) + '）' : '');
      if (state.room) text += ' 房间号 ' + state.room;
    } else if (phase === 'over' && api.result) {
      show = true;
      text = api.result.text || '对局结束';
    }
    dom.overlay.hidden = !show;
    if (show) dom.overlayText.textContent = text;
    dom.overlayFlip.hidden = state.flipped;
  }

  function renderConn() {
    var label = CONN_PHASE[state.conn.phase] || '未连接';
    var mode = TRANSPORT_LABEL[state.conn.mode] || '';
    if (state.conn.phase === 'live') label = '已连接 · ' + mode;
    else if (state.conn.phase === 'connecting' && mode) label = '连接中 · ' + mode;
    else if (state.conn.phase === 'retry' && mode) label = '重连中 · ' + mode;
    dom.connText.textContent = label;
    var cls = 'conn';
    if (state.conn.phase === 'live') cls += ' conn-live';
    else if (state.conn.phase === 'connecting' || state.conn.phase === 'retry') cls += ' conn-busy';
    else if (state.conn.phase === 'idle') cls += ' conn-idle';
    else cls += ' conn-off';
    dom.conn.className = cls;

    var seq = state.app ? state.app.seq : 0;
    dom.metaSeq.textContent = '记谱序号：' + XQ.formatSeq(seq) + '（' + XQ.shortRoom(state.room) + '）';
    dom.metaSide.textContent = state.mySide ? ('你执' + XQ.sideName(state.mySide)) : '观战（未入座）';
  }

  function renderButtons() {
    var api = state.app;
    var over = XQ.isOver(api);
    var hasRoom = !!state.room;
    var spectating = !state.mySide;

    dom.btnNew.disabled = !hasRoom || spectating;
    dom.btnResign.disabled = !hasRoom || spectating || over;
    dom.btnCopy.disabled = !hasRoom;
  }

  function render() {
    /* 连接状态与按钮状态在没有房间时也要正确显示（起始屏） */
    renderConn();
    renderButtons();
    if (!state.app) return;
    if (state.boardSeq !== state.app.seq || state.lastRenderFen !== state.app.fen
        || state.lastRenderFlipped !== state.flipped
        || state.lastRenderSelected !== (state.selected ? XQ.squareName(state.selected.rank, state.selected.file) : '')) {
      drawBoard();
      state.boardSeq = state.app.seq;
      state.lastRenderFen = state.app.fen;
      state.lastRenderFlipped = state.flipped;
      state.lastRenderSelected = state.selected ? XQ.squareName(state.selected.rank, state.selected.file) : '';
    }
    dom.roomCode.textContent = state.room || '------';
    renderStatus();
    renderSeats();
    renderResult();
    renderMoveList();
    renderOverlay();
  }

  /**
   * 同时拿到快照与事件时（如 /resume 的应答）：
   * 先按 seq 幂等合并事件，再用快照覆盖派生状态。
   *
   * 为什么不能只取其一：快照里的 seq 表示“快照生效到的位置”，
   * 而同一批事件里可能包含 seq 正好等于该值的那一条（服务器常见的写法），
   * 如果先套快照再合并事件，applyEvents 会（按设计）把 seq <= state.seq 的事件全部丢弃，
   * 于是这些事件永远进不了着法列表 / 事件日志。
   */
  function applySnapshotWithEvents(current, snapshot, events) {
    var next = current;
    if (Array.isArray(events) && events.length) next = XQ.applyEvents(next, events);
    var snapSeq = (snapshot && typeof snapshot.seq === 'number') ? snapshot.seq : -1;
    if (!next || snapSeq > next.seq) {
      return XQ.applySnapshot(next, snapshot);
    }
    return next;
  }

  /** 服务器快照/事件之后的统一刷新。 */
  function applyIncoming(message) {
    if (!message || typeof message !== 'object') return;
    var before = state.app;
    if (message.t === 'snapshot' && message.snapshot) {
      if (message.snapshot.room && state.room && message.snapshot.room !== state.room) return;
      state.app = applySnapshotWithEvents(state.app, message.snapshot, message.events);
    } else if (Array.isArray(message.events)) {
      var events = message.events;
      state.app = XQ.applyEvents(state.app, events);
      var top = typeof message.seq === 'number' ? message.seq : -1;
      if (top > state.app.seq) {
        state.app = XQ.applyResponse(state.app, { t: 'events', events: [], seq: top });
      }
      /* 我们自己那一手已经出现在事件流里：说明它被服务器接受了，
         即使没有收到 ack 帧，也可以解除“等待确认”。 */
      if (state.pending) {
        for (var i = 0; i < events.length; i += 1) {
          var ev = events[i];
          if (ev && ev.t === 'move' && ev.side === state.mySide
              && typeof ev.seq === 'number' && ev.seq > state.pending.baseSeq) {
            state.pending = null;
            state.staleNotice = '';
            break;
          }
        }
      }
    } else if (message.snapshot) {
      var snapSeq = typeof message.snapshot.seq === 'number' ? message.snapshot.seq : -1;
      if (!state.app || snapSeq > state.app.seq) {
        state.app = XQ.applySnapshot(state.app, message.snapshot);
      }
    }
    if (message.server_ms) {
      state.serverOffset = message.server_ms - Date.now();
    }
    if (state.app !== before) {
      store.set(storageKeys.seq(state.room), String(state.app.seq));
      if (state.selected && !XQ.isMyTurn(state.app, state.mySide)) {
        state.selected = null;
        state.targets = [];
      } else if (state.selected) {
        var name = XQ.squareName(state.selected.rank, state.selected.file);
        if (!XQ.canSelect(state.app, name, state.mySide)) {
          state.selected = null;
          state.targets = [];
        } else {
          state.targets = XQ.legalFrom(state.app, name);
        }
      }
      render();
    }
  }

  /* ====================================================================== */
  /* 第六节 · 网络                                                           */
  /* ====================================================================== */

  function setConn(phase, mode) {
    if (phase) state.conn.phase = phase;
    if (mode) state.conn.mode = mode;
    renderConn();
  }

  function backoffDelay(attempt) {
    var base = Math.min(8000, 500 * Math.pow(2, Math.max(0, attempt - 1)));
    var jitter = base * (0.15 + Math.random() * 0.35);
    return Math.min(9000, Math.round(base * 0.75 + jitter));
  }

  function clearTimers() {
    if (state.timer) { clearTimeout(state.timer); state.timer = null; }
    if (state.ping) { clearInterval(state.ping); state.ping = null; }
    if (state.streamWatchdog) { clearTimeout(state.streamWatchdog); state.streamWatchdog = null; }
  }

  /**
   * 处理一条服务器消息（WS / SSE / 长轮询 三种传输共用）。
   * 只有 WebSocket 会遇到 ack / welcome / pong。
   */
  function handleMessage(message) {
    if (!message || typeof message !== 'object') return;
    switch (message.t) {
      case 'welcome':
        if (message.room && state.room && message.room !== state.room) return;
        if (message.snapshot) {
          state.app = XQ.applySnapshot(state.app, message.snapshot);
        } else if (message.side) {
          state.app = XQ.applyEvents(state.app, []);
        }
        if (message.side && !state.mySide) state.mySide = message.side;
        state.pending = null;
        setConn('live');
        render();
        return;
      case 'events':
        applyIncoming(message);
        setConn('live');
        return;
      case 'snapshot':
        applyIncoming(message);
        setConn('live');
        return;
      case 'ack':
        state.pending = null;
        state.staleNotice = '';
        applyIncoming({ t: 'events', events: message.events || [], seq: message.seq });
        if (state.app) setNotice('', 'info');
        render();
        return;
      case 'error':
        handleServerError(message.code, message.msg, message.snapshot);
        return;
      case 'pong':
        if (typeof message.server_ms === 'number') state.serverOffset = message.server_ms - Date.now();
        return;
      default:
        return;
    }
  }

  function handleServerError(code, msg, snapshot) {
    state.pending = null;
    var text = XQ.errorText(code, msg);
    if (code === 'stale' && snapshot) {
      state.app = XQ.applySnapshot(state.app, snapshot);
      state.selected = null;
      state.targets = [];
      state.staleNotice = '局面已更新，请重新走棋';
      setNotice(text + '（已同步到最新局面，请重新走棋）', 'warn');
      toast('局面已更新，请重新走棋', 'error');
      render();
      return;
    }
    if (snapshot && snapshot.room === state.room) {
      state.app = XQ.applySnapshot(state.app, snapshot);
    }
    setNotice(text, 'error');
    render();
  }

  /* ---------------------------------------------------------------- WebSocket */
  function wsUrl() {
    var scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return scheme + '//' + location.host + '/ws'
      + '?room=' + encodeURIComponent(state.room)
      + '&since=' + (state.app ? state.app.seq : 0)
      + (state.token ? '&token=' + encodeURIComponent(state.token) : '');
  }

  function sendWs(payload) {
    var socket = state.socket;
    if (!socket || socket.readyState !== 1) return false;
    try {
      socket.send(JSON.stringify(payload));
      return true;
    } catch (error) {
      return false;
    }
  }

  function openSocket(attempt) {
    if (!state.room) return;
    if (typeof WebSocket !== 'function') { openStream(); return; }

    state.conn.attempt = attempt;
    setConn('connecting', 'ws');
    var socket = null;
    try {
      socket = new WebSocket(wsUrl());
    } catch (error) {
      openStream();
      return;
    }
    state.socket = socket;
    var opened = false;

    socket.onopen = function () {
      opened = true;
      state.conn.attempt = 0;
      setConn('live', 'ws');
      sendWs({ t: 'hello', room: state.room, token: state.token, since: state.app ? state.app.seq : 0 });
      if (state.ping) clearInterval(state.ping);
      state.ping = setInterval(function () { sendWs({ t: 'ping', ts: Date.now() }); }, 20000);
    };

    socket.onmessage = function (event) {
      var data = null;
      try { data = JSON.parse(event.data); } catch (error) { return; }
      handleMessage(data);
    };

    socket.onerror = function () { /* 由 onclose 统一处理降级 */ };

    socket.onclose = function () {
      if (state.ping) { clearInterval(state.ping); state.ping = null; }
      if (state.socket === socket) state.socket = null;
      if (!state.room) return;
      /* 从未连上、或已连续失败：切到 SSE（单向），之后靠 POST 上行。 */
      if ((!opened && attempt >= 2) || (opened && attempt >= 4)) {
        state.conn.attempt = 0;
        openStream();
        return;
      }
      var delay = backoffDelay(attempt + 1);
      setConn('retry', 'ws');
      state.timer = setTimeout(function () { openSocket(attempt + 1); }, delay);
    };
  }

  /* ---------------------------------------------------------------- SSE */
  function openStream() {
    if (!state.room) return;
    if (typeof EventSource !== 'function') { startPoll(); return; }

    if (state.stream) { try { state.stream.close(); } catch (error) { /* 忽略 */ } }
    setConn('connecting', 'sse');

    var url = roomUrl(state.room) + '/stream?since=' + (state.app ? state.app.seq : 0)
      + (state.token ? '&token=' + encodeURIComponent(state.token) : '');
    var source = null;
    try {
      source = new EventSource(url);
    } catch (error) {
      startPoll();
      return;
    }
    state.stream = source;

    function gotMessage() {
      setConn('live', 'sse');
      if (state.streamWatchdog) clearTimeout(state.streamWatchdog);
      /* 服务器只在有事件时写数据，但每 ~15s 会发一次 :hb 心跳注释。
         心跳不会触发 onmessage，所以这里用“打开后 N 秒内完全没有任何
         具名事件/默认消息”作为链路可疑的信号，降级到长轮询。 */
      state.streamWatchdog = setTimeout(function () {
        if (state.conn.mode === 'sse') startPoll();
      }, 60000);
    }

    source.onmessage = function (event) {
      gotMessage();
      var data = null;
      try { data = JSON.parse(event.data); } catch (error) { return; }
      handleMessage(data);
    };
    /* 具名事件：events / snapshot / hello */
    ['events', 'snapshot', 'hello'].forEach(function (name) {
      source.addEventListener(name, function (event) {
        gotMessage();
        var data = null;
        try { data = JSON.parse(event.data); } catch (error) { return; }
        if (!data.t) data.t = name === 'hello' ? 'welcome' : name;
        handleMessage(data);
      });
    });

    source.onerror = function () {
      /* EventSource 自己会重连；连续失败时降级到长轮询（~30 分钟后服务器会主动结束流）。 */
      if (state.conn.mode !== 'sse') return;
      state.conn.attempt += 1;
      setConn('retry', 'sse');
      if (state.conn.attempt >= 3) {
        try { source.close(); } catch (error) { /* 忽略 */ }
        startPoll();
      }
    };
    source.onopen = function () { gotMessage(); };
  }

  /* ---------------------------------------------------------------- 长轮询 */
  var POLL_MIN_GAP_MS = 350;      // 两次长轮询之间的最小间隔，防止忙等（打爆服务器）
  var POLL_IDLE_GAP_MS = 900;     // 空应答后的间隔

  async function startPoll() {
    if (!state.room) return;
    if (state.stream) { try { state.stream.close(); } catch (error) { /* 忽略 */ } state.stream = null; }
    state.pollAbort = false;
    state.conn.attempt = 0;
    setConn('live', 'poll');
    pollLoop();
  }

  async function pollLoop() {
    while (state.pollAbort === false && state.room) {
      var since = state.app ? state.app.seq : 0;
      var started = Date.now();
      var result = await postJson(roomUrl(state.room) + '/events?since=' + since + '&wait=25', null, 40000);
      var elapsed = Date.now() - started;
      if (state.pollAbort) return;

      if (!result.body || result.status === 0) {
        setConn('retry', 'poll');
        await sleep(backoffDelay(state.conn.attempt + 1));
        state.conn.attempt += 1;
        continue;
      }
      state.conn.attempt = 0;
      setConn('live', 'poll');

      if (result.body.t === 'snapshot' || Array.isArray(result.body.events)) {
        applyIncoming(result.body);
      }
      /* 关键：无论应答是什么形状都必须有间隔。服务器可能对 events 形状的应答
         立刻返回（例如事件已被 seq 去重、或 seq 已是最新），若这里不 sleep，
         长轮询会变成忙等，把服务器打爆。 */
      var gap = elapsed >= 4000 ? POLL_MIN_GAP_MS : POLL_IDLE_GAP_MS;
      await sleep(gap);
    }
  }

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function connect() {
    clearTimers();
    if (!state.room) return;
    state.conn.attempt = 0;
    if (typeof WebSocket === 'function') openSocket(0);
    else openStream();
  }

  function disconnect() {
    state.pollAbort = true;
    clearTimers();
    if (state.socket) { try { state.socket.close(); } catch (error) { /* 忽略 */ } state.socket = null; }
    if (state.stream) { try { state.stream.close(); } catch (error) { /* 忽略 */ } state.stream = null; }
    setConn('idle', 'none');
  }

  /* ---------------------------------------------------------------- 发请求 */
  /** 所有“服务器应答即事件”的下行结果都走这里（HTTP 应答 + WS ack 共用）。 */
  function handleAck(result) {
    var body = result.body || {};
    if (body.ok) {
      state.pending = null;
      state.staleNotice = '';
      applyIncoming({ t: 'events', events: body.events || [], seq: body.seq });
      if (result.status >= 200 && result.status < 300) setNotice('', 'info');
      render();
      return true;
    }
    if (body.error === 'stale' && body.snapshot) {
      handleServerError('stale', body.msg, body.snapshot);
      return false;
    }
    handleServerError(body.error || 'bad_response', body.msg, body.snapshot);
    return false;
  }

  /**
   * 走子：WebSocket 优先；否则 POST /move。
   * iccs 必须是 4 字符坐标记谱（如 h2e2）；from/to 只用于本地提示。
   * base_seq = 当前已知局面序号（乐观并发），cid = 幂等键。
   */
  function sendMove(iccs, from, to) {
    if (!state.app || !state.room) return;
    if (!myTurn()) { toast('现在不该你走棋', 'error'); return; }
    if (state.pending) { toast('上一手还在等待服务器确认…', 'error'); return; }
    if (!/^[a-i][0-9][a-i][0-9]$/.test(String(iccs))) {
      toast('着法格式异常，已取消（' + iccs + '）', 'error');
      return;
    }

    var cid = newCid();
    var baseSeq = state.app.seq;
    state.pending = { cid: cid, baseSeq: baseSeq, iccs: iccs, from: from, to: to };
    state.selected = null;
    state.targets = [];
    render();

    if (state.conn.mode === 'ws' && sendWs({ t: 'move', iccs: iccs, base_seq: baseSeq, cid: cid })) {
      /* 结果由 ack 帧回填；这里只放一个兜底超时 */
      startMoveTimeout(cid);
      return;
    }
    startMoveTimeout(cid);
    postJson(roomUrl(state.room) + '/move',
      { token: state.token, iccs: iccs, base_seq: baseSeq, cid: cid }, 15000)
      .then(function (result) {
        if (!state.pending || state.pending.cid !== cid) return;
        handleAck(result);
      });
  }

  function startMoveTimeout(cid) {
    setTimeout(function () {
      if (!state.pending || state.pending.cid !== cid) return;
      var base = state.pending.baseSeq;
      state.pending = null;
      if (state.app && state.app.seq > base) {
        /* 事件流已经反映出这一步（只是 ack 帧丢了）：静默接受。 */
        render();
        return;
      }
      setNotice('服务器未确认这一步棋：请点棋盘重新走棋（局面以服务器为准）', 'warn');
      render();
    }, 12000);
  }

  async function postAction(path, label) {
    if (!state.room || !state.token) { toast('你未占用座位，无法执行该操作', 'error'); return; }
    state.pending = null;
    var result = await postJson(roomUrl(state.room) + path, { token: state.token }, 12000);
    if (result.body && result.body.ok) {
      handleAck(result);
      toast(label + '成功');
      return;
    }
    if (!result.body) { setNotice('请求失败，请检查网络', 'error'); return; }
    handleServerError(result.body.error || 'bad_response', result.body.msg, result.body.snapshot);
  }

  /* ---------------------------------------------------------------- 入座 */
  function applySeat(body) {
    state.room = body.room || state.room;
    state.token = body.token || state.token;
    if (body.side === 'r' || body.side === 'b') state.mySide = body.side;
    if (body.snapshot) state.app = XQ.applySnapshot(state.app, body.snapshot);
    store.set(storageKeys.room, state.room);
    store.set(storageKeys.token(state.room), state.token);
    store.set(storageKeys.seq(state.room), String(state.app ? state.app.seq : 0));
    state.flipped = (state.mySide === 'b');
    state.selected = null;
    state.targets = [];
    state.pending = null;
  }

  function enterRoom() {
    dom.start.hidden = true;
    dom.game.hidden = false;
    dom.roomCode.textContent = state.room;
    try {
      var url = new URL(location.href);
      url.searchParams.set('room', state.room);
      history.replaceState(null, '', url.pathname + url.search + url.hash);
    } catch (error) { /* 忽略：URL 美化失败不影响对局 */ }
    setNotice('', 'info');
    render();
    scheduleLayout();
    connect();
  }

  async function createRoom() {
    dom.startError.hidden = true;
    var result = await postJson('/api/rooms', {}, 12000);
    if (!result.body || !result.body.ok) {
      var text = XQ.errorText(result.body && result.body.error, result.body && result.body.msg);
      dom.startError.className = 'alert alert-error';
      dom.startError.textContent = text;
      dom.startError.hidden = false;
      return;
    }
    applySeat(result.body);
    enterRoom();
    toast('房间已创建：' + state.room, 'ok');
  }

  async function joinRoom(code, preferredSide) {
    var normalized = XQ.normalizeRoom(code);
    if (!XQ.isValidRoom(normalized)) {
      dom.startError.textContent = '房间号必须是 6 位（字母表 ' + XQ.roomAlphabet + '）';
      dom.startError.hidden = false;
      return false;
    }
    dom.startError.hidden = true;
    var token = store.get(storageKeys.token(normalized)) || '';
    var result = await postJson(roomUrl(normalized) + '/join',
      { side: preferredSide || undefined, token: token || undefined }, 12000);
    if (!result.body || !result.body.ok) {
      var text = XQ.errorText(result.body && result.body.error, result.body && result.body.msg);
      dom.startError.textContent = text;
      dom.startError.hidden = false;
      dom.roomInput.classList.add('is-bad');
      return false;
    }
    dom.roomInput.classList.remove('is-bad');
    applySeat(result.body);
    enterRoom();
    toast(token ? '已恢复座位（' + XQ.sideName(state.mySide) + '）' : '已加入房间：' + state.room, 'ok');
    return true;
  }

  /** 刷新页面后的静默恢复：先公开摘要，再用 token 走 /resume 补齐事件。 */
  async function resumeSession(code) {
    var token = store.get(storageKeys.token(code)) || '';
    if (!token) return false;
    var since = parseInt(store.get(storageKeys.seq(code)) || '0', 10);
    if (!isFinite(since) || since < 0) since = 0;

    var result = await postJson(roomUrl(code) + '/resume', { token: token, since: since }, 12000);
    if (!result.body || !result.body.ok) {
      var error = result.body && result.body.error;
      if (error === 'forbidden' || error === 'room_not_found') {
        store.del(storageKeys.token(code));
        store.del(storageKeys.seq(code));
        if (store.get(storageKeys.room) === code) store.del(storageKeys.room);
        return false;
      }
      /* 网络故障等：不要清掉凭据，留着下次再试 */
      if (result.status === 0) return false;
      return false;
    }
    applySeat({ room: code, side: result.body.side, token: token });
    state.app = applySnapshotWithEvents(state.app, result.body.snapshot, result.body.events);
    enterRoom();
    return true;
  }

  async function leaveRoom() {
    /* 只是本浏览器忘记这局：令牌留在服务器上，方便以后再恢复座位。 */
    disconnect();
    var code = state.room;
    if (code && store.get(storageKeys.room) === code) store.del(storageKeys.room);
    state.room = '';
    state.token = '';
    state.mySide = '';
    state.app = null;
    state.selected = null;
    state.targets = [];
    state.pending = null;
    dom.game.hidden = true;
    dom.start.hidden = false;
    dom.roomInput.value = code || '';
    try {
      var url = new URL(location.href);
      url.searchParams.delete('room');
      history.replaceState(null, '', url.pathname + url.search + url.hash);
    } catch (error) { /* 忽略 */ }
    setConn('idle', 'none');
  }

  /* ====================================================================== */
  /* 第七节 · 房间号输入 / 复制链接                                          */
  /* ====================================================================== */

  function sanitizeRoomInput(value) {
    var upper = String(value || '').toUpperCase();
    var out = '';
    var dropped = false;
    for (var i = 0; i < upper.length && out.length < XQ.roomLength; i += 1) {
      var ch = upper.charAt(i);
      if (XQ.roomAlphabet.indexOf(ch) >= 0) out += ch;
      else if (ch.trim()) dropped = true;
    }
    return { value: out, dropped: dropped };
  }

  async function copyRoomLink() {
    if (!state.room) return;
    var link = location.origin + location.pathname + '?room=' + state.room;
    var ok = false;
    try {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
        await navigator.clipboard.writeText(link);
        ok = true;
      }
    } catch (error) { ok = false; }
    if (!ok) {
      try {
        var helper = document.createElement('textarea');
        helper.value = link;
        helper.setAttribute('readonly', 'readonly');
        helper.style.position = 'fixed';
        helper.style.top = '-1000px';
        helper.style.opacity = '0';
        document.body.appendChild(helper);
        helper.select();
        ok = document.execCommand('copy');
        document.body.removeChild(helper);
      } catch (error) { ok = false; }
    }
    var previous = dom.copyLabel.textContent;
    dom.copyLabel.textContent = ok ? '已复制' : '复制失败';
    setTimeout(function () { dom.copyLabel.textContent = previous; }, 1600);
    if (ok) toast('房间链接已复制：' + link, 'ok');
    else toast('复制失败，请手动复制地址栏里的 ?room=' + state.room, 'error');
  }

  /* ====================================================================== */
  /* 第八节 · 事件绑定与启动                                                 */
  /* ====================================================================== */

  function collectDom() {
    dom.boardShell = $('board-shell');
    dom.board = $('board');
    dom.boardHint = $('board-hint');
    dom.overlay = $('board-overlay');
    dom.overlayText = $('overlay-text');
    dom.overlayFlip = $('overlay-flip');
    dom.conn = $('conn');
    dom.connText = $('conn-text');
    dom.start = $('start');
    dom.game = $('game');
    dom.roomInput = $('room-input');
    dom.startError = $('start-error');
    dom.btnCreate = $('btn-create');
    dom.btnJoin = $('btn-join');
    dom.roomCode = $('room-code');
    dom.btnCopy = $('btn-copy');
    dom.copyLabel = $('copy-label');
    dom.statusLine = $('status-line');
    dom.seatTop = $('seat-top');
    dom.seatSelf = $('seat-self');
    dom.alert = $('alert');
    dom.result = $('result');
    dom.movelist = $('movelist');
    dom.moveCount = $('move-count');
    dom.btnNew = $('btn-new');
    dom.btnResign = $('btn-resign');
    dom.btnLeave = $('btn-leave');
    dom.btnFlip = $('btn-flip');
    dom.metaSeq = $('meta-seq');
    dom.metaSide = $('meta-side');
  }

  function bindEvents() {
    dom.btnCreate.addEventListener('click', function () { createRoom(); });
    dom.btnJoin.addEventListener('click', function () {
      var code = XQ.normalizeRoom(dom.roomInput.value);
      if (!code) {
        dom.startError.textContent = '请先输入 6 位房间号';
        dom.startError.hidden = false;
        dom.roomInput.focus();
        return;
      }
      joinRoom(code, '');
    });
    dom.roomInput.addEventListener('input', function () {
      var result = sanitizeRoomInput(dom.roomInput.value);
      if (result.value !== dom.roomInput.value) dom.roomInput.value = result.value;
      dom.roomInput.classList.remove('is-bad');
      dom.startError.hidden = true;
      if (result.dropped) {
        dom.startError.textContent = '房间号只含 ' + XQ.roomAlphabet + '（已自动过滤其它字符）';
        dom.startError.hidden = false;
        dom.startError.className = 'alert alert-warn';
      } else {
        dom.startError.className = 'alert alert-error';
      }
    });
    dom.roomInput.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') {
        event.preventDefault();
        dom.btnJoin.click();
      }
    });

    dom.btnCopy.addEventListener('click', function () { copyRoomLink(); });
    dom.btnNew.addEventListener('click', function () { postAction('/new', '开新局'); });
    dom.btnResign.addEventListener('click', function () {
      if (XQ.isOver(state.app)) return;
      if (globalThis.confirm && !globalThis.confirm('确定认输吗？本局将判负。')) return;
      postAction('/resign', '认输');
    });
    dom.btnLeave.addEventListener('click', function () { leaveRoom(); });
    dom.btnFlip.addEventListener('click', function () { flipBoard(); });
    dom.overlayFlip.addEventListener('click', function () { flipBoard(); });

    dom.board.addEventListener('click', onBoardClick);
    dom.board.addEventListener('mousemove', onBoardMove);
    dom.board.addEventListener('mouseleave', function () { dom.board.classList.remove('can-pick'); });
    dom.board.addEventListener('contextmenu', function (event) { event.preventDefault(); });

    window.addEventListener('pagehide', function () { state.pollAbort = true; });
    window.addEventListener('pageshow', function () {
      if (state.room && state.conn.phase === 'idle') connect();
    });
    window.addEventListener('focus', function () {
      if (state.room && state.conn.phase === 'idle') connect();
    });
  }

  function flipBoard() {
    state.flipped = !state.flipped;
    render();
  }

  function boot() {
    collectDom();
    bindEvents();
    setupCanvas();

    var params = null;
    try { params = new URL(location.href).searchParams; } catch (error) { params = null; }
    var fromUrl = params ? XQ.normalizeRoom(params.get('room') || '') : '';
    var stored = XQ.normalizeRoom(store.get(storageKeys.room) || '');
    var code = XQ.isValidRoom(fromUrl) ? fromUrl : (XQ.isValidRoom(stored) ? stored : '');

    render();

    if (code) {
      var token = store.get(storageKeys.token(code)) || '';
      if (token) {
        resumeSession(code).then(function (restored) {
          if (!restored) {
            if (store.get(storageKeys.room) === code) store.del(storageKeys.room);
            dom.roomInput.value = code;
            dom.startError.textContent = '未能自动恢复房间 ' + code + '，请重新加入。';
            dom.startError.hidden = false;
          }
        });
        return;
      }
      dom.roomInput.value = code;
      joinRoom(code, '').then(function (joined) {
        if (!joined) dom.roomInput.focus();
      });
      return;
    }

    dom.roomInput.focus();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
}());
