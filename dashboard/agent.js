/* GrayGoo Agent view: prompt the agent, watch it build Lisp tools in a REPL,
   and see measured evidence that reuse cuts tokens without changing answers.
   Vanilla JS + inline SVG. Model/REPL text only ever goes through textContent. */
(function () {
  "use strict";

  var NS = "http://www.w3.org/2000/svg";
  var $ = function (id) { return document.getElementById(id); };
  if (!$("ag-graph")) return;

  var state = {
    tools: [], sel: null, ghost: null, hot: null,
    session: null, next: 0, timer: null,
    history: [], mode: "demo",
    snapshots: [], view: "session", liveOk: false, busy: false, picked: false, rows: [], pick: null
  };
  function curHistory() {
    if (state.view !== "session") {
      var sn = state.snapshots.filter(function (x) { return x.id === state.view; })[0];
      if (sn) return snapRows(sn);
    }
    return state.history;
  }

  function el(tag, attrs, text) {
    var n = document.createElementNS(NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, attrs[k]); });
    if (text != null) n.textContent = text;
    return n;
  }
  function h(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
  function api(method, path, body) {
    return fetch(path, {
      method: method,
      headers: { "Content-Type": "application/json" },
      body: method === "POST" ? JSON.stringify(body || {}) : undefined
    }).then(function (r) {
      return r.json().then(function (d) { return { status: r.status, data: d }; });
    });
  }
  function fmt(n) { return n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(Math.round(n)); }
  function tok(r) { return (r.input_tokens || 0) + (r.output_tokens || 0); }
  function kind(r) {
    if (r.state === "failed" || r.state === "error") return "failed";
    if (r.action === "cache") return "cached";
    return r.action === "use" ? "reuse" : "build";
  }

  // ------------------------------------------------------------ evidence
  // Rows = main-arm sessions that finished, each paired with its no-memory twin.
  function evidence(heldOnly) {
    var twins = {};
    var hist = curHistory();
    hist.forEach(function (r) { if (r.arm === "nomem" && r.pair) twins[r.pair] = r; });
    var rows = hist.filter(function (r) { return r.arm !== "nomem" && r.state !== "running" && !!r.oracle === !!heldOnly; })
      .map(function (r) {
        var tw = twins[r.session_id];
        return { r: r, twin: tw && tw.state !== "running" ? tw : null };
      });
    var measured = rows.filter(function (x) { return x.twin; });
    var avgTwin = measured.length ? measured.reduce(function (a, x) { return a + tok(x.twin); }, 0) / measured.length : null;
    rows.forEach(function (x) {
      x.base = x.twin ? tok(x.twin) : (avgTwin != null ? avgTwin : tok(x.r));
      x.baseMeasured = !!x.twin;
      x.twinFailed = !!(x.twin && (x.twin.state === "failed" || x.twin.state === "error" || x.twin.result == null));
      x.match = x.twin && !x.twinFailed ? (x.twin.result === x.r.result && x.r.result != null) : null;
    });
    return rows;
  }

  function pctText(spent, base) {
    if (!base) return "\u2014";
    var p = Math.round(100 * (base - spent) / base);
    return p >= 0 ? p + "% fewer" : Math.abs(p) + "% more";
  }
  function range(idx) {
    if (!idx.length) return "";
    return idx.length === 1 ? "#" + idx[0] : "#" + idx[0] + "\u2013#" + idx[idx.length - 1];
  }
  function setBig(id, text, est) {
    var e = $(id);
    clear(e);
    e.appendChild(document.createTextNode(text));
    if (est) e.appendChild(h("small", "est", " est."));
  }
  function setLines(id, lines) {
    var el0 = $(id);
    clear(el0);
    lines.filter(Boolean).forEach(function (t) { el0.appendChild(h("span", "ln", t)); });
  }
  function scoreboard(rows) {
    var est = curHistory().some(function (r) { return r.estimated; });
    var tilde = est ? "~" : "";
    var groups = { reuse: [], cached: [], build: [] };
    rows.forEach(function (x, i) {
      var k = kind(x.r);
      if (k === "reuse") groups.reuse.push([x, i + 1]);
      else if (k === "cached") groups.cached.push([x, i + 1]);
      else if (k === "build" && i > 0) groups.build.push([x, i + 1]);
    });
    function sums(list) {
      return [list.reduce(function (a, p) { return a + tok(p[0].r); }, 0), list.reduce(function (a, p) { return a + p[0].base; }, 0)];
    }
    function idx(list) { return list.map(function (p) { return p[1]; }).join(", "); }
    function show(idEl, idSub, list, empty, extra) {
      if (!list.length) { $(idEl).textContent = "\u2014"; $(idEl).className = ""; $(idSub).textContent = empty; return; }
      var t = sums(list);
      $(idEl).textContent = pctText(t[0], t[1]);
      $(idEl).className = t[0] < t[1] ? "good" : "warn";
      $(idSub).textContent = "prompt" + (list.length > 1 ? "s " : " ") + idx(list) + ": " + tilde + fmt(t[0]) + " used vs " + tilde + fmt(t[1]) + (extra || "");
    }
    show("t-reuse", "t-reuse-sub", groups.reuse, "a short check finds the saved tool, then runs it");
    show("t-zero", "t-zero-sub", groups.cached, "an exact repeat is answered from a saved tool", ", no model call");
    var rep = groups.build.reduce(function (a, p) { return a + (p[0].r.repairs || 0); }, 0);
    show("t-saved", "t-saved-sub", groups.build, "new tools cost about the same as without memory", rep ? " (includes " + rep + " repair)" : "");
    var known = rows.filter(function (x) { return x.r.expected != null; });
    var knownOk = known.filter(function (x) { return x.r.expected_ok; }).length;
    var pairs = rows.filter(function (x) { return x.twin && !x.twinFailed; });
    var twinFails = rows.filter(function (x) { return x.twinFailed; }).length;
    var agree = pairs.filter(function (x) { return x.match; }).length;
    var caught = rows.reduce(function (a, x) { return a + (x.r.repairs || 0); }, 0);
    var tp = rows.reduce(function (a, x) { return a + (x.r.tests_passed || 0); }, 0);
    if (known.length) {
      $("t-match").textContent = knownOk + " / " + known.length;
      $("t-match").className = knownOk === known.length ? "good" : "bad";
      setLines("t-match-sub", [
        "equal the known answer (hard-coded for this demo)",
        pairs.length ? agree + " / " + pairs.length + " also equal the no-memory run" : "",
        twinFails ? "no-memory run failed to answer " + twinFails + "×, tokens still spent" : "",
        caught + " wrong tool" + (caught === 1 ? "" : "s") + " rejected by tests and repaired",
        tp + " tests passed before saving"]);
    } else if (pairs.length) {
      $("t-match").textContent = agree + " / " + pairs.length;
      $("t-match").className = agree === pairs.length ? "good" : "bad";
      $("t-match-sub").textContent = "agree with the no-memory run (no known answer to check); " + tp + " tests passed";
    } else {
      $("t-match").textContent = rows.length ? String(tp) + " tests" : "\u2014"; $("t-match").className = "";
      $("t-match-sub").textContent = rows.length ? "passed before saving; tick \u201cAlso run without memory\u201d to compare" : "tests gate every save";
    }
    var spent = rows.reduce(function (a, x) { return a + tok(x.r); }, 0);
    var base = rows.reduce(function (a, x) { return a + x.base; }, 0);
    var both = rows.filter(function (x) { return x.twin && !x.twinFailed; });
    var bs = both.reduce(function (a, x) { return a + tok(x.r); }, 0);
    var bb = both.reduce(function (a, x) { return a + x.base; }, 0);
    $("ag-overall").textContent = rows.length
      ? "Overall: " + pctText(spent, base) + " tokens over " + rows.length + " prompts" +
        (twinFails && both.length ? " · conservative (only the " + both.length + " where both runs answered): " + pctText(bs, bb) : "")
      : "lower is better";
    $("ag-recon").textContent = rows.length
      ? "With memory: " + rows.map(function (x) { return fmt(tok(x.r)); }).join(" + ") + " = " + tilde + fmt(spent) +
        " tokens. No-memory runs: " + rows.map(function (x) { return fmt(x.base); }).join(" + ") + " = " + tilde + fmt(base) + "."
      : "";
    $("ag-est-note").hidden = !est;
    kpis(rows, groups, spent, base, tilde, known, knownOk, pairs, agree, twinFails, caught, both, bs, bb);
  }

  function kpis(rows, groups, spent, base, tilde, known, knownOk, pairs, agree, twinFails, caught, both, bs, bb) {
    if (!rows.length) {
      $("k-saved").textContent = "\u2014"; $("k-reuse").textContent = "\u2014"; $("k-correct").textContent = "\u2014";
      $("k-saved").className = $("k-reuse").className = $("k-correct").className = "";
      $("k-saved-sub").textContent = "run the guided demo to see it"; $("k-reuse-sub").textContent = "\u00a0"; $("k-correct-sub").textContent = "\u00a0";
      return;
    }
    var delta = base - spent;
    var pct = base ? Math.round(100 * Math.abs(delta) / base) : 0;
    var learn = groups.build.concat(rows.length && kind(rows[0].r) === "build" ? [[rows[0], 1]] : []);
    var ls = learn.reduce(function (a, p) { return a + tok(p[0].r); }, 0);
    var lb = learn.reduce(function (a, p) { return a + p[0].base; }, 0);
    var learnTxt = learn.length && lb ? "learning phase (" + learn.length + " new tools): " + pctText(ls, lb).replace("fewer", "fewer").replace(" more", " more") : "";
    if (delta > 0) {
      setBig("k-saved", pct + "% fewer", tilde);
      $("k-saved").className = "good";
      $("k-saved-sub").textContent = tilde + fmt(delta) + " tokens saved of " + tilde + fmt(base) + " (" + rows.length + " prompts)" + (learnTxt ? "; " + learnTxt : "") +
        (twinFails && both.length ? "; " + pctText(bs, bb) + " counting only prompts where both runs answered" : "");
    } else {
      $("k-saved").textContent = pct + "% more";
      $("k-saved").className = "warn";
      $("k-saved-sub").textContent = "memory cost " + tilde + fmt(-delta) + " more over " + rows.length + " prompts: mostly new tasks, where there is nothing to reuse yet, and one run each is noisy";
    }
    var later = groups.reuse.concat(groups.cached);
    if (later.length) {
      var rs = later.reduce(function (a, p) { return a + tok(p[0].r); }, 0);
      var rb = later.reduce(function (a, p) { return a + p[0].base; }, 0);
      var rp = rb ? Math.round(100 * (rb - rs) / rb) : 0;
      setBig("k-reuse", rp + "% cheaper", tilde);
      $("k-reuse").className = rp > 0 ? "good" : "warn";
      $("k-reuse-sub").textContent = "prompt" + (later.length === 1 ? " " : "s ") + later.map(function (p) { return p[1]; }).join(", ") + ": " + tilde + fmt(rs) + " tokens vs " + tilde + fmt(rb) + " for the same prompts with no memory (n = " + later.length + " prompt" + (later.length === 1 ? "" : "s") + ", 1 run each)";
    } else {
      $("k-reuse").textContent = "\u2014"; $("k-reuse").className = "";
      $("k-reuse-sub").textContent = "ask a related prompt to see it";
    }
    if (known.length) {
      $("k-correct").textContent = knownOk + " / " + known.length;
      $("k-correct").className = knownOk === known.length ? "good" : "bad";
      $("k-correct-sub").textContent = "match the known answer" + (caught ? "; " + caught + " wrong tool" + (caught === 1 ? "" : "s") + " caught by tests" : "") + (twinFails ? "; no-memory run failed " + twinFails + "\u00d7" : "");
    } else if (pairs.length) {
      $("k-correct").textContent = agree + " / " + pairs.length;
      $("k-correct").className = agree === pairs.length ? "good" : "bad";
      $("k-correct-sub").textContent = "agree with the no-memory run";
    } else {
      $("k-correct").textContent = "\u2014"; $("k-correct").className = "";
      $("k-correct-sub").textContent = "tests gate every save";
    }
  }

  function niceMax(v) {
    var steps = [100, 200, 400, 600, 800, 1000, 1500, 2000, 3000, 4000, 6000, 8000, 10000];
    for (var i = 0; i < steps.length; i++) if (v <= steps[i]) return steps[i];
    return Math.ceil(v / 1000) * 1000;
  }
  var PHASE = { build: ["LEARN", "#b18cff"], reuse: ["REUSE", "#5ad1ff"], cached: ["REPEAT", "#7bd88a"], failed: ["FAILED", "#ff7a7a"] };

  function drawPairsMobile(svg, data, demo) {
    var W = 360, rowH = 50, top = 8, n = data.length;
    var maxV = niceMax(data.reduce(function (a, d) { return Math.max(a, d.used, d.base); }, 0));
    var bx = 34, bw = 232;
    function X(v) { return bx + bw * v / maxV; }
    svg.setAttribute("viewBox", "0 0 " + W + " " + (top + n * rowH + 6));
    var defs = el("defs");
    var pat = el("pattern", { id: "agHatch", width: 7, height: 7, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" });
    pat.appendChild(el("rect", { width: 7, height: 7, fill: "#3a2a30" }));
    pat.appendChild(el("rect", { width: 3, height: 7, fill: "#a0606c" }));
    defs.appendChild(pat); svg.appendChild(defs);
    data.forEach(function (d, i) {
      var y0 = top + i * rowH;
      var col = d.k === "reuse" ? "#5ad1ff" : (d.k === "cached" ? "#7bd88a" : (d.k === "failed" ? "#ff7a7a" : "#b18cff"));
      var ph = PHASE[d.k] || PHASE.build;
      svg.appendChild(el("rect", { x: 0, y: y0, width: W, height: rowH - 4, rx: 6, fill: ph[1], opacity: 0.08 }));
      svg.appendChild(el("text", { x: 8, y: y0 + 29, "class": "ag-tick", fill: ph[1] }, "#" + (i + 1)));
      var nb = el("rect", { x: bx, y: y0 + 7, width: Math.max(2, X(d.base) - bx), height: 12, rx: 3, "class": "pp-nomem" + (d.ghost ? " ghost" : "") + (d.failedTwin ? " failed" : ""), fill: d.failedTwin ? "url(#agHatch)" : "" });
      if (!d.failedTwin) nb.removeAttribute("fill");
      var ub = el("rect", { x: bx, y: y0 + 23, width: Math.max(d.used === 0 ? 4 : 2, X(d.used) - bx), height: 14, rx: 3, fill: col, "class": "pp-used-h" + (d.ghost ? " ghost" : "") });
      svg.appendChild(nb); svg.appendChild(ub);
      if (!d.ghost) {
        [nb, ub].forEach(function (rr) { rr.style.cursor = "pointer"; rr.addEventListener("click", function () { showBarInfo(i); }); });
        svg.appendChild(el("text", { x: Math.min(X(d.base) + 5, 262), y: y0 + 17, "class": "pp-v nomem" }, fmt(d.base)));
        svg.appendChild(el("text", { x: Math.min(X(d.used) + 5, 262), y: y0 + 35, "class": "pp-v used" }, d.used === 0 ? "0" : fmt(d.used)));
        var pct = d.base ? Math.round(100 * (d.base - d.used) / d.base) : 0;
        if (pct >= 20) svg.appendChild(el("text", { x: W - 6, y: y0 + 22, "class": "pp-delta good", "text-anchor": "end" }, "−" + pct + "%"));
        else if (pct <= -5) svg.appendChild(el("text", { x: W - 6, y: y0 + 22, "class": "pp-delta warn", "text-anchor": "end" }, "+" + Math.abs(pct) + "%"));
        svg.appendChild(el("text", { x: W - 6, y: y0 + 38, "class": "pp-phase", fill: ph[1], "text-anchor": "end", style: "font-size:10px" }, ph[0]));
      }
    });
    if (demo) svg.appendChild(el("text", { x: W / 2, y: 28, "class": "ag-empty", "text-anchor": "middle" }, "Illustration, not data"));
  }

  function drawPairs(rows) {
    var svg = $("ag-pairs");
    clear(svg);
    var W = 760, H = 250, pl = 46, pr = 14, pt = 36, pb = 46;
    var defs = el("defs");
    var pat = el("pattern", { id: "agHatch", width: 7, height: 7, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" });
    pat.appendChild(el("rect", { width: 7, height: 7, fill: "#3a2a30" }));
    pat.appendChild(el("rect", { width: 3, height: 7, fill: "#a0606c" }));
    defs.appendChild(pat);
    svg.appendChild(defs);
    var demo = rows.length === 0;
    var data = rows;
    if (demo) {
      var ghost = [[330, 330, "build"], [420, 420, "build"], [380, 380, "build"], [410, 410, "build"], [140, 1150, "reuse"], [140, 1150, "reuse"], [0, 1150, "cached"]];
      data = ghost.map(function (g) { return { ghost: true, k: g[2], used: g[0], base: g[1] }; });
    } else {
      data = rows.map(function (x) { return { x: x, k: kind(x.r), used: tok(x.r), base: x.base, failedTwin: x.twinFailed, measured: x.baseMeasured }; });
    }
    if (window.innerWidth < 640) { drawPairsMobile(svg, data, demo); return; }
    svg.setAttribute("viewBox", "0 0 760 250");
    var maxV = niceMax(data.reduce(function (a, d) { return Math.max(a, d.used, d.base); }, 0));
    var n = data.length, slot = (W - pl - pr) / n;
    var bw = Math.min(30, slot * 0.34);
    function Y(v) { return H - pb - (H - pb - pt) * v / maxV; }
    // phase bands
    var groups = [];
    data.forEach(function (d, i) {
      var g = groups[groups.length - 1];
      if (g && g.k === d.k) g.to = i; else groups.push({ k: d.k, from: i, to: i });
    });
    groups.forEach(function (g, gi) {
      var x0 = pl + slot * g.from, x1 = pl + slot * (g.to + 1);
      var ph = PHASE[g.k] || PHASE.build;
      svg.appendChild(el("rect", { x: x0 + 1, y: pt - 18, width: x1 - x0 - 2, height: H - pb - pt + 18, rx: 6, fill: ph[1], opacity: gi % 2 ? 0.07 : 0.11 }));
      svg.appendChild(el("text", { x: (x0 + x1) / 2, y: H - 6, "class": "pp-phase", fill: ph[1], "text-anchor": "middle" }, ph[0]));
    });
    for (var t = 0; t <= 4; t++) {
      var y = Y(maxV * t / 4);
      svg.appendChild(el("line", { x1: pl, x2: W - pr, y1: y, y2: y, "class": "ag-grid" }));
      svg.appendChild(el("text", { x: pl - 8, y: y + 4, "class": "ag-tick", "text-anchor": "end" }, fmt(maxV * t / 4)));
    }
    svg.appendChild(el("text", { x: 4, y: 14, "class": "ag-tick" }, "tokens per prompt"));
    data.forEach(function (d, i) {
      var cx = pl + slot * (i + 0.5);
      var gh = d.ghost ? " ghost" : "";
      var bx = cx - bw - 2, ux = cx + 2;
      // no-memory bar
      var nb = el("rect", { x: bx, y: Y(d.base), width: bw, height: Math.max(2, H - pb - Y(d.base)), rx: 3, "class": "pp-nomem" + gh + (d.failedTwin ? " failed" : ""), fill: d.failedTwin ? "url(#agHatch)" : "" });
      if (!d.failedTwin) nb.removeAttribute("fill");
      // with-memory bar
      var col = d.k === "reuse" ? "#5ad1ff" : (d.k === "cached" ? "#7bd88a" : (d.k === "failed" ? "#ff7a7a" : "#b18cff"));
      var ub = el("rect", { x: ux, y: Y(d.used), width: bw, height: Math.max(d.used === 0 ? 4 : 2, H - pb - Y(d.used)), rx: 3, fill: col, "class": "pp-used" + gh });
      if (!d.ghost) {
        var tip = d.x.r.prompt + "\n" + d.used + " tokens with memory (" + d.k + ") vs " + Math.round(d.base) + " with no memory" + (d.failedTwin ? " (that run failed)" : (d.measured ? " (measured)" : " (estimated)"));
        nb.appendChild(el("title", {}, tip)); ub.appendChild(el("title", {}, tip));
      }
      svg.appendChild(nb); svg.appendChild(ub);
      if (!d.ghost) {
        [nb, ub].forEach(function (rr) { rr.style.cursor = "pointer"; rr.addEventListener("click", function () { showBarInfo(i); }); });
        if (n <= 10) svg.appendChild(el("text", { x: bx + bw / 2, y: Y(d.base) - 5, "class": "pp-v nomem", "text-anchor": "middle" }, fmt(d.base)));
        svg.appendChild(el("text", { x: ux + bw / 2, y: Y(d.used) - 5, "class": "pp-v used", "text-anchor": "middle" }, d.used === 0 ? "0" : fmt(d.used)));
        var pct = d.base ? Math.round(100 * (d.base - d.used) / d.base) : 0;
        var top = Math.min(Y(d.base), Y(d.used)) - 22;
        if (pct >= 20) svg.appendChild(el("text", { x: cx, y: Math.max(top, 12), "class": "pp-delta good", "text-anchor": "middle" }, "\u2212" + pct + "%"));
        else if (pct <= -5) svg.appendChild(el("text", { x: cx, y: Math.max(top, 12), "class": "pp-delta warn", "text-anchor": "middle" }, "+" + Math.abs(pct) + "%"));
        svg.appendChild(el("text", { x: cx, y: H - 22, "class": "ag-tick", "text-anchor": "middle" }, "#" + (i + 1)));
      }
    });
    if (demo) {
      svg.appendChild(el("text", { x: W / 2, y: pt + 40, "class": "ag-empty", "text-anchor": "middle" }, "Illustration, not data"));
      svg.appendChild(el("text", { x: W / 2, y: pt + 62, "class": "ag-empty sub", "text-anchor": "middle" }, "Run the guided demo: the green bars should collapse while the red ones stay tall."));
    }
  }

  function drawCumulative(rows) {
    var svg = $("ag-cum");
    clear(svg);
    var W = 640, H = 220, pl = 48, pb = 30, pt = 16, pr = 124;
    var actual = [0], nomem = [0];
    rows.forEach(function (x) {
      actual.push(actual[actual.length - 1] + tok(x.r));
      nomem.push(nomem[nomem.length - 1] + x.base);
    });
    var maxV = Math.max(100, nomem[nomem.length - 1], actual[actual.length - 1]);
    maxV = Math.ceil(maxV / 100) * 100;
    for (var i = 0; i <= 4; i++) {
      var y = H - pb - (H - pb - pt) * i / 4;
      svg.appendChild(el("line", { x1: pl, x2: W - pr + 20, y1: y, y2: y, "class": "ag-grid" }));
      svg.appendChild(el("text", { x: pl - 8, y: y + 4, "class": "ag-tick", "text-anchor": "end" }, fmt(maxV * i / 4)));
    }
    if (!rows.length) {
      var ill = "M" + pl + "," + (H - pb) + " L" + (W - pr) + "," + (pt + 20);
      var ill2 = "M" + pl + "," + (H - pb) + " L" + (pl + 200) + "," + (H - pb - 70) + " L" + (W - pr) + "," + (H - pb - 78);
      svg.appendChild(el("path", { d: ill, fill: "none", "class": "cum-nomem ill" }));
      svg.appendChild(el("path", { d: ill2, fill: "none", "class": "cum-actual ill" }));
      svg.appendChild(el("text", { x: (W - pr + pl) / 2, y: H / 2 - 6, "class": "ag-empty" }, "Illustration, not data"));
      svg.appendChild(el("text", { x: (W - pr + pl) / 2, y: H / 2 + 14, "class": "ag-empty sub" }, "Run the guided demo: the green line should flatten while the red one keeps climbing."));
      return;
    }
    var n = rows.length;
    function X(i) { return pl + (W - pl - pr) * i / Math.max(1, n); }
    function Y(v) { return H - pb - (H - pb - pt) * v / maxV; }
    var upper = nomem.map(function (v, i) { return (i ? "L" : "M") + X(i) + "," + Y(v); }).join(" ");
    var lowerRev = actual.map(function (v, i) { return [X(i), Y(v)]; }).reverse()
      .map(function (p) { return "L" + p[0] + "," + p[1]; }).join(" ");
    svg.appendChild(el("path", { d: upper + " " + lowerRev + " Z", "class": "cum-gap" }));
    svg.appendChild(el("path", { d: upper, fill: "none", "class": "cum-nomem" }));
    svg.appendChild(el("path", {
      d: actual.map(function (v, i) { return (i ? "L" : "M") + X(i) + "," + Y(v); }).join(" "),
      fill: "none", "class": "cum-actual"
    }));
    rows.forEach(function (x, i) {
      var k = kind(x.r);
      var c = el("circle", { cx: X(i + 1), cy: Y(actual[i + 1]), r: 5, "class": "cum-pt k-" + k });
      c.appendChild(el("title", {}, x.r.prompt + "\n" + tok(x.r) + " tokens (" + k + ") vs " + fmt(x.base) + " in a no-memory run" + (x.baseMeasured ? " (measured)" : " (estimated)")));
      svg.appendChild(c);
      svg.appendChild(el("text", { x: X(i + 1), y: H - 10, "class": "ag-tick k-" + k, "text-anchor": "middle" }, "#" + (i + 1) + " " + k));
    });
    var gap = nomem[n] - actual[n];
    var xe = X(n), yt = Y(nomem[n]), yb = Y(actual[n]);
    if (gap > 0) {
      svg.appendChild(el("line", { x1: xe + 4, x2: xe + 4, y1: yt, y2: yb, "class": "cum-brace" }));
      svg.appendChild(el("text", { x: xe + 11, y: (yt + yb) / 2 + 4, "class": "cum-gap-t" }, "saved " + fmt(gap)));
    }
    svg.appendChild(el("text", { x: xe + 11, y: Math.min(yt, yb) - 6, "class": "ag-tick k-failed" }, "no memory " + fmt(nomem[n])));
    svg.appendChild(el("text", { x: xe + 11, y: Math.max(yt, yb) + 16, "class": "ag-tick k-cached" }, "with memory " + fmt(actual[n])));
  }

  function drawLog(rows) {
    var box = $("ag-table");
    clear(box);
    if (!rows.length) {
      box.appendChild(h("p", "muted ag-pad", "Every prompt you send is logged here with what it cost and whether its answer was correct. Click a row to reuse its prompt."));
      return;
    }
    var maxT = Math.max(1, rows.reduce(function (a, x) { return Math.max(a, tok(x.r), x.base); }, 0));
    var tbl = h("table", "ag-tbl");
    var head = h("tr");
    ["#", "Prompt", "Answered by", "Tokens", "No-memory run", "Answer"].forEach(function (c) { head.appendChild(h("th", "", c)); });
    tbl.appendChild(head);
    rows.forEach(function (x, i) {
      var k = kind(x.r);
      var tr = h("tr", "clickable");
      tr.title = "Click to put this prompt back in the box";
      tr.addEventListener("click", function () { $("ag-prompt").value = x.r.prompt; $("ag-prompt").focus(); });
      tr.appendChild(h("td", "num", String(i + 1)));
      var ptd = h("td", "pr");
      ptd.appendChild(document.createTextNode(x.r.prompt));
      var flags = h("div", "flags");
      if (x.held) flags.appendChild(h("span", "chip k-held", "held-out"));
      if (x.r.repairs) flags.appendChild(h("span", "chip k-failed", x.r.repairs + " repair"));
      if (flags.childNodes.length) ptd.appendChild(flags);
      tr.appendChild(ptd);
      var how = h("td");
      how.appendChild(h("span", "chip k-" + k, k === "cached" ? "cached" : (k === "reuse" ? "reused" : (k === "build" ? "built" : "failed"))));
      tr.appendChild(how);
      var tk = h("td", "barcell"); var b = h("div", "mini k-" + k); b.style.width = Math.max(2, 100 * tok(x.r) / maxT) + "%";
      tk.appendChild(b); tk.appendChild(h("span", "", fmt(tok(x.r)))); tr.appendChild(tk);
      var sc = h("td", "barcell"); var b2 = h("div", "mini base" + (x.twinFailed ? " failed" : "")); b2.style.width = Math.max(2, 100 * x.base / maxT) + "%";
      sc.appendChild(b2);
      sc.appendChild(h("span", "", i === 0 ? "same (first prompt)" : fmt(x.base) + (x.twinFailed ? " (it failed)" : (x.baseMeasured ? "" : " est."))));
      tr.appendChild(sc);
      var wrong = x.r.expected_ok === false || x.match === false;
      var none = x.r.result == null || k === "failed";
      wrong = wrong || none;
      var ans = h("td", wrong ? "bad" : "good", (wrong ? "\u2717 " : "\u2713 ") + (none ? "no answer" : x.r.result));
      var why = [];
      if (x.r.expected != null) why.push(x.r.expected_ok ? (x.r.oracle ? "equals the independent oracle (" + x.r.oracle + ")" : "equals the known answer") : "expected " + x.r.expected);
      if (x.twinFailed) why.push("the no-memory run failed to produce an answer");
      else if (x.match != null) why.push(x.match ? "equals the no-memory run" : "differs from the no-memory run");
      if (x.r.nRuns > 1) {
        why.push(x.r.okRuns + " of " + x.r.nRuns + " runs correct; tokens ranged " + fmt(x.r.tokMin) + " to " + fmt(x.r.tokMax));
        ans.appendChild(h("span", "sm", " " + x.r.okRuns + "/" + x.r.nRuns));
      }
      ans.title = why.join("; ");

      tr.appendChild(ans);
      tbl.appendChild(tr);
    });
    box.appendChild(tbl);
  }

  // ------------------------------------------ repeated-run snapshots
  function snapRows(sn) {
    if (!sn.runs || sn.runs.length < 2) return sn.rows;
    if (!sn._agg) sn._agg = aggregateRuns(sn.runs);
    return sn._agg;
  }
  function mean(a) { return a.reduce(function (x, y) { return x + y; }, 0) / (a.length || 1); }
  function modal(vals) {
    var c = {}, best = null, bn = 0;
    vals.forEach(function (v) { var k = String(v); c[k] = (c[k] || 0) + 1; if (c[k] > bn) { bn = c[k]; best = v; } });
    return best;
  }
  function aggregateRuns(runs) {
    var per = runs.map(function (run) {
      var twins = {};
      run.rows.forEach(function (r) { if (r.arm === "nomem" && r.pair) twins[r.pair] = r; });
      return run.rows.filter(function (r) { return r.arm !== "nomem"; }).map(function (r) { return { m: r, t: twins[r.session_id] || null }; });
    });
    var n = Math.min.apply(null, per.map(function (p) { return p.length; }));
    var out = [];
    function synth(list, id, pair, arm) {
      var f = list[0];
      var okc = list.filter(function (r) { return r.expected_ok; }).length;
      var failed = list.filter(function (r) { return r.state === "failed" || r.state === "error" || r.result == null; }).length;
      var toks = list.map(tok);
      var res = modal(list.filter(function (r) { return r.result != null; }).map(function (r) { return r.result; }));
      var o = {};
      Object.keys(f).forEach(function (k) { o[k] = f[k]; });
      o.session_id = id; o.pair = pair; o.arm = arm;
      o.input_tokens = Math.round(mean(list.map(function (r) { return r.input_tokens || 0; })));
      o.output_tokens = Math.round(mean(list.map(function (r) { return r.output_tokens || 0; })));
      o.model_calls = Math.round(mean(list.map(function (r) { return r.model_calls || 0; })));
      o.repairs = Math.round(mean(list.map(function (r) { return r.repairs || 0; })));
      o.state = failed * 2 > list.length ? "failed" : "done";
      o.result = res == null ? null : res;
      o.expected_ok = f.expected != null ? okc === list.length : f.expected_ok;
      o.action = modal(list.map(function (r) { return r.action; }));
      o.nRuns = list.length; o.okRuns = okc; o.failedRuns = failed;
      o.tokMin = Math.min.apply(null, toks); o.tokMax = Math.max.apply(null, toks);
      return o;
    }
    for (var j = 0; j < n; j++) {
      var mains = per.map(function (p) { return p[j].m; });
      var twins = per.map(function (p) { return p[j].t; }).filter(Boolean);
      var id = "agg-" + j;
      out.push(synth(mains, id, null, "main"));
      if (twins.length) out.push(synth(twins, id + "-t", id, "nomem"));
    }
    return out;
  }
  function runRanges(sn) {
    if (!sn || !sn.runs || sn.runs.length < 2) return null;
    return sn.runs.map(function (run) { return summarize(run.rows); });
  }
  function rangeText(sums, key, suffix) {
    var v = sums.map(function (m) { return m[key]; }).filter(function (x) { return x != null; });
    if (!v.length) return "";
    var lo = Math.min.apply(null, v), hi = Math.max.apply(null, v);
    return lo === hi ? lo + suffix + " in every run" : lo + "\u2013" + hi + suffix;
  }

  // ----------------------------------------- evidence source, banner, strength
  function drawEvTabs() {
    var box = $("ag-evtabs");
    clear(box);
    if (!state.snapshots.length && state.view === "session") {
      // still offer pinning when there is something to pin
    }
    function tab(id, label) {
      var b = h("button", "ag-evtab" + (state.view === id ? " on" : ""), label);
      b.setAttribute("role", "tab"); b.setAttribute("aria-selected", state.view === id ? "true" : "false");
      b.addEventListener("click", function () { state.view = id; state.picked = true; state.pick = null; redrawEvidence(); });
      box.appendChild(b);
    }
    state.snapshots.forEach(function (sn) { tab(sn.id, sn.label || sn.id); });
    if (state.snapshots.length) tab("session", "Try it yourself (this session)");
    var sessionRows = state.history.filter(function (r) { return r.arm !== "nomem" && r.state !== "running"; });
    if (state.view === "session" && sessionRows.length) {
      var pin = h("button", "ag-evtab pin", "\u{1F4CC} Pin this run");
      pin.title = "Save this run so you can compare it later";
      pin.addEventListener("click", function () {
        var label = (state.mode === "live" ? "Live run " : "Demo run ") + new Date().toLocaleString();
        api("POST", "/api/agent/pin", { label: label }).then(function () { loadSnapshots(); });
      });
      box.appendChild(pin);
    }
  }

  function drawBanner(rows) {
    var b = $("ag-evbanner");
    var hist = curHistory();
    var main = hist.filter(function (r) { return r.arm !== "nomem" && !r.oracle; });
    var sn = state.view !== "session" ? state.snapshots.filter(function (x) { return x.id === state.view; })[0] : null;
    if (!main.length) {
      var live = state.mode === "live";
      b.className = "ag-evbanner " + (live ? "live" : "demo");
      b.textContent = live ? "LIVE MODE \u00b7 real Cerebras Qwen tokens will appear here" : "DEMO MODE \u00b7 scripted model \u00b7 token counts will be estimates";
      return;
    }
    var est = hist.some(function (r) { return r.estimated && !r.oracle; });
    b.className = "ag-evbanner " + (est ? "demo" : "live");
    $("ag-hero-note").textContent = est
      ? "This view uses a scripted model and tiny functions: it shows the mechanism, it is not a benchmark."
      : "This view is a real model on tiny functions, one run each: a demonstration, not a benchmark.";
    var nR = sn && sn.runs && sn.runs.length > 1 ? sn.runs.length : 1;
    var shortTxt = est
      ? "ESTIMATED \u00b7 scripted model \u00b7 token counts are approximate"
      : "REAL TOKENS \u00b7 Cerebras Qwen \u00b7 " + nR + " run" + (nR === 1 ? "" : "s") + " \u00b7 counts come from the API";
    var detail = est
      ? "Token counts are about 4 characters per token. Demo mode is a scripted offline model that only knows the example chips."
      : (sn && sn.note ? sn.note : "A live session on the real model.");
    clear(b);
    b.appendChild(document.createTextNode(shortTxt + " "));
    var tg = h("button", "ag-evtoggle", "details");
    tg.setAttribute("aria-expanded", "false");
    tg.addEventListener("click", function () {
      var note = $("ag-evnote");
      note.hidden = !note.hidden;
      tg.setAttribute("aria-expanded", String(!note.hidden));
      tg.textContent = note.hidden ? "details" : "hide";
    });
    b.appendChild(tg);
    $("ag-evnote").textContent = detail;
  }

  function drawStrength(rows) {
    var box = $("ag-strength");
    clear(box);
    var hist = curHistory();
    var main = hist.filter(function (r) { return r.arm !== "nomem" && r.state !== "running" && !r.oracle; });
    var heldAll = hist.filter(function (r) { return r.arm !== "nomem" && r.state !== "running" && r.oracle; });
    if (!main.length && !heldAll.length) return;
    function chip(text, cls) { box.appendChild(h("span", "sc " + (cls || ""), text)); }
    var est = main.some(function (r) { return r.estimated; });
    chip(est ? "savings: scripted model (estimated)" : "savings: real model", est ? "warn" : "ok");
    var nr = Math.max.apply(null, main.map(function (r) { return r.nRuns || 1; }));
    chip(nr > 1 ? nr + " runs per prompt (temperature 0: near-identical)" : "1 run per prompt", nr > 1 ? "ok" : "warn");
    var known = main.filter(function (r) { return r.expected != null && !r.oracle; }).length;
    if (known) chip("known answers hard-coded for " + known + " prompt" + (known === 1 ? "" : "s"), "warn");
    var orc = heldAll.filter(function (r) { return r.expected != null; });
    if (orc.length) {
      var ok = orc.filter(function (r) { return r.expected_ok; }).length;
      chip("held-out prompts vs an independent Python oracle: " + ok + " / " + orc.length + (orc.some(function (r) { return r.estimated; }) ? " (scripted)" : " (real model)"), ok === orc.length ? "ok" : "warn");
    } else {
      chip("held-out check: not run yet", "warn");
      if (state.view === "session" && state.liveOk) {
        var b = h("button", "ag-evtab", "Run held-out check (live, about half a cent)");
        b.disabled = state.busy;
        b.addEventListener("click", runHeldout);
        box.appendChild(b);
      }
    }
  }

  function runHeldout() {
    if (state.busy) return;
    setBusy(true);
    $("ag-mode").value = "live"; setModeNote();
    api("GET", "/api/agent/heldout").then(function (r) {
      var tasks = (r.data && r.data.tasks) || [];
      var chain = Promise.resolve();
      tasks.forEach(function (t, i) {
        chain = chain.then(function () {
          $("ag-demo").textContent = "Held-out " + (i + 1) + " of " + tasks.length + "\u2026";
          return runPrompt(t.prompt, "live", false, t.expected, t.oracle);
        });
      });
      return chain;
    }).catch(function (e) { say("Held-out check stopped: " + e.message, "fail"); })
      .then(function () { setBusy(false); $("ag-demo").textContent = "\u25b6 Run all 7 (guided demo)"; redrawEvidence(); });
  }

  function loadSnapshots() {
    return api("GET", "/api/agent/snapshots").then(function (r) {
      state.snapshots = (r.data && r.data.snapshots) || [];
      if (!state.picked && state.view === "session" && state.snapshots.length) {
        var rec = state.snapshots.filter(function (x) { return x.kind === "recorded" && x.runs && x.runs.length > 1; })[0] ||
                  state.snapshots.filter(function (x) { return x.kind === "recorded"; })[0];
        if (rec) state.view = rec.id;
      }
      redrawEvidence(); thesisStrip(); sampleRepl();
    });
  }

  function heldKpi(held) {
    if (!held.length) {
      $("k-held").textContent = "\u2014"; $("k-held").className = "";
      $("k-held-sub").textContent = "not run yet: prompts outside the demo, answers from independent Python";
      return;
    }
    var ok = held.filter(function (x) { return x.r.expected_ok; }).length;
    var toks = held.reduce(function (a, x) { return a + tok(x.r); }, 0);
    var est = held.some(function (x) { return x.r.estimated; });
    $("k-held").textContent = ok + " / " + held.length;
    $("k-held").className = ok === held.length ? "good" : (ok / held.length >= 0.8 ? "warn" : "bad");
    $("k-held-sub").textContent = (est ? "scripted" : "real model") + " \u00b7 " + (est ? "~" : "") + fmt(toks) + " tokens spent over " + held.length + " prompt" + (held.length === 1 ? "" : "s") + ", 1 run each; not counted in the savings";
  }

  function showBarInfo(i) {
    var x = state.rows[i];
    var box = $("ag-barinfo");
    if (!x) { box.hidden = true; return; }
    state.pick = i;
    box.hidden = false;
    clear(box);
    var k = kind(x.r);
    box.appendChild(h("div", "bi-h", "#" + (i + 1) + "  " + x.r.prompt));
    var line;
    if (k === "reuse" || k === "cached") {
      var built = state.rows.filter(function (y) { return y.r.promoted && y.r.promoted === x.r.tool; })[0];
      line = (k === "cached" ? "Exact repeat, no model call. " : "Reused a saved tool. ") + "Answered by `" + (x.r.tool || "?") + "`" +
        (built ? ", built at prompt #" + (state.rows.indexOf(built) + 1) : "") + ": " + (x.r.call || "") + " \u2192 " + (x.r.result == null ? "?" : x.r.result);
    } else if (k === "build") {
      line = "The model wrote a new tool" + (x.r.promoted ? " `" + x.r.promoted + "`" : "") + " and its tests" + (x.r.repairs ? " (" + x.r.repairs + " repair)" : "") + ". Answer: " + (x.r.result == null ? "?" : x.r.result);
    } else { line = "This prompt did not produce a working answer."; }
    box.appendChild(h("div", "bi-l", line));
    box.appendChild(h("div", "bi-l muted", fmt(tok(x.r)) + " tokens with memory vs " + fmt(x.base) + " in the no-memory run" + (x.twinFailed ? " (that run failed)" : "") + "."));
    if (state.view === "session" && x.r.tool && state.tools.some(function (t) { return t.name === x.r.tool; })) {
      var b = h("button", "ag-chip", "Show the tool\u2019s source and tests");
      b.addEventListener("click", function () { state.sel = x.r.tool; drawGraph(); showDetail(); $("ag-detail").scrollIntoView({ block: "center" }); });
      box.appendChild(b);
    }
  }

  function rangeNotes() {
    var sn = state.view !== "session" ? state.snapshots.filter(function (x) { return x.id === state.view; })[0] : null;
    var sums = runRanges(sn);
    if (!sums) return;
    var nR = sums.length;
    function add(id, text) { if (text) $(id).textContent = $(id).textContent + " \u00b7 " + text; }
    var est = curHistory().some(function (r) { return r.estimated; }) ? "~" : "";
    $("k-saved-sub").textContent = "mean of " + nR + " runs (" + rangeText(sums, "net", "%") + "): " + est + fmt(mean(sums.map(function (q) { return q.spent; }))) + " vs " + est + fmt(mean(sums.map(function (q) { return q.base; }))) + " tokens per run. Learning prompts " + rangeText(sums, "lpct", "%").replace(" in every run", "") + " vs no memory, reuse and repeats " + rangeText(sums, "reuse", "%").replace(" in every run", "") + ": the net depends on the mix (" + sums[0].nReuse + " of 7 prompts here are reuse or repeat).";
    $("k-reuse-sub").textContent = "reused or repeated prompts, mean of " + nR + " runs (" + rangeText(sums, "reuse", "%") + "): " + est + fmt(mean(sums.map(function (q) { return q.rs; }))) + " tokens vs " + est + fmt(mean(sums.map(function (q) { return q.rb; }))) + " with no memory";
    var hk = sums.reduce(function (a, m) { return a + m.hok; }, 0), hn = sums.reduce(function (a, m) { return a + m.hn; }, 0);
    var hnk = sums.reduce(function (a, m) { return a + (m.hnok || 0); }, 0), hnn = sums.reduce(function (a, m) { return a + (m.hnn || 0); }, 0);
    $("k-held").textContent = hk + " / " + hn;
    $("k-held").className = hk === hn ? "good" : (hnn && hk >= hnk ? "neutral" : (hk / hn >= 0.8 ? "warn" : "bad"));
    $("k-held-sub").textContent = "across " + nR + " runs" + (hnn ? (hk >= hnk ? "; same as no memory (" + hnk + " / " + hnn + "): memory did not cost accuracy" : "; no memory: " + hnk + " / " + hnn) : "") + "; per-run " + rangeText(sums, "hok", "") + " of " + sums[0].hn;
    var gk = sums.reduce(function (a, m) { return a + m.ok; }, 0), gn = sums.reduce(function (a, m) { return a + m.known; }, 0);
    $("k-correct").textContent = gk + " / " + gn;
    $("k-correct").className = gk === gn ? "good" : (gk / gn >= 0.8 ? "warn" : "bad");
    $("k-correct-sub").textContent = "guided prompts across " + nR + " runs, each checked against the known answer";
  }

  function summarize(hist) {
    var twins = {};
    hist.forEach(function (r) { if (r.arm === "nomem" && r.pair) twins[r.pair] = r; });
    var mains = hist.filter(function (r) { return r.arm !== "nomem" && r.state !== "running" && !r.oracle; });
    var held = hist.filter(function (r) { return r.arm !== "nomem" && r.state !== "running" && r.oracle; });
    function base(r) { var t = twins[r.session_id]; return t ? tok(t) : tok(r); }
    var spent = 0, bs = 0, rs = 0, rb = 0, ok = 0, known = 0;
    mains.forEach(function (r) {
      spent += tok(r); bs += base(r);
      var k = kind(r);
      if (k === "reuse" || k === "cached") { rs += tok(r); rb += base(r); }
      if (r.expected != null) { known++; if (r.expected_ok) ok++; }
    });
    var lsp = 0, lbs = 0, nReuse = 0;
    mains.forEach(function (r) { var k = kind(r); if (k === "build") { lsp += tok(r); lbs += base(r); } else if (k === "reuse" || k === "cached") nReuse++; });
    var hn2 = held.filter(function (r) { return twins[r.session_id]; });
    var hnok = hn2.filter(function (r) { return twins[r.session_id].expected_ok; }).length;
    return { lpct: lbs ? Math.round(100 * (lbs - lsp) / lbs) : null, nReuse: nReuse, hnn: hn2.length, hnok: hnok, n: mains.length, reuse: rb ? Math.round(100 * (rb - rs) / rb) : null, rs: rs, rb: rb, net: bs ? Math.round(100 * (bs - spent) / bs) : null, spent: spent, base: bs,
             ok: ok, known: known, hok: held.filter(function (r) { return r.expected_ok; }).length, hn: held.length, htok: held.reduce(function (a, r) { return a + tok(r); }, 0) };
  }
  function thesisStrip() {
    if (!$("th-k1")) return;
    var rec = state.snapshots.filter(function (x) { return x.kind === "recorded" && x.runs && x.runs.length > 1; })[0] ||
              state.snapshots.filter(function (x) { return x.kind === "recorded"; })[0];
    if (!rec) return;
    var sums = runRanges(rec);
    var m = summarize(snapRows(rec));
    if (sums) {
      var nR = sums.length, hk = sums.reduce(function (a, q) { return a + q.hok; }, 0), hn = sums.reduce(function (a, q) { return a + q.hn; }, 0);
      var gk = sums.reduce(function (a, q) { return a + q.ok; }, 0), gn = sums.reduce(function (a, q) { return a + q.known; }, 0);
      $("th-k1").textContent = mean(sums.map(function (q) { return q.reuse; })).toFixed(0) + "% cheaper";
      $("th-k1s").textContent = "mean of " + nR + " runs, range " + rangeText(sums, "reuse", "%");
      $("th-k2").textContent = mean(sums.map(function (q) { return q.net; })).toFixed(0) + "% fewer";
      $("th-k2s").textContent = "mean of " + nR + " runs, range " + rangeText(sums, "net", "%");
      $("th-k3").textContent = gk + " / " + gn;
      $("th-k3").className = gk === gn ? "good" : "warn";
      $("th-k3s").textContent = "guided prompts across " + nR + " runs";
      $("th-k4").textContent = hk + " / " + hn;
      $("th-k4").className = hk === hn ? "good" : (hk / hn >= 0.8 ? "warn" : "");
      $("th-k4s").textContent = "held-out prompts across " + nR + " runs, answers from independent Python; misses are shown, not hidden";
      return;
    }
    $("th-k1").textContent = m.reuse == null ? "\u2014" : m.reuse + "% cheaper";
    $("th-k1s").textContent = fmt(m.rs) + " vs " + fmt(m.rb) + " tokens on reused or repeated prompts";
    $("th-k2").textContent = m.net == null ? "\u2014" : m.net + "% fewer";
    $("th-k2s").textContent = fmt(m.spent) + " vs " + fmt(m.base) + " tokens over " + m.n + " prompts";
    $("th-k3").textContent = m.ok + " / " + m.known;
    $("th-k3s").textContent = "match the known answer";
    $("th-k4").textContent = m.hn ? m.hok + " / " + m.hn : "\u2014";
    $("th-k4").className = m.hn && m.hok === m.hn ? "good" : (m.hn && m.hok / m.hn >= 0.8 ? "warn" : "");
    $("th-k4s").textContent = m.hn ? m.hn + " prompts not in the demo, answers from independent Python; misses are shown, not hidden" : "not run";
  }

  function sampleRepl() {
    if (state.session) return;                       // a real session owns the pane
    var rec = state.snapshots.filter(function (x) { return x.kind === "recorded"; })[0];
    if (!rec) return;
    clear(repl);
    replLine("c", ";; sample from the recorded live run: each answer is a saved tool running in SBCL");
    rec.rows.filter(function (r) { return r.arm !== "nomem" && !r.oracle && r.call; }).forEach(function (r) {
      replLine("in", "* " + r.call);
      replLine(r.result == null ? "err" : "val", "=> " + (r.result == null ? "(no answer)" : r.result) + "   ;; " + fmt(tok(r)) + " tokens, " + kind(r));
    });
    replLine("c", ";; send a prompt to watch your own run live");
  }

  function redrawEvidence() {
    var rows = evidence(false), held = evidence(true);
    held.forEach(function (x) { x.held = true; });
    state.rows = rows;
    heldKpi(held);
    scoreboard(rows); drawPairs(rows); drawCumulative(rows); drawLog(rows.concat(held)); drawEvTabs(); drawBanner(rows); drawStrength(rows); paintBadge(); if (state.pick != null && rows[state.pick]) showBarInfo(state.pick); else $("ag-barinfo").hidden = true; rangeNotes();
  }

  // --------------------------------------------------------- tools + graph
  function deps(tool, all) {
    var out = [];
    all.forEach(function (o) {
      if (o.name === tool.name) return;
      var re = new RegExp("[\\s(']" + o.name.replace(/[-]/g, "\\-") + "[\\s)]");
      if (re.test(tool.definition || "")) out.push(o.name);
    });
    return out;
  }

  function layout(tools, ghost) {
    var nodes = tools.map(function (t) {
      return { name: t.name, desc: t.description, uses: t.uses || 0, deps: deps(t, tools), ghost: false };
    });
    if (ghost) nodes.push({ name: ghost, desc: "being tested", uses: 0, deps: [], ghost: true });
    var depth = {};
    function d(n, seen) {
      if (depth[n.name] != null) return depth[n.name];
      if (seen[n.name]) return 0;
      seen[n.name] = 1;
      var m = 0;
      n.deps.forEach(function (dn) {
        var o = nodes.filter(function (x) { return x.name === dn; })[0];
        if (o) m = Math.max(m, d(o, seen) + 1);
      });
      depth[n.name] = m;
      return m;
    }
    nodes.forEach(function (n) { n.depth = d(n, {}); });
    var maxD = nodes.reduce(function (a, n) { return Math.max(a, n.depth); }, 0);
    var cols = {};
    nodes.forEach(function (n) { (cols[n.depth] = cols[n.depth] || []).push(n); });
    var W = 640, H = 340, x0 = 170, x1 = W - 80;
    Object.keys(cols).forEach(function (k) {
      var col = cols[k], c = +k;
      col.forEach(function (n, i) {
        n.x = maxD === 0 ? (x0 + x1) / 2 : x0 + (x1 - x0) * c / maxD;
        n.y = H / 2 + (i - (col.length - 1) / 2) * 118 + (c % 2 ? 12 : -12);
      });
    });
    return nodes;
  }

  function drawGraph() {
    var svg = $("ag-graph");
    clear(svg);
    var defs = el("defs");
    var rg = el("radialGradient", { id: "agCore" });
    rg.appendChild(el("stop", { offset: 0, "stop-color": "#b18cff" }));
    rg.appendChild(el("stop", { offset: 1, "stop-color": "#3f6ad8" }));
    defs.appendChild(rg);
    var flt = el("filter", { id: "agGlow", x: "-60%", y: "-60%", width: "220%", height: "220%" });
    flt.appendChild(el("feGaussianBlur", { stdDeviation: 5, result: "b" }));
    var mrg = el("feMerge");
    mrg.appendChild(el("feMergeNode", { "in": "b" }));
    mrg.appendChild(el("feMergeNode", { "in": "SourceGraphic" }));
    flt.appendChild(mrg);
    defs.appendChild(flt);
    svg.appendChild(defs);
    for (var gx = 20; gx < 640; gx += 32)
      for (var gy = 20; gy < 340; gy += 32)
        svg.appendChild(el("circle", { cx: gx, cy: gy, r: 1, fill: "#2b3040" }));

    var nodes = layout(state.tools, state.ghost);
    var by = {};
    nodes.forEach(function (n) { by[n.name] = n; });
    var core = { x: 62, y: 170 };
    function curve(a, b, cls, w) {
      var mx = (a.x + b.x) / 2;
      return el("path", {
        d: "M" + a.x + "," + a.y + " C" + mx + "," + a.y + " " + mx + "," + b.y + " " + b.x + "," + b.y,
        fill: "none", "class": cls, "stroke-width": w || 2
      });
    }
    nodes.forEach(function (n) {
      if (!n.deps.length) svg.appendChild(curve(core, n, "ag-edge root" + (n.ghost ? " ghost" : "")));
      n.deps.forEach(function (dn) {
        if (by[dn]) svg.appendChild(curve(by[dn], n, "ag-edge dep" + (n.ghost ? " ghost" : ""), 2.5));
      });
    });
    svg.appendChild(el("circle", { cx: core.x, cy: core.y, r: 32, fill: "url(#agCore)", filter: "url(#agGlow)", "class": "ag-core" }));
    svg.appendChild(el("text", { x: core.x, y: core.y + 4, "class": "ag-core-t" }, "model"));
    svg.appendChild(el("text", { x: core.x, y: core.y + 54, "class": "ag-note" }, "writes tools"));

    nodes.forEach(function (n, i) {
      var g = el("g", { "class": "ag-node" + (n.ghost ? " ghost" : "") + (state.hot === n.name ? " hot" : "") + (state.sel === n.name ? " sel" : ""), transform: "translate(" + n.x + "," + n.y + ")" });
      g.style.animationDelay = (i * 0.04) + "s";
      g.appendChild(el("circle", { r: 27, "class": "ring" }));
      g.appendChild(el("circle", { r: 19, "class": "dot", filter: n.ghost ? "" : "url(#agGlow)" }));
      g.appendChild(el("text", { y: 5, "class": "glyph" }, n.ghost ? "?" : "λ"));
      g.appendChild(el("text", { y: 46, "class": "lbl" }, n.name));
      if (!n.ghost) g.appendChild(el("text", { y: 60, "class": "lbl sub" }, "reused " + n.uses + "×"));
      g.appendChild(el("title", {}, n.desc || n.name));
      if (!n.ghost) {
        g.style.cursor = "pointer";
        g.addEventListener("click", function () { state.sel = n.name; drawGraph(); showDetail(); });
      }
      svg.appendChild(g);
    });
    if (!nodes.length) {
      svg.appendChild(el("text", { x: 360, y: 166, "class": "ag-empty" }, "No tools yet."));
      svg.appendChild(el("text", { x: 360, y: 190, "class": "ag-empty sub" }, "Ask for something and the model will write one."));
    }
    $("ag-count").textContent = state.tools.length + (state.tools.length === 1 ? " tool" : " tools");
    drawToolTable();
    var ex = $("ag-call-ex");
    clear(ex);
    var t0 = state.tools.filter(function (t) { return (t.tests || []).length; })[0];
    if (t0) {
      var call = t0.tests[0].call;
      var b = h("button", "ag-chip", "Try: " + call);
      b.addEventListener("click", function () { $("ag-call").value = call; tryCall(); });
      ex.appendChild(b);
    }
  }

  function drawToolTable() {
    var box = $("ag-tools");
    clear(box);
    if (!state.tools.length) return;
    var tbl = h("table", "ag-tbl");
    var head = h("tr");
    ["Tool", "Tests passed", "Reused", "Built from"].forEach(function (c) { head.appendChild(h("th", "", c)); });
    tbl.appendChild(head);
    state.tools.forEach(function (t) {
      var tr = h("tr", "clickable" + (state.sel === t.name ? " selrow" : ""));
      tr.addEventListener("click", function () { state.sel = t.name; drawGraph(); showDetail(); });
      tr.appendChild(h("td", "pr", t.name));
      tr.appendChild(h("td", "good", "\u2713 " + (t.tests || []).length));
      tr.appendChild(h("td", "", (t.uses || 0) + "×"));
      var d = deps(t, state.tools);
      tr.appendChild(h("td", "muted", d.length ? "calls " + d.join(", ") : "scratch"));
      tbl.appendChild(tr);
    });
    box.appendChild(tbl);
  }

  function showDetail() {
    var box = $("ag-detail");
    var t = state.tools.filter(function (x) { return x.name === state.sel; })[0];
    if (!t) { box.hidden = true; return; }
    box.hidden = false;
    clear(box);
    var top = h("div", "dt-top");
    top.appendChild(h("b", "", t.name));
    top.appendChild(h("span", "muted", " — " + (t.description || "")));
    box.appendChild(top);
    box.appendChild(h("pre", "dt-src", t.definition || ""));
    (t.tests || []).forEach(function (x) {
      box.appendChild(h("div", "dt-test", "\u2713 " + x.call + "  →  " + x.expect));
    });
  }

  // ----------------------------------------------------------- REPL pane
  var repl = $("ag-repl");
  function replLine(cls, text) {
    var d = h("div", "rl " + cls, text);
    repl.appendChild(d);
    repl.scrollTop = repl.scrollHeight;
  }
  function setStages(stages) {
    var box = $("ag-stages");
    clear(box);
    stages.forEach(function (s) {
      if (s[1] === "skipped" && ["regression", "property", "differential", "performance"].indexOf(s[0]) >= 0) return;
      box.appendChild(h("span", "pill " + s[1], s[0]));
    });
  }
  function say(text, cls) {
    var d = h("div", "ag-say " + (cls || ""), text);
    $("ag-log").appendChild(d);
    $("ag-log").scrollTop = $("ag-log").scrollHeight;
  }

  function onEvent(ev) {
    switch (ev.kind) {
      case "goal":
        clear(repl); clear($("ag-log")); clear($("ag-stages"));
        $("ag-answer").textContent = "…"; $("ag-answer-call").textContent = "working";
        say("Goal: " + ev.prompt, "goal");
        replLine("c", ";; session " + (state.session || "") + " — " + ev.mode + " model");
        break;
      case "registry":
        replLine("c", ";; loaded " + ev.tools.length + " saved tool(s): " + (ev.tools.join(", ") || "none"));
        break;
      case "retrieval":
        say("Saved tool(s) look relevant: " + ev.tools.join(", ") + ". Trying a short, cheap reuse check first.", "use");
        break;
      case "retrieval_miss":
        say("Reuse check said no. Falling back to the full build prompt.", "model");
        break;
      case "model_call":
        say("Asking the model…", "model");
        break;
      case "model_reply":
        say("Model replied · " + ((ev.input_tokens || 0) + (ev.output_tokens || 0)) + " tokens" +
          (ev.estimated ? " (estimated)" : "") + (ev.cost_usd ? " · $" + ev.cost_usd.toFixed(5) : ""), "model");
        break;
      case "decision":
        if (ev.action === "build") {
          state.ghost = ev.plan && ev.plan.name ? ev.plan.name : "new-tool";
          say("Decision: BUILD “" + state.ghost + "” — " + (ev.plan.description || ""), "build");
          if (ev.plan.definition) { replLine("c", ";; candidate (not saved until its tests pass)"); replLine("def", ev.plan.definition); }
        } else if (ev.action === "cache") {
          say("Decision: CACHED — " + ev.plan.why, "use");
        } else {
          say("Decision: REUSE — " + (ev.plan.why || "existing tool"), "use");
        }
        drawGraph();
        break;
      case "repl":
        replLine("in", "* " + ev.code.split("\n").slice(-1)[0]);
        if (ev.stdout) replLine("out", ev.stdout);
        replLine(ev.ok ? "val" : "err", ev.ok ? "=> " + ev.value : "!! " + (ev.error || "error").split("\n")[0]);
        break;
      case "verdict":
        if (ev.stages) setStages(ev.stages);
        say((ev.ok ? "All tests passed" : "Tests FAILED") + (ev.risk ? " · risk " + ev.risk : "") + (ev.ok ? "" : " — " + ev.reason), ev.ok ? "pass" : "fail");
        break;
      case "repair":
        say("Repair round " + ev.attempt + ": " + ev.reason, "fail");
        break;
      case "promoted":
        say("Saved “" + ev.name + "” to the tool registry", "pass");
        state.ghost = null; state.hot = ev.name;
        api("GET", "/api/agent/tools").then(function (r) { state.tools = r.data.tools || []; drawGraph(); });
        break;
      case "result":
        say("Answer: " + (ev.ok ? ev.value : "failed — " + ev.error), ev.ok ? "answer" : "fail");
        $("ag-answer").textContent = ev.ok ? ev.value : "—";
        $("ag-answer-call").textContent = ev.call;
        break;
      case "error":
        say("Error: " + ev.message, "fail");
        if (/demo model only knows/i.test(ev.message || "") && state.liveOk) {
          var sw = h("button", "ag-chip", "Switch to Live and send it again");
          sw.addEventListener("click", function () { $("ag-mode").value = "live"; setModeNote(); send(); });
          $("ag-log").appendChild(sw);
        } state.ghost = null; drawGraph();
        $("ag-answer").textContent = "—"; $("ag-answer-call").textContent = "no answer";
        break;
      case "done":
        state.ghost = null;
        say("Finished (" + ev.state + ") · " + ev.model_calls + " model call(s) · " +
          ((ev.input_tokens || 0) + (ev.output_tokens || 0)) + " tokens", "done");
        break;
    }
  }

  // --------------------------------------------------------- run control
  function setBusy(b) {
    state.busy = b;
    $("ag-send").disabled = b;
    $("ag-demo").disabled = b;
  }

  function poll(done) {
    if (!state.session) return;
    api("GET", "/api/agent/sessions/" + state.session + "?since=" + state.next).then(function (r) {
      if (r.status !== 200) return;
      var s = r.data;
      s.events.forEach(onEvent);
      state.next = s.next;
      if (s.state !== "running") {
        if (s.compare !== "running") {
          clearInterval(state.timer); state.timer = null;
          setTimeout(function () { state.hot = null; drawGraph(); }, 2500);
          refresh().then(function () { if (done) done(true); });
        } else {
          $("ag-compare-note").textContent = "Checking against a no-memory run…";
        }
      }
    });
  }

  function refresh() {
    var a = api("GET", "/api/agent/tools").then(function (r) { state.tools = r.data.tools || []; drawGraph(); showDetail(); });
    var b = api("GET", "/api/agent/history").then(function (r) { state.history = r.data.sessions || []; });
    return Promise.all([a, b]).then(function () {
      redrawEvidence(); $("ag-compare-note").textContent = "";
      var mains = state.history.filter(function (r) { return r.arm !== "nomem" && r.state !== "running"; });
      var last = mains[mains.length - 1];
      if (last && !state.session && last.result != null) {
        $("ag-answer").textContent = last.result;
        $("ag-answer-call").textContent = "last prompt: " + last.prompt + " (send a prompt to watch the REPL live)";
      }
    });
  }

  function runPrompt(prompt, mode, compare, expected, oracle) {
    state.view = "session"; state.picked = true; state.pick = null;
    setBusy(true);
    return api("POST", "/api/agent/prompt", { prompt: prompt, mode: mode, compare: compare, expected: expected, oracle: oracle }).then(function (r) {
      if (r.status !== 200) { setBusy(false); throw new Error((r.data && r.data.error) || "request failed"); }
      state.session = r.data.session_id; state.next = 0;
      $("ag-prompt").value = prompt;
      return new Promise(function (resolve) {
        if (state.timer) clearInterval(state.timer);
        state.timer = setInterval(function () { poll(function () { resolve(); }); }, 400);
      });
    });
  }

  function send() {
    var prompt = $("ag-prompt").value.trim();
    if (!prompt) { $("ag-prompt").focus(); return; }
    runPrompt(prompt, $("ag-mode").value, $("ag-compare").checked)
      .catch(function (e) { say(e.message, "fail"); })
      .then(function () { setBusy(false); });
  }

  var DEMO_STEPS = [
    { p: "write a function that squares a number, then square 12", e: "144" },
    { p: "sum of squares of the list (3 4 5)", e: "50" },
    { p: "write a function that cubes a number, then cube 4", e: "64" },
    { p: "mean of squares of the list (2 4 6 8)", e: "30" },
    { p: "sum of squares of the list (3 4 5), again", e: "50" },
    { p: "mean of squares of the list (2 4 6 8), again", e: "30" },
    { p: "sum of squares of the list (3 4 5)", e: "50" }
  ];
  function guidedDemo() {
    if ($("ag-demo").disabled) return;
    state.view = "session"; state.picked = true;
    setBusy(true);
    api("POST", "/api/agent/reset").then(function () {
      state.tools = []; state.history = []; state.sel = null; state.view = "session"; showDetail();
      drawGraph(); redrawEvidence();
      var chain = Promise.resolve();
      DEMO_STEPS.forEach(function (p, i) {
        chain = chain.then(function () {
          $("ag-demo").textContent = "Step " + (i + 1) + " of " + DEMO_STEPS.length + "…";
          return runPrompt(p.p, $("ag-mode").value, true, p.e);
        });
      });
      return chain;
    }).catch(function (e) { say("Demo stopped: " + e.message, "fail"); })
      .then(function () {
        setBusy(false);
        $("ag-demo").textContent = "▶ Run guided demo";
      });
  }

  // ----------------------------------------------------------------- init
  var NOTES = {
    demo: "Demo model: scripted and offline, free. It only understands the example chips; use Live for anything else.",
    live: "Live model: real Cerebras Qwen calls (reasoning off, at most 3 per prompt). Comparing with no memory doubles the tokens."
  };
  function setModeNote() {
    var m = $("ag-mode").value; state.mode = m;
    $("ag-mode-note").textContent = NOTES[m];
    $("ag-hero-note").textContent = $("ag-hero-note").textContent || "";
    $("ag-demo-cost").textContent = m === "demo"
      ? "Runs the scripted demo model: 7 prompts, about 1 min, free, estimated tokens"
      : "Runs the live model: 7 prompts, about 2 min, roughly 1 cent of real tokens";
    $("ag-compare").checked = m === "demo";
    $("ag-send").textContent = m === "live" ? "Send prompt (about \u00bd\u00a2)" : "Send prompt";
    paintBadge();
  }
  function paintBadge() {
    var badge = $("ag-badge");
    if (!badge) return;  // provenance now lives in the proof-panel banner
    var hist = curHistory();
    var main = hist.filter(function (r) { return r.arm !== "nomem" && !r.oracle; });
    if (!main.length) { badge.className = "badge " + (state.mode === "live" ? "live" : "demo"); badge.textContent = "No run yet: press the demo"; return; }
    var est = main.some(function (r) { return r.estimated; });
    var rec = state.view !== "session";
    badge.className = "badge " + (est ? "demo" : "live");
    badge.textContent = "Showing: " + (rec ? "recorded live run" : "your session") + " \u00b7 " + (est ? "scripted model, estimated tokens" : "real tokens");
  }
  function loadConfig() {
    api("GET", "/api/agent/config").then(function (r) {
      var live = (r.data || {}).live || {};
      state.liveOk = !!live.available;
      if (!live.available) {
        $("ag-live-opt").disabled = true;
        $("ag-live-opt").textContent = "Live: unavailable";
        NOTES.live = "Live unavailable: " + (live.reason || "unknown");
        $("ag-mode").value = "demo";
      }
      setModeNote();
    });
  }

  function tryCall() {
    var text = $("ag-call").value.trim();
    var out = $("ag-callout");
    if (!text) { $("ag-call").focus(); return; }
    out.className = "ag-callout"; out.textContent = "running…";
    api("POST", "/api/agent/call", { call: text }).then(function (r) {
      var d = r.data || {};
      out.className = "ag-callout " + (d.ok ? "ok" : "bad");
      out.textContent = d.ok ? "=> " + d.value + "   (0 tokens, " + Math.round(d.elapsed_ms || 0) + " ms)" : (d.error || "failed");
    });
  }
  $("ag-callbtn").addEventListener("click", tryCall);
  $("ag-call").addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); tryCall(); } });
  var fab = $("ag-fab");
  if (fab && "IntersectionObserver" in window) {
    fab.style.display = "none";
    var seen = false;
    new IntersectionObserver(function (es) { seen = es[0].isIntersecting; setFab(); }).observe($("ag-prompt"));
    window.addEventListener("scroll", setFab, { passive: true });
    function setFab() { fab.style.display = (!seen && window.scrollY > 500 && window.innerWidth < 760) ? "block" : "none"; }
  }
  if (fab) fab.addEventListener("click", function () { $("ag-jump").click(); });
  $("ag-jump").addEventListener("click", function () {
    var t = $("ag-prompt"); t.scrollIntoView({ behavior: "smooth", block: "center" }); t.focus();
  });
  $("ag-send").addEventListener("click", send);
  $("ag-demo").addEventListener("click", guidedDemo);
  $("ag-prompt").addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  Array.prototype.forEach.call(document.querySelectorAll(".ag-chip"), function (c) {
    c.addEventListener("click", function () { $("ag-prompt").value = c.getAttribute("data-p"); $("ag-prompt").focus(); });
  });
  $("ag-mode").addEventListener("change", setModeNote);
  var lastW = window.innerWidth, rt = null;
  window.addEventListener("resize", function () {
    clearTimeout(rt);
    rt = setTimeout(function () {
      if ((lastW < 640) !== (window.innerWidth < 640)) { redrawEvidence(); }
      lastW = window.innerWidth;
    }, 150);
  });
  $("ag-reset").addEventListener("click", function () {
    if (!window.confirm("Forget all learned tools and the prompt log? This cannot be undone.")) return;
    api("POST", "/api/agent/reset").then(function () {
      state.tools = []; state.history = []; state.sel = null; showDetail(); drawGraph(); redrawEvidence();
    });
  });
  document.querySelector('nav button[data-view="agent"]').addEventListener("click", refresh);
  // Thesis tab buttons: jump to the Agent tab, optionally fill a prompt or start the demo.
  Array.prototype.forEach.call(document.querySelectorAll("[data-goto]"), function (b) {
    b.addEventListener("click", function () {
      document.querySelector('nav button[data-view="' + b.getAttribute("data-goto") + '"]').click();
      var p = b.getAttribute("data-p");
      if (p) { $("ag-prompt").value = p; $("ag-prompt").focus(); }
      if (b.getAttribute("data-action") === "demo") guidedDemo();
      window.scrollTo(0, 0);
    });
  });

  drawGraph(); redrawEvidence();
  replLine("c", ";; REPL output appears here when you send a prompt or run the demo.");
  if (window.location.protocol !== "file:") { refresh(); loadConfig(); loadSnapshots(); }
})();
