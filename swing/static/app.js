/* 스윙 모의투자 대시보드 — swing/server.py API 만 읽는다(표시 전용). 디자인은 backtest_stock 메인 대시보드 양식. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  // 청산 사유 — 2026-09-29 부터 목표가·만기는 재판별 후 매도(review_*), target/expiry 는 이전 기록 호환
  var REASON = { stop: "손절", stop_gap: "갭 손절(시가)", review_target: "목표 후 매도", review_expiry: "만기 후 매도",
    max_hold: "최대 보유", pm_sell: "PM 매도", cash: "현금 확보", target: "목표가", expiry: "보유 만기" };
  var REVIEW_ACT = { sell: ["sell-next", "매도 예약"], sell_fallback: ["sell-next", "판단 실패 → 원 규칙대로 매도"],
    hold_updated: ["ext", "계속 보유 · 가격 갱신"], hold: ["hold-ok", "계속 보유"] };
  var KIND_TXT = { target: "목표 도달", expiry: "보유 만기", extended: "연장 재판별", signal: "신호" };
  var STATUS = { filled: "체결", cancelled: "취소", skipped: "스킵", open: "대기" };
  // Short 청산 라벨 — 매도 → 환매(2026-09-29)
  function reasonTxt(r, side) {
    var t = REASON[r] || r;
    return side === "short" ? String(t).replace("PM 매도", "PM 환매").replace("후 매도", "후 환매") : t;
  }
  var SIG_ORDER = ["동반강세", "수급유입", "수급이탈", "동반약세"];
  var SIG_C = { "동반강세": "var(--sig-strong)", "수급유입": "var(--sig-inflow)", "수급이탈": "var(--sig-outflow)", "동반약세": "var(--sig-weak)" };
  var SIG_SUB = { "동반강세": "추세 지속형 상방", "수급유입": "초기 유입형 상방", "수급이탈": "상승 후 약화 · Short 후보 · Long 매도 검토", "동반약세": "약세 지속형 하방 · Short 후보 · Long 매도 검토" };
  // 판단 원문 = 스킬 종합 리포트(complete_report.md)와 같은 I~V 단계 묶음. 수집 정보는 부록(스킬은 사용자 보고에만 표시)
  var ROLE_GROUPS = [
    ["I. 애널리스트 리포트", [["market", "I-1. Market Analyst · 기술적 분석"], ["sentiment", "I-2. Sentiment Analyst · 심리"],
      ["news", "I-3. News Analyst · 뉴스·공시"], ["fundamentals", "I-4. Fundamentals Analyst · 펀더멘털"],
      ["flow", "I-5. Flow Analyst · 수급"]]],
    ["II. 강세·약세 토론과 Research Manager", [["debate", "II-1. 강세·약세 토론"], ["research_manager", "II-2. Research Manager"]]],
    ["III. Trader", [["trader", "Trader"]]],
    ["IV. 리스크 토론", [["risk_debate", "공격 → 보수 → 중립"]]],
    ["V. Portfolio Manager", [["pm", "Portfolio Manager"]]],
    ["부록 · 수집 정보", [["peers", "해외 비교기업"], ["collect", "수집 상태"]]]];

  function HOLD_N() { return (S.config && S.config.holdDays) || 5; }
  var S = { summary: null, positions: [], trades: [], equity: [], config: null, held: {} };

  function api(path) {
    return fetch(path, { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error(path + " " + r.status);
      return r.json();
    });
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  // 판단 원문 마크다운 → HTML. LLM 출력이라 먼저 esc 하고 허용 문법(제목·표·목록·인용·굵게·코드·구분선)만 태그로 바꾼다
  function mdInline(s) {
    return esc(s).replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\s][^*]*)\*(?!\*)/g, "$1<em>$2</em>");
  }
  function md(src) {
    var lines = String(src == null ? "" : src).replace(/\r/g, "").split("\n"), out = [], i = 0, para = [];
    var cells = function (l) { return l.trim().replace(/^\||\|$/g, "").split("|").map(function (c) { return c.trim(); }); };
    var isSep = function (l) { return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l); };
    function flush() { if (para.length) { out.push("<p>" + para.map(mdInline).join("<br>") + "</p>"); para = []; } }
    while (i < lines.length) {
      var l = lines[i], m;
      if (!l.trim()) { flush(); i++; continue; }
      if (/^```/.test(l)) {
        flush(); var code = []; i++;
        while (i < lines.length && !/^```/.test(lines[i])) code.push(lines[i++]);
        out.push("<pre><code>" + esc(code.join("\n")) + "</code></pre>"); i++; continue;
      }
      if ((m = /^(#{1,6})\s+(.*)$/.exec(l))) {
        flush(); var lv = Math.min(m[1].length + 2, 6);   // 대화상자 안이라 h3~h6 로 낮춤
        out.push("<h" + lv + ">" + mdInline(m[2]) + "</h" + lv + ">"); i++; continue;
      }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(l)) { flush(); out.push("<hr>"); i++; continue; }
      if (l.indexOf("|") >= 0 && i + 1 < lines.length && isSep(lines[i + 1])) {
        flush(); var head = cells(l), rows = []; i += 2;
        while (i < lines.length && lines[i].indexOf("|") >= 0 && lines[i].trim()) rows.push(cells(lines[i++]));
        out.push('<div class="md-table"><table><thead><tr>' + head.map(function (c) { return "<th>" + mdInline(c) + "</th>"; }).join("") +
          "</tr></thead><tbody>" + rows.map(function (r) {
            return "<tr>" + r.map(function (c) { return "<td>" + mdInline(c) + "</td>"; }).join("") + "</tr>";
          }).join("") + "</tbody></table></div>");
        continue;
      }
      if (/^\s*>/.test(l)) {
        flush(); var q = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) q.push(lines[i++].replace(/^\s*>\s?/, ""));
        out.push("<blockquote>" + md(q.join("\n")) + "</blockquote>"); continue;
      }
      if ((m = /^\s*([-*+]|\d+[.)])\s+/.exec(l))) {
        flush(); var ol = /\d/.test(m[1]), items = [];
        while (i < lines.length && (m = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(lines[i])) && /\d/.test(m[2]) === ol) {
          items.push((m[1].length >= 2 ? '<li class="sub">' : "<li>") + mdInline(m[3]) + "</li>"); i++;
        }
        out.push((ol ? "<ol>" : "<ul>") + items.join("") + (ol ? "</ol>" : "</ul>")); continue;
      }
      para.push(l); i++;
    }
    flush();
    return out.join("");
  }
  function won(v) { return v == null ? "—" : Math.round(v).toLocaleString("ko-KR"); }
  function eok(v) { return v == null ? "—" : (v / 1e8).toFixed(2) + "억"; }
  function pctTxt(v, d) { return v == null ? "—" : (v > 0 ? "+" : "") + Number(v).toFixed(d == null ? 2 : d) + "%"; }
  function pct(v, d) { return '<span class="num ' + (v > 0 ? "up" : v < 0 ? "down" : "") + '">' + pctTxt(v, d) + "</span>"; }
  function pnlBadge(v, suffix) {
    var cls = v > 0 ? "profit" : v < 0 ? "loss" : "none";
    return '<span class="pnl-badge ' + cls + '">' + (v > 0 ? "▲ " : v < 0 ? "▼ " : "") + suffix + "</span>";
  }
  function sig(s) { return s ? '<span class="sig s-' + esc(s) + '">' + esc(s) + "</span>" : ""; }
  // 판단 원문은 행(셀) 클릭으로 연다 — table() 의 {key, cells} 행(2026-09-29)
  function stock(name, code, sub, key, tag) {
    var nm = '<span class="stock-name">' + esc(name) + "</span>";
    return '<div class="stock-info"><span class="name-row">' + nm + (tag || "") + '</span><span class="stock-code">' +
      esc(code) + (sub ? " · " + sub : "") + "</span></div>";
  }
  function testTag(x) { return x && x.demo ? '<span class="test-tag">TEST</span>' : ""; }
  function levels(e, s, t) {
    return '<div class="levels">' +
      '<div class="param"><span class="param-label">진입</span><span class="param-value entry">' + won(e) + "</span></div>" +
      '<div class="param"><span class="param-label">손절</span><span class="param-value stop">' + won(s) + "</span></div>" +
      '<div class="param"><span class="param-label">목표</span><span class="param-value target">' + (t ? won(t) : "—") + "</span></div></div>";
  }
  function rating(r, err) {
    if (err) return '<span class="rating err">실패</span>';
    var cls = /^(Buy|Overweight)$/.test(r) ? "buy" : /^(Sell|Underweight)$/.test(r) ? "sell" : "";
    return '<span class="rating ' + cls + '">' + esc(r || "—") + "</span>";
  }
  function table(el, head, rows, empty) {
    var h = "<thead><tr>" + head.map(function (c) { return '<th class="' + (c[1] || "") + '">' + c[0] + "</th>"; }).join("") + "</tr></thead><tbody>";
    h += rows.length ? rows.map(function (r) {
      if (Array.isArray(r)) return "<tr>" + r.join("") + "</tr>";
      return "<tr" + (r.key ? ' data-key="' + esc(r.key) + '" title="판단 원문 보기"' : "") + ">" + r.cells.join("") + "</tr>";
    }).join("")
      : '<tr class="empty"><td colspan="' + head.length + '">' + empty + "</td></tr>";
    el.innerHTML = h + "</tbody>";
  }
  function td(v, cls) { return '<td class="' + (cls || "") + '">' + v + "</td>"; }
  function setCount(id, n) { $(id).textContent = n ? n : ""; }

  // ── 탭 ──
  document.querySelectorAll(".main-tab").forEach(function (b) {
    b.addEventListener("click", function () {
      document.querySelectorAll(".main-tab").forEach(function (x) { x.classList.toggle("active", x === b); });
      document.querySelectorAll(".tab-pane").forEach(function (p) { p.classList.toggle("active", p.id === "pane-" + b.dataset.tab); });
      try { localStorage.setItem("swing.tab", b.dataset.tab); } catch (e) { /* 저장 불가 무시 */ }
      if (b.dataset.tab === "report" && S.summary) renderReport();
    });
  });
  try {
    var saved = localStorage.getItem("swing.tab");
    var btn = saved && document.querySelector('.main-tab[data-tab="' + saved + '"]');
    if (btn) btn.click();
  } catch (e) { /* 무시 */ }

  // ── 헤더·계좌 ──
  function renderHeader(s) {
    var lr = s.lastRun, dot = $("run-dot");
    if (!lr) { dot.className = "status-dot idle"; $("run-status").textContent = "실행 기록 없음"; }
    else {
      dot.className = "status-dot" + ((lr.notes || []).length || lr.skip ? " warn" : "");
      $("run-status").textContent = "최근 실행 " + lr.date + (lr.finishedAt ? " " + lr.finishedAt.slice(11, 16) : "");
    }
    $("agent-status").textContent = lr && lr.agent ? "판단 엔진 " + lr.agent : "판단 엔진 —";
  }
  // ── 계좌 현황: 평가액(원) · 기간 수익률(YTD/3M/1M/1W) · 포트폴리오 평가 지표(2026-09-29) ──
  // 일별 평가액(equity 행) 기준. 기간 시작 전 기록이 없으면 운용 시작(초기 자본) 대비로 계산하고 *로 표시.
  function periodReturn(rows, cap, days, ytd) {
    if (!rows.length) return null;
    var last = rows[rows.length - 1], ref = new Date(last.date + "T00:00:00");
    var start = ytd ? new Date(ref.getFullYear(), 0, 1) : new Date(ref.getTime() - days * 864e5);
    var startIso = start.getFullYear() + "-" + ("0" + (start.getMonth() + 1)).slice(-2) + "-" + ("0" + start.getDate()).slice(-2);
    var base = null;
    for (var i = rows.length - 1; i >= 0; i--) {
      if (rows[i].date < startIso) { base = rows[i].equity; break; }  // 기간 시작 직전 마지막 평가액
    }
    var since = base == null;
    return { v: ((last.equity / (since ? cap : base)) - 1) * 100, since: since };
  }
  function riskMetrics(rows, cap) {
    var eq = [cap].concat(rows.map(function (r) { return r.equity; }));
    var rets = [];
    for (var i = 1; i < eq.length; i++) rets.push(eq[i] / eq[i - 1] - 1);
    var n = rets.length;
    if (n < 2) return { n: n };
    var mean = rets.reduce(function (a, b) { return a + b; }, 0) / n;
    var sd = Math.sqrt(rets.reduce(function (a, r) { return a + (r - mean) * (r - mean); }, 0) / (n - 1));
    var dn = Math.sqrt(rets.reduce(function (a, r) { return a + Math.min(r, 0) * Math.min(r, 0); }, 0) / n);
    var peak = eq[0], mdd = 0, pkI = 0, ddFrom = null, ddTo = null;
    for (var j = 1; j < eq.length; j++) {
      if (eq[j] > peak) { peak = eq[j]; pkI = j; }
      var dd = eq[j] / peak - 1;
      if (dd < mdd) { mdd = dd; ddFrom = pkI; ddTo = j; }
    }
    var dates = ["시작"].concat(rows.map(function (r) { return r.date.slice(5); }));
    var total = eq[eq.length - 1] / eq[0] - 1, ann = Math.pow(1 + total, 252 / n) - 1;
    return { n: n, sharpe: sd > 0 ? mean / sd * Math.sqrt(252) : null, sortino: dn > 0 ? mean / dn * Math.sqrt(252) : null,
      vol: sd * Math.sqrt(252) * 100, mdd: mdd * 100, mddSpan: ddFrom == null ? "" : dates[ddFrom] + " → " + dates[ddTo],
      calmar: mdd < 0 ? ann / -mdd : null, curDD: (eq[eq.length - 1] / peak - 1) * 100 };
  }
  function renderPortfolio(s) {
    var rows = S.equity || [], cap = s.capital0, eqNow = s.equity;
    var rt = [["YTD", periodReturn(rows, cap, 0, true)], ["1W", periodReturn(rows, cap, 7)],
      ["1M", periodReturn(rows, cap, 30)], ["3M", periodReturn(rows, cap, 91)]];
    var m = riskMetrics(rows, cap);
    function f2(v) { return v == null ? "—" : v.toFixed(2); }
    // 총노출 = (Long+Short) ÷ 평가액, 순노출 = (Long−Short) ÷ 평가액 — 막대는 Long·Short 비중
    var lv = 0, sv = 0;
    (S.positions || []).forEach(function (p) { if (sideOf(p) === "short") sv += p.value || 0; else lv += p.value || 0; });
    var base = eqNow || 1, wL = lv / base * 100, wS = sv / base * 100;
    var h = '<div class="port-item wide4 hero"><div class="hero-row"><div class="hero-main"><span class="port-lbl">평가액</span><span class="port-val">' +
      '<span class="num">' + won(eqNow) + '</span><small>원</small></span><span class="port-sub">초기 ' + won(cap) + "원 대비 " +
      pct(s.retPct) + " · 기준일 " + (s.asof || "—") + "</span></div>" +
      '<div class="hero-weight"><div class="exp-pair"><div><span class="port-lbl">총노출</span><span class="port-val num">' + (wL + wS).toFixed(1) +
      '%</span></div><div><span class="port-lbl">순노출</span><span class="port-val num ' + (wL - wS > 0 ? "up" : wL - wS < 0 ? "down" : "") + '">' +
      (wL - wS > 0 ? "+" : "") + (wL - wS).toFixed(1) + "%</span></div></div>" +
      '<div class="wt-bar"><span class="b-long" style="width:' + Math.min(wL, 100) + '%"></span><span class="b-short" style="width:' +
      Math.min(wS, Math.max(0, 100 - wL)) + '%"></span></div>' +
      '<span class="port-sub">Long <b class="up">' + wL.toFixed(1) + "%</b> · Short <b class=\"down\">" + wS.toFixed(1) + "%</b> · " +
      (S.positions || []).length + "종목</span></div></div></div>";
    h += rt.map(function (x) {
      var r = x[1];
      return '<div class="port-item"><span class="port-lbl">' + x[0] + (r && r.since ? " *" : "") + '</span><span class="port-val">' +
        (r ? pct(r.v) : "—") + "</span></div>";
    }).join("");
    var mm = [
      ["Sharpe Ratio", f2(m.sharpe), "연환산 · 무위험 0%"],
      ["Sortino Ratio", f2(m.sortino), "하방 변동성 기준"],
      ["MDD", m.mdd == null ? "—" : '<span class="' + (m.mdd < 0 ? "down" : "") + '">' + m.mdd.toFixed(2) + "%</span>",
        m.mddSpan || "낙폭 없음"],
      ["변동성(연)", m.vol == null ? "—" : m.vol.toFixed(2) + "%", "현재 낙폭 " + (m.curDD == null ? "—" : m.curDD.toFixed(2) + "%")]
    ];
    h += mm.map(function (x) {
      return '<div class="port-item"><span class="port-lbl">' + x[0] + '</span><span class="port-val">' + x[1] +
        '</span><span class="port-sub">' + x[2] + "</span></div>";
    }).join("");
    $("portfolio").innerHTML = h;
    var since = rt.some(function (x) { return x[1] && x[1].since; });
    $("pf-note").textContent = "일별 평가액 " + (m.n || 0) + "영업일 기준" + (since ? " · * 기간 시작 전 기록이 없어 운용 시작 대비" : "") +
      ((m.n || 0) < 60 ? " · 표본이 짧아 Sharpe·Sortino 는 참고용" : "");
  }
  function renderRules(c) {
    if (!c) return;
    var rows = [
      ["초기 자본", eok(c.initialCapital)],
      ["매수 후보", c.buySignals.join(" · ")],
      ["진입 조건", "PM " + c.buyRatings.join("/") + " + Trader Buy"],
      ["주문", "다음 영업일 지정가(1일)"],
      ["보유", c.holdDays + "영업일(체결일=1) → 재판별"],
      ["목표가 도달", "재판별 → 매도(다음 날 시가) / 보유(본전 손절)"],
      ["연장 보유", "매일 재판별 · 최대 " + (c.maxHoldDays || 15) + "영업일"],
      ["비중", "Trader %(생략 " + (c.defaultWeight * 100) + "%, 상한 " + (c.maxWeight * 100) + "%)"],
      ["매도 검토", c.sellSignals.join(" · ") + " → PM " + c.sellRatings.join("/")],
      ["비용", "매도세 " + (c.sellTax * 100).toFixed(1) + "% · 슬리피지 없음"],
      ["하루 신규 분석", "최대 " + c.maxNewPerDay + "종목"]
    ];
    $("rules").innerHTML = rows.map(function (r) {
      return '<div class="rule"><span>' + r[0] + "</span><span>" + esc(r[1]) + "</span></div>";
    }).join("") + '<div class="rule-note">체결은 trading_agent 가격 그대로(진입가·손절가·목표가). 시가가 손절가 아래면 시가 손절, ' +
      "손절·목표 동시 도달은 손절 우선, 체결일엔 목표가 미적용. 현금 부족 시 평가손실 종목부터 시가 청산.</div>";
  }

  // ── 평가액 추이 ──
  function renderEquity(rows, cap) {
    var box = $("equity-chart");
    if (!rows.length) { box.innerHTML = '<div class="empty-chart">평가 기록이 없습니다</div>'; return; }
    $("eq-caption").textContent = rows[0].date.slice(5) + " ~ " + rows[rows.length - 1].date.slice(5);
    var W = box.clientWidth || 320, H = box.clientHeight || 190, m = { l: 44, r: 8, t: 10, b: 22 };
    var vals = rows.map(function (r) { return r.equity; }).concat([cap]);
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    var pad = (hi - lo) * 0.15 || cap * 0.005; lo -= pad; hi += pad;
    var x = function (i) { return m.l + (rows.length === 1 ? (W - m.l - m.r) / 2 : i * (W - m.l - m.r) / (rows.length - 1)); };
    var y = function (v) { return m.t + (hi - v) / (hi - lo) * (H - m.t - m.b); };
    var svg = '<svg viewBox="0 0 ' + W + " " + H + '"><defs><linearGradient id="eqfill" x1="0" x2="0" y1="0" y2="1">' +
      '<stop offset="0%" stop-color="#00f0ff" stop-opacity=".22"/><stop offset="100%" stop-color="#00f0ff" stop-opacity="0"/></linearGradient></defs><g class="axis">';
    for (var k = 0; k <= 3; k++) {
      var v = lo + (hi - lo) * k / 3;
      svg += '<line class="gridline" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + y(v) + '" y2="' + y(v) + '"/>' +
        '<text x="' + (m.l - 5) + '" y="' + (y(v) + 3) + '" text-anchor="end">' + (v / 1e8).toFixed(2) + "억</text>";
    }
    var step = Math.max(1, Math.ceil(rows.length / 4));
    rows.forEach(function (r, i) {
      if (i % step === 0 || i === rows.length - 1)
        svg += '<text x="' + x(i) + '" y="' + (H - 5) + '" text-anchor="' + (i === rows.length - 1 && rows.length > 1 ? "end" : i === 0 && rows.length > 1 ? "start" : "middle") + '">' + r.date.slice(5) + "</text>";
    });
    svg += '</g><line class="base" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + y(cap) + '" y2="' + y(cap) + '"/>';
    var pts = rows.map(function (r, i) { return x(i) + "," + y(r.equity); });
    svg += '<path class="area" d="M' + pts.join("L") + "L" + x(rows.length - 1) + "," + (H - m.b) + "L" + x(0) + "," + (H - m.b) + 'Z"/>';
    svg += '<path class="ln" d="M' + pts.join("L") + '"/>';
    svg += '<line class="cross" id="eq-cross" y1="' + m.t + '" y2="' + (H - m.b) + '" visibility="hidden"/>';
    svg += '<circle class="dot" id="eq-dot" r="4.5" visibility="hidden"/>';
    svg += '<rect x="' + m.l + '" y="0" width="' + (W - m.l - m.r) + '" height="' + H + '" fill="transparent" id="eq-hit"/></svg>';
    box.innerHTML = svg + '<div class="tip" id="eq-tip" hidden></div>';
    var hit = $("eq-hit"), cross = $("eq-cross"), dot = $("eq-dot"), tip = $("eq-tip");
    hit.addEventListener("mousemove", function (ev) {
      var rect = box.getBoundingClientRect(), px = (ev.clientX - rect.left) * W / rect.width;
      var i = rows.length === 1 ? 0 : Math.round((px - m.l) / ((W - m.l - m.r) / (rows.length - 1)));
      i = Math.max(0, Math.min(rows.length - 1, i));
      var r = rows[i];
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(r.equity)); dot.setAttribute("visibility", "visible");
      tip.hidden = false;
      tip.innerHTML = "<b>" + r.date + "</b>" + won(r.equity) + "원 · " + pctTxt(r.retPct) + "<br>현금 " + eok(r.cash) + " · 보유 " + r.positions;
      var left = x(i) * rect.width / W + 10;
      if (left + tip.offsetWidth > rect.width) left -= tip.offsetWidth + 20;
      tip.style.left = left + "px"; tip.style.top = "4px";
    });
    hit.addEventListener("mouseleave", function () {
      cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); tip.hidden = true;
    });
  }

  // ── 보유 ──
  function holdBar(d) {
    var h = '<span class="hold">';
    var n = HOLD_N();
    for (var i = 1; i <= n; i++) h += '<i class="' + (i <= d ? "on" : "") + '"></i>';
    return h + "<b>" + d + "/" + n + (d > n ? ' <span class="ext-n">+' + (d - n) + "</span>" : "") + "</b></span>";
  }
  function sideOf(x) { return (x && x.side) === "short" ? "short" : "long"; }
  function renderPositions(all) {
    var longs = all.filter(function (p) { return sideOf(p) === "long"; });
    var shorts = all.filter(function (p) { return sideOf(p) === "short"; });
    setCount("cnt-long", longs.length);
    setCount("cnt-short", shorts.length);
    posTable($("positions"), longs, "Long 보유 종목이 없습니다");
    posTable($("short-positions"), shorts, "Short 보유 종목이 없습니다");
  }
  function posTable(el, ps, empty) {
    table(el, [["종목"], ["신호"], ["가격 레벨"], ["수량", "r"], ["종가", "r"], ["평가손익", "r"], ["보유일"], ["상태"]],
      ps.map(function (p) {
        return { key: p.decisionKey, cells: [td(stock(p.name, p.code, "체결 " + (p.fillDate || "").slice(5), null, testTag(p))),
          td('<span class="sec-name">' + esc(p.sector || "") + "</span>" + sig(p.signal)),
          td(levels(p.entry, p.stop, p.target)), td('<span class="num">' + won(p.qty) + "</span>", "r"),
          td('<span class="num">' + won(p.lastClose) + "</span>", "r"),
          td(pnlBadge(p.unrealPct, pctTxt(p.unrealPct).replace(/^[+-]/, "")), "r"),
          td(holdBar(p.holdDay)),
          td(p.sellPending ? '<span class="state sell-next" title="다음 영업일 시가 ' + (sideOf(p) === "short" ? "환매" : "매도") + " — " + esc(p.sellPending.reason) + '">익일 시가 ' + (sideOf(p) === "short" ? "환매" : "매도") + "</span>"
            : p.extended ? '<span class="state ext" title="목표 도달·만기 후 trading_agent 가 계속 보유로 판단 — 매일 재판별">연장 보유</span>'
            : '<span class="state hold-ok">보유</span>')] };
      }), empty);
  }

  // ── 판단·주문 (기준일) — Long·Short 탭이 같은 표 양식, 방향으로 나눔 ──
  function renderRun(run, orders) {
    run = run || {};
    var bySd = function (xs, sd) { return (xs || []).filter(function (x) { return sideOf(x) === sd; }); };
    var ex = orders.executed || [];
    runTables("", bySd(run.reviews, "long"), run.candidates || [], bySd(ex, "long"), run, "long");
    runTables("short-", bySd(run.reviews, "short"), run.shortCandidates || [], bySd(ex, "short"), run, "short");
    renderLogs(run);
    return { cands: (run.candidates || []).concat(run.shortCandidates || []), reviews: run.reviews || [], ex: ex };
  }
  function runTables(pre, reviews, cands, ex, run, sd) {
    table($(pre + "reviews"), [["종목"], ["트리거"], ["PM"], ["결과"]],
      reviews.map(function (r) {
        return { key: r.decisionKey, cells: [td(stock(r.name, r.code)), td(esc(r.trigger), "wrap"), td(rating(r.rating, r.error)),
          td(reviewResult(r))] };
      }), sd === "short" ? "환매 검토 대상 없음" : "매도 검토 대상 없음");
    table($(pre + "candidates"), [["종목"], ["신호"], ["PM"], ["Trader"], ["가격 레벨"], ["결과"]],
      cands.map(function (c) {
        return { key: c.decisionKey, cells: [td(stock(c.name, c.code)), td('<span class="sec-name">' + esc(c.sector || "") + "</span>" + sig(c.signal)),
          td(rating(c.rating, /판단 실패/.test(c.why || ""))), td(esc(c.action || "—")),
          td(c.entry ? levels(c.entry, c.stop, c.target) : '<span class="muted">—</span>'),
          td(c.ordered ? '<span class="state ordered">' + (sd === "short" ? "공매도 " : "주문 ") + won(c.qty) + "주</span>"
            : '<span class="muted" style="font-size:12px">' + esc(c.why || "미주문") + "</span>")] };
      }), run.skip ? esc(run.skip) : "후보 없음");
    table($(pre + "executed"), [["종목"], ["결과"], ["진입가", "r"], ["수량", "r"], ["비고"]],
      ex.map(function (o) {
        return [td(stock(o.name, o.code)), td('<span class="state ' + o.status + '">' + (STATUS[o.status] || o.status) + "</span>"),
          td('<span class="num">' + won(o.entry) + "</span>", "r"), td('<span class="num">' + won(o.qty) + "</span>", "r"),
          td(esc(o.note || ""), "wrap")];
      }), "이 날 유효했던 주문 없음");
  }
  function reviewResult(r) {
    var a = REVIEW_ACT[r.action] || (r.sell ? REVIEW_ACT.sell : r.error ? ["skipped", "판단 실패 — 보유 유지"] : REVIEW_ACT.hold);
    if (sideOf(r) === "short") a = [a[0], String(a[1]).replace("매도", "환매")];
    var extra = r.action === "hold_updated" ? '<div class="stock-code">목표 ' + (r.target ? won(r.target) : "없음") + " · 손절 " + won(r.stop) + "</div>" : "";
    var kinds = (r.kinds || []).map(function (k) { return '<span class="kind-chip k-' + k + '">' + (KIND_TXT[k] || k) + "</span>"; }).join("");
    return (kinds ? '<div class="kinds">' + kinds + "</div>" : "") + '<span class="state ' + a[0] + '">' + a[1] + "</span>" + extra;
  }
  function renderLogs(run) {
    var L = [];
    if (!run || !run.date) { $("logs").innerHTML = '<div class="dim">실행 기록 없음</div>'; return; }
    if (run.skip) L.push('<div class="warn">[' + run.date + "] 건너뜀 — " + esc(run.skip) + "</div>");
    else {
      var nOrd = (run.candidates || []).filter(function (c) { return c.ordered; }).length;
      var sc = run.shortCandidates || [], nS = sc.filter(function (c) { return c.ordered; }).length;
      L.push("<div>[" + run.date + "] 청산 " + (run.exits || []).length + " · 검토 " + (run.reviews || []).length +
        " · 후보 " + (run.candidates || []).length + " · 주문 " + nOrd +
        (sc.length ? " · 숏 후보 " + sc.length + " · 공매도 " + nS : "") + "</div>");
    }
    (run.notes || []).forEach(function (n) { L.push('<div class="warn">⚠ ' + esc(n) + "</div>"); });
    (run.exits || []).forEach(function (e) {
      L.push("<div>· 청산 " + esc(e.name) + " " + reasonTxt(e.reason, e.side) + " " + pctTxt(e.retPct) + "</div>");
    });
    if ((run.capSkipped || []).length) L.push('<div class="dim">· 상한 초과로 미분석 ' + run.capSkipped.length + "종목</div>");
    if ((run.shortCapSkipped || []).length) L.push('<div class="dim">· Short 상한 초과로 미분석 ' + run.shortCapSkipped.length + "종목</div>");
    if (run.startedAt) L.push('<div class="dim">' + esc(run.startedAt) + " → " + esc(run.finishedAt || "…") + " · " + esc(run.agent || "") + "</div>");
    $("logs").innerHTML = L.join("");
  }

  // ── 섹터 시그널 (기준일) ──
  function renderSignals(res) {
    var sgn = res && res.signals;
    $("sig-asof").textContent = sgn ? "주간 브리핑 " + (sgn.asof || res.date) + " 기준 · ● 보유 종목 섹터" : "이 날 주간 브리핑 신호 없음(정산만 진행)";
    var m = (sgn && sgn.sectorScreen && sgn.sectorScreen.matrix) || {};
    $("matrix").innerHTML = SIG_ORDER.map(function (k) {
      var xs = m[k] || [];
      var list = xs.length ? "<ul>" + xs.map(function (x) {
        return '<li class="' + (S.held[x.code] ? "held" : "") + '"><span><span class="stock-name" style="font-size:13px">' + esc(x.name) +
          '</span> <span class="mx-sec">' + esc(x.sector || "") + '</span></span><span class="num muted">' + (x.score > 0 ? "+" : "") + (x.score == null ? "" : x.score) + "</span></li>";
      }).join("") + "</ul>" : '<div class="mx-empty">해당 종목 없음</div>';
      return '<div class="mx-cell" style="--c:' + SIG_C[k] + '"><h4>' + k + "<small>" + xs.length + '종목</small></h4><div class="mx-tag">' +
        SIG_SUB[k] + "</div>" + list + "</div>";
    }).join("");
    renderSectorMatrix(sgn && sgn.sectorFlow);
    renderShortWatch(m, res && res.date);
    var buy = (m["동반강세"] || []).length + (m["수급유입"] || []).length;
    return { buy: buy, down: (m["수급이탈"] || []).length + (m["동반약세"] || []).length };
  }

  // ── 섹터 x 수급 매트릭스 — 주간 브리핑(main index.html sectorFlow 렌더)을 그대로 옮김(주간 축 YTD/3M/1M/1W) ──
  var WB_AXES = ["YTD", "3M", "1M", "1W"], WB_KEYS = ["ytd", "m3", "m1", "chgPct"];
  var WB_SIG_C = { "동반강세": "#f04452", "수급이탈": "#fbbf24", "수급유입": "#2dd4bf", "동반약세": "#3182f6", "데이터부족": "#64748b" };
  function fmtAmt(v) { return (v > 0 ? "+" : "") + Math.round(v).toLocaleString(); }
  // 기관 세부(금융투자·투신(사모)·연기금·보험) 접기 — 기본 접힘(사이드바 옆 폭에 맞춤), 상태는 브라우저에 기억
  var sfOpen = false;
  try { sfOpen = localStorage.getItem("swing.sfDetail") === "1"; } catch (e) { /* 무시 */ }
  function sfMinW(det, etc, open) { return 640 + (open ? (det ? 4 : 1) * 78 : 0) + (etc ? 78 : 0); }
  function renderSectorMatrix(sfm) {
    var box = $("sector-matrix");
    if (!sfm || !(sfm.rows || []).length) { box.innerHTML = '<div class="muted" style="font-size:12px">섹터 신호 없음</div>'; return; }
    var heldSec = {};
    S.positions.forEach(function (p) { if (p.sector) heldSec[String(p.sector).replace(/[\s·・()]/g, "")] = 1; });
    var sfDet = sfm.rows.some(function (r) { return r.finInv != null; });
    var sfEtc = sfm.rows.some(function (r) { return r.etcCorp != null; });
    function col(v) { return v > 0 ? "#f04452" : v < 0 ? "#3182f6" : "inherit"; }
    function fp(v) {
      if (v == null || v === "") return '<td class="sf-p na">—</td>';
      return '<td class="sf-p" style="color:' + col(v) + '">' + (v > 0 ? "+" : "") + Number(v).toFixed(2) + "</td>";
    }
    function amt(v, det) {
      return '<td class="sf-a' + (det ? " sf-det" : "") + '" style="color:' + col(v) + '">' + (v == null ? "—" : fmtAmt(v)) + "</td>";
    }
    var rows = sfm.rows.map(function (r) {
      var sg = r.signal === "혼조" ? "" : (r.signal || "");            // 혼조는 표기 생략(주간 브리핑 규칙)
      var badge = sg ? '<span title="핵심수급(외인+기관) ' + fmtAmt(r.coreFlow != null ? r.coreFlow : (r.frgn || 0) + (r.orgn || 0)) +
        '억 · 가격추세=' + WB_AXES.join("/") + ' 3개 이상 동일 방향" style="color:' + (WB_SIG_C[sg] || "#94a3b8") +
        ';font-weight:600;font-size:11px">' + sg + "</span>" : "";
      var held = heldSec[String(r.name || "").replace(/[\s·・()]/g, "")] ? " held" : "";
      return '<tr class="sf-row' + held + '"><td class="sf-name">' + esc(r.name) + '</td><td class="sf-s">' + badge + "</td>" +
        WB_KEYS.map(function (k) { return fp(r[k]); }).join("") +
        amt(r.frgn) + amt(r.orgn) + (sfDet ? amt(r.finInv, 1) + amt(r.trust, 1) : "") + amt(r.fund, 1) + (sfDet ? amt(r.insur, 1) : "") +
        amt(r.prsn) + (sfEtc ? amt(r.etcCorp) : "") + "</tr>";
    }).join("");
    var th = function (t, det) { return "<th" + (det ? ' class="sf-det"' : "") + ">" + t + "</th>"; };
    var orgTh = '<th><button type="button" class="sf-toggle" aria-expanded="' + sfOpen + '" title="기관 세부(금융투자·투신(사모)·연기금·보험) ' +
      (sfOpen ? "접기" : "펼치기") + '">' + (sfDet ? "기관(합)" : "기관") + ' <span class="sf-caret">' + (sfOpen ? "◂" : "▸") + "</span></button></th>";
    var basis = sfm.chgBasis ? " · 1W=전주말 종가 대비" : "";
    var weak = sfm.neutralRatio ? " 또는 수급 미미(|외인+기관| &lt; 섹터 평소 규모의 " + Math.round(sfm.neutralRatio * 100) + "%)" : "";
    var foot = "가격추세 = " + WB_AXES.join("·") + "(%) 중 3개 이상 동일 방향" + basis + " · 핵심수급 = 외인+기관 주간 누적 · KIS 업종(KRX 산업분류)<br>" +
      '<span style="color:#f04452">동반강세</span>=가격강세+수급유입 · <span style="color:#fbbf24">수급이탈</span>=가격강세+수급이탈 · ' +
      '<span style="color:#2dd4bf">수급유입</span>=가격약세+수급유입 · <span style="color:#3182f6">동반약세</span>=가격약세+수급이탈 · ' +
      "공란=혼조(가격방향 불명확" + weak + ") · 데이터부족=일부 기간 결측 · 기관(합) 머리글을 누르면 기관 세부를 펼치거나 접습니다";
    box.innerHTML = '<details class="flow-details" open><summary>섹터 × 수급 매트릭스 — 가격 추세(%) vs 투자자 순매수 (주간, 억원) ▾</summary>' +
      '<div class="sf-wrap"><table class="sf-table' + (sfOpen ? "" : " det-hidden") + '" style="min-width:' + sfMinW(sfDet, sfEtc, sfOpen) +
      'px"><tr class="sf-head"><th>업종</th><th class="sf-sh">신호</th>' +
      WB_AXES.map(function (t) { return th(t); }).join("") + th("외인") + orgTh + (sfDet ? th("금융투자", 1) + th("투신(사모)", 1) : "") +
      th("연기금", 1) + (sfDet ? th("보험", 1) : "") + th("개인") + (sfEtc ? th("기타법인") : "") + "</tr>" + rows +
      '</table><div class="sf-foot">' + foot + "</div></div></details>";
    var tbl = box.querySelector(".sf-table"), btn = box.querySelector(".sf-toggle");
    btn.addEventListener("click", function () {
      sfOpen = !sfOpen;
      try { localStorage.setItem("swing.sfDetail", sfOpen ? "1" : "0"); } catch (e) { /* 무시 */ }
      tbl.classList.toggle("det-hidden", !sfOpen);
      tbl.style.minWidth = sfMinW(sfDet, sfEtc, sfOpen) + "px";
      btn.setAttribute("aria-expanded", sfOpen);
      btn.title = "기관 세부(금융투자·투신(사모)·연기금·보험) " + (sfOpen ? "접기" : "펼치기");
      btn.querySelector(".sf-caret").textContent = sfOpen ? "◂" : "▸";
    });
  }

  // ── Short 후보 칸(동반약세·수급이탈 전체 — 보유·상한으로 분석하지 않은 종목 포함) ──
  function renderShortWatch(m, date) {
    var xs = [];
    ["동반약세", "수급이탈"].forEach(function (k) { (m[k] || []).forEach(function (x) { xs.push([k, x]); }); });
    $("short-watch-sub").textContent = "동반약세·수급이탈 칸 전체" + (date ? " · " + date : "") + " — 판단·주문 결과는 위 표";
    table($("short-watch"), [["종목"], ["신호"], ["근거"], ["점수", "r"], ["보유 상태"]],
      xs.map(function (p) {
        var x = p[1];
        return [td(stock(x.name, x.code)), td('<span class="sec-name">' + esc(x.sector || "") + "</span>" + sig(p[0])),
          td(esc((x.reason || "").split(" — ")[0]), "wrap"), td('<span class="num">' + (x.score == null ? "—" : x.score) + "</span>", "r"),
          td(S.held[x.code] ? '<span class="state sell-next">Long 보유 중 · 매도 검토 대상</span>'
            : S.heldShort[x.code] ? '<span class="side-tag short">SHORT 보유 중</span>' : '<span class="muted">—</span>')];
      }), "이 날 Short 후보 칸이 비어 있습니다");
  }

  // ── 트레이딩 리포트(청산 거래 성과) — 손익 색은 한국식(빨강=이익·파랑=손실)으로 통일(2026-09-29) ──
  var RP = { period: "all", side: "all", sort: "date", limit: 10, day: null };
  var RP_SIDE = { long: ["Long", "var(--up-red)"], short: ["Short", "var(--down-blue)"], all: ["합계", "var(--kospi-blue)"] };
  function signCls(v) { return v > 0 ? "up" : v < 0 ? "down" : ""; }
  function wonS(v) { return v == null ? "—" : (v > 0 ? "+" : "") + won(v); }
  function isoWeek(d) {
    var t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
    var day = t.getUTCDay() || 7;
    t.setUTCDate(t.getUTCDate() + 4 - day);
    var y = new Date(Date.UTC(t.getUTCFullYear(), 0, 1));
    return t.getUTCFullYear() + "-" + Math.ceil(((t - y) / 864e5 + 1) / 7);
  }
  function periodTrades(all) {
    var ref = (S.summary && S.summary.asof) || (all.length ? all[all.length - 1].exitDate : null);
    if (RP.period === "day") return all.filter(function (t) { return t.exitDate === RP.day; });
    if (RP.period === "all" || !ref) return all;
    var r = new Date(ref + "T00:00:00");
    return all.filter(function (t) {
      var d = new Date(t.exitDate + "T00:00:00");
      return RP.period === "month" ? (d.getFullYear() === r.getFullYear() && d.getMonth() === r.getMonth())
        : isoWeek(d) === isoWeek(r);
    });
  }
  function stats(ts) {
    var w = ts.filter(function (t) { return t.pnl > 0; }), l = ts.filter(function (t) { return t.pnl < 0; });
    var gp = w.reduce(function (a, t) { return a + t.pnl; }, 0), gl = -l.reduce(function (a, t) { return a + t.pnl; }, 0);
    var n = ts.length, rets = ts.map(function (t) { return t.retPct; });
    function avg(a) { return a.length ? a.reduce(function (x, y) { return x + y; }, 0) / a.length : null; }
    return { n: n, pnl: gp - gl, pf: gl > 0 ? gp / gl : (gp > 0 ? Infinity : null),
      win: n ? w.length / n * 100 : null, avgRet: avg(rets), exp: n ? (gp - gl) / n : null,
      avgWin: avg(w.map(function (t) { return t.retPct; })), avgLoss: avg(l.map(function (t) { return t.retPct; })),
      best: n ? Math.max.apply(null, rets) : null, worst: n ? Math.min.apply(null, rets) : null,
      hold: avg(ts.map(function (t) { return t.holdDays || 0; })) };
  }
  function pfTxt(v) { return v == null ? "—" : v === Infinity ? "∞" : v.toFixed(2); }
  function bySide(ts, sd) { return sd === "all" ? ts : ts.filter(function (t) { return sideOf(t) === sd; }); }

  function renderReport() {
    var all = S.trades.slice().sort(function (a, b) { return a.exitDate < b.exitDate ? -1 : a.exitDate > b.exitDate ? 1 : 0; });
    setCount("cnt-trades", all.length);
    rpDaySelect(all);
    var ts = periodTrades(all);
    var lbl = { all: "전체 기간", week: "이번 주", month: "이번 달", day: RP.day + " 하루" }[RP.period];
    $("rp-sub").textContent = lbl + " · 청산 " + ts.length + "건 · 세후(매도세 0.2%) · 기준일 " + ((S.summary && S.summary.asof) || "—");
    var dayMode = RP.period === "day";          // 일자 모드: 곡선·요약은 전체 기간을 보여 주고 선택일만 표시
    rpKpis(ts); rpCum(dayMode ? all : ts, dayMode ? RP.day : null); rpDaily(all, dayMode ? all : ts); rpCompare(ts); rpReasons(ts); rpDist(ts); rpTrades(ts);
  }

  // 일자 선택 목록(청산일, 최신 먼저) — "일자" 모드일 때만 보임
  function rpDaySelect(all) {
    var sel = $("rp-day"), days = [], seen = {};
    all.forEach(function (t) { if (!seen[t.exitDate]) { seen[t.exitDate] = 1; days.push(t.exitDate); } });
    days.reverse();
    if (!RP.day || days.indexOf(RP.day) < 0) RP.day = days[0] || null;
    var cur = Array.prototype.map.call(sel.options, function (o) { return o.value; }).join(",");
    if (cur !== days.join(",")) sel.innerHTML = days.map(function (d) { return "<option>" + d + "</option>"; }).join("");
    sel.value = RP.day || "";
    sel.hidden = RP.period !== "day";
  }

  // 일자별 요약 — 선택 기간 안의 청산일마다 건수·손익·승률·누적, 행 클릭 = 그 날짜 조회
  function rpDaily(all, ts) {
    var days = {}, order = [];
    ts.forEach(function (t) {
      if (!days[t.exitDate]) { days[t.exitDate] = []; order.push(t.exitDate); }
      days[t.exitDate].push(t);
    });
    var acc = 0, rows = order.map(function (d) {
      var a = days[d], p = a.reduce(function (x, t) { return x + t.pnl; }, 0);
      acc += p;
      return { d: d, a: a, p: p, acc: acc };
    }).reverse();
    var maxAbs = Math.max.apply(null, rows.map(function (r) { return Math.abs(r.p); }).concat([1]));
    var h = '<thead><tr><th>청산일</th><th class="r">건수</th><th class="r">Long · Short</th><th class="r">승률</th>' +
      '<th class="r">평균 수익률</th><th class="r">실현 손익</th><th></th><th class="r">누적 손익</th></tr></thead><tbody>';
    h += rows.length ? rows.map(function (r) {
      var n = r.a.length, w = r.a.filter(function (t) { return t.pnl > 0; }).length;
      var l = r.a.filter(function (t) { return sideOf(t) === "long"; }).length;
      var avg = r.a.reduce(function (x, t) { return x + t.retPct; }, 0) / n;
      return '<tr data-day="' + r.d + '"' + (RP.period === "day" && r.d === RP.day ? ' class="sel"' : "") + ' title="' + r.d + ' 조회">' +
        '<td class="num">' + r.d + "</td>" + '<td class="r num">' + n + "건</td>" +
        '<td class="r num">' + l + " · " + (n - l) + "</td>" + '<td class="r num">' + (w / n * 100).toFixed(0) + "%</td>" +
        '<td class="r">' + pct(avg) + "</td>" + '<td class="r num ' + signCls(r.p) + '">' + wonS(r.p) + "</td>" +
        '<td class="dcell"><div class="dbar"><span class="' + (r.p >= 0 ? "l" : "s") + '" style="width:' + (Math.abs(r.p) / maxAbs * 50) + '%"></span></div></td>' +
        '<td class="r num ' + signCls(r.acc) + '">' + wonS(r.acc) + "</td></tr>";
    }).join("") : '<tr class="empty"><td colspan="8">이 기간 청산 거래가 없습니다</td></tr>';
    $("rp-daily").innerHTML = h + "</tbody>";
  }

  // ① 핵심 지표
  function rpKpis(ts) {
    var a = stats(ts), L = stats(bySide(ts, "long")), Sh = stats(bySide(ts, "short"));
    function sub(f) { return "Long " + f(L) + " · Short " + f(Sh); }
    var k = [
      ["실현 손익", '<span class="' + signCls(a.pnl) + '">' + (a.n ? wonS(a.pnl) + "원" : "—") + "</span>",
        sub(function (s) { return s.n ? '<span class="' + signCls(s.pnl) + '">' + wonS(s.pnl) + "</span>" : "—"; })],
      ["Profit Factor", pfTxt(a.pf), sub(function (s) { return pfTxt(s.pf); }) + " · 1 초과 = 이익 우위"],
      ["승률", a.win == null ? "—" : a.win.toFixed(1) + "%", sub(function (s) { return s.win == null ? "—" : s.win.toFixed(0) + "%"; })],
      ["거래당 기대값", '<span class="' + signCls(a.exp) + '">' + (a.exp == null ? "—" : wonS(a.exp) + "원") + "</span>",
        "평균 수익률 " + (a.avgRet == null ? "—" : pct(a.avgRet))]
    ];
    $("rp-kpis").innerHTML = k.map(function (x) {
      return '<div class="kpi"><div class="kpi-lbl">' + x[0] + '</div><div class="kpi-val">' + x[1] + '</div><div class="kpi-sub">' + x[2] + "</div></div>";
    }).join("");
  }

  // ② 누적 실현손익(청산일 기준) — 합계·Long·Short 3선, 크로스헤어 툴팁
  function rpCum(ts, markDay) {
    var box = $("rp-cum");
    if (!ts.length) { box.innerHTML = '<div class="empty-chart">이 기간 청산 거래가 없습니다</div>'; $("rp-cum-legend").innerHTML = ""; return; }
    var dates = [], seen = {};
    ts.forEach(function (t) { if (!seen[t.exitDate]) { seen[t.exitDate] = 1; dates.push(t.exitDate); } });
    var series = ["all", "long", "short"].map(function (sd) {
      var acc = 0, pts = dates.map(function (d) {
        bySide(ts, sd).forEach(function (t) { if (t.exitDate === d) acc += t.pnl; });
        return acc;
      });
      return { sd: sd, pts: pts, any: bySide(ts, sd).length > 0 };
    }).filter(function (s) { return s.any; });
    var W = box.clientWidth || 800, H = box.clientHeight || 220, m = { l: 64, r: 12, t: 12, b: 24 };
    var vals = [0];
    series.forEach(function (s) { vals = vals.concat(s.pts); });
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals), pad = (hi - lo) * 0.12 || 1e5;
    lo -= pad; hi += pad;
    var n = dates.length;
    var x = function (i) { return m.l + (n === 1 ? (W - m.l - m.r) / 2 : i * (W - m.l - m.r) / (n - 1)); };
    var y = function (v) { return m.t + (hi - v) / (hi - lo) * (H - m.t - m.b); };
    var svg = '<svg viewBox="0 0 ' + W + " " + H + '"><g class="axis">';
    for (var k = 0; k <= 4; k++) {
      var v = lo + (hi - lo) * k / 4;
      svg += '<line class="gridline" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + y(v) + '" y2="' + y(v) + '"/>' +
        '<text x="' + (m.l - 6) + '" y="' + (y(v) + 3) + '" text-anchor="end">' + (v / 1e4).toFixed(0) + "만</text>";
    }
    var step = Math.max(1, Math.ceil(n / 7));
    dates.forEach(function (d, i) {
      if (i % step === 0 || i === n - 1)
        svg += '<text x="' + x(i) + '" y="' + (H - 6) + '" text-anchor="' + (i === 0 && n > 1 ? "start" : i === n - 1 && n > 1 ? "end" : "middle") + '">' + d.slice(5) + "</text>";
    });
    svg += '</g><line class="base" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + y(0) + '" y2="' + y(0) + '"/>';
    var mi = markDay ? dates.indexOf(markDay) : -1;
    if (mi >= 0) {
      svg += '<line class="mark-ln" x1="' + x(mi) + '" x2="' + x(mi) + '" y1="' + m.t + '" y2="' + (H - m.b) + '"/>' +
        '<text class="mark-tx" x="' + x(mi) + '" y="' + (m.t + 10) + '" text-anchor="' + (mi > n / 2 ? "end" : "start") + '" dx="' +
        (mi > n / 2 ? -6 : 6) + '">' + markDay.slice(5) + " 조회</text>";
    }
    series.forEach(function (s) {
      svg += '<path class="cum-ln ' + s.sd + '" d="M' + s.pts.map(function (v, i) { return x(i) + "," + y(v); }).join("L") + '"/>';
    });
    svg += '<line class="cross" id="rp-cross" y1="' + m.t + '" y2="' + (H - m.b) + '" visibility="hidden"/>';
    series.forEach(function (s) { svg += '<circle class="cum-dot ' + s.sd + '" id="rp-dot-' + s.sd + '" r="4" visibility="hidden"/>'; });
    svg += '<rect x="' + m.l + '" y="0" width="' + (W - m.l - m.r) + '" height="' + H + '" fill="transparent" id="rp-hit"/></svg>';
    box.innerHTML = svg + '<div class="tip" id="rp-tip" hidden></div>';
    $("rp-cum-legend").innerHTML = series.map(function (s) {
      var last = s.pts[s.pts.length - 1];
      return '<span><i class="lg ' + s.sd + '"></i>' + RP_SIDE[s.sd][0] + ' <b class="' + signCls(last) + '">' + wonS(last) + "</b></span>";
    }).join("");
    var hit = $("rp-hit"), tip = $("rp-tip"), cross = $("rp-cross");
    hit.addEventListener("mousemove", function (ev) {
      var rect = box.getBoundingClientRect(), px = (ev.clientX - rect.left) * W / rect.width;
      var i = n === 1 ? 0 : Math.max(0, Math.min(n - 1, Math.round((px - m.l) / ((W - m.l - m.r) / (n - 1)))));
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
      var h = "<b>" + dates[i] + "</b>";
      series.forEach(function (s) {
        var d = $("rp-dot-" + s.sd);
        d.setAttribute("cx", x(i)); d.setAttribute("cy", y(s.pts[i])); d.setAttribute("visibility", "visible");
        h += RP_SIDE[s.sd][0] + " " + wonS(s.pts[i]) + "원<br>";
      });
      tip.hidden = false; tip.innerHTML = h;
      var left = x(i) * rect.width / W + 12;
      if (left + tip.offsetWidth > rect.width) left -= tip.offsetWidth + 24;
      tip.style.left = left + "px"; tip.style.top = "6px";
    });
    hit.addEventListener("mouseleave", function () {
      tip.hidden = true; cross.setAttribute("visibility", "hidden");
      series.forEach(function (s) { $("rp-dot-" + s.sd).setAttribute("visibility", "hidden"); });
    });
  }

  // ③ Long vs Short 비교표 — 두 방향 모두 거래가 있을 때만 더 좋은 값 강조
  function rpCompare(ts) {
    var st = { long: stats(bySide(ts, "long")), short: stats(bySide(ts, "short")), all: stats(ts) };
    var both = st.long.n && st.short.n;
    var rows = [
      ["청산 거래", function (s) { return s.n + "건"; }, null],
      ["실현 손익", function (s) { return s.n ? '<span class="' + signCls(s.pnl) + '">' + wonS(s.pnl) + "</span>" : "—"; }, "pnl"],
      ["Profit Factor", function (s) { return pfTxt(s.pf); }, "pf"],
      ["승률", function (s) { return s.win == null ? "—" : s.win.toFixed(1) + "%"; }, "win"],
      ["평균 수익률", function (s) { return s.avgRet == null ? "—" : pct(s.avgRet); }, "avgRet"],
      ["거래당 기대값", function (s) { return s.exp == null ? "—" : '<span class="' + signCls(s.exp) + '">' + wonS(s.exp) + "</span>"; }, "exp"],
      ["평균 이익 / 평균 손실", function (s) { return (s.avgWin == null ? "—" : pct(s.avgWin)) + " / " + (s.avgLoss == null ? "—" : pct(s.avgLoss)); }, null],
      ["최대 이익 / 최대 손실", function (s) { return (s.best == null ? "—" : pct(s.best)) + " / " + (s.worst == null ? "—" : pct(s.worst)); }, null],
      ["평균 보유", function (s) { return s.hold == null ? "—" : s.hold.toFixed(1) + "일"; }, null]
    ];
    var h = '<thead><tr><th>지표</th>' + ["long", "short", "all"].map(function (sd) {
      return '<th class="r"><span class="cmp-h" style="--c:' + RP_SIDE[sd][1] + '">' + RP_SIDE[sd][0] + "</span></th>";
    }).join("") + "</tr></thead><tbody>";
    rows.forEach(function (r) {
      var best = null;
      if (both && r[2]) {
        var a = st.long[r[2]], b = st.short[r[2]];
        if (a != null && b != null && a !== b) best = a > b ? "long" : "short";
      }
      h += "<tr><td>" + r[0] + "</td>" + ["long", "short", "all"].map(function (sd) {
        var off = sd !== "all" && !st[sd].n;
        return '<td class="r num' + (off ? " off" : "") + (best === sd ? " best" : "") + '">' + (off ? "—" : r[1](st[sd])) + "</td>";
      }).join("") + "</tr>";
    });
    $("rp-compare").innerHTML = h + "</tbody>";
    $("rp-compare-note").textContent = both ? "굵게 = Long·Short 중 더 좋은 값" : "한쪽 방향 거래가 없어 비교 강조는 생략";
  }

  // ④ 청산 사유 — 평균 수익률 높은 순(이익 사유 위), 막대 = 건수, 칩 = 평균 수익률, 기여 = 손익 합
  function rpReasons(ts) {
    var g = {};
    ts.forEach(function (t) { (g[t.reason] = g[t.reason] || []).push(t); });
    var keys = Object.keys(g).map(function (k) {
      var a = g[k], avg = a.reduce(function (x, t) { return x + t.retPct; }, 0) / a.length;
      return { k: k, n: a.length, avg: avg, pnl: a.reduce(function (x, t) { return x + t.pnl; }, 0),
        l: a.filter(function (t) { return sideOf(t) === "long"; }).length, s: a.filter(function (t) { return sideOf(t) === "short"; }).length };
    }).sort(function (a, b) { return b.avg - a.avg; });
    var maxN = Math.max.apply(null, keys.map(function (r) { return r.n; }).concat([1]));
    $("rp-reasons").innerHTML = keys.length ? keys.map(function (r) {
      return '<div class="rs-row"><span class="reason ' + r.k + '">' + (REASON[r.k] || r.k) + "</span>" +
        '<div class="rs-bar"><span class="' + signCls(r.avg) + '" style="width:' + (r.n / maxN * 100) + '%"></span></div>' +
        '<span class="rs-n num">' + r.n + '건 <small>L' + r.l + " · S" + r.s + "</small></span>" +
        '<span class="rs-chip ' + signCls(r.avg) + '">' + pctTxt(r.avg) + "</span>" +
        '<span class="rs-pnl num ' + signCls(r.pnl) + '">' + wonS(r.pnl) + "원</span></div>";
    }).join("") : '<div class="muted" style="font-size:13px">이 기간 청산 거래가 없습니다</div>';
  }

  // ⑤ 수익률 분포 — 2%p 구간, 막대 위 건수 직접 표기
  function rpDist(ts) {
    var edges = [-Infinity, -8, -6, -4, -2, 0, 2, 4, 6, 8, Infinity];
    var labels = ["≤−8", "−8~−6", "−6~−4", "−4~−2", "−2~0", "0~2", "2~4", "4~6", "6~8", "≥8"];
    var cnt = labels.map(function () { return 0; });
    ts.forEach(function (t) {
      for (var i = 0; i < labels.length; i++) if (t.retPct >= edges[i] && t.retPct < edges[i + 1]) { cnt[i]++; break; }
    });
    var mx = Math.max.apply(null, cnt.concat([1]));
    $("rp-dist").innerHTML = ts.length ? '<div class="dist">' + cnt.map(function (c, i) {
      return '<div class="dist-col" title="' + labels[i] + "%: " + c + '건"><span class="dist-n">' + (c || "") + '</span>' +
        '<span class="dist-bar ' + (i < 5 ? "down" : "up") + '" style="height:' + (c / mx * 100) + '%"></span>' +
        '<span class="dist-lbl">' + labels[i] + "</span></div>";
    }).join("") + '</div><div class="dist-foot">거래별 수익률(%) · 손절·목표 구간에 몰리는지, 꼬리 손실이 있는지 확인</div>'
      : '<div class="muted" style="font-size:13px">이 기간 청산 거래가 없습니다</div>';
  }

  // ⑥ 청산 내역 — 방향 필터·정렬·최근 N건 + 더 보기
  function rpTrades(ts0) {
    var ts = bySide(ts0, RP.side).slice();
    if (RP.sort === "date") ts.sort(function (a, b) { return a.exitDate < b.exitDate ? 1 : a.exitDate > b.exitDate ? -1 : 0; });
    else if (RP.sort === "best") ts.sort(function (a, b) { return b.retPct - a.retPct; });
    else ts.sort(function (a, b) { return a.retPct - b.retPct; });
    var shown = ts.slice(0, RP.limit);
    table($("trades"), [["청산일"], ["방향"], ["종목"], ["사유"], ["진입 → 청산", "r"], ["보유", "r"], ["수익률 · 손익", "r"]],
      shown.map(function (t) {
        return { key: t.decisionKey, cells: [td('<span class="num">' + t.exitDate.slice(5) + "</span>"),
          td('<span class="side-tag ' + sideOf(t) + '">' + (sideOf(t) === "long" ? "LONG" : "SHORT") + "</span>"),
          td(stock(t.name, t.code, "진입 " + (t.entryDate || "").slice(5), null, testTag(t))),
          td(t.exitDecisionKey
            ? '<button type="button" class="reason ' + t.reason + ' name-link" data-key="' + esc(t.exitDecisionKey) + '" title="청산 판단 원문 보기">' + esc(reasonTxt(t.reason, t.side)) + " ›</button>"
            : '<span class="reason ' + t.reason + '"' + (t.note ? ' title="' + esc(t.note) + '"' : "") + ">" + esc(reasonTxt(t.reason, t.side)) + "</span>"),
          td('<div class="stock-info" style="align-items:flex-end"><span class="num">' + won(t.entryPrice) + " → " + won(t.exitPrice) +
            '</span><span class="stock-code">' + won(t.qty) + "주</span></div>", "r"),
          td('<span class="num">' + t.holdDays + "일</span>", "r"),
          td('<div class="stock-info" style="align-items:flex-end">' + pnlBadge(t.retPct, pctTxt(t.retPct).replace(/^[+-]/, "")) +
            '<span class="stock-code ' + signCls(t.pnl) + '">' + wonS(t.pnl) + "원</span></div>", "r")] };
      }), RP.side === "short" ? "Short 청산 내역 없음" : "이 기간 청산 내역이 없습니다");
    var more = $("trades-more");
    if (ts.length > RP.limit) { more.hidden = false; more.textContent = "더 보기 (" + (ts.length - RP.limit) + "건 남음)"; }
    else more.hidden = true;
    $("trade-caption").textContent = ts.length + "건 중 " + shown.length + "건 표시 · 행을 누르면 진입 판단, 사유 ›는 매도 판단";
  }

  function segBind(id, key, reset) {
    document.querySelectorAll("#" + id + " button").forEach(function (b) {
      b.addEventListener("click", function () {
        RP[key] = b.dataset.v;
        if (reset) RP.limit = 10;
        document.querySelectorAll("#" + id + " button").forEach(function (x) { x.classList.toggle("active", x === b); });
        renderReport();
      });
    });
  }
  segBind("rp-period", "period", true);
  $("rp-day").addEventListener("change", function () { RP.day = this.value; RP.limit = 10; renderReport(); });
  $("rp-daily").addEventListener("click", function (e) {
    var tr = e.target.closest("tr[data-day]");
    if (!tr) return;
    RP.period = "day"; RP.day = tr.getAttribute("data-day"); RP.limit = 10;
    document.querySelectorAll("#rp-period button").forEach(function (x) { x.classList.toggle("active", x.dataset.v === "day"); });
    renderReport();
  });
  segBind("trade-filter", "side", true);
  segBind("trade-sort", "sort", false);
  $("trades-more").addEventListener("click", function () { RP.limit += 20; renderReport(); });

  // ── Long·Short 포트폴리오(현재 보유 구성: 노출·비중·섹터) ──
  function renderHoldingsPF(ps, summary) {
    var total = summary.equity || 1, cash = summary.cash || 0;
    var L = ps.filter(function (p) { return sideOf(p) === "long"; });
    var Sh = ps.filter(function (p) { return sideOf(p) === "short"; });
    function sum(a, f) { return a.reduce(function (x, p) { return x + (f(p) || 0); }, 0); }
    function val(p) { return p.value; }
    function unreal(p) { return (p.value || 0) * (p.unrealPct || 0) / (100 + (p.unrealPct || 0)); }
    function pc(v) { return (v / total * 100).toFixed(1) + "%"; }
    function sgn(v) { return (v > 0 ? "+" : "") + won(v); }
    function cls(v) { return v > 0 ? "up" : v < 0 ? "down" : ""; }
    var lv = sum(L, val), sv = sum(Sh, val), ul = sum(L, unreal), us = sum(Sh, unreal);
    setCount("cnt-pf", ps.length);
    $("pf-asof").textContent = "현재 보유 기준(" + (summary.asof || "—") + " 종가) · 비중 = 평가금액 ÷ 총자산 " + eok(total);
    var maxV = Math.max.apply(null, ps.map(val).concat([0]));
    var tiles = [
      ["총자산", eok(total), "현금 " + eok(cash) + " · " + pc(cash)],
      ["Long 노출", '<span class="up">' + eok(lv) + "</span>", L.length + "종목 · " + pc(lv)],
      ["Short 노출", '<span class="down">' + eok(sv) + "</span>", Sh.length + "종목 · " + pc(sv)],
      ["총노출 · 순노출", pc(lv + sv) + " · " + (lv - sv > 0 ? "+" : "") + pc(lv - sv), "Gross = L+S · Net = L−S"],
      ["미실현 손익", '<span class="' + cls(ul + us) + '">' + sgn(ul + us) + "원</span>", "Long " + sgn(ul) + " · Short " + sgn(us)],
      ["보유 종목", ps.length + "종목", "Long " + L.length + " · Short " + Sh.length],
      ["최대 비중", ps.length ? pc(maxV) : "—", "종목당 상한 " + (((S.config && S.config.maxWeight) || 0.1) * 100) + "%"],
      ["섹터 수", Object.keys(ps.reduce(function (a, p) { a[p.sector || "-"] = 1; return a; }, {})).length + "개", "보유 종목 기준"]
    ];
    $("pf-tiles").innerHTML = tiles.map(function (x) {
      return '<div class="port-item"><span class="port-lbl">' + x[0] + '</span><span class="port-val">' + x[1] +
        '</span><span class="port-sub">' + x[2] + "</span></div>";
    }).join("");
    var base = Math.max(total, lv + sv + cash);
    $("pf-exposure").innerHTML = '<div class="exp-bar"><span class="b-long" style="width:' + (lv / base * 100) + '%"></span>' +
      '<span class="b-short" style="width:' + (sv / base * 100) + '%"></span><span class="b-cash" style="width:' + (cash / base * 100) + '%"></span></div>' +
      '<div class="exp-legend"><span><i style="background:var(--up-red)"></i>Long<b>' + pc(lv) + "</b></span>" +
      '<span><i style="background:var(--down-blue)"></i>Short<b>' + pc(sv) + "</b></span>" +
      '<span><i style="background:rgba(148,163,184,.5)"></i>현금<b>' + pc(cash) + "</b></span></div>";

    var sec = {};
    ps.forEach(function (p) {
      var k = p.sector || "기타", e = sec[k] = sec[k] || { l: 0, s: 0, n: 0 };
      e[sideOf(p) === "short" ? "s" : "l"] += p.value || 0;
      e.n += 1;
    });
    var secRows = Object.keys(sec).map(function (k) { return [k, sec[k]]; })
      .sort(function (a, b) { return (b[1].l + b[1].s) - (a[1].l + a[1].s); });
    var maxAbs = Math.max.apply(null, secRows.map(function (r) { return Math.max(r[1].l, r[1].s); }).concat([1]));
    table($("pf-sectors"), [["섹터"], ["종목", "r"], ["Long", "r"], ["Short", "r"], ["순노출", "r"], ["순비중", "r"], ["Short ◀ ▶ Long"]],
      secRows.map(function (r) {
        var e = r[1], net = e.l - e.s;
        return [td('<span class="stock-name" style="font-size:13px">' + esc(r[0]) + "</span>"),
          td('<span class="num">' + e.n + "</span>", "r"),
          td('<span class="num up">' + (e.l ? eok(e.l) : "—") + "</span>", "r"),
          td('<span class="num down">' + (e.s ? eok(e.s) : "—") + "</span>", "r"),
          td('<span class="num ' + cls(net) + '">' + (net > 0 ? "+" : net < 0 ? "−" : "") + eok(Math.abs(net)) + "</span>", "r"),
          td(pct(net / total * 100, 1), "r"),
          td('<div class="dbar"><span class="l" style="width:' + (e.l / maxAbs * 50) + '%"></span><span class="s" style="width:' +
            (e.s / maxAbs * 50) + '%"></span></div>')];
      }), "보유 종목 없음");

    var sorted = ps.slice().sort(function (a, b) { return (b.value || 0) - (a.value || 0); });
    table($("pf-holdings"), [["방향"], ["종목"], ["섹터"], ["평가금액", "r"], ["비중"], ["평가손익", "r"], ["보유일"]],
      sorted.map(function (p) {
        return { key: p.decisionKey, cells: [td('<span class="side-tag ' + sideOf(p) + '">' + sideOf(p).toUpperCase() + "</span>"),
          td(stock(p.name, p.code, null, null, testTag(p))), td('<span class="sec-name">' + esc(p.sector || "") + "</span>"),
          td('<span class="num">' + won(p.value) + "</span>", "r"),
          td('<span class="wbar ' + sideOf(p) + '" style="width:' + Math.max(4, (p.value || 0) / (maxV || 1) * 90) + 'px"></span>' +
            '<span class="num">' + pc(p.value || 0) + "</span>"),
          td(pnlBadge(p.unrealPct, pctTxt(p.unrealPct).replace(/^[+-]/, "")), "r"), td(holdBar(p.holdDay))] };
      }), "보유 종목 없음");
  }

  // ── 판단 원문 ──
  function openDecision(key) {
    api("/api/swing/decision?key=" + encodeURIComponent(key)).then(function (d) {
      var sh = d.side === "short";
      $("dec-title").textContent = d.code + " · " + d.date + " · " +
        (d.purpose === "review" ? (sh ? "Short 환매 검토" : "매도 검토") : (sh ? "공매도 진입" : "신규 진입"));
      var g = [["PM 등급", rating(d.rating, d.error)], ["Trader", esc(d.action || "—")],
        ["진입가", '<span class="param-value entry">' + won(d.entry) + "</span>"], ["손절가", '<span class="param-value stop">' + won(d.stop) + "</span>"],
        ["목표가(PM)", '<span class="param-value target">' + (d.target ? won(d.target) : "—") + "</span>"],
        ["비중", d.weight ? (d.weight * 100).toFixed(1) + "%" : "—"]];
      var h = '<div class="dec-grid">' + g.map(function (x) {
        return '<div class="port-item"><span class="port-lbl">' + x[0] + '</span><span class="port-val">' + x[1] + "</span></div>";
      }).join("") + "</div>";
      if (d.error) h += '<div class="dec-sec"><h4>판단 실패</h4><div class="report" style="color:#fca5a5">' + esc(d.error) + "</div></div>";
      if (d.summary) h += '<div class="dec-sec"><h4>PM 요약</h4><div class="report">' + esc(d.summary) + "</div></div>";
      var reps = d.reports || {}, known = {};
      ROLE_GROUPS.forEach(function (g) { g[1].forEach(function (r) { known[r[0]] = 1; }); });
      var extra = Object.keys(reps).filter(function (k) { return !known[k]; }).map(function (k) { return [k, k]; });
      var groups = ROLE_GROUPS.map(function (g, i) {
        return [g[0], g[1].concat(i === ROLE_GROUPS.length - 1 ? extra : []).filter(function (r) { return reps[r[0]]; })];
      }).filter(function (g) { return g[1].length; });
      // 단계 묶음은 접힌 상태. 역할이 하나인 단계는 원문 바로, 여럿이면 역할별로 한 번 더 접는다
      if (groups.length) h += '<div class="dec-sec"><h4>종합 리포트</h4>' + groups.map(function (g) {
        var rep = function (k) {   // peers 는 JSON 이라 코드 블록, 나머지는 마크다운
          return '<div class="report md">' + (k === "peers" ? "<pre><code>" + esc(reps[k]) + "</code></pre>" : md(reps[k])) + "</div>";
        };
        var body = g[1].length === 1 ? rep(g[1][0][0])
          : '<div class="role-list">' + g[1].map(function (r) {
            return "<details><summary>" + esc(r[1]) + "</summary>" + rep(r[0]) + "</details>";
          }).join("") + "</div>";
        return '<details class="stage"><summary>' + esc(g[0]) + (g[1].length > 1 ? ' <span class="muted">' + g[1].length + "</span>" : "") +
          "</summary>" + body + "</details>";
      }).join("") + "</div>";
      h += '<p class="muted" style="font-size:11px;margin-top:14px">판단 엔진 ' + esc(d.agent || "—") + "</p>";
      $("dec-body").innerHTML = h;
      $("decision").showModal();
    }).catch(function (e) { alert("판단 원문을 불러오지 못했습니다: " + e.message); });
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-key]");    // 사유 배지(매도 판단)가 행(진입 판단)보다 우선
    if (b) openDecision(b.getAttribute("data-key"));
  });

  // ── 기준일 로드 ──
  function loadDate(date) {
    var enc = encodeURIComponent(date);
    return Promise.all([
      api("/api/swing/runs?date=" + enc).catch(function () { return null; }),
      api("/api/swing/orders?date=" + enc),
      api("/api/swing/signals?date=" + enc).catch(function () { return null; })
    ]).then(function (r) {
      renderRun(r[0], r[1]);
      renderSignals(r[2]);
    });
  }

  // ── 부트 ──
  Promise.all([api("/api/swing/summary"), api("/api/swing/equity"), api("/api/swing/positions"),
    api("/api/swing/runs"), api("/api/swing/trades"), api("/api/swing/config")]).then(function (r) {
    S.summary = r[0]; S.equity = r[1]; S.positions = r[2]; S.trades = r[4]; S.config = r[5];
    S.heldShort = {};
    S.positions.forEach(function (p) { if (sideOf(p) === "short") S.heldShort[p.code] = 1; else S.held[p.code] = 1; });
    renderHeader(r[0]); renderPortfolio(r[0]); renderRules(r[5]);
    document.querySelectorAll(".hold-n").forEach(function (x) { x.textContent = HOLD_N(); });
    if (S.config && S.config.borrowRate != null) $("cfg-borrow").textContent = +(S.config.borrowRate * 100).toFixed(2);
    if (S.config && S.config.shortMaxGross != null) $("cfg-sgross").textContent = Math.round(S.config.shortMaxGross * 100);
    renderEquity(r[1], r[0].capital0); renderPositions(r[2]); renderHoldingsPF(r[2], r[0]); renderReport();
    var dates = r[3].slice().reverse(), sel = $("run-date");
    sel.innerHTML = dates.length ? dates.map(function (d) { return "<option>" + d + "</option>"; }).join("") : "<option>—</option>";
    sel.addEventListener("change", function () { loadDate(sel.value); });
    if (dates.length) loadDate(dates[0]);
    else { renderRun(null, {}); renderSignals(null); }
    var t;
    window.addEventListener("resize", function () { clearTimeout(t); t = setTimeout(function () { renderEquity(S.equity, S.summary.capital0); renderReport(); }, 150); });
  }).catch(function (e) {
    $("run-status").textContent = "API 연결 실패";
    $("run-dot").className = "status-dot warn";
    $("portfolio").innerHTML = '<div class="port-item wide"><span class="port-lbl">오류</span><span class="port-val">' + esc(e.message) + "</span></div>";
  });
})();
