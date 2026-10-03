/* 中国象棋 · 离线规则引擎（纯 JS，零依赖，浏览器与 Node 通用）
 * ---------------------------------------------------------------------------
 * 联机版由 Python 服务器判定规则；断网 / 单机模式没有服务器，所以这里把
 * xiangqi/board.py、xiangqi/notation.py、xiangqi/ai.py 的规则**逐条搬到浏览器**：
 *
 *   XQEngine.legalMap(fen)          -> { "起点": ["终点", ...] }（与服务器快照同形）
 *   XQEngine.moves(fen)             -> ["a0a1", ...]（全部合法着法，顺序与 Python 一致）
 *   XQEngine.applyMove(fen, iccs)   -> 走子结果（含新 FEN、记谱、将军/胜负）
 *   XQEngine.chinese(fen, iccs)     -> "炮二平五"（走子**之前**的记谱）
 *   XQEngine.status(fen, history)   -> 将军 / 结束 / 结果文本
 *   XQEngine.bestMove(fen, level, seed) -> AI 着法（Alpha-Beta 负极大值）
 *   XQEngine.perft(fen, depth)      -> 结点计数（与 Python Board.perft 对照）
 *
 * 坐标与常量约定与 constants.py 完全一致：
 *   board[rank][file]，rank 0 = 红方底线，rank 9 = 黑方底线；
 *   file 0 = a 列（红方视角最左），file 8 = i 列；河界在 rank 4 与 rank 5 之间。
 * 棋子用字母（大写红、小写黑）：K 帅 A 仕 B 相 N 马 R 车 C 炮 P 兵。
 *
 * 代码风格：ES5（var / function），不依赖任何第三方库；棋盘的解析等纯数据转换
 * 直接复用同目录 logic.js 导出的 globalThis.XQ（浏览器里 logic.js 先加载）。
 * ---------------------------------------------------------------------------
 */
(function (root) {
  'use strict';

  /* logic.js 的纯函数层：浏览器里由 <script> 先加载好，Node 里按需 require。 */
  var XQ = root.XQ || (typeof require === 'function' ? require('./logic.js') : null);

  /* ====================================================================== */
  /* 常量                                                                    */
  /* ====================================================================== */

  /** 标准开局 FEN（与 constants.START_FEN 一致）。 */
  var START_FEN = 'rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1';

  /** 阵营中文名（与 constants.SIDE_NAMES 一致）。 */
  var SIDE_NAMES = { r: '红方', b: '黑方' };

  var FILES = 'abcdefghi';

  /** 棋子类型 -> 汉字名（大写红 / 小写黑）。 */
  var NAMES = {
    r: { K: '帅', A: '仕', B: '相', N: '马', R: '车', C: '炮', P: '兵' },
    b: { K: '将', A: '士', B: '象', N: '马', R: '车', C: '炮', P: '卒' }
  };

  /** 棋子类型 -> 子力价值（与 ai.PIECE_VALUE 一致）。 */
  var PIECE_VALUE = { K: 60000, R: 1000, C: 500, N: 450, B: 250, A: 250, P: 100 };
  var MATE = 100000;
  var INF = MATE * 2;

  /** 连续 60 回合（120 个半回合）无吃子判和（与 board.NATURAL_LIMIT_PLIES 一致）。 */
  var NATURAL_LIMIT_PLIES = 120;

  var ORTHO = [[1, 0], [-1, 0], [0, 1], [0, -1]];
  var DIAG = [[1, 1], [1, -1], [-1, 1], [-1, -1]];
  var ELEPHANT = [[2, 2], [2, -2], [-2, 2], [-2, -2]];
  var HORSE = [[2, 1], [2, -1], [-2, 1], [-2, -1],
               [1, 2], [1, -2], [-1, 2], [-1, -2]];

  /**
   * Alpha-Beta 搜索的空闲上限：``[最大深度, 时间上限(秒), 随机化分值, 结点上限]``。
   * 深度/时间/随机化取值参照 ai.LEVELS；结点上限是给浏览器兜底的硬闸门，
   * 保证低端设备上也不会假死（Python 端没有这个限制）。
   */
  var LEVELS = {
    easy: [2, 1.5, 120, 40000],
    normal: [4, 3.0, 40, 160000],
    hard: [6, 6.0, 0, 400000]
  };
  var LEVEL_NAMES = { easy: '入门', normal: '普通', hard: '较难' };

  /* ====================================================================== */
  /* 小工具                                                                  */
  /* ====================================================================== */

  function other(side) {
    return side === 'r' ? 'b' : 'r';
  }

  /** 棋子字母 -> 阵营（大写红、小写黑）。 */
  function sideOf(piece) {
    return (piece >= 'A' && piece <= 'Z') ? 'r' : 'b';
  }

  /** 棋子字母 -> 类型字母。 */
  function typeOf(piece) {
    return piece.toUpperCase();
  }

  /** 阵营 -> 前进方向（rank 增量）。 */
  function forwardOf(side) {
    return side === 'r' ? 1 : -1;
  }

  /** 该方在 ``rank`` 行的兵/卒是否已过河。 */
  function crossedRiver(side, rank) {
    return side === 'r' ? rank >= 5 : rank <= 4;
  }

  /** 坐标是否在九宫内。 */
  function inPalace(side, rank, file) {
    if (file < 3 || file > 5) return false;
    return side === 'r' ? (rank >= 0 && rank <= 2) : (rank >= 7 && rank <= 9);
  }

  /** 坐标是否在指定方的半场（相/象不得过河）。 */
  function ownHalf(side, rank) {
    return side === 'r' ? rank <= 4 : rank >= 5;
  }

  /** 生成 ``rows`` 行 ``cols`` 列的空二维数组。 */
  function empty2d(rows, cols) {
    var out = [];
    for (var r = 0; r < rows; r += 1) {
      var row = [];
      for (var c = 0; c < cols; c += 1) row.push('');
      out.push(row);
    }
    return out;
  }

  /** 深拷贝棋盘（两行互换等操作会改动外层数组）。 */
  function cloneGrid(grid) {
    var out = [];
    for (var r = 0; r < 10; r += 1) out.push(grid[r].slice());
    return out;
  }

  /** ICCS 两字符 -> ``{rank, file}``；非法返回 null。 */
  function parseSquareName(text) {
    if (typeof text !== 'string' || text.length !== 2) return null;
    var file = FILES.indexOf(text.charAt(0).toLowerCase());
    var rank = text.charCodeAt(1) - 48;
    if (file < 0 || rank < 0 || rank > 9) return null;
    return { rank: rank, file: file };
  }

  function squareName(rank, file) {
    return FILES.charAt(file) + rank;
  }

  /* ====================================================================== */
  /* 局面                                                                    */
  /* ====================================================================== */

  /**
   * FEN -> 内部局面。宽松接受 ``开始局面`` 简写（无走子方时按红方），
   * 与 board.Board.set_fen 的默认值一致；非法 FEN 抛 Error。
   */
  function parsePosition(fen) {
    if (typeof fen !== 'string') throw new Error('FEN 必须是字符串');
    var parts = fen.trim().split(/\s+/);
    if (!parts[0]) throw new Error('FEN 不能为空');
    var grid = XQ.parseFenBoard(parts[0]);
    var reds = 0;
    var blacks = 0;
    for (var rank = 0; rank < 10; rank += 1) {
      for (var file = 0; file < 9; file += 1) {
        var piece = grid[rank][file];
        if (piece === 'K') reds += 1;
        else if (piece === 'k') blacks += 1;
      }
    }
    if (reds !== 1 || blacks !== 1) {
      throw new Error('FEN 必须各有一个将/帅（红 ' + reds + ' 个，黑 ' + blacks + ' 个）');
    }

    var side = 'r';
    if (parts.length > 1) {
      var token = parts[1].toLowerCase();
      if (token === 'w' || token === 'r') side = 'r';
      else if (token === 'b') side = 'b';
      else throw new Error('FEN 走子方无效: ' + parts[1]);
    }
    var halfmove = parts.length > 4 ? parseInt(parts[4], 10) : 0;
    var fullmove = parts.length > 5 ? parseInt(parts[5], 10) : 1;
    if (!isFinite(halfmove) || halfmove < 0) throw new Error('FEN 无吃子回合数无效');
    if (!isFinite(fullmove) || fullmove < 1) throw new Error('FEN 回合数无效');

    return { grid: grid, side: side, halfmove: halfmove, fullmove: fullmove };
  }

  /** 内部局面 -> FEN（与 board.Board.to_fen 输出格式一致）。 */
  function toFen(board) {
    var rows = [];
    for (var rank = 9; rank >= 0; rank -= 1) {
      var text = '';
      var empty = 0;
      for (var file = 0; file < 9; file += 1) {
        var piece = board.grid[rank][file];
        if (!piece) {
          empty += 1;
          continue;
        }
        if (empty) {
          text += empty;
          empty = 0;
        }
        text += piece;
      }
      if (empty) text += empty;
      rows.push(text);
    }
    return rows.join('/') + ' ' + (board.side === 'r' ? 'w' : 'b') + ' - - '
      + board.halfmove + ' ' + board.fullmove;
  }

  /** 找出将/帅位置；找不到返回 null。 */
  function kingOf(grid, side) {
    var target = side === 'r' ? 'K' : 'k';
    for (var rank = 0; rank < 10; rank += 1) {
      var row = grid[rank];
      for (var file = 0; file < 9; file += 1) {
        if (row[file] === target) return [rank, file];
      }
    }
    return null;
  }

  /* ====================================================================== */
  /* 着法生成                                                                */
  /* ====================================================================== */

  /** 该方在 ``(rank, file)`` 上的棋子是否为自己人。 */
  function isOwn(grid, rank, file, side) {
    if (rank < 0 || rank > 9 || file < 0 || file > 8) return false;
    var piece = grid[rank][file];
    return !!piece && sideOf(piece) === side;
  }

  /** 单枚棋子的伪合法着法（不剔除送将/照面），顺序与 board.gen_pseudo_moves 一致。 */
  function movesOfPiece(grid, rank, file, side) {
    var piece = grid[rank][file];
    var ptype = typeOf(piece);
    var moves = [];
    var i;
    var dr;
    var df;
    var nr;
    var nf;
    var occupant;

    if (ptype === 'K') {
      for (i = 0; i < ORTHO.length; i += 1) {
        nr = rank + ORTHO[i][0];
        nf = file + ORTHO[i][1];
        if (inPalace(side, nr, nf) && !isOwn(grid, nr, nf, side)) {
          moves.push([rank, file, nr, nf]);
        }
      }
      return moves;
    }

    if (ptype === 'A') {
      for (i = 0; i < DIAG.length; i += 1) {
        nr = rank + DIAG[i][0];
        nf = file + DIAG[i][1];
        if (inPalace(side, nr, nf) && !isOwn(grid, nr, nf, side)) {
          moves.push([rank, file, nr, nf]);
        }
      }
      return moves;
    }

    if (ptype === 'B') {
      for (i = 0; i < ELEPHANT.length; i += 1) {
        dr = ELEPHANT[i][0];
        df = ELEPHANT[i][1];
        nr = rank + dr;
        nf = file + df;
        if (nr < 0 || nr > 9 || nf < 0 || nf > 8) continue;
        if (!ownHalf(side, nr)) continue;
        if (grid[rank + dr / 2][file + df / 2]) continue;   // 塞象眼
        if (!isOwn(grid, nr, nf, side)) moves.push([rank, file, nr, nf]);
      }
      return moves;
    }

    if (ptype === 'N') {
      for (i = 0; i < HORSE.length; i += 1) {
        dr = HORSE[i][0];
        df = HORSE[i][1];
        nr = rank + dr;
        nf = file + df;
        if (nr < 0 || nr > 9 || nf < 0 || nf > 8) continue;
        var lr = Math.abs(dr) === 2 ? dr / 2 : 0;           // 蹩马腿
        var lf = Math.abs(dr) === 2 ? 0 : df / 2;
        if (grid[rank + lr][file + lf]) continue;
        if (!isOwn(grid, nr, nf, side)) moves.push([rank, file, nr, nf]);
      }
      return moves;
    }

    if (ptype === 'R') {
      for (i = 0; i < ORTHO.length; i += 1) {
        dr = ORTHO[i][0];
        df = ORTHO[i][1];
        nr = rank + dr;
        nf = file + df;
        while (nr >= 0 && nr <= 9 && nf >= 0 && nf <= 8) {
          occupant = grid[nr][nf];
          if (!occupant) {
            moves.push([rank, file, nr, nf]);
          } else {
            if (sideOf(occupant) !== side) moves.push([rank, file, nr, nf]);
            break;
          }
          nr += dr;
          nf += df;
        }
      }
      return moves;
    }

    if (ptype === 'C') {
      for (i = 0; i < ORTHO.length; i += 1) {
        dr = ORTHO[i][0];
        df = ORTHO[i][1];
        nr = rank + dr;
        nf = file + df;
        while (nr >= 0 && nr <= 9 && nf >= 0 && nf <= 8 && !grid[nr][nf]) {
          moves.push([rank, file, nr, nf]);                 // 不吃子时同车
          nr += dr;
          nf += df;
        }
        if (nr < 0 || nr > 9 || nf < 0 || nf > 8) continue;
        nr += dr;                                           // 越过炮架，找第一个棋子
        nf += df;
        while (nr >= 0 && nr <= 9 && nf >= 0 && nf <= 8) {
          occupant = grid[nr][nf];
          if (occupant) {
            if (sideOf(occupant) !== side) moves.push([rank, file, nr, nf]);
            break;
          }
          nr += dr;
          nf += df;
        }
      }
      return moves;
    }

    /* 兵/卒：向前一步；过河后可左右一步，永不后退。 */
    var forward = forwardOf(side);
    nr = rank + forward;
    if (nr >= 0 && nr <= 9 && !isOwn(grid, nr, file, side)) {
      moves.push([rank, file, nr, file]);
    }
    if (crossedRiver(side, rank)) {
      for (i = -1; i <= 1; i += 2) {
        nf = file + i;
        if (nf >= 0 && nf <= 8 && !isOwn(grid, rank, nf, side)) {
          moves.push([rank, file, rank, nf]);
        }
      }
    }
    return moves;
  }

  /** 全部伪合法着法；``square`` 给定时只生成该起点的着法。 */
  function pseudoMoves(board, side, square) {
    var grid = board.grid;
    var moves = [];
    for (var rank = 0; rank < 10; rank += 1) {
      for (var file = 0; file < 9; file += 1) {
        var piece = grid[rank][file];
        if (!piece || sideOf(piece) !== side) continue;
        if (square && (square[0] !== rank || square[1] !== file)) continue;
        moves = moves.concat(movesOfPiece(grid, rank, file, side));
      }
    }
    return moves;
  }

  /** ``(rank, file)`` 是否被 ``bySide`` 方攻击（含飞将照面）。 */
  function isAttacked(grid, rank, file, bySide) {
    var i;
    var dr;
    var df;
    var nr;
    var nf;
    var occupant;
    var pawn = bySide === 'r' ? 'P' : 'p';
    var horse = bySide === 'r' ? 'N' : 'n';
    var elephant = bySide === 'r' ? 'B' : 'b';
    var advisor = bySide === 'r' ? 'A' : 'a';
    var rook = bySide === 'r' ? 'R' : 'r';
    var cannon = bySide === 'r' ? 'C' : 'c';
    var king = bySide === 'r' ? 'K' : 'k';

    /* 兵/卒：向前攻击；过河后可横向攻击。 */
    var pawnRank = rank - forwardOf(bySide);
    if (pawnRank >= 0 && pawnRank <= 9 && grid[pawnRank][file] === pawn) return true;
    if (crossedRiver(bySide, rank)) {
      for (i = -1; i <= 1; i += 2) {
        nf = file + i;
        if (nf >= 0 && nf <= 8 && grid[rank][nf] === pawn) return true;
      }
    }

    /* 马（检查马腿）。 */
    for (i = 0; i < HORSE.length; i += 1) {
      dr = HORSE[i][0];
      df = HORSE[i][1];
      var hr = rank + dr;
      var hf = file + df;
      if (hr < 0 || hr > 9 || hf < 0 || hf > 8 || grid[hr][hf] !== horse) continue;
      var lr = Math.abs(dr) === 2 ? -dr / 2 : 0;
      var lf = Math.abs(dr) === 2 ? 0 : -df / 2;
      if (!grid[hr + lr][hf + lf]) return true;
    }

    /* 相/象（检查象眼与河界）。 */
    if (ownHalf(bySide, rank)) {
      for (i = 0; i < ELEPHANT.length; i += 1) {
        dr = ELEPHANT[i][0];
        df = ELEPHANT[i][1];
        var er = rank + dr;
        var ef = file + df;
        if (er < 0 || er > 9 || ef < 0 || ef > 8 || grid[er][ef] !== elephant) continue;
        if (!grid[(er + rank) / 2][(ef + file) / 2]) return true;
      }
    }

    /* 仕/士。 */
    if (inPalace(bySide, rank, file)) {
      for (i = 0; i < DIAG.length; i += 1) {
        nr = rank + DIAG[i][0];
        nf = file + DIAG[i][1];
        if (nr >= 0 && nr <= 9 && nf >= 0 && nf <= 8 && grid[nr][nf] === advisor) {
          return true;
        }
      }
    }

    /* 车、炮：沿四个方向扫，遇到第一个子看是不是车，越过炮架看是不是炮。 */
    for (i = 0; i < ORTHO.length; i += 1) {
      dr = ORTHO[i][0];
      df = ORTHO[i][1];
      nr = rank + dr;
      nf = file + df;
      var screen = false;
      while (nr >= 0 && nr <= 9 && nf >= 0 && nf <= 8) {
        occupant = grid[nr][nf];
        if (occupant) {
          if (!screen) {
            if (occupant === rook) return true;
            screen = true;
          } else {
            if (occupant === cannon) return true;
            break;
          }
        }
        nr += dr;
        nf += df;
      }
    }

    /* 将/帅：贴身一步；以及对将/帅的飞将照面（同一纵线中间无子）。 */
    for (i = 0; i < ORTHO.length; i += 1) {
      nr = rank + ORTHO[i][0];
      nf = file + ORTHO[i][1];
      if (nr >= 0 && nr <= 9 && nf >= 0 && nf <= 8 && grid[nr][nf] === king) return true;
    }
    if (grid[rank][file] === king) {
      for (i = 0; i < ORTHO.length; i += 1) {
        dr = ORTHO[i][0];
        df = ORTHO[i][1];
        nr = rank + dr;
        nf = file + df;
        while (nr >= 0 && nr <= 9 && nf >= 0 && nf <= 8) {
          occupant = grid[nr][nf];
          if (occupant) {
            if (occupant === king) return true;
            break;
          }
          nr += dr;
          nf += df;
        }
      }
    }
    return false;
  }

  /**
   * 该方是否被将军（含将帅照面）。
   *
   * 双方将帅照面时，``isAttacked`` 沿纵线的扫描会命中对方将/帅，因此照面已经
   * 被包含在攻击判定里；这里再显式检查一次，保证“将帅对面”无论何时都被判为非法。
   */
  function inCheck(board, side) {
    var position = kingOf(board.grid, side);
    if (!position) return true;
    if (isAttacked(board.grid, position[0], position[1], other(side))) return true;
    return kingsFacing(board.grid);
  }

  /** 双方将帅是否在同一纵线上照面（中间无子）。 */
  function kingsFacing(grid) {
    var red = kingOf(grid, 'r');
    var black = kingOf(grid, 'b');
    if (!red || !black || red[1] !== black[1]) return false;
    var file = red[1];
    var low = Math.min(red[0], black[0]);
    var high = Math.max(red[0], black[0]);
    for (var rank = low + 1; rank < high; rank += 1) {
      if (grid[rank][file]) return false;
    }
    return true;
  }

  /** 该着法是否合法（走完之后自己没有处于被将军/照面状态）。 */
  /**
   * 这一步是否符合该棋子的走法（伪合法），例如帅不得出九宫、马蹩腿、炮翻山。
   *
   * ``moveIsLegal`` 只负责“走完不能自己被将/照面”，所以必须先用它把走法本身
   * 校验一遍，否则 applyMove 会接受“帅走出九宫”这类根本不合规则的着法。
   */
  function isPseudoLegal(board, move, side) {
    var candidates = pseudoMoves(board, side, [move[0], move[1]]);
    for (var i = 0; i < candidates.length; i += 1) {
      var other = candidates[i];
      if (other[0] === move[0] && other[1] === move[1]
          && other[2] === move[2] && other[3] === move[3]) return true;
    }
    return false;
  }

  function moveIsLegal(board, move, side) {
    var info = makeMove(board, move);
    var legal;
    try {
      legal = !inCheck(board, side);
    } finally {
      unmakeMove(board, move, info);
    }
    return legal;
  }

  /** 全部合法着法（已剔除送将/照面）。 */
  function legalMoves(board, side) {
    var pseudo = pseudoMoves(board, side, null);
    var out = [];
    for (var i = 0; i < pseudo.length; i += 1) {
      if (moveIsLegal(board, pseudo[i], side)) out.push(pseudo[i]);
    }
    return out;
  }

  /** 某起点的全部合法着法。 */
  function legalMovesFrom(board, side, rank, file) {
    var pseudo = pseudoMoves(board, side, [rank, file]);
    var out = [];
    for (var i = 0; i < pseudo.length; i += 1) {
      if (moveIsLegal(board, pseudo[i], side)) out.push(pseudo[i]);
    }
    return out;
  }

  function hasLegalMoves(board, side) {
    var pseudo = pseudoMoves(board, side, null);
    for (var i = 0; i < pseudo.length; i += 1) {
      if (moveIsLegal(board, pseudo[i], side)) return true;
    }
    return false;
  }

  /** 在棋盘上原地走一步（不做合法性校验，供搜索使用）；返回撤销所需信息。 */
  function makeMove(board, move) {
    var grid = board.grid;
    var piece = grid[move[0]][move[1]];
    var captured = grid[move[2]][move[3]];
    var boardRef = grid.slice();
    boardRef[move[0]] = grid[move[0]].slice();
    boardRef[move[2]] = (move[0] === move[2]) ? boardRef[move[0]] : grid[move[2]].slice();
    boardRef[move[0]][move[1]] = '';
    boardRef[move[2]][move[3]] = piece;
    board.grid = boardRef;
    return { boardRef: boardRef, captured: captured };
  }

  /** 撤销 makeMove。 */
  function unmakeMove(board, move, info) {
    board.grid = info.boardRef;
    info.boardRef[move[0]][move[1]] = info.boardRef[move[2]][move[3]];
    info.boardRef[move[2]][move[3]] = info.captured;
  }

  /* ====================================================================== */
  /* ICCS 与中文记谱                                                         */
  /* ====================================================================== */

  function moveIccs(move) {
    return squareName(move[0], move[1]) + squareName(move[2], move[3]);
  }

  /** 宽松解析 ICCS：允许空格、``-``、``_``、逗号，允许大写。 */
  function parseIccs(text) {
    if (typeof text !== 'string') return null;
    var raw = text.replace(/[\s\-_,]+/g, '').toLowerCase();
    if (raw.length !== 4) return null;
    var from = parseSquareName(raw.slice(0, 2));
    var to = parseSquareName(raw.slice(2, 4));
    if (!from || !to) return null;
    return [from.rank, from.file, to.rank, to.file];
  }

  var CN_DIGITS = '一二三四五六七八九';
  var ARABIC_DIGITS = '123456789';

  /** file 索引 -> 该方纵线编号（红方由右至左 1-9）。 */
  function fileNumber(side, file) {
    return side === 'r' ? 9 - file : file + 1;
  }

  /** 纵线/步数编号 -> 显示用字符。 */
  function numeral(side, number) {
    return side === 'r' ? CN_DIGITS.charAt(number - 1) : ARABIC_DIGITS.charAt(number - 1);
  }

  /** 同一纵线上第 index 个同类子的称谓（前/中/后/二/三…）。 */
  function stackLabel(index, count) {
    if (count === 2) return index === 0 ? '前' : '后';
    if (count === 3) return ['前', '中', '后'][index];
    var labels = ['前'];
    for (var i = 1; i < count - 1; i += 1) labels.push(CN_DIGITS.charAt(i - 1));
    labels.push('后');
    return labels[index];
  }

  /** ``{file: [rank, ...]}``：同类棋子数 >= 2 的纵线，按前->后排序。 */
  function stackedLines(grid, piece, side) {
    var lines = {};
    for (var file = 0; file < 9; file += 1) {
      var ranks = [];
      for (var rank = 0; rank < 10; rank += 1) {
        if (grid[rank][file] === piece) ranks.push(rank);
      }
      if (ranks.length >= 2) {
        ranks.sort(function (a, b) { return side === 'r' ? b - a : a - b; });
        lines[file] = ranks;
      }
    }
    return lines;
  }

  /** 着法 -> 中文纵线记谱（以走子**之前**的局面为准），与 notation.move_to_chinese 一致。 */
  function moveToChinese(board, move) {
    var grid = board.grid;
    var piece = grid[move[0]][move[1]];
    if (!piece) throw new Error('起点上没有棋子');
    var side = sideOf(piece);
    var ptype = typeOf(piece);
    var name = NAMES[side][ptype];
    var forward = forwardOf(side);
    var lines = stackedLines(grid, piece, side);
    var head;

    if (Object.prototype.hasOwnProperty.call(lines, move[1])) {
      var ordered = lines[move[1]];
      var index = ordered.indexOf(move[0]);
      if (index < 0) index = 0;
      var label = stackLabel(index, ordered.length);
      head = label + name;
      /* 其它纵线上是否也存在同样称谓的同类子；若有则附纵线号消歧。 */
      var clashes = 0;
      for (var file in lines) {
        if (!Object.prototype.hasOwnProperty.call(lines, file)) continue;
        var ranks = lines[file];
        for (var i = 0; i < ranks.length; i += 1) {
          if (stackLabel(i, ranks.length) === label) {
            clashes += 1;
            break;
          }
        }
      }
      if (clashes > 1) head += numeral(side, fileNumber(side, move[1]));
    } else {
      head = name + numeral(side, fileNumber(side, move[1]));
    }

    if (move[2] === move[0]) {
      return head + '平' + numeral(side, fileNumber(side, move[3]));
    }
    var action = (move[2] - move[0]) * forward > 0 ? '进' : '退';
    if (ptype === 'K' || ptype === 'R' || ptype === 'C' || ptype === 'P') {
      return head + action + numeral(side, Math.abs(move[2] - move[0]));
    }
    return head + action + numeral(side, fileNumber(side, move[3]));
  }

  /* ====================================================================== */
  /* 胜负判定                                                                */
  /* ====================================================================== */

  /**
   * 判定局面结果。
   *
   * :param board: 内部局面（会临时改动 ``halfmove`` 以模拟 60 回合规则）
   * :param seen:  ``{fenKey: 出现次数}``，含当前局面；用于三次重复
   * :param moves: 从最近一次同局面到现在的着法列表（用于长将判定）
   */
  function judgeResult(board, seen, moves) {
    var side = board.side;
    if (!hasLegalMoves(board, side)) {
      if (inCheck(board, side)) {
        return { kind: 'checkmate', winner: other(side),
                 text: SIDE_NAMES[other(side)] + '胜（' + SIDE_NAMES[side] + '被将死）' };
      }
      return { kind: 'stalemate', winner: other(side),
               text: SIDE_NAMES[other(side)] + '胜（' + SIDE_NAMES[side] + '困毙，无着可走）' };
    }

    var count = seen[positionKey(board)] || 1;
    if (count >= 3) {
      var guilty = perpetualChecker(board, moves);
      if (guilty) {
        return { kind: 'repetition', winner: other(guilty),
                 text: SIDE_NAMES[other(guilty)] + '胜（' + SIDE_NAMES[guilty] + '长将判负）' };
      }
      return { kind: 'repetition', winner: null,
               text: '和棋（同一局面三次重复，判和）' };
    }

    if (board.halfmove >= NATURAL_LIMIT_PLIES) {
      return { kind: 'no_capture', winner: null, text: '和棋（连续 60 回合无吃子，判和）' };
    }
    return null;
  }

  /**
   * 若重复局面源于单方**连续将军**，返回该方；否则返回 null。
   * 复刻 board.Board._perpetual_checker 的判定方式（逐着法回放 ``gave_check``）。
   */
  function perpetualChecker(board, moves) {
    if (!moves || !moves.length) return null;
    var work = { grid: cloneGrid(board.grid), side: board.side,
                 halfmove: board.halfmove, fullmove: board.fullmove };
    /* 回退到段起点。 */
    for (var i = moves.length - 1; i >= 0; i -= 1) {
      var parsed = parseIccs(moves[i] && moves[i].iccs ? moves[i].iccs : moves[i]);
      if (!parsed) return null;
      work.side = other(work.side);
    }
    /* 正向回放并记录每一步走完后的 ``gave_check``。 */
    var flags = [];
    var sides = [];
    for (var k = 0; k < moves.length; k += 1) {
      var item = moves[k];
      var text = (item && item.iccs) ? item.iccs : item;
      var mv = parseIccs(text);
      if (!mv || sideOf(work.grid[mv[0]][mv[1]]) !== work.side) return null;
      sides.push(work.side);
      var info = makeMove(work, mv);
      work.side = other(work.side);
      flags.push(inCheck(work, work.side));
      unmakeMove(work, mv, info);
    }
    var guilty = [];
    var names = ['r', 'b'];
    for (var s = 0; s < names.length; s += 1) {
      var any = false;
      var all = true;
      for (var j = 0; j < sides.length; j += 1) {
        if (sides[j] !== names[s]) continue;
        any = true;
        if (!flags[j]) all = false;
      }
      if (any && all) guilty.push(names[s]);
    }
    return guilty.length === 1 ? guilty[0] : null;
  }

  /**
   * 局面标识（棋盘 + 走子方），等价于 Python 端的 Zobrist 散列判重。
   *
   * 只取 FEN 的**棋盘段与走子方**：``halfmove`` / ``fullmove`` 回合计数
   * 每步都在变，把整条 FEN 当键会让三次重复永远判不出来。
   */
  function positionKey(board) {
    /* 必须走 toFen 的编码：grid 里的空格是空串，直接 join('') 会把
       "1c5c1" 和 "2c4c1" 都压成 "cc"，不同局面折叠成同一个键，
       于是普通开局（炮八平五 炮2平3）会被误判成三次重复。 */
    var fen = toFen(board);
    var parts = fen.split(' ');
    return parts[0] + ' ' + (parts[1] || board.side);
  }

  /** FEN -> 局面标识（棋盘段 + 走子方）；非法 FEN 返回 ``''``。 */
  function fenKey(fen) {
    try {
      return positionKey(parsePosition(fen));
    } catch (error) {
      return '';
    }
  }

  /**
   * 把 ``history``（FEN 字符串数组、``{fen}`` 数组，或 ``{iccs}`` 数组）归一化成
   * 着法列表 + 局面计数表。
   *
   * 计数表把历史里出现过的**每一个局面**都记一次；当前局面额外计一次。于是
   * “当前局面在历史上已经出现过两次”就等于三次重复（与 board.Board 的
   * ``_key_counts`` 语义一致）。
   */
  function scanHistory(board, history) {
    var entries = [];
    var seen = {};
    var list = Array.isArray(history) ? history : [];
    for (var i = 0; i < list.length; i += 1) {
      var item = list[i];
      var entry = { iccs: '', fen: '' };
      if (typeof item === 'string') {
        entry.fen = item;
      } else if (item && typeof item === 'object') {
        entry.iccs = typeof item.iccs === 'string' ? item.iccs : '';
        entry.fen = typeof item.fen === 'string' ? item.fen : '';
        if (typeof item.key === 'string') seen[item.key] = (seen[item.key] || 0) + 1;
      } else {
        continue;
      }
      entries.push(entry);
      if (entry.fen) {
        var key = fenKey(entry.fen);
        if (key) seen[key] = (seen[key] || 0) + 1;
      }
    }

    var current = positionKey(board);
    seen[current] = (seen[current] || 0) + 1;

    /* 最近一次同局面的位置之后的所有着法，就是重复段的着法列表。 */
    var start = 0;
    for (var j = entries.length - 1; j >= 0; j -= 1) {
      if (entries[j].fen && fenKey(entries[j].fen) === current) {
        start = j + 1;
        break;
      }
    }
    return { moves: entries.slice(start), seen: seen };
  }

  /* ====================================================================== */
  /* AI：Alpha-Beta 负极大值 + 子力/位置评估                                  */
  /* ====================================================================== */

  /* 位置价值表（红方视角，rank 0 为红方底线；黑方按 9-rank 镜像），与 ai.py 相同。 */
  var PAWN_PST = [
    [0, 0, 0, 0, 0, 0, 0, 0, 0],
    [0, 0, 0, 0, 0, 0, 0, 0, 0],
    [0, 0, 0, 0, 0, 0, 0, 0, 0],
    [6, 8, 10, 14, 16, 14, 10, 8, 6],
    [12, 16, 20, 26, 30, 26, 20, 16, 12],
    [22, 28, 36, 44, 50, 44, 36, 28, 22],
    [32, 40, 50, 60, 66, 60, 50, 40, 32],
    [42, 52, 62, 74, 82, 74, 62, 52, 42],
    [52, 62, 74, 86, 94, 86, 74, 62, 52],
    [40, 50, 60, 70, 76, 70, 60, 50, 40]
  ];

  var HORSE_PST = [
    [0, -4, 0, 0, 0, 0, 0, -4, 0],
    [0, 2, 4, 4, 2, 4, 4, 2, 0],
    [4, 6, 10, 12, 10, 12, 10, 6, 4],
    [6, 10, 14, 16, 16, 16, 14, 10, 6],
    [8, 12, 16, 20, 20, 20, 16, 12, 8],
    [8, 12, 16, 20, 20, 20, 16, 12, 8],
    [6, 10, 14, 18, 18, 18, 14, 10, 6],
    [4, 8, 12, 14, 16, 14, 12, 8, 4],
    [2, 4, 8, 10, 10, 10, 8, 4, 2],
    [0, 2, 4, 6, 6, 6, 4, 2, 0]
  ];

  var CANNON_PST = [
    [0, 0, 2, 4, 6, 4, 2, 0, 0],
    [0, 2, 4, 6, 6, 6, 4, 2, 0],
    [2, 4, 8, 10, 12, 10, 8, 4, 2],
    [2, 6, 10, 14, 16, 14, 10, 6, 2],
    [4, 8, 12, 16, 18, 16, 12, 8, 4],
    [4, 8, 12, 16, 18, 16, 12, 8, 4],
    [2, 6, 10, 14, 16, 14, 10, 6, 2],
    [0, 4, 8, 10, 12, 10, 8, 4, 0],
    [0, 2, 4, 6, 6, 6, 4, 2, 0],
    [0, 0, 2, 4, 4, 4, 2, 0, 0]
  ];

  var ROOK_PST = [
    [6, 8, 10, 14, 16, 14, 10, 8, 6],
    [8, 10, 12, 16, 18, 16, 12, 10, 8],
    [8, 12, 14, 18, 20, 18, 14, 12, 8],
    [10, 14, 16, 20, 22, 20, 16, 14, 10],
    [12, 16, 18, 22, 24, 22, 18, 16, 12],
    [12, 16, 18, 22, 24, 22, 18, 16, 12],
    [10, 14, 16, 20, 22, 20, 16, 14, 10],
    [8, 12, 14, 18, 20, 18, 14, 12, 8],
    [8, 10, 12, 16, 18, 16, 12, 10, 8],
    [6, 8, 10, 14, 16, 14, 10, 8, 6]
  ];

  var PST = { P: PAWN_PST, N: HORSE_PST, C: CANNON_PST, R: ROOK_PST };

  /** 从 ``side`` 视角评估局面（正数表示占优），与 ai.evaluate 一致。 */
  function evaluate(board, side) {
    var score = 0;
    var grid = board.grid;
    for (var rank = 0; rank < 10; rank += 1) {
      for (var file = 0; file < 9; file += 1) {
        var piece = grid[rank][file];
        if (!piece) continue;
        var ptype = typeOf(piece);
        var value = PIECE_VALUE[ptype];
        var table = PST[ptype];
        var upper = sideOf(piece) === 'r';
        if (table) value += table[upper ? rank : 9 - rank][file];
        score += upper ? value : -value;
      }
    }
    return side === 'r' ? score : -score;
  }

  /** MVV-LVA 着法排序（吃子优先）。 */
  function orderMoves(board, moves) {
    var grid = board.grid;
    var scored = [];
    for (var i = 0; i < moves.length; i += 1) {
      var move = moves[i];
      var victim = grid[move[2]][move[3]];
      var score = 0;
      if (victim) {
        var attacker = grid[move[0]][move[1]];
        score = PIECE_VALUE[typeOf(victim)] * 10 - PIECE_VALUE[typeOf(attacker)];
      }
      scored.push({ move: move, score: score, index: i });
    }
    scored.sort(function (a, b) {
      if (a.score !== b.score) return b.score - a.score;
      return a.index - b.index;
    });
    var out = [];
    for (var k = 0; k < scored.length; k += 1) out.push(scored[k].move);
    return out;
  }

  function SearchTimeout() {}

  /** 搜索上下文（闭包太重，这里用普通对象）。 */
  function makeSearch(board, deadline, maxNodes) {
    return {
      grid: cloneGrid(board.grid),
      side: board.side,
      nodes: 0,
      deadline: deadline,
      maxNodes: maxNodes
    };
  }

  function checkBudget(search) {
    search.nodes += 1;
    if (search.nodes >= search.maxNodes) throw new SearchTimeout();
    if ((search.nodes & 1023) === 0 && Date.now() > search.deadline) throw new SearchTimeout();
  }

  /**
   * 负极大值搜索。``state`` 是搜索自身维护的局面（``grid`` 会被原地换行篡改）。
   */
  function negamax(search, state, depth, alpha, beta, ply) {
    checkBudget(search);
    var moves = legalMoves(state, state.side);
    if (!moves.length) return { score: -MATE + ply, move: null };

    if (depth <= 0) {
      return { score: quiesce(search, state, alpha, beta, ply, 0), move: null };
    }

    moves = orderMoves(state, moves);
    var best = -INF;
    var bestMove = null;
    for (var i = 0; i < moves.length; i += 1) {
      var info = makeMove(state, moves[i]);
      state.side = other(state.side);
      var child;
      try {
        child = negamax(search, state, depth - 1, -beta, -alpha, ply + 1);
      } finally {
        state.side = other(state.side);
        unmakeMove(state, moves[i], info);
      }
      var score = -child.score;
      if (score > best) {
        best = score;
        bestMove = moves[i];
      }
      if (best > alpha) alpha = best;
      if (alpha >= beta) break;
    }
    return { score: best, move: bestMove };
  }

  /** 吃子延伸，缓解水平线效应。 */
  function quiesce(search, state, alpha, beta, ply, qdepth) {
    checkBudget(search);
    var stand = evaluate(state, state.side);
    if (qdepth >= 4) return stand;
    if (stand >= beta) return stand;
    var best = stand;
    if (stand > alpha) alpha = stand;

    var moves = legalMoves(state, state.side);
    var captures = [];
    for (var i = 0; i < moves.length; i += 1) {
      if (state.grid[moves[i][2]][moves[i][3]]) captures.push(moves[i]);
    }
    captures = orderMoves(state, captures);
    for (var k = 0; k < captures.length; k += 1) {
      var info = makeMove(state, captures[k]);
      state.side = other(state.side);
      var score;
      try {
        score = -quiesce(search, state, -beta, -alpha, ply + 1, qdepth + 1);
      } finally {
        state.side = other(state.side);
        unmakeMove(state, captures[k], info);
      }
      if (score > best) best = score;
      if (best > alpha) alpha = best;
      if (alpha >= beta) break;
    }
    return best;
  }

  /** 简单线性同余随机数（不用 Math.random，保证同种子同结果）。 */
  function makeRng(seed) {
    var state = (seed === undefined || seed === null ? 1 : seed) >>> 0;
    if (state === 0) state = 0x9e3779b9;
    return function () {
      state = (state * 1664525 + 1013904223) >>> 0;
      return state / 4294967296;
    };
  }

  /**
   * 为 ``fen`` 的走子方选择一步棋；无合法着法返回 ``''``。
   *
   * 迭代加深 + Alpha-Beta + 吃子延伸；低难度在“接近最优”的着法里随机挑选
   * （分值取自满窗口重搜，避免按上界随机挑到吃亏的着法），与 ai.Engine 同思路。
   */
  function bestMove(fen, level, seed) {
    var board = parsePosition(fen);
    var settings = LEVELS[level] || LEVELS.normal;
    var maxDepth = settings[0];
    var timeLimit = settings[1] * 1000;
    var randomness = settings[2];
    var maxNodes = settings[3];
    var root = legalMoves(board, board.side);
    if (!root.length) return '';

    var rng = makeRng(seed);
    var search = makeSearch(board, Date.now() + timeLimit, maxNodes);
    var state = { grid: cloneGrid(board.grid), side: board.side, halfmove: 0, fullmove: 1 };
    var best = root[0];
    var bestScore = -INF;
    var scored = [];
    var reached = 0;

    for (var depth = 1; depth <= maxDepth; depth += 1) {
      var alpha = -INF;
      var beta = INF;
      var roundBest = null;
      var roundScore = -INF;
      var roundScored = [];
      var timedOut = false;
      var ordered = orderMoves(state, root);
      for (var i = 0; i < ordered.length; i += 1) {
        var info = makeMove(state, ordered[i]);
        state.side = other(state.side);
        var child;
        try {
          child = negamax(search, state, depth - 1, -beta, -alpha, 1);
        } catch (error) {
          if (error instanceof SearchTimeout) {
            timedOut = true;
          } else {
            throw error;
          }
        } finally {
          state.side = other(state.side);
          unmakeMove(state, ordered[i], info);
        }
        if (timedOut) break;
        var score = -child.score;
        roundScored.push({ move: ordered[i], score: score });
        if (score > roundScore) {
          roundScore = score;
          roundBest = ordered[i];
        }
        if (roundScore > alpha) alpha = roundScore;
      }
      if (timedOut) break;
      if (roundBest) {
        best = roundBest;
        bestScore = roundScore;
        scored = roundScored;
        reached = depth;
      }
      if (bestScore >= MATE - 100) break;             // 已找到杀棋
    }

    if (randomness && scored.length > 0) {
      return pickRandom(state, best, bestScore, scored, randomness, reached, rng);
    }
    return moveIccs(best);
  }

  /** 在接近最优的着法里随机挑一个（低难度），候选逐一满窗口复核。 */
  function pickRandom(state, best, bestScore, scored, randomness, reached, rng) {
    var pool = [];
    for (var i = 0; i < scored.length; i += 1) {
      if (scored[i].score >= bestScore - randomness) pool.push(scored[i].move);
    }
    if (pool.length <= 1) return moveIccs(best);

    var depth = Math.max(0, reached - 1);
    var search = makeSearch(state, Date.now() + 1000, 200000);
    var exact = [];
    var fallback = moveIccs(best);
    for (var k = 0; k < pool.length; k += 1) {
      var info = makeMove(state, pool[k]);
      state.side = other(state.side);
      var value;
      try {
        value = -negamax(search, state, depth, -INF, INF, 1).score;
      } catch (error) {
        if (error instanceof SearchTimeout) return fallback;
        throw error;
      } finally {
        state.side = other(state.side);
        unmakeMove(state, pool[k], info);
      }
      exact.push({ move: pool[k], value: value });
    }
    var top = -INF;
    for (var j = 0; j < exact.length; j += 1) {
      if (exact[j].value > top) top = exact[j].value;
    }
    var finalPool = [];
    for (var m = 0; m < exact.length; m += 1) {
      if (exact[m].value >= top - randomness) finalPool.push(exact[m].move);
    }
    if (!finalPool.length) return fallback;
    return moveIccs(finalPool[Math.floor(rng() * finalPool.length) % finalPool.length]);
  }

  /** 结点计数（与 board.Board.perft 一致：depth <= 0 返回 1）。 */
  function perft(fen, depth) {
    var board = parsePosition(fen);
    return perftBoard(board, depth);
  }

  function perftBoard(board, depth) {
    if (depth <= 0) return 1;
    var total = 0;
    var moves = legalMoves(board, board.side);
    for (var i = 0; i < moves.length; i += 1) {
      var info = makeMove(board, moves[i]);
      board.side = other(board.side);
      try {
        total += perftBoard(board, depth - 1);
      } finally {
        board.side = other(board.side);
        unmakeMove(board, moves[i], info);
      }
    }
    return total;
  }

  /* ====================================================================== */
  /* 对外 API（与 xiangqi/net/rooms.py 的快照字段一一对应）                    */
  /* ====================================================================== */

  var XQEngine = {};

  XQEngine.START_FEN = START_FEN;
  XQEngine.SIDE_NAMES = SIDE_NAMES;
  XQEngine.PIECE_VALUE = PIECE_VALUE;
  XQEngine.LEVELS = LEVELS;
  XQEngine.LEVEL_NAMES = LEVEL_NAMES;

  /** 当前走子方的合法着法表：``{"b0": ["a2", "c2"], ...}``（服务器 legal 字段同形）。 */
  XQEngine.legalMap = function (fen) {
    var board = parsePosition(fen);
    var legal = {};
    var moves = legalMoves(board, board.side);
    for (var i = 0; i < moves.length; i += 1) {
      var from = squareName(moves[i][0], moves[i][1]);
      if (!legal[from]) legal[from] = [];
      legal[from].push(squareName(moves[i][2], moves[i][3]));
    }
    return legal;
  };

  /** 全部合法着法的 ICCS 列表（顺序与 Python 端 gen_legal_moves 一致）。 */
  XQEngine.moves = function (fen) {
    var board = parsePosition(fen);
    var moves = legalMoves(board, board.side);
    var out = [];
    for (var i = 0; i < moves.length; i += 1) out.push(moveIccs(moves[i]));
    return out;
  };

  /** 走子**之前**的中文纵线记谱，如 ``炮二平五``。 */
  XQEngine.chinese = function (fen, iccs) {
    var board = parsePosition(fen);
    var move = parseIccs(iccs);
    if (!move) throw new Error('bad_iccs');
    return moveToChinese(board, move);
  };

  /** 局面状态：走子方、是否被将军、是否结束、结果。 */
  XQEngine.status = function (fen, history) {
    var board = parsePosition(fen);
    var scanned = scanHistory(board, history);
    var saved = board.halfmove;
    var result = judgeResult(board, scanned.seen, scanned.moves);
    board.halfmove = saved;
    return {
      side: board.side,
      check: inCheck(board, board.side),
      over: !!result,
      result: result
    };
  };

  /**
   * 走一步棋。成功返回::
   *
   *     {ok:true, fen, side, from, to, piece, captured, chinese, check, result}
   *
   * 失败返回 ``{ok:false, error:'illegal'|'bad_iccs'}``。
   */
  XQEngine.applyMove = function (fen, iccs) {
    var board = parsePosition(fen);
    var move = parseIccs(iccs);
    if (!move) return { ok: false, error: 'bad_iccs' };
    var piece = board.grid[move[0]][move[1]];
    if (!piece || sideOf(piece) !== board.side) return { ok: false, error: 'illegal' };
    if (!isPseudoLegal(board, move, board.side)) return { ok: false, error: 'illegal' };
    if (!moveIsLegal(board, move, board.side)) return { ok: false, error: 'illegal' };

    var chinese = moveToChinese(board, move);
    var captured = board.grid[move[2]][move[3]] || '';
    var info = makeMove(board, move);
    if (captured) board.halfmove = 0;
    else board.halfmove += 1;
    if (board.side === 'b') board.fullmove += 1;
    board.side = other(board.side);

    var saved = board.halfmove;
    var seen = {};
    seen[positionKey(board)] = 1;
    var side = board.side;
    var check = inCheck(board, side);
    var result;
    if (!hasLegalMoves(board, side)) {
      if (check) {
        result = { kind: 'checkmate', winner: other(side),
                   text: SIDE_NAMES[other(side)] + '胜（' + SIDE_NAMES[side] + '被将死）' };
      } else {
        result = { kind: 'stalemate', winner: other(side),
                   text: SIDE_NAMES[other(side)] + '胜（' + SIDE_NAMES[side] + '困毙，无着可走）' };
      }
    } else if (board.halfmove >= NATURAL_LIMIT_PLIES) {
      result = { kind: 'no_capture', winner: null, text: '和棋（连续 60 回合无吃子，判和）' };
    } else {
      result = null;
    }
    board.halfmove = saved;

    return {
      ok: true,
      fen: toFen(board),
      side: side,
      from: squareName(move[0], move[1]),
      to: squareName(move[2], move[3]),
      piece: piece,
      captured: captured,
      chinese: chinese,
      check: check,
      result: result
    };
  };

  /** AI 着法（ICCS）；无合法着法返回 ``''``。``level`` 取 easy/normal/hard。 */
  XQEngine.bestMove = function (fen, level, seed) {
    return bestMove(fen, level, seed);
  };

  /** 结点计数（供测试与 Python 端 Board.perft 对照）。 */
  XQEngine.perft = function (fen, depth) {
    return perft(fen, depth);
  };

  /** 便于调试/复用的底层入口。 */
  XQEngine.toFen = function (fen) {
    return toFen(parsePosition(fen));
  };
  XQEngine.inCheck = function (fen) {
    var board = parsePosition(fen);
    return inCheck(board, board.side);
  };

  root.XQEngine = XQEngine;
  if (typeof module !== 'undefined' && module.exports) module.exports = XQEngine;
}(typeof globalThis !== 'undefined' ? globalThis : this));
