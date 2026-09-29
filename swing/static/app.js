/* 스윙 모의투자 대시보드 — swing/server.py API 만 읽는다(표시 전용). 디자인은 backtest_stock 메인 대시보드 양식. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var REASON = { stop: "손절", stop_gap: "갭 손절(시가)", target: "목표가", expiry: "3일 만기", pm_sell: "PM 매도", cash: "현금 확보" };
  var STATUS = { filled: "체결", cancelled: "취소", skipped: "스킵", open: "대기" };
  var SIG_ORDER = ["동반강세", "수급유입", "수급이탈", "동반약세"];
  var SIG_C = { "동반강세": "var(--sig-strong)", "수급유입": "var(--sig-inflow)", "수급이탈": "var(--sig-outflow)", "동반약세": "var(--sig-weak)" };
  var SIG_SUB = { "동반강세": "추세 지속형 상방", "수급유입": "초기 유입형 상방", "수급이탈": "상승 후 약화 · 매도 검토 트리거", "동반약세": "약세 지속형 하방 · 매도 검토 트리거" };
  var ROLE_ORDER = [["peers", "해외 비교기업"], ["collect", "수집 상태"], ["market", "기술적 분석"], ["sentiment", "심리"],
    ["news", "뉴스·공시"], ["fundamentals", "펀더멘털"], ["flow", "수급"], ["debate", "강세·약세 토론"],
    ["research_manager", "리서치 매니저"], ["trader", "트레이더"], ["risk_debate", "리스크 토론"], ["pm", "포트폴리오 매니저"]];

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
  function won(v) { return v == null ? "—" : Math.round(v).toLocaleString("ko-KR"); }
  function eok(v) { return v == null ? "—" : (v / 1e8).toFixed(2) + "억"; }
  function pctTxt(v, d) { return v == null ? "—" : (v > 0 ? "+" : "") + Number(v).toFixed(d == null ? 2 : d) + "%"; }
  function pct(v, d) { return '<span class="num ' + (v > 0 ? "up" : v < 0 ? "down" : "") + '">' + pctTxt(v, d) + "</span>"; }
  function pnlBadge(v, suffix) {
    var cls = v > 0 ? "profit" : v < 0 ? "loss" : "none";
    return '<span class="pnl-badge ' + cls + '">' + (v > 0 ? "▲ " : v < 0 ? "▼ " : "") + suffix + "</span>";
  }
  function sig(s) { return s ? '<span class="sig s-' + esc(s) + '">' + esc(s) + "</span>" : ""; }
  function stock(name, code, sub) {
    return '<div class="stock-info"><span class="stock-name">' + esc(name) + '</span><span class="stock-code">' +
      esc(code) + (sub ? " · " + sub : "") + "</span></div>";
  }
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
  function decBtn(key, label) {
    return key ? '<button class="link" data-key="' + esc(key) + '">' + (label || "판단 보기") + "</button>" : "";
  }
  function table(el, head, rows, empty) {
    var h = "<thead><tr>" + head.map(function (c) { return '<th class="' + (c[1] || "") + '">' + c[0] + "</th>"; }).join("") + "</tr></thead><tbody>";
    h += rows.length ? rows.map(function (r) { return "<tr>" + r.join("") + "</tr>"; }).join("")
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
  function renderPortfolio(s) {
    var t = s.trades;
    var items = [
      ["평가액", '<span class="num">' + eok(s.equity) + "</span>", "초기 " + eok(s.capital0), "wide"],
      ["누적 수익률", pct(s.retPct), "기준일 " + (s.asof || "—")],
      ["실현 손익", t.n ? '<span class="num ' + (t.pnl > 0 ? "up" : t.pnl < 0 ? "down" : "") + '">' + (t.pnl > 0 ? "+" : "") + won(t.pnl) + "</span>" : "—", "세후, 원"],
      ["현금", '<span class="num">' + eok(s.cash) + "</span>", s.equity ? "비중 " + (s.cash / s.equity * 100).toFixed(1) + "%" : ""],
      ["보유 · 대기 주문", '<span class="num">' + s.positions + " · " + s.openOrders + "</span>", "주문은 다음 영업일 1일 유효"],
      ["청산 거래", t.n ? '<span class="num">' + t.n + "건</span>" : "—", t.n ? "승률 " + t.winRate + "%" : ""],
      ["평균 수익률", t.n ? pct(t.avgRetPct) : "—", "거래당, 세후"]
    ];
    $("portfolio").innerHTML = items.map(function (x) {
      return '<div class="port-item ' + (x[3] || "") + '"><span class="port-lbl">' + x[0] + '</span><span class="port-val">' + x[1] +
        '</span><span class="port-sub">' + x[2] + "</span></div>";
    }).join("");
    if (t.n) $("trade-caption").textContent = "세후 손익(매도세 0.2%) · " + Object.keys(t.byReason).map(function (k) {
      return (REASON[k] || k) + " " + t.byReason[k];
    }).join(" · ");
  }
  function renderRules(c) {
    if (!c) return;
    var rows = [
      ["초기 자본", eok(c.initialCapital)],
      ["매수 후보", c.buySignals.join(" · ")],
      ["진입 조건", "PM " + c.buyRatings.join("/") + " + Trader Buy"],
      ["주문", "다음 영업일 지정가(1일)"],
      ["보유", c.holdDays + "영업일(체결일=1)"],
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
    for (var i = 1; i <= 3; i++) h += '<i class="' + (i <= d ? "on" : "") + '"></i>';
    return h + "<b>" + d + "/3</b></span>";
  }
  function renderPositions(ps) {
    setCount("cnt-positions", ps.length);
    table($("positions"), [["종목"], ["신호"], ["가격 레벨"], ["수량", "r"], ["종가", "r"], ["평가손익", "r"], ["보유일"], ["상태"], [""]],
      ps.map(function (p) {
        return [td(stock(p.name, p.code, "체결 " + (p.fillDate || "").slice(5))),
          td('<span class="sec-name">' + esc(p.sector || "") + "</span>" + sig(p.signal)),
          td(levels(p.entry, p.stop, p.target)), td('<span class="num">' + won(p.qty) + "</span>", "r"),
          td('<span class="num">' + won(p.lastClose) + "</span>", "r"),
          td(pnlBadge(p.unrealPct, pctTxt(p.unrealPct).replace(/^[+-]/, "")), "r"),
          td(holdBar(p.holdDay)),
          td(p.sellPending ? '<span class="state sell-next" title="' + esc(p.sellPending.reason) + '">다음 영업일 시가 매도</span>' : '<span class="state hold-ok">보유</span>'),
          td(decBtn(p.decisionKey, "진입 판단"))];
      }), "보유 종목이 없습니다");
  }

  // ── 판단·주문 (기준일) ──
  function renderRun(run, orders) {
    run = run || {};
    var reviews = run.reviews || [], cands = run.candidates || [], ex = orders.executed || [];
    setCount("cnt-decisions", cands.length + reviews.length);
    table($("reviews"), [["종목"], ["트리거"], ["PM"], ["결과"], [""]],
      reviews.map(function (r) {
        return [td(stock(r.name, r.code)), td(esc(r.trigger), "wrap"), td(rating(r.rating, r.error)),
          td(r.error ? '<span class="state skipped">판단 실패 — 보유 유지</span>' : r.sell ? '<span class="state sell-next">매도 예약</span>' : '<span class="state hold-ok">보유 유지</span>'),
          td(decBtn(r.decisionKey))];
      }), "매도 검토 대상 없음");
    table($("candidates"), [["종목"], ["신호"], ["PM"], ["Trader"], ["가격 레벨"], ["결과"], [""]],
      cands.map(function (c) {
        return [td(stock(c.name, c.code)), td('<span class="sec-name">' + esc(c.sector || "") + "</span>" + sig(c.signal)),
          td(rating(c.rating, /판단 실패/.test(c.why || ""))), td(esc(c.action || "—")),
          td(c.entry ? levels(c.entry, c.stop, c.target) : '<span class="muted">—</span>'),
          td(c.ordered ? '<span class="state ordered">주문 ' + won(c.qty) + "주</span>" : '<span class="muted" style="font-size:12px">' + esc(c.why || "미주문") + "</span>"),
          td(decBtn(c.decisionKey))];
      }), run.skip ? esc(run.skip) : "후보 없음");
    table($("executed"), [["종목"], ["결과"], ["진입가", "r"], ["수량", "r"], ["비고"]],
      ex.map(function (o) {
        return [td(stock(o.name, o.code)), td('<span class="state ' + o.status + '">' + (STATUS[o.status] || o.status) + "</span>"),
          td('<span class="num">' + won(o.entry) + "</span>", "r"), td('<span class="num">' + won(o.qty) + "</span>", "r"),
          td(esc(o.note || ""), "wrap")];
      }), "이 날 유효했던 주문 없음");
    renderLogs(run);
    return { cands: cands, reviews: reviews, ex: ex };
  }
  function renderLogs(run) {
    var L = [];
    if (!run || !run.date) { $("logs").innerHTML = '<div class="dim">실행 기록 없음</div>'; return; }
    if (run.skip) L.push('<div class="warn">[' + run.date + "] 건너뜀 — " + esc(run.skip) + "</div>");
    else {
      var nOrd = (run.candidates || []).filter(function (c) { return c.ordered; }).length;
      L.push("<div>[" + run.date + "] 청산 " + (run.exits || []).length + " · 검토 " + (run.reviews || []).length +
        " · 후보 " + (run.candidates || []).length + " · 주문 " + nOrd + "</div>");
    }
    (run.notes || []).forEach(function (n) { L.push('<div class="warn">⚠ ' + esc(n) + "</div>"); });
    (run.exits || []).forEach(function (e) {
      L.push("<div>· 청산 " + esc(e.name) + " " + (REASON[e.reason] || e.reason) + " " + pctTxt(e.retPct) + "</div>");
    });
    if ((run.capSkipped || []).length) L.push('<div class="dim">· 상한 초과로 미분석 ' + run.capSkipped.length + "종목</div>");
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
      return '<tr class="sf-row' + held + '"><td class="sf-name">' + esc(r.name) + "</td>" +
        WB_KEYS.map(function (k) { return fp(r[k]); }).join("") +
        amt(r.frgn) + amt(r.orgn) + (sfDet ? amt(r.finInv, 1) + amt(r.trust, 1) : "") + amt(r.fund, 1) + (sfDet ? amt(r.insur, 1) : "") +
        amt(r.prsn) + (sfEtc ? amt(r.etcCorp) : "") + '<td class="sf-s">' + badge + "</td></tr>";
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
      'px"><tr class="sf-head"><th>업종</th>' +
      WB_AXES.map(function (t) { return th(t); }).join("") + th("외인") + orgTh + (sfDet ? th("금융투자", 1) + th("투신(사모)", 1) : "") +
      th("연기금", 1) + (sfDet ? th("보험", 1) : "") + th("개인") + (sfEtc ? th("기타법인") : "") + th("신호") + "</tr>" + rows +
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

  // ── 흐름 요약 ──
  function renderFlow(sg, r, run) {
    var nOrd = r.cands.filter(function (c) { return c.ordered; }).length;
    var filled = r.ex.filter(function (o) { return o.status === "filled"; }).length;
    var steps = [
      ["Signal", sg.buy, "매수 후보 칸 · 하방 " + sg.down, "var(--sig-strong)"],
      ["Decision", r.cands.length + r.reviews.length, "신규 " + r.cands.length + " · 매도 검토 " + r.reviews.length, "var(--kospi-blue)"],
      ["Order", nOrd, "다음 영업일 지정가 · 이 날 체결 " + filled, "var(--kospi-blue)"],
      ["Holding", S.positions.length, "현재 보유(최신 기준)", "var(--kosdaq-green)"],
      ["Exit", (run && run.exits || []).length, "이 날 청산", "var(--amber)"]
    ];
    $("flow-strip").innerHTML = steps.map(function (s) {
      return '<div class="flow-step" style="--c:' + s[3] + '"><div class="fs-lbl">' + s[0] + '</div><div class="fs-val">' + s[1] +
        '</div><div class="fs-sub">' + s[2] + "</div></div>";
    }).join("");
  }

  // ── 청산 내역 ──
  function renderTrades(ts) {
    setCount("cnt-trades", ts.length);
    table($("trades"), [["청산일"], ["종목"], ["사유"], ["진입 → 청산", "r"], ["수량", "r"], ["보유", "r"], ["손익(세후)", "r"], ["수익률", "r"], [""]],
      ts.map(function (t) {
        return [td('<span class="num">' + t.exitDate + "</span>"), td(stock(t.name, t.code, "진입 " + (t.entryDate || "").slice(5))),
          td('<span class="reason ' + t.reason + '"' + (t.note ? ' title="' + esc(t.note) + '"' : "") + ">" + esc(REASON[t.reason] || t.reason) + "</span>"),
          td('<span class="num">' + won(t.entryPrice) + ' → ' + won(t.exitPrice) + "</span>", "r"),
          td('<span class="num">' + won(t.qty) + "</span>", "r"), td('<span class="num">' + t.holdDays + "일</span>", "r"),
          td('<span class="num ' + (t.pnl > 0 ? "up" : t.pnl < 0 ? "down" : "") + '">' + (t.pnl > 0 ? "+" : "") + won(t.pnl) + "</span>", "r"),
          td(pnlBadge(t.retPct, pctTxt(t.retPct).replace(/^[+-]/, "")), "r"),
          td(decBtn(t.exitDecisionKey || t.decisionKey, t.exitDecisionKey ? "매도 판단" : "진입 판단"))];
      }), "청산 내역이 없습니다");
  }

  // ── 판단 원문 ──
  function openDecision(key) {
    api("/api/swing/decision?key=" + encodeURIComponent(key)).then(function (d) {
      $("dec-title").textContent = d.code + " · " + d.date + " · " + (d.purpose === "review" ? "매도 검토" : "신규 진입");
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
      ROLE_ORDER.forEach(function (r) { known[r[0]] = 1; });
      var list = ROLE_ORDER.filter(function (r) { return reps[r[0]]; }).concat(
        Object.keys(reps).filter(function (k) { return !known[k]; }).map(function (k) { return [k, k]; }));
      if (list.length) h += '<div class="dec-sec"><h4>역할별 리포트</h4>' + list.map(function (r) {
        return "<details><summary>" + esc(r[1]) + '</summary><div class="report">' + esc(reps[r[0]]) + "</div></details>";
      }).join("") + "</div>";
      h += '<p class="muted" style="font-size:11px;margin-top:14px">판단 엔진 ' + esc(d.agent || "—") + "</p>";
      $("dec-body").innerHTML = h;
      $("decision").showModal();
    }).catch(function (e) { alert("판단 원문을 불러오지 못했습니다: " + e.message); });
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest("button.link[data-key]");
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
      var rr = renderRun(r[0], r[1]);
      var sg = renderSignals(r[2]);
      renderFlow(sg, rr, r[0]);
    });
  }

  // ── 부트 ──
  Promise.all([api("/api/swing/summary"), api("/api/swing/equity"), api("/api/swing/positions"),
    api("/api/swing/runs"), api("/api/swing/trades"), api("/api/swing/config")]).then(function (r) {
    S.summary = r[0]; S.equity = r[1]; S.positions = r[2]; S.trades = r[4]; S.config = r[5];
    S.positions.forEach(function (p) { S.held[p.code] = 1; });
    renderHeader(r[0]); renderPortfolio(r[0]); renderRules(r[5]);
    renderEquity(r[1], r[0].capital0); renderPositions(r[2]); renderTrades(r[4]);
    var dates = r[3].slice().reverse(), sel = $("run-date");
    sel.innerHTML = dates.length ? dates.map(function (d) { return "<option>" + d + "</option>"; }).join("") : "<option>—</option>";
    sel.addEventListener("change", function () { loadDate(sel.value); });
    if (dates.length) loadDate(dates[0]);
    else { renderRun(null, {}); renderFlow(renderSignals(null), { cands: [], reviews: [], ex: [] }, null); }
    var t;
    window.addEventListener("resize", function () { clearTimeout(t); t = setTimeout(function () { renderEquity(S.equity, S.summary.capital0); }, 150); });
  }).catch(function (e) {
    $("run-status").textContent = "API 연결 실패";
    $("run-dot").className = "status-dot warn";
    $("portfolio").innerHTML = '<div class="port-item wide"><span class="port-lbl">오류</span><span class="port-val">' + esc(e.message) + "</span></div>";
  });
})();
