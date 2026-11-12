/* 판단 팝업 '당일 등락 요인 분해' 3D 카드(2026-11-12, swing/PLAN_3D.md) — 시안 swing/design/pf3d_preview.html 과 같은 그림.
   종목(가운데, 당일 등락률) → 리포트 항목 구(애널리스트 5역할, 서로 다른 궤도) ⊃ 요인 공(크기 = |%p|, 빨강 상승·파랑 하락).
   데이터 = /api/swing/decision 의 priceFactors(같은 회차에서 요인이 있는 가장 최근 판단) + priceToday(오늘 등락 — 있으면 그 값으로 다시 나눔).
   three.js r128 은 카드를 처음 그릴 때만 cdnjs 에서 불러온다. 못 불러오거나 WebGL 이 없으면 카드만 숨긴다(팝업은 그대로).
   window.PF3D = { html(d), mount(d, name, onPick), unmount() } — app.js openDecision 이 쓴다. */
(function () {
  "use strict";
  var THREE_URL = "https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js";
  var CATS = [["market", "기술적 분석"], ["sentiment", "심리"], ["news", "뉴스·공시"], ["fundamentals", "펀더멘털"], ["flow", "수급"], ["etc", "기타·시장"]];
  function esc(s) { return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }
  function sgn(v) { return v > 1e-9 ? "up" : v < -1e-9 ? "down" : "neu"; }
  function won(v) { return v == null ? "—" : Math.round(v).toLocaleString(); }
  function pctTxt(v) { return (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(2) + "%"; }
  function fp(v, cat) { return (v > 1e-9 ? "+" : v < -1e-9 ? "−" : "±") + Math.abs(v).toFixed(1) + (cat ? "%p" : ""); }
  function mdd(d) { return d ? String(d).slice(5).replace("-", "/") : ""; }

  function Orb3D(stage, opts) {
    var canvas = stage.querySelector("canvas"), labelBox = stage.querySelector(".o3-labels"), infoEl = stage.querySelector(".o3-info");
    var T = window.THREE, RED = 0xef4444, BLUE = 0x3b82f6, GRAY = 0x94a3b8;
    var scene = new T.Scene(), camera = new T.PerspectiveCamera(40, 1, 0.1, 200);
    var renderer = new T.WebGLRenderer({ canvas: canvas, antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    scene.add(new T.AmbientLight(0xffffff, 0.6));
    var key = new T.DirectionalLight(0xffffff, 0.95); key.position.set(5, 9, 12); scene.add(key);
    var rim = new T.PointLight(0x38bdf8, 0.45); rim.position.set(-8, -4, -6); scene.add(rim);
    var world = new T.Group(); scene.add(world);
    var gc = document.createElement("canvas"); gc.width = gc.height = 128; var g2 = gc.getContext("2d"), gr = g2.createRadialGradient(64, 64, 0, 64, 64, 64);
    gr.addColorStop(0, "rgba(255,255,255,.9)"); gr.addColorStop(0.35, "rgba(255,255,255,.28)"); gr.addColorStop(1, "rgba(255,255,255,0)");
    g2.fillStyle = gr; g2.fillRect(0, 0, 128, 128); var glowTex = new T.CanvasTexture(gc);
    var ray = new T.Raycaster(), ptr = new T.Vector2(), v = new T.Vector3(), dv = new T.Vector3();
    var nodes = [], links = [], M = null, MAXV = 1;
    var HOME = { z: 17, rx: 0.3, ry: -0.18 }, zoom = 17, spinTo = null, panTo = 0, panX = 0, narrow = false;
    var catFocus = null, selected = null, hovered = null, drag = null, moved = 0, idleAt = 0, t0 = 0, last = 0;
    var alive = true;
    var auto = !(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    var ORBITS = [[4.2, 0.10, 0.06], [5.0, -0.34, 0.16], [3.9, 0.38, -0.22], [5.3, 0.22, 0.32], [4.6, -0.18, -0.36], [5.6, -0.46, 0.05], [4.9, 0.30, 0.20]];
    function colorOf(x) { return x > 1e-9 ? RED : x < -1e-9 ? BLUE : GRAY; }
    function mkLabel(html, cls) { var el = document.createElement("div"); el.className = "o3-lbl " + cls; el.innerHTML = html; labelBox.appendChild(el); return el; }
    function glowOf(pos, r, color, op) {
      var gl = new T.Sprite(new T.SpriteMaterial({ map: glowTex, color: color, blending: T.AdditiveBlending, transparent: true, opacity: op, depthWrite: false }));
      gl.scale.setScalar(r); gl.position.copy(pos); world.add(gl); return gl;
    }
    function ballNode(kind, pos, r, color, html, cls, extra) {
      var m = new T.Mesh(new T.SphereGeometry(r, 32, 22), new T.MeshStandardMaterial({ color: color, emissive: color, emissiveIntensity: kind === "root" ? 0.5 : 0.38, roughness: 0.3, metalness: 0.15, transparent: true }));
      m.position.copy(pos); world.add(m);
      var n = { kind: kind, mesh: m, glow: kind === "root" ? glowOf(pos, r * 4.6, color, 0.5) : null, glowBase: 0.5, label: mkLabel(html, cls), pos: pos.clone(), r: r, op: 1, base: 1 };
      for (var k in (extra || {})) n[k] = extra[k];
      m.userData.node = n; nodes.push(n); return n;
    }
    function shellNode(pos, R, color, html, extra) {
      var m = new T.Mesh(new T.SphereGeometry(R, 44, 30), new T.MeshStandardMaterial({ color: color, emissive: color, emissiveIntensity: 0.3, roughness: 0.12, metalness: 0.25, transparent: true, opacity: 0.13, depthWrite: false }));
      m.position.copy(pos); m.renderOrder = 2; world.add(m);
      var back = new T.Mesh(new T.SphereGeometry(R * 1.015, 44, 30), new T.MeshBasicMaterial({ color: color, transparent: true, opacity: 0.2, side: T.BackSide, depthWrite: false }));
      back.position.copy(pos); world.add(back);
      var n = { kind: "cat", mesh: m, back: back, glow: glowOf(pos, R * 3.3, color, 0.3), glowBase: 0.3, label: mkLabel(html, "cat"), pos: pos.clone(), r: R, op: 1, base: 0.13 };
      for (var k in (extra || {})) n[k] = extra[k];
      m.userData.node = n; nodes.push(n); return n;
    }
    function orbitPos(o) { return new T.Vector3(Math.cos(o.a) * o.R, 0, Math.sin(o.a) * o.R).applyQuaternion(o.q); }
    function pack(rs) {
      var n = rs.length, ps = [], i, j, it, d = new T.Vector3();
      for (i = 0; i < n; i++) {
        var t = (i + 0.5) / n, ph = Math.acos(1 - 2 * t), th = Math.PI * (1 + Math.sqrt(5)) * i;
        ps.push(new T.Vector3(Math.sin(ph) * Math.cos(th), Math.cos(ph), Math.sin(ph) * Math.sin(th)).multiplyScalar(n === 1 ? 0 : (rs[0] + rs[i]) * 1.2));
      }
      for (it = 0; it < 400; it++) {
        for (i = 0; i < n; i++) ps[i].multiplyScalar(0.975);
        for (i = 0; i < n; i++) for (j = i + 1; j < n; j++) {
          d.subVectors(ps[j], ps[i]); var dist = d.length(), min = rs[i] + rs[j] + 0.05;
          if (dist < min) { if (dist < 1e-6) d.set(1, 0, 0); else d.multiplyScalar(1 / dist); var push = (min - dist) / 2; ps[i].addScaledVector(d, -push); ps[j].addScaledVector(d, push); }
        }
      }
      var c = new T.Vector3(), ws = 0; ps.forEach(function (p, k) { c.addScaledVector(p, rs[k]); ws += rs[k]; }); c.multiplyScalar(1 / (ws || 1));
      var R = 0; ps.forEach(function (p, k) { p.sub(c); R = Math.max(R, p.length() + rs[k]); });
      return { ps: ps, R: R + 0.14 };
    }
    function clear() {
      while (world.children.length) { var o = world.children[0]; world.remove(o); if (o.geometry) o.geometry.dispose(); if (o.material) o.material.dispose(); }
      labelBox.innerHTML = ""; nodes = []; links = []; catFocus = null; selected = null; hovered = null; drag = null; spinTo = null; panTo = 0; panX = 0;
    }
    function load(model) {
      clear(); M = model;
      var G = model.groups;
      MAXV = 1e-9; G.forEach(function (g) { g.items.forEach(function (it) { MAXV = Math.max(MAXV, Math.abs(it.v)); }); });
      ballNode("root", new T.Vector3(), 0.9, 0x06b6d4, esc(model.root.name) + model.root.valueHtml, "root");
      G.forEach(function (g, gi) {
        var rs = g.items.map(function (it) { return 0.17 + 0.43 * Math.sqrt(Math.abs(it.v) / MAXV); }), pk = pack(rs);
        var ob = ORBITS[gi % ORBITS.length];
        var orbit = { R: ob[0], q: new T.Quaternion().setFromEuler(new T.Euler(ob[1], 0, ob[2])), a: -Math.PI / 2 + gi / G.length * Math.PI * 2 };
        var cn = shellNode(orbitPos(orbit), pk.R, colorOf(g.net), esc(g.name) + '<span class="pc ' + sgn(g.net) + '">' + model.fmt(g.net, true) + "</span>", { cat: g.id, orbit: orbit, group: g });
        var pts = []; for (var a = 0; a <= 96; a++) pts.push(new T.Vector3(Math.cos(a / 96 * Math.PI * 2) * orbit.R, 0, Math.sin(a / 96 * Math.PI * 2) * orbit.R).applyQuaternion(orbit.q));
        cn.ring = new T.Line(new T.BufferGeometry().setFromPoints(pts), new T.LineBasicMaterial({ color: colorOf(g.net), transparent: true, opacity: 0.3 })); world.add(cn.ring);
        var ln = new T.Line(new T.BufferGeometry().setFromPoints([new T.Vector3(), new T.Vector3()]), new T.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.12 })); world.add(ln);
        links.push({ line: ln, owner: cn });
        g.items.forEach(function (it, k) {
          ballNode("leaf", cn.pos.clone().add(pk.ps[k]), rs[k], colorOf(it.v), '<span class="pc">' + model.fmt(it.v) + '</span><span class="tt">' + esc(it.title) + "</span>", "leaf", { cat: g.id, item: it, host: cn, local: pk.ps[k].clone() });
        });
      });
      home(); camera.position.set(0, 0, zoom); applyVisual(); renderInfo(); t0 = last = performance.now();
    }
    function applyVisual() {
      nodes.forEach(function (n) {
        var on = true;
        if (n.kind === "leaf") { on = !catFocus || n.cat === catFocus; if (selected) on = n === selected; }
        else if (n.kind === "cat") on = !catFocus || n.cat === catFocus;
        n.dim = !on; n.op = on ? 1 : (n.kind === "cat" ? 0.3 : 0.1);
        n.label.classList.toggle("sel", n === selected);
      });
    }
    function renderInfo() {
      if (selected) { infoEl.innerHTML = M.info(selected.item, selected.host.group); infoEl.style.display = ""; }
      else if (catFocus) { var cn = catNode(catFocus); infoEl.innerHTML = M.groupInfo(cn.group); infoEl.style.display = ""; }
      else infoEl.style.display = "none";
    }
    function catNode(id) { for (var i = 0; i < nodes.length; i++) if (nodes[i].kind === "cat" && nodes[i].cat === id) return nodes[i]; return null; }
    function faceNode(cn) {
      var want = Math.atan2(cn.pos.z, cn.pos.x) - Math.PI / 2 + 0.45, cur = world.rotation.y;
      spinTo = cur + Math.atan2(Math.sin(want - cur), Math.cos(want - cur));
      var q = cn.pos.clone().applyEuler(new T.Euler(world.rotation.x, spinTo, 0, "XYZ"));
      panTo = -q.y; panX = -q.x + (narrow ? 0 : 0.9); zoom = q.z + Math.max(6.2, cn.r * 6.4) * (narrow ? 1.7 : 1);
    }
    function focusCat(id) { catFocus = id; selected = null; applyVisual(); renderInfo(); var cn = catNode(id); if (cn) faceNode(cn); if (opts.onPick) opts.onPick(id, null); }
    function select(n) { selected = n; catFocus = n.cat; faceNode(n.host); applyVisual(); renderInfo(); if (opts.onPick) opts.onPick(n.cat, n.item); }
    function home() { spinTo = null; panTo = 0; panX = 0; world.position.set(0, 0, 0); world.rotation.set(HOME.rx, HOME.ry, 0); zoom = HOME.z + (narrow ? 8 : 0); }
    function reset() { catFocus = null; selected = null; applyVisual(); renderInfo(); home(); }
    function pick(e) {
      var r = canvas.getBoundingClientRect();
      ptr.set((e.clientX - r.left) / r.width * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1); ray.setFromCamera(ptr, camera);
      var hl = ray.intersectObjects(nodes.filter(function (n) { return n.kind === "leaf" && !n.dim; }).map(function (n) { return n.mesh; }));
      var leaf = hl.length ? hl[0].object.userData.node : null;
      if (leaf && catFocus === leaf.cat) return leaf;
      if (leaf) return leaf.host;
      var hs = ray.intersectObjects(nodes.filter(function (n) { return n.kind === "cat"; }).map(function (n) { return n.mesh; }));
      return hs.length ? hs[0].object.userData.node : null;
    }
    var btnRot = stage.querySelector('[data-a="rot"]');
    function setAuto(x) { auto = x; btnRot.textContent = x ? "⏸ 공전 멈춤" : "▶ 공전"; }
    setAuto(auto);
    btnRot.onclick = function (e) { e.stopPropagation(); setAuto(!auto); };
    stage.querySelector('[data-a="reset"]').onclick = function (e) { e.stopPropagation(); reset(); };
    stage.addEventListener("pointerleave", function () { hovered = null; stage.style.cursor = ""; });
    stage.addEventListener("pointerdown", function (e) {
      if (e.target.closest(".o3-tools") || e.target.closest(".o3-info")) return;
      drag = { x: e.clientX, y: e.clientY }; moved = 0; spinTo = null; stage.classList.add("drag"); try { stage.setPointerCapture(e.pointerId); } catch (err) {}
    });
    stage.addEventListener("pointermove", function (e) {
      if (!drag) { var hn = (e.target.closest(".o3-tools") || e.target.closest(".o3-info")) ? null : pick(e); if (hn !== hovered) { hovered = hn; stage.style.cursor = hn ? "pointer" : ""; } return; }
      var dx = e.clientX - drag.x, dy = e.clientY - drag.y; moved += Math.abs(dx) + Math.abs(dy);
      world.rotation.y += dx * 0.008; world.rotation.x = Math.max(-1.1, Math.min(1.1, world.rotation.x + dy * 0.006));
      drag = { x: e.clientX, y: e.clientY }; idleAt = performance.now() + 2500;
    });
    stage.addEventListener("pointerup", function (e) {
      if (!drag) return; drag = null; stage.classList.remove("drag");
      if (moved < 6) {
        var n = pick(e);
        if (!n) reset();
        else if (n.kind === "cat") { if (catFocus === n.cat && selected) { selected = null; applyVisual(); renderInfo(); } else focusCat(n.cat); }
        else select(n);
        idleAt = performance.now() + 4000;
      }
    });
    stage.addEventListener("wheel", function (e) { e.preventDefault(); zoom = Math.max(7, Math.min(40, zoom + e.deltaY * 0.014)); idleAt = performance.now() + 2500; }, { passive: false });
    function resize() {
      var w = stage.clientWidth, h = stage.clientHeight; if (!w || !h) return;
      renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix();
      var nw = w / h < 1.2; if (nw !== narrow) { narrow = nw; if (!catFocus) zoom = HOME.z + (narrow ? 8 : 0); }
      nodes.forEach(function (n) { n.lw = 0; });
    }
    window.addEventListener("resize", resize);
    function frame(now) {
      if (!alive) return;
      requestAnimationFrame(frame);
      var w = stage.clientWidth, h = stage.clientHeight, dt = Math.min(0.05, (now - last) / 1000); last = now;
      if (!w || !h || !M) return;
      if (w !== frame.w || h !== frame.h) { frame.w = w; frame.h = h; resize(); }
      var k = Math.min(1, (now - t0) / 1000), ease = 1 - Math.pow(1 - k, 3);
      world.scale.setScalar(0.25 + 0.75 * ease);
      if (spinTo != null) { world.rotation.y += (spinTo - world.rotation.y) * 0.1; if (Math.abs(spinTo - world.rotation.y) < 0.002) spinTo = null; }
      var orbiting = auto && !drag && now > idleAt && !catFocus && !selected && !hovered;
      nodes.forEach(function (n) { if (!n.orbit) return; if (orbiting) n.orbit.a += 0.08 * dt; n.pos.copy(orbitPos(n.orbit)); n.mesh.position.copy(n.pos); n.back.position.copy(n.pos); n.glow.position.copy(n.pos); });
      nodes.forEach(function (n) { if (n.host) { n.pos.copy(n.host.pos).add(n.local); n.mesh.position.copy(n.pos); } });
      if (!catFocus && !selected && (panX || panTo)) { panX = 0; panTo = 0; zoom = HOME.z + (narrow ? 8 : 0); }
      camera.position.z += (zoom - camera.position.z) * 0.12; world.position.y += (panTo - world.position.y) * 0.1; world.position.x += (panX - world.position.x) * 0.1;
      nodes.forEach(function (n) {
        n.cur = n.cur == null ? 1 : n.cur + (n.op - n.cur) * 0.2;
        var hot = n === hovered || (n.kind === "cat" && n.cat === catFocus);
        n.mesh.material.opacity = n.base * n.cur * (n.kind === "cat" && hot ? 1.5 : 1);
        if (n.back) n.back.material.opacity = 0.2 * n.cur * (hot ? 1.5 : 1);
        if (n.glow) n.glow.material.opacity = n.glowBase * n.cur;
      });
      var rootR = nodes[0] ? nodes[0].r : 0.9;
      links.forEach(function (t) {
        var o = t.owner, g = t.line.geometry.attributes.position; dv.copy(o.pos).normalize();
        g.setXYZ(0, dv.x * rootR, dv.y * rootR, dv.z * rootR); g.setXYZ(1, o.pos.x - dv.x * o.r, o.pos.y - dv.y * o.r, o.pos.z - dv.z * o.r); g.needsUpdate = true;
        t.line.material.opacity = 0.12 * o.cur; o.ring.material.opacity = (catFocus === o.cat ? 0.6 : 0.3) * Math.max(o.cur, 0.5);
      });
      world.updateMatrixWorld();
      var pxPerUnit = h / (2 * Math.tan(camera.fov * Math.PI / 360));
      nodes.forEach(function (n) {
        n.mesh.getWorldPosition(v); var depth = v.z, dist = Math.max(1, camera.position.z - depth); v.project(camera);
        if (!n.lw) { n.label.classList.remove("compact"); n.lw = n.label.offsetWidth; n.lh = n.label.offsetHeight; if (n.kind === "leaf") { n.label.classList.add("compact"); n.lwc = n.label.offsetWidth; n.lhc = n.label.offsetHeight; } }
        n.sx = (v.x * 0.5 + 0.5) * w; n.cy = (-v.y * 0.5 + 0.5) * h; n.pr = n.r * world.scale.x * pxPerUnit / dist; n.near = Math.max(0, Math.min(1, (depth + 9) / 18));
        n.prio = n.kind === "root" ? 1000 : n === selected ? 950 : (n.kind === "leaf" && n === hovered) ? 900 : n.dim ? -1 :
                 n.kind === "cat" ? 500 + n.near * 10 + Math.abs(n.group.net) / (MAXV || 1) : Math.abs(n.item.v) / MAXV * 150 + n.near * 25 + (catFocus === n.cat ? 200 : 0);
      });
      var placed = [];
      function fits(b) { return !placed.some(function (p) { return b[0] < p[1] && b[1] > p[0] && b[2] < p[3] && b[3] > p[2]; }); }
      function bx(x, y, lw, lh) { return [x - lw / 2 - 2, x + lw / 2 + 2, y - lh / 2 - 1, y + lh / 2 + 1]; }
      nodes.slice().sort(function (a, b) { return b.prio - a.prio; }).forEach(function (n) {
        var el = n.label, b, y, compact = false;
        if (n.prio < 0) { el.style.opacity = 0; return; }
        if (n.kind !== "leaf") {
          y = n.kind === "root" ? n.cy + n.pr + n.lh / 2 + 3 : n.cy - n.pr - n.lh / 2 - 3;
          b = bx(n.sx, y, n.lw, n.lh);
          if (!fits(b) && n.kind === "cat") { y = Math.min(n.cy + n.pr + n.lh / 2 + 3, h - n.lh / 2 - 24); b = bx(n.sx, y, n.lw, n.lh); }   // 위가 막히면 구 아래에(화면 밖으로는 안 나가게)
          if (!fits(b) && n.kind !== "root") { el.style.opacity = 0; return; }
        } else {
          var full = (catFocus === n.cat && n.pr >= 22) || n.prio >= 900;
          y = n.cy; b = bx(n.sx, y, n.lw, n.lh);
          if (!full || (!fits(b) && n.prio < 900)) { compact = true; b = bx(n.sx, y, n.lwc, n.lhc); if (n.pr < 9 || !fits(b)) { el.style.opacity = 0; return; } }
        }
        placed.push(b); el.classList.toggle("compact", compact);
        el.style.transform = "translate(-50%, -50%) translate(" + n.sx.toFixed(1) + "px," + y.toFixed(1) + "px)";
        el.style.opacity = ease; el.style.zIndex = n.prio >= 900 ? 4 : n.kind === "leaf" ? (compact ? 1 : 3) : 2;
      });
      renderer.render(scene, camera);
    }
    requestAnimationFrame(frame);
    function destroy() {
      alive = false; window.removeEventListener("resize", resize); clear();
      try { glowTex.dispose(); renderer.dispose(); renderer.forceContextLoss(); } catch (e) { /* 무시 */ }
    }
    return { load: load, reset: reset, focus: focusCat, destroy: destroy, state: function () { return { nodes: nodes.length, catFocus: catFocus, selected: !!selected }; } };
  }

  function attribute(items, rate) {
    var r = +rate, U = 0, D = 0, r1 = function (x) { return Math.round(x * 10) / 10; };
    items.forEach(function (x) { if (x.dir > 0) U += x.st; else if (x.dir < 0) D += x.st; });
    var gross;
    if ((r > 0 && U === 0) || (r < 0 && D === 0)) gross = Math.abs(r) * 2;
    else { gross = (U || D) ? Math.abs(r) * Math.min((U + D) / Math.max(Math.abs(U - D), 1e-9), 3) : Math.abs(r); if (U && D) gross = Math.max(gross, 1); }
    gross = Math.max(gross, Math.abs(r));
    var u = (gross + r) / 2, d = (gross - r) / 2, resid = 0;
    if (U === 0) { resid += u; u = 0; }
    if (D === 0) { resid -= d; d = 0; }
    var out = items.map(function (x) { return { cat: x.cat, title: x.title, dir: x.dir, st: x.st, ev: x.ev, pp: r1(x.dir > 0 ? u * x.st / U : x.dir < 0 ? -d * x.st / D : 0) }; });
    if (Math.abs(resid) >= 0.05) out.push({ cat: "etc", title: "요인으로 설명되지 않은 변동", dir: resid > 0 ? 1 : -1, st: 0, ev: "제시된 요인의 방향만으로는 등락률을 설명할 수 없어 남은 부분(시장·업종 흐름 등).", pp: r1(resid) });
    var diff = r1(r1(r) - out.reduce(function (s, x) { return s + x.pp; }, 0));
    if (out.length && Math.abs(diff) >= 0.05) { var big = out.reduce(function (a, b) { return Math.abs(b.pp) > Math.abs(a.pp) ? b : a; }); big.pp = r1(big.pp + diff); }
    return out;
  }

  // ── 모델: 저장된 요인(강도·방향)을 기준 등락률에 맞춰 다시 나눈다(swing/agent/price_factors.attribute 와 같은 식) ──
  function current(d) {
    var pf = d.priceFactors, t = d.priceToday, b = pf.basis || {};
    if (t && typeof t.ratePct === "number" && isFinite(t.ratePct)) return { rate: t.ratePct, date: t.date, ts: t.ts, price: t.price, prev: t.prevClose, kind: t.kind };
    return { rate: b.ratePct, date: b.date, ts: null, price: b.close, prev: b.prevClose, kind: "stored" };
  }
  function model(d, name) {
    var pf = d.priceFactors, cur = current(d);
    var base = (pf.items || []).filter(function (x) { return x.cat !== "etc"; }).map(function (x) { return { cat: x.cat, title: x.title, dir: x.dir, st: x.strength, ev: x.evidence }; });
    var rows = attribute(base, cur.rate);
    var groups = CATS.map(function (c) {
      var items = rows.filter(function (x) { return x.cat === c[0]; }).map(function (x) { return { title: x.title, v: x.pp, dir: x.dir, st: x.st, ev: x.ev }; });
      var net = 0; items.forEach(function (it) { net += it.v; });
      return { id: c[0], name: c[1], net: Math.round(net * 10) / 10, items: items };
    }).filter(function (g) { return g.items.length; });
    return { root: { name: name || d.code, valueHtml: '<span class="pc ' + sgn(cur.rate) + '">' + fp(cur.rate) + "%</span>" }, groups: groups, fmt: fp, n: rows.length,
      info: function (it, g) { return '<div class="k">' + esc(g.name) + " › " + (it.dir > 0 ? "상승 요인" : it.dir < 0 ? "하락 요인" : "중립") + (it.st ? " · 강도 " + it.st : "") + '</div><div class="t">' + esc(it.title) + ' <b class="' + sgn(it.v) + '">' + fp(it.v, true) + '</b></div><div class="e">' + esc(it.ev) + "</div>"; },
      groupInfo: function (g) { return '<div class="k">리포트 항목</div><div class="t">' + esc(g.name) + ' <b class="' + sgn(g.net) + '">' + fp(g.net, true) + '</b></div><div class="e">요인 ' + g.items.length + "개" + (g.id === "etc" ? "" : " — 아래 종합 리포트의 이 항목이 함께 열립니다.") + "</div>"; } };
  }

  // ── 카드 HTML(openDecision 이 맨 윗줄 오른쪽에 넣는다). 요인이 없으면 "" ──
  function html(d) {
    if (!d || !d.priceFactors || !(d.priceFactors.items || []).length) return "";
    var pf = d.priceFactors, cur = current(d);
    if (typeof cur.rate !== "number" || !isFinite(cur.rate)) return "";
    var when = cur.kind === "live" ? mdd(cur.date) + (cur.ts ? " " + esc(cur.ts) : "") + " 기준" : cur.kind === "close" ? mdd(cur.date) + " 종가 기준" : mdd(cur.date) + " 판단일 종가 기준";
    var move = cur.price && cur.prev ? " · " + won(cur.prev) + " → " + won(cur.price) : "";
    var from = (pf.fromPurpose === "review" ? mdd(pf.fromDate) + " 재판별" : mdd(pf.fromDate) + " 진입 판단") + " 요인";
    return '<div class="dec-sec" id="dec-pf3d"><div class="pf3d-card"><div class="pf3d-top"><strong>당일 등락 요인 분해</strong>' +
      '<span class="basis">' + when + ' <b class="' + sgn(cur.rate) + '">' + pctTxt(cur.rate) + "</b>" + move + "</span>" +
      '<span class="pf3d-est" title="' + esc(pf.method || "") + '">추정</span><em id="pf3d-fold">접기 ▴</em></div>' +
      '<div class="o3-stage" id="pf3d-stage"><canvas></canvas><div class="o3-labels"></div>' +
      '<div class="o3-tools"><button type="button" class="o3-tool" data-a="rot">⏸ 공전 멈춤</button><button type="button" class="o3-tool" data-a="reset">⟲ 처음 시점</button></div>' +
      '<div class="o3-info" style="display:none"></div></div>' +
      '<div class="pf3d-foot" id="pf3d-foot">' + esc(from) + " · 리포트의 요인 강도(1~5)를 등락률에 맞춰 %p 로 나눈 추정치 — 빨강 = 주가를 올린 요인, 파랑 = 내린 요인" +
      (d.side === "short" ? "(Short 포지션에는 반대로 작용)" : "") + "</div></div></div>";
  }

  var loading = null, inst = null, token = 0;
  function loadThree() {
    if (window.THREE) return Promise.resolve(true);
    if (!loading) loading = new Promise(function (ok) {
      var s = document.createElement("script"); s.src = THREE_URL; s.async = true;
      s.onload = function () { ok(!!window.THREE); }; s.onerror = function () { loading = null; ok(false); };
      document.head.appendChild(s);
    });
    return loading;
  }
  function webgl() { try { var c = document.createElement("canvas"); return !!(c.getContext("webgl") || c.getContext("experimental-webgl")); } catch (e) { return false; } }
  function unmount() { token++; if (inst) { try { inst.destroy(); } catch (e) { /* 무시 */ } inst = null; } }
  function hideCard() {
    var sec = document.getElementById("dec-pf3d"), top = sec && sec.closest(".dec-top");
    if (sec) sec.remove();
    if (top) top.classList.add("solo");                      // 왼쪽 카드만 남으면 전체 폭으로
  }
  // d = 판단 문서(priceFactors 포함), name = 가운데 이름, onPick(cat) = 항목·요인을 눌렀을 때(종합 리포트 연동)
  function mount(d, name, onPick) {
    unmount();
    if (!document.getElementById("dec-pf3d")) return;
    var my = token;
    loadThree().then(function (ok) {
      if (my !== token || !document.getElementById("pf3d-stage")) return;   // 그사이 다른 판단을 열었거나 닫았으면 버림
      if (!ok || !webgl()) { hideCard(); return; }
      try {
        inst = Orb3D(document.getElementById("pf3d-stage"), { onPick: function (cat) { if (onPick && cat !== "etc") onPick(cat); } });
        inst.load(model(d, name));
      } catch (e) { console.warn("[PF3D]", e); hideCard(); return; }
      var fold = document.getElementById("pf3d-fold");
      if (fold) fold.onclick = function () {
        var st = document.getElementById("pf3d-stage"), hid = !st.hidden; st.hidden = hid;
        document.getElementById("pf3d-foot").hidden = hid; fold.textContent = hid ? "펼치기 ▾" : "접기 ▴";
      };
    });
  }
  window.PF3D = { html: html, mount: mount, unmount: unmount, _attribute: attribute, _model: model, _state: function () { return inst && inst.state(); }, _focus: function (id) { if (inst) inst.focus(id); } };
})();
