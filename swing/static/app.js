/* 스윙 모의투자 대시보드 — swing/server.py API 만 읽는다(표시 전용). */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var REASON = { stop: "손절", stop_gap: "갭 손절(시가)", target: "목표가", expiry: "3일 만기",
    pm_sell: "PM 매도", cash: "현금 확보" };
  var STATUS = { filled: "체결", cancelled: "취소", skipped: "스킵", open: "대기" };

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
  function pct(v, d) {
    if (v == null) return "—";
    var s = (v > 0 ? "+" : "") + Number(v).toFixed(d == null ? 2 : d) + "%";
    return '<span class="' + (v > 0 ? "up" : v < 0 ? "down" : "") + '">' + s + "</span>";
  }
  function signed(v) {
    if (v == null) return "—";
    return '<span class="' + (v > 0 ? "up" : v < 0 ? "down" : "") + '">' + (v > 0 ? "+" : "") + won(v) + "</span>";
  }
  function stock(name, code) {
    return '<span class="name">' + esc(name) + '</span><span class="code">' + esc(code) + "</span>";
  }
  function decBtn(key, label) {
    return key ? '<button class="link" data-key="' + esc(key) + '">' + (label || "판단") + "</button>" : "";
  }
  function table(el, head, rows, empty) {
    var h = "<thead><tr>" + head.map(function (c) {
      return '<th class="' + (c[1] || "") + '">' + c[0] + "</th>";
    }).join("") + "</tr></thead><tbody>";
    if (!rows.length) h += '<tr class="empty"><td colspan="' + head.length + '">' + empty + "</td></tr>";
    else h += rows.map(function (r) { return "<tr>" + r.join("") + "</tr>"; }).join("");
    el.innerHTML = h + "</tbody>";
  }
  function td(v, cls) { return '<td class="' + (cls || "") + '">' + v + "</td>"; }

  // ── 요약 ──
  function renderSummary(s) {
    $("asof").textContent = s.asof ? "기준일 " + s.asof : "아직 실행 기록 없음";
    var t = s.trades;
    var tiles = [
      ["평가액", eok(s.equity), "초기 " + eok(s.capital0)],
      ["누적 수익률", pct(s.retPct), "실현손익 " + (t.n ? signed(t.pnl) + "원" : "—")],
      ["현금", eok(s.cash), "비중 " + (s.equity ? (s.cash / s.equity * 100).toFixed(1) + "%" : "—")],
      ["보유 · 대기 주문", s.positions + " · " + s.openOrders, "주문은 다음 영업일 1일 유효"],
      ["청산 거래", t.n ? t.n + "건" : "—", t.n ? "승률 " + t.winRate + "% · 평균 " + pct(t.avgRetPct) : ""]
    ];
    $("kpis").innerHTML = tiles.map(function (x) {
      return '<div class="kpi"><div class="lbl">' + x[0] + '</div><div class="val">' + x[1] +
        '</div><div class="sm">' + x[2] + "</div></div>";
    }).join("");
    var notes = (s.lastRun && s.lastRun.notes) || [];
    $("notes").innerHTML = notes.map(function (n) { return "<div>" + esc(n) + "</div>"; }).join("");
    if (t.n) {
      $("trade-caption").textContent = Object.keys(t.byReason).map(function (k) {
        return (REASON[k] || k) + " " + t.byReason[k];
      }).join(" · ");
    }
  }

  // ── 평가액 추이: 단일 계열 라인 + 초기자본 기준선 + 크로스헤어 툴팁 ──
  function renderEquity(rows, cap) {
    var box = $("equity-chart");
    if (!rows.length) { box.innerHTML = '<div class="empty-chart">평가 기록이 없습니다</div>'; return; }
    $("eq-caption").textContent = rows[0].date + " ~ " + rows[rows.length - 1].date + " · 점선 = 초기 자본";
    var W = box.clientWidth || 800, H = box.clientHeight || 260;
    var m = { l: 56, r: 16, t: 12, b: 26 };
    var vals = rows.map(function (r) { return r.equity; }).concat([cap]);
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    var pad = (hi - lo) * 0.15 || cap * 0.005; lo -= pad; hi += pad;
    var x = function (i) { return m.l + (rows.length === 1 ? (W - m.l - m.r) / 2 : i * (W - m.l - m.r) / (rows.length - 1)); };
    var y = function (v) { return m.t + (hi - v) / (hi - lo) * (H - m.t - m.b); };
    var ticks = [], n = 4;
    for (var k = 0; k <= n; k++) ticks.push(lo + (hi - lo) * k / n);
    var svg = '<svg viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none"><g class="axis">';
    ticks.forEach(function (v) {
      svg += '<line class="gridline" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + y(v) + '" y2="' + y(v) + '"/>' +
        '<text x="' + (m.l - 6) + '" y="' + (y(v) + 4) + '" text-anchor="end">' + (v / 1e8).toFixed(2) + "억</text>";
    });
    var step = Math.max(1, Math.ceil(rows.length / 6));
    rows.forEach(function (r, i) {
      if (i % step === 0 || i === rows.length - 1)
        svg += '<text x="' + x(i) + '" y="' + (H - 6) + '" text-anchor="middle">' + r.date.slice(5) + "</text>";
    });
    svg += "</g>";
    svg += '<line class="base" x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + y(cap) + '" y2="' + y(cap) + '"/>';
    var pts = rows.map(function (r, i) { return x(i) + "," + y(r.equity); });
    svg += '<path class="area" d="M' + pts.join("L") + "L" + x(rows.length - 1) + "," + (H - m.b) + "L" + x(0) + "," + (H - m.b) + 'Z"/>';
    svg += '<path class="ln" d="M' + pts.join("L") + '"/>';
    svg += '<line class="cross" id="eq-cross" y1="' + m.t + '" y2="' + (H - m.b) + '" visibility="hidden"/>';
    svg += '<circle class="dot" id="eq-dot" r="5" visibility="hidden"/>';
    svg += '<rect x="' + m.l + '" y="0" width="' + (W - m.l - m.r) + '" height="' + H + '" fill="transparent" id="eq-hit"/></svg>';
    box.innerHTML = svg + '<div class="tip" id="eq-tip" hidden></div>';
    var hit = $("eq-hit"), cross = $("eq-cross"), dot = $("eq-dot"), tip = $("eq-tip");
    function show(ev) {
      var rect = box.getBoundingClientRect();
      var px = (ev.clientX - rect.left) * W / rect.width;
      var i = rows.length === 1 ? 0 : Math.round((px - m.l) / ((W - m.l - m.r) / (rows.length - 1)));
      i = Math.max(0, Math.min(rows.length - 1, i));
      var r = rows[i];
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(r.equity)); dot.setAttribute("visibility", "visible");
      tip.hidden = false;
      tip.innerHTML = "<b>" + r.date + "</b>평가액 " + won(r.equity) + "원<br>수익률 " + pct(r.retPct) +
        "<br>현금 " + eok(r.cash) + " · 보유 " + r.positions + "종목";
      var left = x(i) * rect.width / W + 12;
      if (left + tip.offsetWidth > rect.width) left -= tip.offsetWidth + 24;
      tip.style.left = left + "px"; tip.style.top = "8px";
    }
    hit.addEventListener("mousemove", show);
    hit.addEventListener("mouseleave", function () {
      cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); tip.hidden = true;
    });
  }

  // ── 보유 ──
  function renderPositions(ps) {
    $("pos-caption").textContent = ps.length ? ps.length + "종목" : "";
    table($("positions"), [["종목", "l"], ["섹터·신호", "l"], ["수량"], ["진입가"], ["손절가"], ["목표가"],
      ["종가"], ["평가손익"], ["보유일"], ["상태", "l"], ["", "l"]],
    ps.map(function (p) {
      return [td(stock(p.name, p.code)),
        td(esc(p.sector || "") + ' <span class="chip">' + esc(p.signal || "") + "</span>", "l"),
        td(won(p.qty)), td(won(p.entry)), td(won(p.stop)), td(p.target ? won(p.target) : "—"),
        td(won(p.lastClose)), td(pct(p.unrealPct)), td(p.holdDay + " / 3"),
        td(p.sellPending ? '<span class="chip">다음 영업일 시가 매도</span>' : "보유", "l"),
        td(decBtn(p.decisionKey, "진입 판단"), "l")];
    }), "보유 종목이 없습니다");
  }

  // ── 일자별 판단·주문 ──
  function renderRun(run, orders) {
    run = run || {};
    table($("reviews"), [["종목", "l"], ["트리거", "l"], ["PM 등급", "l"], ["결과", "l"], ["", "l"]],
      (run.reviews || []).map(function (r) {
        return [td(stock(r.name, r.code)), td(esc(r.trigger), "wrap"), td(esc(r.rating), "l"),
          td(r.error ? "판단 실패 — 보유 유지" : r.sell ? "매도 예약" : "보유 유지", "l"), td(decBtn(r.decisionKey), "l")];
      }), "매도 검토 대상 없음");
    table($("candidates"), [["종목", "l"], ["섹터·신호", "l"], ["PM", "l"], ["Trader", "l"], ["진입가"],
      ["손절가"], ["목표가"], ["주문", "l"], ["", "l"]],
    (run.candidates || []).map(function (c) {
      return [td(stock(c.name, c.code)),
        td(esc(c.sector || "") + ' <span class="chip">' + esc(c.signal) + "</span>", "l"),
        td(esc(c.rating), "l"), td(esc(c.action || "—"), "l"), td(won(c.entry)), td(won(c.stop)),
        td(c.target ? won(c.target) : "—"),
        td(c.ordered ? "주문 " + won(c.qty) + "주" : '<span class="muted">' + esc(c.why || "미주문") + "</span>", "l"),
        td(decBtn(c.decisionKey), "l")];
    }), run.skip ? esc(run.skip) : "후보 없음");
    table($("executed"), [["종목", "l"], ["결과", "l"], ["진입가"], ["수량"], ["비고", "l"]],
      (orders.executed || []).map(function (o) {
        return [td(stock(o.name, o.code)), td(STATUS[o.status] || o.status, "l"), td(won(o.entry)),
          td(won(o.qty)), td(esc(o.note || ""), "wrap")];
      }), "이 날 유효했던 주문 없음");
  }

  function loadRun(date) {
    return Promise.all([api("/api/swing/runs?date=" + encodeURIComponent(date)).catch(function () { return null; }),
      api("/api/swing/orders?date=" + encodeURIComponent(date))]).then(function (r) { renderRun(r[0], r[1]); });
  }

  // ── 청산 내역 ──
  function renderTrades(ts) {
    table($("trades"), [["청산일", "l"], ["종목", "l"], ["사유", "l"], ["진입일", "l"], ["진입가"], ["청산가"],
      ["수량"], ["보유일"], ["손익(세후)"], ["수익률"], ["", "l"]],
    ts.map(function (t) {
      return [td(t.exitDate), td(stock(t.name, t.code), "l"),
        td(esc(REASON[t.reason] || t.reason) + (t.note ? ' <span class="muted">' + esc(t.note) + "</span>" : ""), "l"),
        td(t.entryDate, "l"), td(won(t.entryPrice)), td(won(t.exitPrice)), td(won(t.qty)), td(t.holdDays),
        td(signed(t.pnl)), td(pct(t.retPct)),
        td(decBtn(t.exitDecisionKey || t.decisionKey, t.exitDecisionKey ? "매도 판단" : "진입 판단"), "l")];
    }), "청산 내역이 없습니다");
  }

  // ── 판단 원문 ──
  var ROLE_ORDER = ["market", "sentiment", "news", "fundamentals", "flow", "debate", "research_manager",
    "trader", "risk_debate", "pm"];
  function openDecision(key) {
    api("/api/swing/decision?key=" + encodeURIComponent(key)).then(function (d) {
      $("dec-title").textContent = d.code + " · " + d.date + " · " + (d.purpose === "review" ? "매도 검토" : "신규 진입");
      var g = [["PM 등급", d.rating], ["Trader", d.action || "—"], ["진입가", won(d.entry)],
        ["손절가", won(d.stop)], ["목표가(PM)", d.target ? won(d.target) : "—"],
        ["비중", d.weight ? (d.weight * 100).toFixed(1) + "%" : "—"]];
      var h = '<div class="dec-grid">' + g.map(function (x) {
        return '<div><div class="lbl">' + x[0] + "</div>" + esc(x[1]) + "</div>";
      }).join("") + "</div>";
      if (d.error) h += '<p class="down">판단 실패: ' + esc(d.error) + "</p>";
      if (d.summary) h += "<h3>PM 요약</h3><div class=\"report\">" + esc(d.summary) + "</div>";
      var reps = d.reports || {};
      var keys = ROLE_ORDER.filter(function (k) { return reps[k]; }).concat(
        Object.keys(reps).filter(function (k) { return ROLE_ORDER.indexOf(k) < 0; }));
      keys.forEach(function (k) {
        h += "<details><summary>" + esc(k) + '</summary><div class="report">' + esc(reps[k]) + "</div></details>";
      });
      h += '<p class="muted">판단 엔진: ' + esc(d.agent || "—") + "</p>";
      $("dec-body").innerHTML = h;
      $("decision").showModal();
    }).catch(function (e) { alert("판단 원문을 불러오지 못했습니다: " + e.message); });
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest("button.link[data-key]");
    if (b) openDecision(b.getAttribute("data-key"));
  });

  // ── 부트 ──
  var cap = 0;
  Promise.all([api("/api/swing/summary"), api("/api/swing/equity"), api("/api/swing/positions"),
    api("/api/swing/runs"), api("/api/swing/trades")]).then(function (r) {
    cap = r[0].capital0;
    renderSummary(r[0]);
    renderEquity(r[1], cap);
    renderPositions(r[2]);
    renderTrades(r[4]);
    var dates = r[3].slice().reverse(), sel = $("run-date");
    sel.innerHTML = dates.map(function (d) { return "<option>" + d + "</option>"; }).join("");
    sel.addEventListener("change", function () { loadRun(sel.value); });
    if (dates.length) loadRun(dates[0]); else renderRun(null, {});
    var t;
    window.addEventListener("resize", function () {
      clearTimeout(t); t = setTimeout(function () { renderEquity(r[1], cap); }, 150);
    });
  }).catch(function (e) {
    $("kpis").innerHTML = '<div class="kpi"><div class="lbl">오류</div><div class="val">API 연결 실패</div><div class="sm">' +
      esc(e.message) + "</div></div>";
  });
})();
