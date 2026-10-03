/* 中国象棋 · 联机网页版 —— 纯函数层（无任何 DOM / 网络访问）
 * ---------------------------------------------------------------------------
 * 本文件只做“数据 -> 数据”的转换，可在 Node 中直接单元测试：
 *
 *     node --check logic.js
 *     node -e "require('./logic.js'); ..."        // 见文件末尾的 globalThis.XQ
 *
 * 设计取舍：客户端**不实现任何象棋规则**。所有合法性判定都由 Python 服务器完成，
 * 客户端只负责：
 *   * 解析 ICCS 坐标 / FEN 字符串（只为画棋盘与判断“这枚子是不是我的”）；
 *   * 把服务器事件（events）按 seq 幂等地合并进本地状态；
 *   * 用服务器给的 legal 表画提示点。
 *
 * 坐标约定（与 Python 端 constants.py 完全一致）：
 *   rank 0 = 红方底线（棋盘下方），rank 9 = 黑方底线；
 *   file 0 = a 列（红方视角最左），file 8 = i 列。
 *   ICCS 记谱把两者拼起来：h2 = rank 2, file 7。
 * ---------------------------------------------------------------------------
 */
(function (root) {
  'use strict';

  var XQ = {};

  /* ====================================================================== */
  /* 常量                                                                    */
  /* ====================================================================== */

  var FILE_LETTERS = 'abcdefghi';
  var SIDES = ['r', 'b'];

  /** 阵营中文名。 */
  var SIDE_NAMES = { r: '红方', b: '黑方' };

  /** 棋子字母 -> 传统汉字名。大写为红方，小写为黑方。 */
  var PIECE_NAMES = {
    r: { K: '帅', A: '仕', B: '相', N: '马', R: '车', C: '炮', P: '兵' },
    b: { K: '将', A: '士', B: '象', N: '马', R: '车', C: '炮', P: '卒' }
  };

  /** 房间号字母表（去掉了 0/O/1/I 等易混字符），与服务器 protocol.py 一致。 */
  var ROOM_ALPHABET = 'ACDEFGHJKLMNPQRSTUVWXYZ23456789';
  var ROOM_LEN = 6;
  var START_FEN = 'rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1';

  /** 服务器错误码 -> 中文提示（协议未给 msg 时兜底）。 */
  var ERROR_TEXT = {
    bad_request: '请求格式不正确',
    room_not_found: '房间不存在或已回收',
    room_full: '房间已满（红黑两方都已入座）',
    forbidden: '身份校验失败，无法恢复座位',
    not_your_turn: '还没轮到你走棋',
    illegal_move: '不合法的着法',
    stale: '局面已更新，请按最新局面重新走棋',
    game_over: '对局已结束，请开始新的一局',
    too_large: '请求内容过大',
    bad_room_code: '房间号格式不正确',
    bad_response: '服务器返回了无法解析的内容',
    network: '网络请求失败，请检查与服务器的连接'
  };

  /* ====================================================================== */
  /* 小工具                                                                  */
  /* ====================================================================== */

  function isInt(value) {
    return typeof value === 'number' && isFinite(value) && Math.floor(value) === value;
  }

  function toInt(value, fallback) {
    var n = typeof value === 'number' ? value : parseInt(value, 10);
    return isFinite(n) ? Math.trunc(n) : fallback;
  }

  /** 浅拷贝 + 需要时深拷贝数组/对象（不依赖 structuredClone，兼容性优先）。 */
  function cloneState(state) {
    var out = {};
    for (var key in state) {
      if (!Object.prototype.hasOwnProperty.call(state, key)) continue;
      var value = state[key];
      if (Array.isArray(value)) {
        out[key] = value.map(function (item) {
          return (item && typeof item === 'object') ? shallowCopy(item) : item;
        });
      } else if (value && typeof value === 'object') {
        out[key] = shallowCopy(value);
      } else {
        out[key] = value;
      }
    }
    return out;
  }

  function shallowCopy(obj) {
    var out = {};
    for (var key in obj) {
      if (Object.prototype.hasOwnProperty.call(obj, key)) out[key] = obj[key];
    }
    return out;
  }

  /* ====================================================================== */
  /* 名称                                                                    */
  /* ====================================================================== */

  /** 阵营 -> "红方" / "黑方"。 */
  XQ.sideName = function (side) {
    return SIDE_NAMES[side] || '未知';
  };

  /** "红方" -> 'r'；"黑方" -> 'b'；无法识别返回 ''。 */
  XQ.sideOfName = function (name) {
    if (name === SIDE_NAMES.r) return 'r';
    if (name === SIDE_NAMES.b) return 'b';
    return '';
  };

  /** 另一方。 */
  XQ.other = function (side) {
    return side === 'r' ? 'b' : (side === 'b' ? 'r' : '');
  };

  /** 棋子字母 -> 汉字；非法输入返回 '?'。 */
  XQ.pieceName = function (piece) {
    if (typeof piece !== 'string' || piece.length !== 1) return '?';
    var upper = piece.toUpperCase();
    if (!PIECE_NAMES.r[upper]) return '?';
    return PIECE_NAMES[piece === upper ? 'r' : 'b'][upper];
  };

  /** 棋子字母 -> 'r' / 'b'（大写红、小写黑）。 */
  XQ.pieceSide = function (piece) {
    if (typeof piece !== 'string' || !piece) return '';
    return piece === piece.toUpperCase() ? 'r' : 'b';
  };

  /* ====================================================================== */
  /* 坐标与 ICCS                                                             */
  /* ====================================================================== */

  /** 坐标 -> 方格名，如 (2, 1) -> "b2"。 */
  XQ.squareName = function (rank, file) {
    rank = toInt(rank, -1);
    file = toInt(file, -1);
    if (rank < 0 || rank > 9 || file < 0 || file > 8) return '';
    return FILE_LETTERS.charAt(file) + rank;
  };

  /** 方格名 -> {rank, file}；非法返回 null。 */
  XQ.parseSquare = function (name) {
    if (typeof name !== 'string' || name.length < 2) return null;
    var file = FILE_LETTERS.indexOf(name.charAt(0).toLowerCase());
    var rank = name.charCodeAt(1) - 48;
    if (file < 0 || rank < 0 || rank > 9) return null;
    return { rank: rank, file: file };
  };

  /** (rank, file) -> ICCS 两字符，如 (0, 0) -> "a0"。 */
  XQ.toIccs = function (rank, file) {
    return XQ.squareName(rank, file);
  };

  /**
   * ICCS 记谱 -> {fr, fc, tr, tc}；非法返回 null。
   * parseIccs('h2e2') -> {fr: 2, fc: 7, tr: 2, tc: 4}
   */
  XQ.parseIccs = function (text) {
    if (typeof text !== 'string') return null;
    var raw = text.trim().toLowerCase().replace(/[\s,-]/g, '');
    if (raw.length !== 4) return null;
    var from = XQ.parseSquare(raw.slice(0, 2));
    var to = XQ.parseSquare(raw.slice(2, 4));
    if (!from || !to) return null;
    return { fr: from.rank, fc: from.file, tr: to.rank, tc: to.file };
  };

  /** {fr,fc,tr,tc}（或带 from/to 字段的对象）-> "h2e2"。 */
  XQ.iccsOf = function (move) {
    if (!move) return '';
    var from = XQ.squareName(move.fr, move.fc);
    var to = XQ.squareName(move.tr, move.tc);
    return from && to ? from + to : '';
  };

  /** ICCS 记谱 -> "h2 → e2"（棋盘翻转时由调用方决定是否反转，这里保持原样）。 */
  XQ.iccsArrow = function (text) {
    var move = XQ.parseIccs(text);
    if (!move) return String(text || '');
    return XQ.squareName(move.fr, move.fc) + ' → ' + XQ.squareName(move.tr, move.tc);
  };

  /* ====================================================================== */
  /* FEN                                                                     */
  /* ====================================================================== */

  /**
   * FEN 的棋盘段 -> 10×9 的二维数组，元素为棋子字母或 ''。
   * 第 0 行是黑方底线（FEN 从黑方底线写起），第 9 行是红方底线；
   * 与项目内部 board[rank][file] 恰好相反，解析后统一按 rank 存放：
   *   grid[rank][file]，rank 0 = 红方底线，rank 9 = 黑方底线。
   */
  XQ.parseFenBoard = function (fen) {
    var grid = [];
    for (var r = 0; r < 10; r += 1) grid.push(new Array(9).fill(''));
    if (typeof fen !== 'string') return grid;
    var rows = fen.trim().split(/\s+/)[0].split('/');
    if (rows.length < 10) return grid;
    for (var i = 0; i < 10; i += 1) {
      var fenRow = 9 - i;                 // FEN 第 0 行 = 黑方底线 = rank 9
      var file = 0;
      var row = rows[i];
      for (var j = 0; j < row.length; j += 1) {
        var ch = row.charAt(j);
        if (ch >= '1' && ch <= '9') {
          file += ch.charCodeAt(0) - 48;
        } else {
          if (file < 9) grid[fenRow][file] = ch;
          file += 1;
        }
      }
    }
    return grid;
  };

  /** FEN 中的走子方（'r' / 'b'），缺省为 'r'。 */
  XQ.fenSide = function (fen) {
    if (typeof fen !== 'string') return '';
    var parts = fen.trim().split(/\s+/);
    if (parts.length > 1 && (parts[1] === 'w' || parts[1] === 'b')) {
      return parts[1] === 'w' ? 'r' : 'b';
    }
    return '';
  };

  /** 取 FEN 中某格的棋子字母（'' 表示空）。 */
  XQ.pieceAt = function (fen, rank, file) {
    var grid = XQ.parseFenBoard(fen);
    var square = XQ.parseSquare(typeof rank === 'string' ? rank : XQ.squareName(rank, file));
    if (!square) return '';
    return grid[square.rank][square.file] || '';
  };

  /* ====================================================================== */
  /* 状态                                                                    */
  /* ====================================================================== */

  var ACTIVE = 'active';      // 至少一方在座（对局进行中）
  var WAITING = 'waiting';    // 房间还没有对手
  var OVER = 'over';          // 已分出胜负 / 和棋

  /** 建一个空状态（服务器快照到来前使用）。 */
  XQ.makeState = function () {
    return {
      room: '',
      seq: 0,
      fen: START_FEN,
      side: 'r',
      turnName: '红方',
      check: false,
      legal: {},
      seats: { r: { joined: false, connected: false }, b: { joined: false, connected: false } },
      moves: [],
      lastMove: null,
      result: null,
      resultText: '',
      serverMs: 0,
      log: []
    };
  };

  /** 状态 -> 显示用阶段：'active' / 'waiting' / 'over'。 */
  XQ.phaseOf = function (state) {
    if (!state) return WAITING;
    if (state.result) return OVER;
    var seats = state.seats || {};
    if (seats.r && seats.b && seats.r.joined && seats.b.joined) return ACTIVE;
    return WAITING;
  };

  /** 对局是否已结束。 */
  XQ.isOver = function (state) {
    return !!(state && state.result);
  };

  var STATUS_TEXT = {
    active: '对局进行中',
    waiting: '等待对手加入',
    over: '对局结束'
  };

  XQ.statusText = function (state) {
    return STATUS_TEXT[XQ.phaseOf(state)] || '';
  };

  /** 是否轮到我走棋（轮次 + 对手在座 + 未结束）。 */
  XQ.isMyTurn = function (state, mySide) {
    if (!state || !mySide) return false;
    if (XQ.isOver(state)) return false;
    if (state.side !== mySide) return false;
    return XQ.phaseOf(state) === ACTIVE;
  };

  /** 我的将/帅是否正被将军。 */
  XQ.isCheckOn = function (state, side) {
    return !!(state && state.check && state.side === side && !XQ.isOver(state));
  };

  /* ====================================================================== */
  /* 事件与快照                                                              */
  /* ====================================================================== */

  /** 事件 -> 一行的中文描述（着法列表用）。 */
  XQ.describeEvent = function (ev) {
    if (!ev) return '';
    var type = ev.t || '';
    if (type === 'move') {
      var who = (ev.side === 'r' || ev.side === 'b') ? XQ.sideName(ev.side) : '';
      var body = ev.chinese || XQ.iccsArrow(ev.iccs);
      return who ? who + ' ' + body : body;
    }
    if (type === 'end') {
      return ev.result_text || ev.text || ('对局结束' + (ev.reason ? '（' + ev.reason + '）' : ''));
    }
    if (type === 'resign') {
      return (ev.side ? XQ.sideName(ev.side) : '一方') + '认输';
    }
    if (type === 'new') {
      return '新开一局' + (ev.side ? '（' + XQ.sideName(ev.side) + '发起）' : '');
    }
    if (type === 'join') {
      return ev.name ? (ev.name + ' 入座' + (ev.side ? '（' + XQ.sideName(ev.side) + '）' : ''))
                     : ((ev.side ? XQ.sideName(ev.side) : '一方') + '入座');
    }
    if (type === 'presence') {
      return (ev.side ? XQ.sideName(ev.side) : '一方') + (ev.connected ? '已连接' : '已断开');
    }
    if (type === 'start') return '房间已创建';
    return type;
  };

  /** 记谱序号 -> "第 12 手"。 */
  XQ.formatSeq = function (seq) {
    var n = toInt(seq, -1);
    return n < 0 ? '第 ? 手' : '第 ' + n + ' 手';
  };

  /** 房间号 -> 'A7K2…' 之类的短码。 */
  XQ.shortRoom = function (code) {
    var text = String(code == null ? '' : code).trim().toUpperCase();
    if (text.length <= 4) return text;
    return text.slice(0, 4) + '…';
  };

  /** 房间号规范化：去空白、转大写（不做合法性判定，交给服务器）。 */
  XQ.normalizeRoom = function (code) {
    return String(code == null ? '' : code).replace(/[^0-9A-Za-z]/g, '').toUpperCase();
  };

  /** 房间号是否合法（6 位、字母表内）。 */
  XQ.isValidRoom = function (code) {
    var text = XQ.normalizeRoom(code);
    if (text.length !== ROOM_LEN) return false;
    for (var i = 0; i < text.length; i += 1) {
      if (ROOM_ALPHABET.indexOf(text.charAt(i)) < 0) return false;
    }
    return true;
  };

  XQ.roomAlphabet = ROOM_ALPHABET;
  XQ.roomLength = ROOM_LEN;
  XQ.startFen = START_FEN;
  XQ.errorText = function (code, msg) {
    if (msg) return msg;
    return ERROR_TEXT[code] || ('未知错误：' + code);
  };
  XQ.ERROR_TEXT = ERROR_TEXT;

  /** 把一条事件合并进 state（原地修改，供 applyEvents 内部使用）。 */
  function applyOne(state, ev) {
    var type = ev.t || '';
    var seq = toInt(ev.seq, state.seq);
    if (seq > state.seq) state.seq = seq;

    if (type === 'move') {
      state.moves = state.moves.concat([{
        seq: seq,
        side: ev.side || '',
        iccs: ev.iccs || '',
        chinese: ev.chinese || XQ.iccsArrow(ev.iccs)
      }]);
      if (typeof ev.fen === 'string' && ev.fen) state.fen = ev.fen;
      state.lastMove = (ev.from && ev.to) ? { from: ev.from, to: ev.to } : null;
      state.check = !!ev.check;
      if (!state.result) state.resultText = '';
      if (ev.side) state.side = XQ.other(ev.side);   // 服务器权威：走子方随后交换
      return;
    }

    if (type === 'end') {
      state.result = {
        winner: (ev.winner === 'r' || ev.winner === 'b') ? ev.winner : null,
        reason: ev.reason || '',
        text: ev.result_text || ev.text || ''
      };
      state.resultText = state.result.text;
      state.check = false;
      return;
    }

    if (type === 'resign') {
      if (!state.result) {
        var loser = (ev.side === 'r' || ev.side === 'b') ? ev.side : state.side;
        var winner = XQ.other(loser);
        state.result = {
          winner: winner,
          reason: XQ.sideName(loser) + '认输',
          text: XQ.sideName(winner) + '胜（' + XQ.sideName(loser) + '认输）'
        };
        state.resultText = state.result.text;
        state.check = false;
      }
      return;
    }

    if (type === 'new') {
      state.moves = [];
      state.lastMove = null;
      state.check = false;
      state.result = null;
      state.resultText = '';
      state.legal = {};
      state.side = 'r';
      state.turnName = '红方';
      state.fen = START_FEN;
      state.log = [];
      return;
    }

    if (type === 'join') {
      var side = (ev.side === 'r' || ev.side === 'b') ? ev.side : '';
      if (side) {
        var joined = shallowCopy(state.seats[side] || { joined: false, connected: false, name: '' });
        joined.joined = true;
        if (ev.name) joined.name = ev.name;
        state.seats = shallowCopy(state.seats);
        state.seats[side] = joined;
      }
      return;
    }

    if (type === 'presence') {
      var who = (ev.side === 'r' || ev.side === 'b') ? ev.side : '';
      if (who) {
        var seat = state.seats[who] || { joined: true, connected: false, name: '' };
        seat = shallowCopy(seat);
        seat.connected = !!ev.connected;
        state.seats[who] = seat;
      }
      return;
    }
    /* start / 未知类型：只推进 seq。 */
  }

  /**
   * 合并一批事件，返回**新**状态。
   *
   * 幂等且容忍乱序：
   *   * 忽略 seq <= state.seq 的事件（重放、重复、迟到）；
   *   * 其余按 seq 升序应用；
   *   * 一条事件都没用上时返回原对象（方便调用方做 === 判断）。
   */
  XQ.applyEvents = function (state, events) {
    var base = state || XQ.makeState();
    if (!Array.isArray(events) || !events.length) return base;

    var fresh = [];
    for (var i = 0; i < events.length; i += 1) {
      var ev = events[i];
      if (!ev || typeof ev !== 'object') continue;
      var seq = toInt(ev.seq, -1);
      if (seq < 0) continue;
      if (seq <= base.seq) continue;      // 过期 / 重复
      fresh.push(ev);
    }
    if (!fresh.length) return base;

    fresh.sort(function (a, b) { return toInt(a.seq, 0) - toInt(b.seq, 0); });

    var next = cloneState(base);
    next.moves = base.moves.slice();
    next.log = (base.log || []).slice();
    for (var k = 0; k < fresh.length; k += 1) {
      var event = fresh[k];
      applyOne(next, event);
      var line = XQ.describeEvent(event);
      if (line) next.log.push({ seq: toInt(event.seq, next.seq), text: line, type: event.t || '' });
    }
    return next;
  };

  /** 服务器快照 -> 新状态（快照永远是权威，直接替换）。 */
  XQ.applySnapshot = function (state, snapshot) {
    var snap = snapshot && typeof snapshot === 'object' ? snapshot : {};
    var next = XQ.makeState();
    var previous = state || {};

    next.room = String(snap.room || previous.room || '');
    next.seq = toInt(snap.seq, 0);
    next.fen = typeof snap.fen === 'string' && snap.fen ? snap.fen : START_FEN;
    next.side = (snap.side === 'b') ? 'b' : (snap.side === 'r' ? 'r' : XQ.fenSide(next.fen) || 'r');
    next.turnName = snap.turn_name || XQ.sideName(next.side);
    next.check = !!snap.check;

    next.legal = {};
    if (snap.legal && typeof snap.legal === 'object') {
      for (var square in snap.legal) {
        if (!Object.prototype.hasOwnProperty.call(snap.legal, square)) continue;
        var targets = snap.legal[square];
        next.legal[square] = Array.isArray(targets) ? targets.slice() : [];
      }
    }

    var seats = { r: { joined: false, connected: false, name: '' },
                  b: { joined: false, connected: false, name: '' } };
    for (var s = 0; s < SIDES.length; s += 1) {
      var side = SIDES[s];
      var raw = snap.seats && snap.seats[side];
      if (raw && typeof raw === 'object') {
        seats[side] = {
          joined: !!raw.joined,
          connected: !!raw.connected,
          name: raw.name || ''
        };
      }
    }
    next.seats = seats;

    next.moves = [];
    if (Array.isArray(snap.moves)) {
      for (var m = 0; m < snap.moves.length; m += 1) {
        var item = snap.moves[m];
        if (!item || typeof item !== 'object') continue;
        next.moves.push({
          seq: toInt(item.seq, 0),
          side: item.side || '',
          iccs: item.iccs || '',
          chinese: item.chinese || XQ.iccsArrow(item.iccs)
        });
      }
    }

    next.lastMove = (snap.last_move && snap.last_move.from && snap.last_move.to)
      ? { from: String(snap.last_move.from), to: String(snap.last_move.to) }
      : null;

    next.result = null;
    if (snap.result && typeof snap.result === 'object') {
      next.result = {
        winner: (snap.result.winner === 'r' || snap.result.winner === 'b') ? snap.result.winner : null,
        reason: snap.result.reason || '',
        text: snap.result.text || ''
      };
    }
    next.resultText = snap.result_text || (next.result ? next.result.text : '');
    next.serverMs = toInt(snap.server_ms, 0);

    /* 续局时保留事件日志（记谱列表右侧的提示行），新局则清空。 */
    var continuing = next.moves.length > 0 && previous.room === next.room;
    next.log = continuing ? (previous.log || []).slice() : [];

    return next;
  };

  /**
   * 服务器 t=events 应答 -> 新状态。
   * 应答里的顶层 seq 表示服务器的总序号：事件不足时只推进 seq。
   */
  XQ.applyResponse = function (state, message) {
    var base = state || XQ.makeState();
    var msg = message || {};
    if (msg.t === 'snapshot' && msg.snapshot) return XQ.applySnapshot(base, msg.snapshot);
    var next = XQ.applyEvents(base, msg.events);
    var serverSeq = toInt(msg.seq, -1);
    if (serverSeq > next.seq) {
      next = (next === base) ? cloneState(base) : next;
      next.seq = serverSeq;
      next.moves = next.moves.slice();
      next.log = (next.log || []).slice();
    }
    return next;
  };

  /* ====================================================================== */
  /* 选中与合法落点                                                          */
  /* ====================================================================== */

  /**
   * 这一格能否选中？
   * 三个条件：轮到我、对局未结束、格内棋子的颜色属于我。
   * 棋子颜色**从 FEN 推导**（大写红、小写黑），不依赖客户端规则引擎。
   */
  XQ.canSelect = function (state, square, mySide) {
    if (!state || !mySide) return false;
    if (!XQ.isMyTurn(state, mySide)) return false;
    var name = (typeof square === 'string') ? square : XQ.squareName(square && square.rank, square && square.file);
    var parsed = XQ.parseSquare(name);
    if (!parsed) return false;
    var piece = XQ.pieceAt(state.fen, parsed.rank, parsed.file);
    if (!piece) return false;
    return XQ.pieceSide(piece) === mySide;
  };

  /** 某一格的合法落点数组（来自服务器给的 state.legal）；无则空数组。 */
  XQ.legalFrom = function (state, square) {
    if (!state || !state.legal) return [];
    var name = (typeof square === 'string') ? square : XQ.squareName(square && square.rank, square && square.file);
    var targets = state.legal[name];
    return Array.isArray(targets) ? targets.slice() : [];
  };

  /** 目标格是否在合法落点内。 */
  XQ.isLegalTarget = function (state, from, to) {
    return XQ.legalFrom(state, from).indexOf(to) >= 0;
  };

  /** 记谱轮次：[(序号, 红着, 黑着), ...]，用于两列显示。 */
  XQ.moveRounds = function (moves) {
    var list = Array.isArray(moves) ? moves : [];
    var rounds = [];
    for (var i = 0; i < list.length; i += 2) {
      rounds.push({
        round: i / 2 + 1,
        red: list[i] ? (list[i].chinese || XQ.iccsArrow(list[i].iccs)) : '',
        black: list[i + 1] ? (list[i + 1].chinese || XQ.iccsArrow(list[i + 1].iccs)) : ''
      });
    }
    return rounds;
  };

  /** 走子方 + 记谱 -> "红方 炮二平五"。 */
  XQ.moveText = function (side, chinese) {
    var who = (side === 'r' || side === 'b') ? XQ.sideName(side) : '';
    return who ? who + ' ' + (chinese || '') : String(chinese || '');
  };

  /* ====================================================================== */

  XQ.SIDES = SIDES;
  XQ.FILE_LETTERS = FILE_LETTERS;
  XQ.PHASE = { ACTIVE: ACTIVE, WAITING: WAITING, OVER: OVER };

  root.XQ = XQ;
  if (typeof module !== 'undefined' && module.exports) module.exports = XQ;
}(typeof globalThis !== 'undefined' ? globalThis : this));
