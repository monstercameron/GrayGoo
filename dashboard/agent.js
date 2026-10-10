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
    vp: { x: 0, y: 0, k: 1 }, vpTouched: false, vpMoved: false, gdims: { w: 640, h: 340 },
    snapshots: [], view: "session", liveOk: false, busy: false, picked: false, rows: [], pick: null,
    project: "scratch", projects: [], projMode: null, projPending: false
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
    // Prediction for a prompt with no measured no-memory run: answering it
    // without the saved tool would mean building that tool again, so it costs
    // what the build cost (or the average build, when the builder is not in view).
    var builds = rows.filter(function (x) { return kind(x.r) === "build" && tok(x.r) > 0; });
    var avgBuild = builds.length ? builds.reduce(function (a, x) { return a + tok(x.r); }, 0) / builds.length : null;
    function predicted(x) {
      var k = kind(x.r);
      if (k !== "reuse" && k !== "cached") return tok(x.r);
      var maker = rows.filter(function (y) { return y.r.promoted && y.r.promoted === x.r.tool && tok(y.r) > 0; })[0];
      if (maker) return tok(maker.r);
      return avgBuild != null ? avgBuild : (avgTwin != null ? avgTwin : tok(x.r));
    }
    rows.forEach(function (x) {
      x.base = x.twin ? tok(x.twin) : predicted(x);
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

  // ---- tokens per prompt, over time (line chart) -------------------------
  // Money and time for a row. Measured values are used where the run recorded
  // them; a prediction scales the row's tokens by the rates measured elsewhere.
  function secondsOf(r) {
    if (!r) return null;
    if (r.wall_s != null) return r.wall_s;
    return r.model_ms ? r.model_ms / 1000 : null;
  }
  function rates(rows) {
    var tk = 0, usd = 0, tks = 0, sec = 0;
    rows.forEach(function (x) {
      [x.r, x.twin].forEach(function (r) {
        if (!r) return;
        var t = tok(r), s = secondsOf(r);
        if (t && r.cost_usd) { tk += t; usd += r.cost_usd; }
        if (t && s != null) { tks += t; sec += s; }
      });
    });
    return { usdPerTok: tk ? usd / tk : 0, secPerTok: tks ? sec / tks : null };
  }
  function seriesOf(rows) {
    var rt = rates(rows);
    return rows.map(function (x) {
      var used = tok(x.r), s = secondsOf(x.r), bs = secondsOf(x.twin);
      return {
        x: x, k: kind(x.r), used: used, base: x.base, measured: x.baseMeasured, failedTwin: x.twinFailed,
        cost: x.r.cost_usd || 0,
        baseCost: x.twin ? (x.twin.cost_usd || 0) : x.base * rt.usdPerTok,
        secs: s != null ? s : (rt.secPerTok != null ? used * rt.secPerTok : null),
        baseSecs: bs != null ? bs : (rt.secPerTok != null ? x.base * rt.secPerTok : null)
      };
    });
  }
  // The next few prompts if the pattern holds: without tools every prompt costs
  // the average so far; with tools, the average of the latest reuse/repeat prompts.
  function forecast(data, n) {
    if (data.length < 3) return [];
    var reuse = data.filter(function (d) { return d.k === "reuse" || d.k === "cached"; }).slice(-3);
    if (!reuse.length) return [];
    var mean = function (arr, f) { return arr.reduce(function (a, d) { return a + f(d); }, 0) / arr.length; };
    var out = [];
    for (var i = 0; i < n; i++) out.push({ base: mean(data, function (d) { return d.base; }), used: mean(reuse, function (d) { return d.used; }) });
    return out;
  }
  function money(v) { return v >= 1 ? "$" + v.toFixed(2) : (v >= 0.01 ? "$" + v.toFixed(3) : "$" + v.toFixed(4)); }
  function secs(v) { return v == null ? "n/a" : (v >= 10 ? Math.round(v) + " s" : v.toFixed(1) + " s"); }

  function drawSavings(data) {
    var box = $("ag-savings");
    clear(box);
    if (!data.length) return;
    var sum = function (f) { return data.reduce(function (a, d) { return a + (f(d) || 0); }, 0); };
    var measured = data.filter(function (d) { return d.measured; }).length;
    var note = measured === data.length ? "baseline measured on every prompt"
      : "baseline measured on " + measured + " of " + data.length + " prompts, predicted for the rest";
    var reused = data.some(function (d) { return d.k === "reuse" || d.k === "cached"; });
    if (!measured && !reused) {
      // every prompt so far built something new: there is nothing to compare against yet
      var none = h("div", "sv");
      none.appendChild(h("div", "sv-l", "Savings"));
      none.appendChild(h("div", "sv-v", "none yet"));
      none.appendChild(h("div", "sv-s", "Every prompt here built something new, so it cost what it would cost without saved tools (" +
        fmt(sum(function (d) { return d.used; })) + " tokens so far). Savings appear when a later prompt reuses these tools, or tick \"Also run without memory\" to measure the baseline."));
      box.appendChild(none);
      box.style.gridTemplateColumns = "1fr";
      return;
    }
    box.style.gridTemplateColumns = "";
    function tile(label, base, used, show, extra) {
      var d = h("div", "sv");
      d.appendChild(h("div", "sv-l", label));
      var v = h("div", "sv-v", show(Math.abs(base - used)) + (base >= used ? " saved" : " more"));
      if (base > 0) v.appendChild(h("small", "", Math.round(100 * Math.abs(base - used) / base) + "% " + (base >= used ? "less" : "more")));
      d.appendChild(v);
      d.appendChild(h("div", "sv-s", show(used) + " with saved tools vs " + show(base) + " without. " + extra));
      box.appendChild(d);
    }
    tile("Tokens", sum(function (d) { return d.base; }), sum(function (d) { return d.used; }), fmt, note + ".");
    var baseCost = sum(function (d) { return d.baseCost; }), cost = sum(function (d) { return d.cost; });
    if (baseCost > 0 || cost > 0) {
      tile("Cost", baseCost, cost, money, "Real API prices; " + note + ".");
    } else {
      var free = h("div", "sv");
      free.appendChild(h("div", "sv-l", "Cost"));
      free.appendChild(h("div", "sv-v", "free"));
      free.appendChild(h("div", "sv-s", "The scripted demo model costs nothing. Switch to Live to see dollars."));
      box.appendChild(free);
    }
    if (data.some(function (d) { return d.secs != null && d.baseSecs != null; })) {
      tile("Waiting time", sum(function (d) { return d.baseSecs; }), sum(function (d) { return d.secs; }), secs,
        "Time from sending a prompt to its answer; " + note + ".");
    } else {
      var na = h("div", "sv");
      na.appendChild(h("div", "sv-l", "Waiting time"));
      na.appendChild(h("div", "sv-v", "n/a"));
      na.appendChild(h("div", "sv-s", "This recording did not store timings. New runs do."));
      box.appendChild(na);
    }
  }

  function drawPairs(rows) {
    var svg = $("ag-pairs"), tip = $("ag-linetip");
    clear(svg);
    tip.hidden = true;
    var wrapW = (svg.parentNode && svg.parentNode.clientWidth) || 760;
    var W = Math.max(340, Math.min(1100, wrapW)), H = 290;
    var narrow = W < 560;
    var pl = 46, pr = narrow ? 14 : 96, pt = 18, pb = 44;
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    var demo = rows.length === 0;
    var data = demo
      ? [[330, 330, "build"], [420, 420, "build"], [380, 380, "build"], [410, 410, "build"], [140, 1150, "reuse"], [140, 1150, "reuse"], [0, 1150, "cached"]]
          .map(function (g) { return { ghost: true, k: g[2], used: g[0], base: g[1], measured: true }; })
      : seriesOf(rows);
    drawSavings(demo ? [] : data);
    var fc = demo ? [] : forecast(data, narrow ? 2 : 3);
    var n = data.length, total = n + fc.length;
    var maxV = niceMax(data.concat(fc).reduce(function (a, d) { return Math.max(a, d.used, d.base); }, 0));
    function X(i) { return total === 1 ? (pl + W - pr) / 2 : pl + (W - pl - pr) * i / (total - 1); }
    function Y(v) { return H - pb - (H - pb - pt) * v / maxV; }
    var op = demo ? 0.3 : 1;

    for (var t = 0; t <= 4; t++) {
      var y = Y(maxV * t / 4);
      svg.appendChild(el("line", { x1: pl, x2: W - pr, y1: y, y2: y, "class": "ag-grid" }));
      svg.appendChild(el("text", { x: pl - 8, y: y + 4, "class": "ag-tick", "text-anchor": "end" }, fmt(maxV * t / 4)));
    }
    if (fc.length) {
      var fx = (X(n - 1) + X(n)) / 2;
      svg.appendChild(el("rect", { x: fx, y: pt, width: W - pr - fx + 6, height: H - pb - pt, "class": "ln-forecast" }));
      svg.appendChild(el("text", { x: fx + 6, y: pt + 12, "class": "ln-kind" }, "FORECAST"));
    }
    var all = data.concat(fc);
    function path(key, from, to) {
      var d = "";
      for (var i = from; i <= to; i++) d += (i === from ? "M" : "L") + X(i).toFixed(1) + "," + Y(all[i][key]).toFixed(1) + " ";
      return d;
    }
    if (n > 1) {
      // the gap between the lines is what the saved tools saved
      var area = path("base", 0, n - 1);
      for (var q = n - 1; q >= 0; q--) area += "L" + X(q).toFixed(1) + "," + Y(all[q].used).toFixed(1) + " ";
      svg.appendChild(el("path", { d: area + "Z", "class": "ln-gap", opacity: demo ? 0.05 : 0.13 }));
      // baseline: solid between two measured points, dashed where either end is predicted
      for (var i = 1; i < n; i++) {
        var solid = data[i].measured && data[i - 1].measured;
        svg.appendChild(el("path", { d: path("base", i - 1, i), "class": "ln-nomem" + (solid ? "" : " pred"), opacity: op }));
      }
      svg.appendChild(el("path", { d: path("used", 0, n - 1), "class": "ln-used", opacity: op }));
    }
    if (fc.length) {
      svg.appendChild(el("path", { d: path("base", n - 1, total - 1), "class": "ln-nomem pred", opacity: 0.6 }));
      svg.appendChild(el("path", { d: path("used", n - 1, total - 1), "class": "ln-used", "stroke-dasharray": "5 4", opacity: 0.6 }));
    }
    var step = Math.max(1, Math.ceil(n / (narrow ? 7 : 16)));
    data.forEach(function (d, i) {
      svg.appendChild(el("circle", { cx: X(i), cy: Y(d.base), r: 4, "class": "ln-pt " + (d.measured ? "nomem" : "hollow"), opacity: op }));
      svg.appendChild(el("circle", { cx: X(i), cy: Y(d.used), r: 4.5, "class": "ln-pt used", opacity: op }));
      if (!demo && (i % step === 0 || i === n - 1)) {
        var anchor = narrow && n > 1 ? (i === 0 ? "start" : (i === n - 1 && !fc.length ? "end" : "middle")) : "middle";
        svg.appendChild(el("text", { x: X(i), y: H - pb + 16, "class": "ag-tick", "text-anchor": anchor }, "#" + (i + 1)));
        if (!narrow || n <= 8) svg.appendChild(el("text", { x: X(i), y: H - pb + 30, "class": "ln-kind", "text-anchor": anchor }, (PHASE[d.k] || PHASE.build)[0]));
      }
    });
    if (!demo && n && !narrow) {
      // direct labels at the right end of each line (text ink, not series colour)
      var last = all[total - 1], yb = Y(last.base), yu = Y(last.used);
      if (Math.abs(yb - yu) < 14) { yb -= 7; yu += 7; }
      svg.appendChild(el("text", { x: X(total - 1) + 8, y: yb + 4, "class": "ln-lab" }, "without " + fmt(last.base)));
      svg.appendChild(el("text", { x: X(total - 1) + 8, y: yu + 4, "class": "ln-lab" }, "with " + fmt(last.used)));
    }
    if (demo) {
      svg.appendChild(el("text", { x: W / 2, y: pt + 40, "class": "ag-empty", "text-anchor": "middle" }, "Illustration, not data"));
      svg.appendChild(el("text", { x: W / 2, y: pt + 62, "class": "ag-empty sub", "text-anchor": "middle" }, "Run the guided demo: the blue line should drop while the red one stays high."));
      return;
    }
    // hover: crosshair plus a tooltip with tokens, cost and time for that prompt
    var cross = el("line", { y1: pt, y2: H - pb, "class": "ln-cross", visibility: "hidden" });
    svg.appendChild(cross);
    function show(i) {
      var d = data[i];
      cross.setAttribute("x1", X(i)); cross.setAttribute("x2", X(i)); cross.setAttribute("visibility", "visible");
      clear(tip);
      tip.appendChild(h("div", "tt-p", "#" + (i + 1) + "  " + d.x.r.prompt));
      var tbl = h("table");
      function row(label, a, b) {
        var tr = h("tr");
        tr.appendChild(h("td", "tt-k", label)); tr.appendChild(h("td", "n", a)); tr.appendChild(h("td", "n", b));
        tbl.appendChild(tr);
      }
      row("", "with tools", "without");
      row("tokens", fmt(d.used), fmt(d.base));
      if (d.cost || d.baseCost) row("cost", money(d.cost), money(d.baseCost));
      if (d.secs != null && d.baseSecs != null) row("time", secs(d.secs), secs(d.baseSecs));
      tip.appendChild(tbl);
      var kindText = { build: "built a new tool", reuse: "reused a saved tool", cached: "exact repeat, no model call", failed: "this run failed" }[d.k];
      tip.appendChild(h("div", "tt-k", kindText + " · baseline " + (d.failedTwin ? "measured, and that run failed" : (d.measured ? "measured" : "predicted"))));
      tip.hidden = false;
      var px = X(i) / W * wrapW;
      tip.style.left = Math.max(4, Math.min(wrapW - tip.offsetWidth - 4, px + (px > wrapW / 2 ? -tip.offsetWidth - 12 : 12))) + "px";
    }
    function hide() { cross.setAttribute("visibility", "hidden"); tip.hidden = true; }
    var bandW = n > 1 ? (X(1) - X(0)) : (W - pl - pr);
    data.forEach(function (d, i) {
      var hit = el("rect", { x: X(i) - bandW / 2, y: pt, width: bandW, height: H - pb - pt, "class": "ln-hit", tabindex: 0, role: "button",
        "aria-label": "Prompt " + (i + 1) + ": " + d.used + " tokens with saved tools, " + Math.round(d.base) + " without" });
      hit.addEventListener("mouseenter", function () { show(i); });
      hit.addEventListener("mousemove", function () { show(i); });
      hit.addEventListener("focus", function () { show(i); });
      hit.addEventListener("mouseleave", hide);
      hit.addEventListener("blur", hide);
      hit.addEventListener("click", function () { showBarInfo(i); });
      hit.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); showBarInfo(i); } });
      svg.appendChild(hit);
    });
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
      tr.addEventListener("click", function () { setPrompt(x.r.prompt); $("ag-prompt").focus(); });
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
      state.cancelled = false;
      tasks.forEach(function (t, i) {
        chain = chain.then(function () {
          if (state.cancelled) return;   // a cancelled build ends the check: no next prompt
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
      // The recorded evidence run is Scratchpad's opening view only: a project always
      // opens on its own prompts.
      if (!state.picked && state.view === "session" && currentProject().builtin && state.snapshots.length) {
        var rec = state.snapshots.filter(function (x) { return x.kind === "recorded" && x.runs && x.runs.length > 1; })[0] ||
                  state.snapshots.filter(function (x) { return x.kind === "recorded"; })[0];
        if (rec) state.view = rec.id;
      }
      redrawEvidence(); thesisStrip(); machinerySection(); sampleRepl();
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

  // ------------------------------------------------- machinery section (Thesis)
  // "The machinery specialises too": block A replays the harness's free repairs on
  // logged replies, block B shows what one saved function cost by period, block C
  // what each specialization saved. Every figure comes from /api/agent/machinery.
  var mxSeq = 0;
  var MX_BASIS = { measured: ["measured", "mx-b-measured"], estimate: ["estimate", "mx-b-estimate"], count: ["counted", "mx-b-counted"] };
  function mxInt(n) { return typeof n === "number" ? Math.round(n).toLocaleString("en-US") : "—"; }
  function mxPct(x) { return typeof x === "number" ? Math.round(100 * x) + "%" : "—"; }
  function mxPl(n, one, many) { return n === 1 ? one : many; }
  function mxClamp(p) { return typeof p === "number" && isFinite(p) ? Math.max(0, Math.min(100, p)) : 0; }
  // Dollars: two decimals from 10 cents up, four below (a per-function cost is a fraction of a cent).
  function mxUsd(v) { return typeof v === "number" ? "$" + v.toFixed(v >= 0.1 ? 2 : 4) : "—"; }
  // Seconds: "40 s" under 90 s, otherwise minutes with one decimal.
  function mxSec(s) { return typeof s === "number" ? (s < 90 ? Math.round(s) + " s" : (s / 60).toFixed(1) + " min") : "—"; }
  function mxBlock(title, nodes) {
    var b = h("div", "mx-block");
    b.appendChild(h("h3", "mx-sub", title));
    nodes.forEach(function (n) { b.appendChild(n); });
    return b;
  }
  function mxBarRow(label, rate, cls) {
    var row = h("div", "mx-bar");
    row.appendChild(h("div", "mx-bar-label", label));
    var track = h("div", "mx-track");
    track.setAttribute("role", "img");
    track.setAttribute("aria-label", label);
    var fill = h("span", "mx-fill " + cls);
    fill.style.width = mxClamp(typeof rate === "number" ? 100 * rate : 0) + "%";
    track.appendChild(fill);
    row.appendChild(track);
    return row;
  }
  // Block A: both bars share one 0-100% scale, each labelled in words.
  function mxReplay(rp) {
    if (!rp) return [h("p", "muted", "Not measured yet. Run: uv run python replay_evidence.py")];
    var out = [];
    var bars = h("div", "mx-bars");
    bars.appendChild(mxBarRow("As the model wrote them: " + mxPct(rp.raw_rate) + " pass their own tests (" + mxInt(rp.raw_pass) + " of " + mxInt(rp.cases) + ")", rp.raw_rate, "mx-fill-raw"));
    bars.appendChild(mxBarRow("After today’s free repairs: " + mxPct(rp.fixed_rate) + " pass (" + mxInt(rp.fixed_pass) + " of " + mxInt(rp.cases) + ")", rp.fixed_rate, "mx-fill-after"));
    var axis = h("div", "mx-axis");
    axis.setAttribute("aria-hidden", "true");
    axis.appendChild(h("span", "", "0%"));
    axis.appendChild(h("span", "", "100%"));
    bars.appendChild(axis);
    out.push(bars);
    var rescued = rp.rescued || 0, broken = rp.broken || 0;
    out.push(h("p", "mx-note", mxInt(rescued) + " " + mxPl(rescued, "reply was", "replies were") + " rescued without a model call, worth about " + mxUsd(rp.rescued_usd) + " of repair calls."));
    if (broken > 0) {
      var caution = h("p", "mx-caution");
      caution.appendChild(h("span", "mx-tag", "Defect to fix"));
      caution.appendChild(document.createTextNode(mxInt(broken) + " " + mxPl(broken, "reply passed", "replies passed") + " as written and " + mxPl(broken, "fails", "fail") + " after repair."));
      out.push(caution);
    }
    var fd = rp.by_kind && rp.by_kind["first drafts"];
    if (fd && fd.cases > 0) {
      out.push(h("p", "mx-note", "First drafts alone: " + mxPct(fd.raw_pass / fd.cases) + " pass as written, " + mxPct(fd.fixed_pass / fd.cases) +
        " after the free repairs (" + mxInt(fd.cases) + " replies)."));
    }
    // a docstring added by the harness cannot change a test result, so it is not listed as a rescuer
    var fixes = (rp.by_fix || []).filter(function (f) { return f.fix !== "added-docstring"; }).sort(function (a, b) { return (b.rescued || 0) - (a.rescued || 0); }).slice(0, 6);
    if (fixes.length) {
      out.push(h("h4", "mx-kicker", "Which repairs were on the rescued replies (a reply can get several)"));
      var list = h("ul", "mx-fixlist");
      fixes.forEach(function (f) {
        var li = h("li");
        li.appendChild(h("code", "", String(f.fix)));
        li.appendChild(document.createTextNode(" " + mxInt(f.rescued) + " rescued "));
        li.appendChild(h("span", "muted", "of " + mxInt(f.cases) + " cases"));
        list.appendChild(li);
      });
      out.push(list);
    }
    out.push(h("p", "muted mx-src", mxInt(rp.cases) + " replies from " + mxInt(rp.sessions) + " builds, each judged as written and again after the free repairs."));
    return out;
  }
  // One cell: the number with its unit, and (for per-function columns) a thin bar under it.
  function mxCell(label, text, frac) {
    var td = h("td", "mx-num");
    td.setAttribute("data-label", label);
    var cell = h("span", "mx-cell");
    cell.appendChild(h("span", "mx-val", text));
    if (typeof frac === "number") {
      var meter = h("span", "mx-meter");
      meter.setAttribute("aria-hidden", "true");
      var fill = h("span", "mx-meter-fill");
      fill.style.width = mxClamp(100 * frac) + "%";
      meter.appendChild(fill);
      cell.appendChild(meter);
    }
    td.appendChild(cell);
    return td;
  }
  // Block B: a real table; each per-function column is scaled to its own maximum.
  function mxPeriods(rep) {
    if (!rep) return [h("p", "muted", "No live builds logged yet.")];
    // app-sized builds only: early one-function builds would otherwise set the baseline
    var ps = rep.app_periods || [];
    if (ps.length < 2) return [h("p", "muted", "Too few app-sized builds to compare periods yet.")];
    var top = { paid: 0, free: 0, usd: 0 };
    ps.forEach(function (p) {
      top.paid = Math.max(top.paid, p.repairs_per_function || 0);
      top.free = Math.max(top.free, p.free_repairs_per_function || 0);
      top.usd = Math.max(top.usd, p.usd_per_function || 0);
    });
    function fracOf(v, max) { return typeof v === "number" && max > 0 ? v / max : null; }
    function per(v, unit) { return typeof v === "number" ? v.toFixed(2) + " " + unit : "\u2014"; }
    var COLS = ["Period", "Builds", "Functions saved", "Paid repair calls per function", "Free repairs per function",
                "Dollars per function", "Passed first try"];
    var head = h("tr");
    COLS.forEach(function (name, i) {
      var th = h("th", i ? "mx-num" : "", name);
      th.setAttribute("scope", "col");
      head.appendChild(th);
    });
    var tbody = h("tbody");
    ps.forEach(function (p) {
      var tr = h("tr");
      var rowHead = h("th", "", String(p.label));
      rowHead.setAttribute("scope", "row");
      tr.appendChild(rowHead);
      tr.appendChild(mxCell(COLS[1], mxInt(p.builds) + " " + mxPl(p.builds, "build", "builds"), null));
      tr.appendChild(mxCell(COLS[2], mxInt(p.functions_saved) + " " + mxPl(p.functions_saved, "function", "functions"), null));
      tr.appendChild(mxCell(COLS[3], per(p.repairs_per_function, "calls"), fracOf(p.repairs_per_function, top.paid)));
      tr.appendChild(mxCell(COLS[4], per(p.free_repairs_per_function, "repairs"), fracOf(p.free_repairs_per_function, top.free)));
      tr.appendChild(mxCell(COLS[5], mxUsd(p.usd_per_function), fracOf(p.usd_per_function, top.usd)));
      tr.appendChild(mxCell(COLS[6], mxPct(p.first_try_rate), null));
      tbody.appendChild(tr);
    });
    var thead = h("thead");
    thead.appendChild(head);
    var table = h("table", "mx-table");
    table.appendChild(thead);
    table.appendChild(tbody);
    var wrap = h("div", "mx-scroll");
    wrap.tabIndex = 0;
    wrap.setAttribute("role", "region");
    wrap.setAttribute("aria-label", "What one saved function cost, period by period");
    wrap.appendChild(table);
    return [wrap, h("p", "muted mx-src", "Builds that saved three or more functions, oldest first. A paid repair is a model call that fixes a failed function; " +
      "a free repair is one the harness made itself. The prompts differ from period to period, so this table describes the builds and does not prove a trend. " +
      "The like-for-like evidence is the replay above.")];
  }
  // Block C: one row per specialization, then the total from report.saved.
  function mxMechanisms(rep) {
    if (!rep) return [h("p", "muted", "No live builds logged yet.")];
    var out = [h("p", "muted mx-src", "Across " + mxInt(rep.builds) + " " + mxPl(rep.builds, "build", "builds") + ": " + mxInt(rep.model_calls) + " model calls, " + mxUsd(rep.spent_usd) + " spent and " + mxSec(rep.seconds) + " of model time.")];
    var ms = rep.mechanisms || [];
    if (!ms.length) {
      out.push(h("p", "muted", "No specialization has logged a saving yet."));
    } else {
      var list = h("ul", "mx-mech");
      ms.forEach(function (m) {
        var li = h("li", "mx-mech-row");
        var main = h("div");
        var name = h("div", "mx-mech-name");
        name.appendChild(h("b", "", String(m.label)));
        var basis = MX_BASIS[m.basis] || [String(m.basis || "unknown"), "mx-b-counted"];
        name.appendChild(h("span", "mx-basis " + basis[1], basis[0]));
        main.appendChild(name);
        if (m.how) main.appendChild(h("p", "muted mx-how", String(m.how)));
        li.appendChild(main);
        var nums = h("div", "mx-mech-nums");
        nums.appendChild(h("span", "mx-n", mxInt(m.events) + " " + mxPl(m.events, "event", "events")));
        if (typeof m.saved_calls === "number") nums.appendChild(h("span", "mx-n", mxInt(m.saved_calls) + " " + mxPl(m.saved_calls, "call", "calls")));
        if (typeof m.saved_usd === "number") nums.appendChild(h("span", "mx-n", mxUsd(m.saved_usd)));
        if (typeof m.saved_seconds === "number") nums.appendChild(h("span", "mx-n", mxSec(m.saved_seconds)));
        if (m.saved_calls == null && m.saved_usd == null && m.saved_seconds == null) nums.appendChild(h("span", "muted", "no saving figure"));
        li.appendChild(nums);
        list.appendChild(li);
      });
      out.push(list);
    }
    var sv = rep.saved || {};
    var parts = [];
    if (typeof sv.calls === "number") parts.push(mxInt(sv.calls) + " model " + mxPl(sv.calls, "call", "calls"));
    if (typeof sv.usd === "number") parts.push(mxUsd(sv.usd));
    if (typeof sv.seconds === "number") parts.push(mxSec(sv.seconds));
    if (parts.length) {
      var total = parts.length > 1 ? parts.slice(0, -1).join(", ") + " and " + parts[parts.length - 1] : parts[0];
      out.push(h("p", "mx-total", "Together: about " + total + " not spent."));
    }
    return out;
  }
  function paintMachinery(sec, rep, rp, failed) {
    clear(sec);
    var title = h("h2", "", "The machinery specialises too");
    title.id = "mx-title";
    sec.appendChild(title);
    if (failed) { sec.appendChild(h("p", "muted", "Could not load these figures from the dashboard server.")); return; }
    if (!rep && !rp) { sec.appendChild(h("p", "muted", "No live builds logged yet.")); return; }
    sec.appendChild(h("p", "mx-lede", "Each failure the harness has seen became a free repair, a shorter prompt or a faster check. These figures are read from the logs of real builds. A figure that is an estimate says so."));
    sec.appendChild(mxBlock("Same replies, with and without the harness’s repairs", mxReplay(rp)));
    sec.appendChild(mxBlock("What one saved function cost, period by period", mxPeriods(rep)));
    sec.appendChild(mxBlock("What each specialization saved", mxMechanisms(rep)));
  }
  function machinerySection() {
    var sec = $("th-machinery");
    if (!sec) return;
    var my = ++mxSeq;   // a slower earlier response must not overwrite a newer one
    api("GET", "/api/agent/machinery").then(function (r) {
      if (my !== mxSeq) return;
      var d = (r && r.data) || {};
      paintMachinery(sec, d.report || null, d.replay || null, false);
    }, function () {
      if (my === mxSeq) paintMachinery(sec, null, null, true);
    });
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

  // ---------------------------------------------------------------- graph
  // One persistent SVG scene. Nodes and edges are created once and then updated in
  // place, so animations survive events, and pan/zoom is never reset by a redraw.
  // The canvas has a fixed CSS size; the viewBox is the canvas's own pixel size and
  // the single group #agVp (translate + scale) is what pan, zoom and fit change.
  var NW = 184, NH = 64, COLW = 244, ROWH = 82, KW = 200, KH = 76;
  var ENTRY_NAMES = { "handle-request": 1, "handle-command": 1 };
  var VP_MIN = 0.08, VP_MAX = 4;
  function reduced() { return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches); }
  // A long name on two lines, broken at a hyphen where possible: ["render-post-", "row"].
  function wrapName(name, max) {
    name = String(name || "");
    if (name.length <= max) return [name, ""];
    var cut = name.lastIndexOf("-", max - 1);
    cut = cut >= Math.floor(max / 3) ? cut + 1 : max;
    return [name.slice(0, cut), trunc(name.slice(cut), max)];
  }
  function trunc(s, n) { s = String(s == null ? "" : s); return s.length > n ? s.slice(0, n - 1) + "…" : s; }
  function ease(p) { return p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2; }
  function nowMs() { return window.performance && performance.now ? performance.now() : Date.now(); }

  // 24x24 stroke icons, drawn as inline SVG paths (no emoji, no icon font)
  var ICON = {
    "pure": "M7 4h3l7 16 M13.5 12L7 20",
    "reads-request": "M12 3v11 M7.5 9.5L12 14l4.5-4.5 M4 15v5h16v-5",
    "reads-state": "M12 9V2 M9.5 4.5L12 2l2.5 2.5 M5 13c0-1.7 3.1-3 7-3s7 1.3 7 3-3.1 3-7 3-7-1.3-7-3z M5 13v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6",
    "writes-state": "M12 2v7 M9.5 6.5L12 9l2.5-2.5 M5 15c0-1.7 3.1-3 7-3s7 1.3 7 3-3.1 3-7 3-7-1.3-7-3z M5 15v4c0 1.7 3.1 3 7 3s7-1.3 7-3v-4",
    "http-response": "M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18 M3 12h18 M12 3c3.2 3 3.2 15 0 18 M12 3c-3.2 3-3.2 15 0 18",
    "sets-cookie": "M12 3a9 9 0 1 0 9 9a4 4 0 0 1-4-4a4 4 0 0 1-5-5z M8.5 11h.01 M12 16h.01 M15.5 13h.01 M9 15.5h.01",
    "uses-time": "M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18 M12 7v5l3 2",
    "uses-random": "M5 5h14v14H5z M9 9h.01 M15 9h.01 M12 12h.01 M9 15h.01 M15 15h.01",
    "entry-point": "M3 12h11 M10 7l5 5-5 5 M20 4v16",
    "kit": "M4 8h16v11H4z M9 8V5h6v3 M4 13h16",
    "cpu": "M7 7h10v10H7z M10 3v4 M14 3v4 M10 17v4 M14 17v4 M3 10h4 M3 14h4 M17 10h4 M17 14h4",
    "disk": "M3 14h18v6H3z M3 14l3-8h12l3 8 M17 17h.01",
    "network": "M2 9a15 15 0 0 1 20 0 M5.5 12.5a10 10 0 0 1 13 0 M9 16a5 5 0 0 1 6 0 M12 19.5h.01",
    "plan": "M6 12h.01 M12 12h.01 M18 12h.01",
    "fail": "M6 6l12 12 M18 6L6 18",
    "fix": "M20 12a8 8 0 1 1-2.3-5.7 M20 4v5h-5",
    "unused": "M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18 M5.6 5.6l12.8 12.8",
    "other":"M12 8a4 4 0 1 0 0 8a4 4 0 1 0 0-8"
  };
  var FX = {
    "entry-point": { label: "Entry point", text: "the function the harness calls for every request or command (handle-request / handle-command)" },
    "kit": { label: "Supplied by the harness", text: "supplied by the harness (the web kit), not written by the model" },
    "writes-state": { label: "Writes state", text: "returns new app state, which the harness saves to disk (SQLite) when the app is mounted" },
    "http-response": { label: "HTTP response", text: "produces an HTTP response (page or redirect) sent over the network" },
    "sets-cookie": { label: "Sets cookie", text: "sets a browser cookie" },
    "reads-state": { label: "Reads state", text: "reads the app’s stored data (the state the harness keeps in SQLite)" },
    "reads-request": { label: "Reads request", text: "reads the incoming request (method, path, form, cookies)" },
    "uses-time": { label: "Uses time", text: "depends on the current time passed in with the request" },
    "uses-random": { label: "Uses random", text: "depends on the random nonce passed in with the request" },
    "pure": { label: "Pure function", text: "result depends only on its arguments; no outside data at all" }
  };
  var FX_ORDER = ["entry-point", "kit", "writes-state", "http-response", "sets-cookie", "reads-state", "reads-request", "uses-time", "uses-random", "pure"];
  var LAT = {
    cpu: { label: "CPU", text: "the Lisp evaluation itself" },
    disk: { label: "Disk", text: "state is persisted" },
    network: { label: "Network", text: "request and response travel over HTTP" }
  };
  var ST_LEGEND = {
    pending: { icon: "plan", label: "Planned", text: "dashed box: planned, not built yet" },
    active: { icon: "plan", label: "Building now", text: "moving dashed ring: the agent is writing and testing it" },
    failed: { icon: "fail", label: "Gave up", text: "red box with a cross: it never passed its tests, nothing was saved" },
    notbuilt: { icon: "plan", label: "Not built", text: "dashed box: the run ended before this one was built" },
    repairs: { icon: "fix", label: "Repair rounds", text: "the badge “fix 2” means the tests failed and the code was repaired twice" }
  };

  var G = {
    nodes: {}, edges: {}, seq: 0, plan: [], failed: {}, promoted: {}, spawn: {}, animate: {}, kitNew: null,
    pulses: [], active: null, running: false, finished: false, replay: false, dragging: false,
    stepI: 0, stepN: 0, stepSub: false, splitOf: null, note: "", dirty: false, scene: null,
    bounds: null, raf: 0, guard: 0, fxEnd: 0, lit: null, legendSig: "", vpAnim: null, lastFit: "",
    ready: false, vw: 0, vh: 0, namesSig: "", reloading: false, reloadAgain: false, touchedAt: 0, split: {}, rs: {}, noteBad: false, selSig: "", lastSaved: null, reloadP: null, lanes: {}, parallel: 0
  };

  function iconPath(key) { return el("path", { d: ICON[key] || ICON.other }); }
  function iconG(key, x, y, size, cls) {
    var g = el("g", { transform: "translate(" + (x - size / 2).toFixed(1) + "," + (y - size / 2).toFixed(1) + ") scale(" + (size / 24).toFixed(3) + ")", "class": "ico" + (cls ? " " + cls : "") });
    g.appendChild(iconPath(key));
    return g;
  }

  // Effects, latency and calls of one tool, from the backend's meta or a minimal client fallback.
  function metaOf(t) {
    var m = t && t.meta && typeof t.meta === "object" ? t.meta : null;
    var have = !!(m && Array.isArray(m.effects));
    var eff = [], seen = {};
    function add(key, label, via) {
      if (!key) return;
      key = String(key);
      if (seen[key]) { if (seen[key].via && !via) seen[key].via = null; return; }   // direct beats inherited
      seen[key] = { key: key, label: label ? String(label) : "", via: via ? String(via) : null };
      eff.push(seen[key]);
    }
    if (have) {
      m.effects.forEach(function (e) { if (e && e.key) add(e.key, e.label, e.via); });
    } else {
      if (t && t.session === "web-kit") add("kit");
      if (t && ENTRY_NAMES[t.name]) add("entry-point");
    }
    if (eff.length > 1) eff = eff.filter(function (e) { return e.key !== "pure"; });
    if (!eff.length) add("pure");
    var lat = m && m.latency && typeof m.latency === "object" ? m.latency : null;
    var kinds = lat && Array.isArray(lat.kinds) ? lat.kinds.map(String) : ["cpu"];
    var ms = lat && typeof lat.cpu_ms === "number" && isFinite(lat.cpu_ms) ? lat.cpu_ms : null;
    return { effects: eff, kinds: kinds, ms: ms, calls: m && Array.isArray(m.calls) ? m.calls.map(String) : null, unused: !!(m && m.unused === true) };
  }
  function primaryOf(eff) {
    var i, k;
    function direct(e) { return e.key === k && !e.via; }
    function any(e) { return e.key === k; }
    for (i = 0; i < FX_ORDER.length; i++) { k = FX_ORDER[i]; if (eff.filter(direct)[0]) return k; }
    for (i = 0; i < FX_ORDER.length; i++) { k = FX_ORDER[i]; if (eff.filter(any)[0]) return k; }
    return eff.length ? eff[0].key : "pure";
  }
  function fxLabel(e) { return (FX[e.key] && FX[e.key].label) || e.label || e.key; }
  function fxText(e) { return (FX[e.key] && FX[e.key].text) || ""; }
  function msText(ms) { return ms < 1 ? "<1 ms" : Math.round(ms) + " ms"; }

  // Everything a node can be filtered by in the legend.
  function nodeHas(n, key) {
    if (key === "fix") return n.repairs > 0;
    if (key === "unused") return !!(n.meta && n.meta.unused);
    var kind = key.slice(0, key.indexOf(":")), v = key.slice(key.indexOf(":") + 1);
    if (kind === "st") return n.st === v;
    if (!n.meta) return false;
    if (kind === "lat") return n.meta.kinds.indexOf(v) >= 0;
    return n.meta.effects.some(function (e) { return e.key === v; });
  }

  function tipOf(n) {
    var lines = [n.name];
    if (n.desc) lines.push(n.desc);
    if (n.st === "pending") lines.push("Planned: not built yet.");
    else if (n.st === "notbuilt") lines.push("Not built: the run ended before this one.");
    else if (n.st === "failed") lines.push("Gave up: it never passed its tests; nothing was saved.");
    else if (n.st === "active") lines.push("Building now (" + (n.phaseText || "building") + ").");
    if (n.meta && n.meta.unused) lines.push("Nothing calls this function yet.");
    if (n.repairs) lines.push(n.repairs + " repair round" + (n.repairs === 1 ? "" : "s") + " so far.");
    if (n.meta) {
      lines.push("");
      n.meta.effects.forEach(function (e) {
        lines.push(fxLabel(e) + ": " + fxText(e) + (e.via ? " (through " + e.via + ")" : ""));
      });
      n.meta.kinds.forEach(function (k) {
        var L = LAT[k] || { label: k, text: "" };
        lines.push("Latency, " + L.label + (k === "cpu" && n.meta.ms != null ? " " + msText(n.meta.ms) : "") + ": " + L.text);
      });
    }
    if (n.calls.length) lines.push("Calls: " + n.calls.join(", "));
    if (n.callers.length) lines.push("Called by: " + n.callers.join(", "));
    return lines.join("\n");
  }
  function roleSummary(n) {
    if (!n.meta) return n.st === "failed" ? "gave up" : "not built yet";
    return n.meta.effects.map(function (e) { return fxLabel(e) + (e.via ? " (through " + e.via + ")" : ""); }).join(", ");
  }

  // ---- scene
  function ensureScene() {
    var svg = $("ag-graph");
    if (G.scene && G.scene.svg === svg && G.scene.vp.parentNode === svg) return G.scene;
    clear(svg);
    G.nodes = {}; G.edges = {}; G.pulses = [];
    var defs = el("defs");
    var pat = el("pattern", { id: "agDots", width: 32, height: 32, patternUnits: "userSpaceOnUse" });
    pat.appendChild(el("circle", { cx: 2, cy: 2, r: 1, fill: "#2b3040" }));
    defs.appendChild(pat);
    var mk = el("marker", { id: "agArrow", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 6, markerHeight: 6, orient: "auto" });
    mk.appendChild(el("path", { d: "M0 0L10 5L0 10z", fill: "#b18cff" }));
    defs.appendChild(mk);
    svg.appendChild(defs);
    var vp = el("g", { id: "agVp" });
    vp.appendChild(el("rect", { x: -6000, y: -6000, width: 12000, height: 12000, fill: "url(#agDots)" }));
    var kit = el("g", { "class": "ag-kitbox" });
    kit.appendChild(el("rect", { rx: 14 }));
    kit.appendChild(el("text", { "class": "ag-kittitle" }));
    var edges = el("g", { "class": "ag-edges" }), nodes = el("g", { "class": "ag-nodes" }), fx = el("g", { "class": "ag-fx" });
    vp.appendChild(kit); vp.appendChild(edges); vp.appendChild(nodes); vp.appendChild(fx);
    svg.appendChild(vp);
    var empty = el("g", { "class": "ag-emptyg" });
    empty.appendChild(el("text", { "class": "ag-empty" }, "No functions yet."));
    empty.appendChild(el("text", { "class": "ag-empty sub" }, "Ask for something and the agent will write them here."));
    svg.appendChild(empty);
    G.scene = { svg: svg, vp: vp, kit: kit, edges: edges, nodes: nodes, fx: fx, empty: empty };
    G.vw = 0; G.vh = 0; G.lastFit = "";
    return G.scene;
  }
  // The viewBox is the canvas's own pixel size. It changes only when the canvas is resized.
  function measure() {
    var svg = $("ag-graph"), r = svg.getBoundingClientRect();
    var w = Math.round(r.width), h = Math.round(r.height);
    if (w < 40 || h < 40) return false;
    if (w !== G.vw || h !== G.vh) {
      G.vw = w; G.vh = h;
      svg.setAttribute("viewBox", "0 0 " + w + " " + h);
      state.gdims = { w: w, h: h };
      if (G.scene) {
        var t = G.scene.empty.childNodes;
        t[0].setAttribute("x", w / 2); t[0].setAttribute("y", h / 2 - 4);
        t[1].setAttribute("x", w / 2); t[1].setAttribute("y", h / 2 + 20);
      }
    }
    return true;
  }

  // ---- layout: kit tools in a grid on the left, the rest layered by dependency depth
  function byIdx(a, b) { return a.idx - b.idx; }
  function layoutNodes(list) {
    var by = {}, kitBox = null;
    list.forEach(function (n) { by[n.name] = n; n.callers = []; });
    list.forEach(function (n) {
      n.calls.forEach(function (c) {
        var o = by[c];
        if (o && o !== n && o.callers.indexOf(n.name) < 0) o.callers.push(n.name);
      });
    });
    var kit = list.filter(function (n) { return n.kit; }).sort(byIdx);
    var app = list.filter(function (n) { return !n.kit; });
    var memo = {};
    function dep(n, stack) {
      if (memo[n.name] != null) return memo[n.name];
      if (!n.tool) return (memo[n.name] = n.hint || 0);
      if (stack[n.name]) return 0;
      stack[n.name] = 1;
      var m = -1;
      n.calls.forEach(function (c) { var o = by[c]; if (o && !o.kit) m = Math.max(m, dep(o, stack)); });
      delete stack[n.name];
      return (memo[n.name] = m + 1);
    }
    app.forEach(function (n) { n.depth = dep(n, {}); });
    var maxOther = 0;
    app.forEach(function (n) { if (!n.entry) maxOther = Math.max(maxOther, n.depth); });
    app.forEach(function (n) { if (n.entry) n.depth = Math.max(n.depth, maxOther); });
    var depths = [];
    app.forEach(function (n) { if (depths.indexOf(n.depth) < 0) depths.push(n.depth); });
    depths.sort(function (a, b) { return a - b; });
    var cols = depths.map(function (d) { return app.filter(function (n) { return n.depth === d; }).sort(byIdx); });
    // Shape the layout to the canvas: wrap crowded layers into sub-columns so the whole graph
    // has about the canvas's aspect ratio, then pick the wrap width that fits at the largest scale.
    var aw = Math.max(200, (G.vw || 1200) - 48), ah = Math.max(150, (G.vh || 560) - 48);
    var SUBW = NW + 18, LGAP = 40;
    var maxCount = cols.reduce(function (a, c) { return Math.max(a, c.length); }, Math.max(kit.length, 1));
    function shape(R) {
      var kc = kit.length ? Math.ceil(kit.length / R) : 0;
      var W = kit.length ? kc * KW + 56 : 0, Hh = NH + 20;
      if (kit.length) Hh = Math.max(Hh, Math.ceil(kit.length / kc) * KH + 52);
      cols.forEach(function (col, i) {
        var sub = Math.ceil(col.length / R), rows = Math.ceil(col.length / sub);
        W += (sub - 1) * SUBW + NW + (i ? LGAP : 0);
        Hh = Math.max(Hh, rows * ROWH);
      });
      return { W: W, H: Hh, k: Math.min(aw / W, ah / Hh, 1) };
    }
    var bestR = maxCount, bestK = -1;
    for (var R = maxCount; R >= 1; R--) {
      var sh = shape(R);
      if (sh.k > bestK + 1e-9) { bestK = sh.k; bestR = R; }
    }
    var kitCols = kit.length ? Math.ceil(kit.length / bestR) : 0;
    var kitRows = kit.length ? Math.ceil(kit.length / kitCols) : 0;
    var best = shape(bestR), H = best.H, rh = ROWH;
    // spare height: spread rows a little so the graph also fills the canvas vertically
    if (best.H * best.k < ah) { var sc0 = Math.min(1.45, ah / (best.H * best.k)); rh = ROWH * sc0; H = best.H * sc0; }
    var minX = 1e9, maxX = -1e9, minY = 1e9, maxY = -1e9;
    function grow(n) {
      minX = Math.min(minX, n.tx - NW / 2); maxX = Math.max(maxX, n.tx + NW / 2);
      minY = Math.min(minY, n.ty - NH / 2 - 10); maxY = Math.max(maxY, n.ty + NH / 2 + 10);
    }
    kit.forEach(function (n, i) {
      n.tx = KW / 2 + (i % kitCols) * KW;
      n.ty = (H - kitRows * KH) / 2 + KH / 2 + Math.floor(i / kitCols) * KH;
      grow(n);
    });
    if (kit.length) {
      var top = (H - kitRows * KH) / 2;
      kitBox = { x: -(KW - NW) / 2 - 14, y: top - 40, w: kitCols * KW + 8, h: kitRows * KH + 52 };
      minX = Math.min(minX, kitBox.x); minY = Math.min(minY, kitBox.y); maxY = Math.max(maxY, kitBox.y + kitBox.h);
    }
    var cursor = kit.length ? kitCols * KW + 56 : 0;
    cols.forEach(function (col, c) {
      var sub = Math.ceil(col.length / bestR), rows = Math.ceil(col.length / sub);
      var keyed = col.map(function (n, i) {
        var ys = [];
        n.calls.forEach(function (cn) { var o = by[cn]; if (o && !o.kit && o._col != null && o._col < c && o.ty != null) ys.push(o.ty); });
        return { n: n, key: ys.length ? ys.reduce(function (a, b) { return a + b; }, 0) / ys.length : (H - col.length * rh) / 2 + i * rh };
      });
      keyed.sort(function (a, b) { return a.key - b.key || a.n.idx - b.n.idx; });
      keyed.forEach(function (k, i) {
        var sc = Math.floor(i / rows), r = i % rows, inCol = Math.min(rows, col.length - sc * rows);
        k.n.tx = cursor + NW / 2 + sc * SUBW;
        k.n.ty = (H - inCol * rh) / 2 + rh / 2 + r * rh;
        k.n._col = c; grow(k.n);
      });
      cursor += (sub - 1) * SUBW + NW + LGAP;
    });
    var b = list.length ? { x0: minX - 8, y0: minY - 8, x1: maxX + 8, y1: maxY + 8 } : null;
    return { bounds: b, kit: kitBox };
  }

  // ---- node DOM
  function buildNode(n) {
    var g = el("g", { "class": "gn", tabindex: 0, role: "button", "data-name": n.name });
    var title = el("title");
    var inner = el("g", { "class": "gn-in" });
    var ring = el("rect", { "class": "ring", x: -NW / 2 - 5, y: -NH / 2 - 5, width: NW + 10, height: NH + 10, rx: 15 });
    var box = el("rect", { "class": "box", x: -NW / 2, y: -NH / 2, width: NW, height: NH, rx: 11 });
    var disc = el("circle", { "class": "disc", cx: -NW / 2 + 24, cy: -6, r: 15 });
    var prim = el("g", { "class": "prim" });
    var nm = el("text", { "class": "nm", x: -NW / 2 + 46, y: -9 });
    var sb = el("text", { "class": "sb", x: -NW / 2 + 46, y: 5 });
    // zoomed-out graphs hide the status line, so a long name gets that room as a second line
    var w1 = el("text", { "class": "nmw", x: -NW / 2 + 46, y: "-.3em" });
    var w2 = el("text", { "class": "nmw", x: -NW / 2 + 46, y: ".8em" });
    var row = el("g", { "class": "row" });
    var fix = el("g", { "class": "fix" });
    fix.appendChild(el("rect", { x: NW / 2 - 52, y: -NH / 2 - 9, width: 56, height: 18, rx: 9 }));
    var ft = el("text", { x: NW / 2 - 24, y: -NH / 2 + 4 });
    fix.appendChild(ft);
    var ftt = el("title"); fix.appendChild(ftt);
    // top-left badge: a function nothing calls (the text says it, so colour is not the only signal)
    var unusedB = el("g", { "class": "unusedb" });
    unusedB.appendChild(el("rect", { x: -NW / 2 + 8, y: -NH / 2 - 9, width: 60, height: 18, rx: 9 }));
    unusedB.appendChild(el("text", { x: -NW / 2 + 38, y: -NH / 2 + 4 }, "unused"));
    [ring, box, disc, prim, nm, sb, w1, w2, row, fix, unusedB].forEach(function (c) { inner.appendChild(c); });
    g.appendChild(title); g.appendChild(inner);
    n.d = { g: g, title: title, inner: inner, box: box, prim: prim, nm: nm, sb: sb, w1: w1, w2: w2, row: row, fix: fix, ft: ft, ftt: ftt, unusedB: unusedB };
    n.r = {};
    g.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") { e.preventDefault(); e.stopPropagation(); selectTool(n.name); }
    });
    G.scene.nodes.appendChild(g);
  }
  function put(n, key, node, attr, val) {
    if (n.r[key] === val) return;
    n.r[key] = val; node.setAttribute(attr, val);
  }
  function putT(n, key, node, val) {
    if (n.r[key] === val) return;
    n.r[key] = val; node.textContent = val;
  }
  function placeNode(n) { n.d.g.setAttribute("transform", "translate(" + n.x.toFixed(1) + " " + n.y.toFixed(1) + ")"); }

  var PHASE_TEXT = { building: "building", thinking: "thinking…", testing: "testing", repairing: "repairing", splitting: "splitting…", passed: "tests passed" };

  function paintNode(n) {
    var d = n.d, unused = !!(n.meta && n.meta.unused);
    var cls = "gn st-" + n.st + (n.kit ? " kit" : "") + (n.entry ? " entry" : "") + (unused ? " unused" : "");
    if (n.spawnCls) cls += " " + n.spawnCls;
    if (n.flashN) cls += n.flashN % 2 ? " flash-a" : " flash-b";
    if (state.sel === n.name) cls += " sel";
    if (state.hot === n.name) cls += " hot";
    if (G.lit) cls += nodeHas(n, G.lit) ? " lit" : " dim";
    put(n, "cls", d.g, "class", cls);
    if (n.spawnDelay != null) { var dl = n.spawnDelay + "ms"; if (d.inner.style.animationDelay !== dl) d.inner.style.animationDelay = dl; }
    var max = G.nmMax || 19;
    putT(n, "nm", d.nm, trunc(n.name, max));
    var lines = wrapName(n.name, max);
    putT(n, "w1", d.w1, lines[0]);
    putT(n, "w2", d.w2, lines[1]);
    put(n, "wrapped", d.g, "data-wrapped", lines[1] ? "1" : "0");
    var sub;
    if (n.st === "active") { n.phaseText = PHASE_TEXT[n.phase] || "building"; sub = n.phaseText; }
    else if (n.st === "pending") sub = n.split ? "split into parts" : "planned";
    else if (n.st === "notbuilt") sub = "not built";
    else if (n.st === "failed") sub = "gave up";
    else sub = n.kit ? "web kit" : "saved" + (n.uses ? " · reused " + n.uses + "×" : "");
    putT(n, "sb", d.sb, sub);
    putT(n, "tip", d.title, tipOf(n));
    put(n, "aria", d.g, "aria-label", n.name + ", " + (n.st === "done" ? "saved" : n.st === "notbuilt" ? "not built" : n.st === "failed" ? "gave up" : n.st === "active" ? "building, " + (n.phaseText || "") : "planned") + ". " + roleSummary(n) + ". " + (unused ? "Unused: nothing calls it yet. " : "") + "Press Enter to show details.");
    put(n, "pressed", d.g, "aria-pressed", state.sel === n.name ? "true" : "false");
    put(n, "unusedv", d.unusedB, "style", unused ? "" : "display:none");
    var fx = n.repairs > 0;
    put(n, "fixv", d.fix, "style", fx ? "" : "display:none");
    if (fx) { putT(n, "fixt", d.ft, "fix " + n.repairs); putT(n, "fixtt", d.ftt, n.repairs + " repair round" + (n.repairs === 1 ? "" : "s") + ": the tests failed and the code was repaired"); }
    var sig = n.st + "|" + (n.meta ? n.meta.effects.map(function (e) { return e.key + (e.via ? ">" + e.via : ""); }).join(",") + "|" + n.meta.kinds.join(",") + "|" + n.meta.ms : "-");
    if (n.r.sig !== sig) { n.r.sig = sig; paintIcons(n); }
  }

  function paintIcons(n) {
    var d = n.d;
    clear(d.prim); clear(d.row);
    var cx = -NW / 2 + 24, cy = -6;
    if (!n.meta || n.st === "pending" || n.st === "notbuilt" || (n.st === "active" && !n.tool)) {
      d.prim.appendChild(iconG("plan", cx, cy, 22, "ico-st"));
      return;
    }
    if (n.st === "failed") { d.prim.appendChild(iconG("fail", cx, cy, 20, "ico-st")); return; }
    var eff = n.meta.effects, pk = primaryOf(eff);
    var pe = eff.filter(function (e) { return e.key === pk; })[0];
    var pg = iconG(pk, cx, cy, 20, pe && pe.via ? "ico-inh" : "");
    pg.appendChild(el("title", {}, fxLabel(pe || { key: pk }) + (pe && pe.via ? " (through " + pe.via + ")" : "") + ": " + (FX[pk] ? FX[pk].text : "")));
    d.prim.appendChild(pg);
    var rest = eff.filter(function (e) { return e.key !== pk; });
    var shown = rest.length > 5 ? rest.slice(0, 4) : rest, bx = -NW / 2 + 14, by = NH / 2 - 15;
    shown.forEach(function (e, i) {
      var cxb = bx + i * 18;
      var g = el("g", { "class": "bdg" + (e.via ? " inh" : "") });
      g.appendChild(el("circle", { cx: cxb, cy: by, r: 8 }));
      g.appendChild(iconG(e.key, cxb, by, 11));
      g.appendChild(el("title", {}, fxLabel(e) + (e.via ? " (through " + e.via + ")" : "") + ": " + fxText(e)));
      d.row.appendChild(g);
    });
    if (rest.length > shown.length) {
      var more = el("text", { "class": "more", x: bx + shown.length * 18 - 6, y: by + 4 }, "+" + (rest.length - shown.length));
      more.appendChild(el("title", {}, rest.slice(shown.length).map(function (e) { return fxLabel(e); }).join(", ")));
      d.row.appendChild(more);
    }
    var lx = 6;
    n.meta.kinds.forEach(function (k) {
      if (!ICON[k]) return;
      var g = el("g", { "class": "lat" });
      g.appendChild(iconG(k, lx + 6, by, 12));
      var L = LAT[k] || { label: k, text: "" };
      var msShow = k === "cpu" && n.meta.ms != null;
      g.appendChild(el("title", {}, "Latency, " + L.label + (msShow ? " " + msText(n.meta.ms) : "") + ": " + L.text));
      lx += 16;
      if (msShow) {
        g.appendChild(el("text", { x: lx, y: by + 3.5 }, msText(n.meta.ms)));
        lx += msText(n.meta.ms).length * 5.6 + 6;
      }
      d.row.appendChild(g);
    });
  }

  // ---- edges ("strings"): one path per call, drawn from the callee to the caller
  function edgePath(a, b) {
    var hw = NW / 2, x1, x2, mx;
    if (b.x - a.x > NW + 10) {
      x1 = a.x + hw; x2 = b.x - hw; mx = (x1 + x2) / 2;
      return "M" + x1.toFixed(1) + "," + a.y.toFixed(1) + " C" + mx.toFixed(1) + "," + a.y.toFixed(1) + " " + mx.toFixed(1) + "," + b.y.toFixed(1) + " " + x2.toFixed(1) + "," + b.y.toFixed(1);
    }
    if (a.x - b.x > NW + 10) {
      x1 = a.x - hw; x2 = b.x + hw; mx = (x1 + x2) / 2;
      return "M" + x1.toFixed(1) + "," + a.y.toFixed(1) + " C" + mx.toFixed(1) + "," + a.y.toFixed(1) + " " + mx.toFixed(1) + "," + b.y.toFixed(1) + " " + x2.toFixed(1) + "," + b.y.toFixed(1);
    }
    var xa = a.x + hw, xb = b.x + hw, bulge = 56;
    return "M" + xa.toFixed(1) + "," + a.y.toFixed(1) + " C" + (xa + bulge).toFixed(1) + "," + a.y.toFixed(1) + " " + (xb + bulge).toFixed(1) + "," + b.y.toFixed(1) + " " + xb.toFixed(1) + "," + b.y.toFixed(1);
  }
  function setEdgeD(ed) {
    var a = G.nodes[ed.from], b = G.nodes[ed.to];
    if (!a || !b) return;
    ed.el.setAttribute("d", edgePath(a, b));
  }
  function addEdge(key, from, to, delay, t) {
    var path = el("path", { "class": "ge" });
    var ed = { key: key, from: from, to: to, el: path, mode: "static", t0: 0, d: 450 };
    G.edges[key] = ed;
    setEdgeD(ed);
    G.scene.edges.appendChild(path);
    if (delay != null) {
      ed.mode = "wait"; ed.t0 = t + delay;
      path.style.opacity = "0";
      G.fxEnd = Math.max(G.fxEnd, ed.t0 + ed.d + 900);
    } else {
      path.setAttribute("marker-end", "url(#agArrow)");
      if (G.ready && !G.replay && !reduced()) path.setAttribute("class", "ge fade");
    }
    return ed;
  }
  function removeEdge(key) {
    var ed = G.edges[key];
    if (!ed) return;
    if (ed.el.parentNode) ed.el.parentNode.removeChild(ed.el);
    delete G.edges[key];
  }
  function edgeStep(ed, now, force) {
    var a = G.nodes[ed.from], b = G.nodes[ed.to];
    if (!a || !b) return false;
    var moving = !!(a.mv || b.mv);
    if (ed.mode === "wait") {
      if (!force && now < ed.t0) return true;
      ed.mode = "draw"; ed.t0 = force ? now - ed.d : Math.max(now, ed.t0);
    }
    if (ed.mode === "draw") {
      var p = force ? 1 : (now - ed.t0) / ed.d;
      setEdgeD(ed);
      if (p >= 1) {
        ed.mode = "done";
        ed.el.style.opacity = ""; ed.el.removeAttribute("stroke-dasharray"); ed.el.removeAttribute("stroke-dashoffset");
        ed.el.setAttribute("marker-end", "url(#agArrow)");
        if (!force && !reduced()) startPulse(ed, now);
        return false;
      }
      var len = 0;
      try { len = ed.el.getTotalLength(); } catch (e) { len = 300; }
      ed.el.style.opacity = "";
      ed.el.setAttribute("stroke-dasharray", len.toFixed(1) + " " + len.toFixed(1));
      ed.el.setAttribute("stroke-dashoffset", (len * (1 - ease(Math.max(0, p)))).toFixed(1));
      return true;
    }
    if (moving) setEdgeD(ed);
    return moving;
  }
  function startPulse(ed, now) {
    var c = el("circle", { r: 4, "class": "gpulse" });
    G.scene.fx.appendChild(c);
    G.pulses.push({ c: c, key: ed.key, t0: now, d: 700 });
    G.fxEnd = Math.max(G.fxEnd, now + 1200);
  }

  // ---- animation loop: node glides, string draws, pulses, view fits. Time based; one frame loop.
  function kick() {
    if (!G.raf) G.raf = window.requestAnimationFrame(function () { G.raf = 0; tick(nowMs(), false); });
    clearTimeout(G.guard);
    G.guard = setTimeout(function () { tick(nowMs(), true); }, Math.max(700, G.fxEnd - nowMs() + 300));
  }
  function tick(now, force) {
    var more = false, name, key;
    for (name in G.nodes) {
      var n = G.nodes[name];
      if (!n.mv) continue;
      var p = force ? 1 : (now - n.mv.t0) / n.mv.d;
      if (p >= 1) { n.x = n.tx; n.y = n.ty; n.mv = null; }
      else { p = ease(Math.max(0, p)); n.x = n.mv.x0 + (n.tx - n.mv.x0) * p; n.y = n.mv.y0 + (n.ty - n.mv.y0) * p; more = true; }
      placeNode(n);
    }
    for (key in G.edges) { if (edgeStep(G.edges[key], now, force)) more = true; }
    if (G.vpAnim) {
      var v = G.vpAnim, q = force ? 1 : (now - v.t0) / v.d;
      if (q >= 1) { state.vp = v.to; G.vpAnim = null; }
      else { q = ease(Math.max(0, q)); state.vp = { k: v.from.k + (v.to.k - v.from.k) * q, x: v.from.x + (v.to.x - v.from.x) * q, y: v.from.y + (v.to.y - v.from.y) * q }; more = true; }
      applyVp();
    }
    G.pulses = G.pulses.filter(function (pl) {
      var ed = G.edges[pl.key], u = force ? 1 : (now - pl.t0) / pl.d;
      if (!ed || u >= 1) { if (pl.c.parentNode) pl.c.parentNode.removeChild(pl.c); return false; }
      try {
        var len = ed.el.getTotalLength(), pt = ed.el.getPointAtLength(len * Math.max(0, u));
        pl.c.setAttribute("cx", pt.x.toFixed(1)); pl.c.setAttribute("cy", pt.y.toFixed(1));
        pl.c.setAttribute("opacity", (Math.sin(Math.max(0, u) * Math.PI)).toFixed(2));
      } catch (e) { /* path not measurable yet */ }
      more = true;
      return true;
    });
    if (more && !force) { if (!G.raf) G.raf = window.requestAnimationFrame(function () { G.raf = 0; tick(nowMs(), false); }); }
  }

  // ---- pan, zoom, fit
  function applyVp() {
    var g = document.getElementById("agVp");
    if (!g) return;
    g.setAttribute("transform", "translate(" + state.vp.x.toFixed(1) + " " + state.vp.y.toFixed(1) + ") scale(" + state.vp.k.toFixed(3) + ")");
    // level of detail: far out, drop the small print so names stay readable
    var svg = $("ag-graph"), k = state.vp.k;
    svg.classList.toggle("lod-mid", k < 0.55);
    svg.classList.toggle("lod-low", k < 0.35);
    // Names keep at least 12.5px on screen: the label size is set in graph units so that
    // zooming out does not shrink it. A longer size gets a shorter name; the full name stays in the tooltip.
    var fs = Math.max(13, Math.round(12.5 / k * 2) / 2);
    if (G.nmFs !== fs) {
      G.nmFs = fs;
      G.nmMax = fs > 13 ? Math.max(8, Math.floor(138 / (0.56 * fs))) : 19;
      svg.style.setProperty("--nm-fs", fs + "px");
      Object.keys(G.nodes || {}).forEach(function (name) { var n = G.nodes[name]; if (n.d && n.r) { n.r.nm = null; paintNode(n); } });
    }
  }
  function svgPoint(svg, cx, cy) {
    var m = svg.getScreenCTM();
    if (!m) return { x: 0, y: 0 };
    var p = svg.createSVGPoint(); p.x = cx; p.y = cy;
    var q = p.matrixTransform(m.inverse());
    return { x: q.x, y: q.y };
  }
  function userMoved() { state.vpTouched = true; G.touchedAt = nowMs(); G.vpAnim = null; }
  function zoomAt(px, py, factor) {
    var k0 = state.vp.k, k1 = Math.min(VP_MAX, Math.max(VP_MIN, k0 * factor));
    if (k1 === k0) return;
    state.vp = { k: k1, x: px - (px - state.vp.x) * (k1 / k0), y: py - (py - state.vp.y) * (k1 / k0) };
    userMoved(); applyVp();
  }
  // Transform that puts every node inside the canvas with padding; shrinks as far as needed, never grows past 1.
  function fitTarget() {
    var b = G.bounds, w = G.vw, h = G.vh, pad = 24;
    if (!b) return { k: 1, x: 0, y: 0 };
    var bw = Math.max(1, b.x1 - b.x0), bh = Math.max(1, b.y1 - b.y0);
    var k = Math.min((w - pad * 2) / bw, (h - pad * 2) / bh, 1);
    k = Math.max(VP_MIN, k);
    return { k: k, x: (w - bw * k) / 2 - b.x0 * k, y: (h - bh * k) / 2 - b.y0 * k };
  }
  function fitView(auto, animate) {
    if (!measure()) return;
    var to = fitTarget();
    if (!auto) { state.vpTouched = false; G.touchedAt = 0; }
    if (animate && G.ready && !reduced()) {
      G.vpAnim = { from: { k: state.vp.k, x: state.vp.x, y: state.vp.y }, to: to, t0: nowMs(), d: 380 };
      G.fxEnd = Math.max(G.fxEnd, nowMs() + 800);
      kick();
    } else { G.vpAnim = null; state.vp = to; applyVp(); }
  }
  function mayAutoFit() {
    if (G.dragging) return false;
    if (!state.vpTouched) return true;
    return G.running && nowMs() - G.touchedAt > 2000;
  }
  function initGraphView() {
    var svg = $("ag-graph");
    if (!svg || svg._pz) return;
    svg._pz = true;
    var ptrs = {}, drag = null, pinch = null;
    svg.addEventListener("wheel", function (e) {
      e.preventDefault();
      var p = svgPoint(svg, e.clientX, e.clientY);
      zoomAt(p.x, p.y, Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0018)));
    }, { passive: false });
    svg.addEventListener("pointerdown", function (e) {
      ptrs[e.pointerId] = { x: e.clientX, y: e.clientY };
      svg.setPointerCapture && svg.setPointerCapture(e.pointerId);
      var ids = Object.keys(ptrs);
      state.vpMoved = false;
      if (ids.length === 1) drag = { x: e.clientX, y: e.clientY, vx: state.vp.x, vy: state.vp.y };
      if (ids.length === 2) {
        var a = ptrs[ids[0]], b = ptrs[ids[1]];
        pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), k: state.vp.k };
        drag = null;
      }
      svg.classList.add("grabbing");
    });
    svg.addEventListener("pointermove", function (e) {
      if (!ptrs[e.pointerId]) return;
      ptrs[e.pointerId] = { x: e.clientX, y: e.clientY };
      var ids = Object.keys(ptrs);
      if (ids.length === 2 && pinch) {
        var a = ptrs[ids[0]], b = ptrs[ids[1]];
        var d = Math.hypot(a.x - b.x, a.y - b.y);
        var mid = svgPoint(svg, (a.x + b.x) / 2, (a.y + b.y) / 2);
        zoomAt(mid.x, mid.y, (pinch.k * d / (pinch.d || 1)) / state.vp.k);
        state.vpMoved = true; G.dragging = true;
      } else if (drag) {
        var m = svg.getScreenCTM(); if (!m) return;
        var dx = (e.clientX - drag.x) / m.a, dy = (e.clientY - drag.y) / m.d;
        if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) > 4) { state.vpMoved = true; G.dragging = true; }
        if (!state.vpMoved) return;
        state.vp = { k: state.vp.k, x: drag.vx + dx, y: drag.vy + dy };
        userMoved(); applyVp();
      }
    });
    function end(e) {
      delete ptrs[e.pointerId]; pinch = null;
      if (!Object.keys(ptrs).length) {
        drag = null; G.dragging = false; svg.classList.remove("grabbing");
        if (state.vpMoved) G.touchedAt = nowMs();
        setTimeout(function () { state.vpMoved = false; }, 0);
      }
    }
    svg.addEventListener("pointerup", end);
    svg.addEventListener("pointercancel", end);
    svg.addEventListener("dblclick", function (e) { e.preventDefault(); fitView(false, true); });
    // selection: one delegated handler (pointer capture can retarget a click to the svg itself)
    svg.addEventListener("click", function (e) {
      if (state.vpMoved) return;
      var t = e.target && e.target.closest ? e.target.closest(".gn") : null;
      if (!t) {
        var u = document.elementFromPoint(e.clientX, e.clientY);
        t = u && u.closest ? u.closest(".gn") : null;
      }
      if (t) selectTool(t.getAttribute("data-name"));
    });
    svg.addEventListener("keydown", function (e) {
      var d = state.gdims, step = 40, c = { x: d.w / 2, y: d.h / 2 };
      if (e.key === "+" || e.key === "=") zoomAt(c.x, c.y, 1.2);
      else if (e.key === "-" || e.key === "_") zoomAt(c.x, c.y, 1 / 1.2);
      else if (e.key === "0") fitView(false, true);
      else if (e.key === "ArrowLeft") { state.vp.x += step; userMoved(); applyVp(); }
      else if (e.key === "ArrowRight") { state.vp.x -= step; userMoved(); applyVp(); }
      else if (e.key === "ArrowUp") { state.vp.y += step; userMoved(); applyVp(); }
      else if (e.key === "ArrowDown") { state.vp.y -= step; userMoved(); applyVp(); }
      else return;
      e.preventDefault();
    });
    function center() { var d = state.gdims; return { x: d.w / 2, y: d.h / 2 }; }
    $("ag-zin").addEventListener("click", function () { var c = center(); zoomAt(c.x, c.y, 1.3); });
    $("ag-zout").addEventListener("click", function () { var c = center(); zoomAt(c.x, c.y, 1 / 1.3); });
    $("ag-zfit").addEventListener("click", function () { fitView(false, true); });
    $("ag-legend-btn").addEventListener("click", function () {
      var wrap = svg.parentNode, open = !wrap.classList.contains("legend-open");
      wrap.classList.toggle("legend-open", open);
      this.setAttribute("aria-expanded", open ? "true" : "false");
    });
    if (window.ResizeObserver) {
      var rtimer = null;
      new ResizeObserver(function () {
        clearTimeout(rtimer);
        rtimer = setTimeout(function () {
          if (!measure()) return;
          if (mayAutoFit() || !G.ready) { fitView(true, false); G.ready = true; } else applyVp();
        }, 60);
      }).observe(svg);
    }
  }

  // ---- reconcile the scene with the data (incremental; never rebuilds what exists)
  function hasEff(meta, key) { return !!meta && meta.effects.some(function (e) { return e.key === key; }); }

  function syncGraph() {
    var sc = ensureScene();
    var haveSize = measure();
    var t = nowMs(), rm = reduced(), tools = state.tools || [];
    G.namesSig = tools.map(function (x) { return x.name; }).join("\u0001");
    var seen = {};
    function node(name) {
      var n = G.nodes[name];
      if (!n) {
        n = G.nodes[name] = { name: name, idx: G.seq++, st: "pending", calls: [], callers: [], repairs: 0, phase: "", uses: 0, hint: 0, x: 0, y: 0, tx: 0, ty: 0, isNew: true, depth: 0 };
      }
      seen[name] = 1;
      return n;
    }
    tools.forEach(function (tl) {
      var n = node(tl.name);
      n.desc = tl.description; n.uses = tl.uses || 0;
      if (n.tool !== tl || n.namesSeen !== G.namesSig) {
        n.tool = tl; n.meta = metaOf(tl); n.namesSeen = G.namesSig;
        n.calls = (n.meta.calls || deps(tl, tools)).filter(function (c) { return c !== tl.name; });
      }
      n.kit = hasEff(n.meta, "kit") || tl.session === "web-kit";
      n.entry = hasEff(n.meta, "entry-point");
      n.st = (G.running && (G.active === n.name || G.lanes[n.name]) && !G.promoted[n.name]) ? "active" : "done";
    });
    Object.keys(G.promoted).forEach(function (name) {
      if (seen[name]) return;
      node(name).st = "done";
    });
    function unbuilt(name, spec, hint) {
      var n = node(name);
      if (hint != null) n.hint = hint;
      if (spec) n.desc = spec;
      n.tool = n.tool || null;
      n.st = G.failed[name] ? "failed" : (!G.running ? "notbuilt" : ((G.active === name || G.lanes[name]) ? "active" : "pending"));
      n.split = !!G.split[name] && n.st === "pending";
      return n;
    }
    G.plan.forEach(function (p) { if (!seen[p.name]) unbuilt(p.name, p.spec, p.hint); });
    if (state.ghost && !seen[state.ghost]) unbuilt(state.ghost, "being tested", 0);
    if (G.active && !seen[G.active]) unbuilt(G.active, "", 0);
    Object.keys(G.failed).forEach(function (name) { if (!seen[name]) unbuilt(name, "", 0); });
    Object.keys(G.nodes).forEach(function (name) {
      if (seen[name]) return;
      var n = G.nodes[name];
      if (n.d && n.d.g.parentNode) n.d.g.parentNode.removeChild(n.d.g);
      delete G.nodes[name];
    });
    var list = Object.keys(G.nodes).map(function (k) { return G.nodes[k]; });
    var lay = layoutNodes(list);
    G.bounds = lay.bounds;
    // kit backdrop
    var kb = sc.kit;
    if (lay.kit) {
      kb.style.display = "";
      var kr = kb.firstChild, kt = kb.lastChild;
      kr.setAttribute("x", lay.kit.x); kr.setAttribute("y", lay.kit.y); kr.setAttribute("width", lay.kit.w); kr.setAttribute("height", lay.kit.h);
      kt.setAttribute("x", lay.kit.x + 14); kt.setAttribute("y", lay.kit.y + 22);
      kt.textContent = "Web kit · supplied by the harness";
    } else kb.style.display = "none";
    var kitIdx = 0;
    list.forEach(function (n) {
      var fresh = !n.d;
      if (fresh) { buildNode(n); n.x = n.tx; n.y = n.ty; placeNode(n); }
      else if (n.x !== n.tx || n.y !== n.ty) {
        if (rm || !G.ready || G.replay || !haveSize) { n.x = n.tx; n.y = n.ty; n.mv = null; placeNode(n); }
        else { n.mv = { x0: n.x, y0: n.y, t0: t, d: 460 }; G.fxEnd = Math.max(G.fxEnd, t + 900); }
      }
      if (!rm && G.ready && !G.replay) {
        if (n.st === "done" && G.spawn[n.name]) { n.spawnCls = "spawn"; delete G.spawn[n.name]; }
        else if (n.st === "done" && G.kitNew && fresh && G.kitNew.indexOf(n.name) >= 0) { n.spawnCls = "spawn"; n.spawnDelay = (kitIdx++) * 70; G.fxEnd = Math.max(G.fxEnd, t + 70 * kitIdx + 900); }
        else if (fresh && n.st !== "done" && !n.spawnCls) n.spawnCls = "appear";
      } else delete G.spawn[n.name];
      paintNode(n);
    });
    if (G.kitNew && G.kitNew.some(function (nm) { return G.nodes[nm]; })) G.kitNew = null;
    // edges
    var want = {};
    list.forEach(function (n) {
      if (!n.tool) return;
      n.calls.forEach(function (c) { if (G.nodes[c] && c !== n.name) want[c + ">" + n.name] = { from: c, to: n.name }; });
    });
    Object.keys(G.edges).forEach(function (key) { if (!want[key]) removeEdge(key); });
    Object.keys(want).forEach(function (key) {
      var w = want[key], ed = G.edges[key];
      if (!ed) {
        var a = G.animate[w.to], delay = null;
        if (a && !rm && !G.replay && G.ready) { delay = Math.max(0, a.at - t) + a.i * 520; a.i++; }
        ed = addEdge(key, w.from, w.to, delay, t);
      } else if (ed.mode === "static" || ed.mode === "done") setEdgeD(ed);
      var kk = G.nodes[w.from].kit && G.nodes[w.to].kit;
      var wantCls = "ge" + (kk ? " kk" : "");
      if (ed.mode !== "wait" && ed.mode !== "draw" && ed.el.getAttribute("class") !== wantCls && ed.el.getAttribute("class") !== wantCls + " fade") ed.el.setAttribute("class", wantCls);
    });
    Object.keys(G.animate).forEach(function (name) { if (G.nodes[name] && G.nodes[name].tool) delete G.animate[name]; });
    applyMarks();
    sc.empty.style.display = list.length ? "none" : "";
    renderLegend(list);
    renderSel();
    renderStatus();
    // auto-fit: first draw, new nodes, plan arrival; never while dragging
    if (haveSize) {
      var sig = list.length + ":" + G.vw + "x" + G.vh + ":" + (G.bounds ? [G.bounds.x0, G.bounds.y0, G.bounds.x1, G.bounds.y1].join(",") : "");
      if (sig !== G.lastFit && mayAutoFit()) { fitView(true, G.ready); G.lastFit = sig; G.ready = true; }
      else if (!G.ready) { G.ready = true; }
    }
    kick();
  }

  function applyMarks() {
    var sel = state.sel;
    Object.keys(G.edges).forEach(function (key) {
      var ed = G.edges[key], hl = sel && (ed.from === sel || ed.to === sel);
      var dim = G.lit && !(G.nodes[ed.from] && G.nodes[ed.to] && nodeHas(G.nodes[ed.from], G.lit) && nodeHas(G.nodes[ed.to], G.lit));
      ed.el.classList.toggle("hl", !!hl);
      ed.el.classList.toggle("dim", !!dim);
    });
  }

  function selectTool(name) {
    state.sel = name;
    drawGraph();
    showDetail();
  }

  // ---- status strip, selection strip, legend
  function renderStatus() {
    var box = $("ag-gstatus"), t = $("ag-gstatus-t");
    var text = G.note;
    if (!text) {
      var all = state.tools || [], kitN = all.filter(function (t) { return t.session === "web-kit"; }).length;
      var n = all.length - kitN;
      text = G.running ? "Working…"
        : (all.length ? n + (n === 1 ? " function" : " functions") + " in " + currentProject().name +
            (kitN ? ", plus " + kitN + " kit helper" + (kitN === 1 ? "" : "s") + " from the harness" : "") + ". Select one to see its source and tests."
             : "No functions yet in " + currentProject().name + ". Describe what to build in the box on the left.");
    }
    if (t.textContent !== text) t.textContent = text;
    box.classList.toggle("busy", G.running);
    box.classList.toggle("bad", !G.running && G.noteBad);
  }

  function renderSel() {
    var box = $("ag-gsel"), n = G.nodes[state.sel];
    if (!n) { if (!box.hidden) { box.hidden = true; clear(box); } G.selSig = ""; return; }
    var sig = [n.name, n.st, roleSummary(n), n.calls.length, n.callers.length, n.meta && n.meta.ms].join("|");
    if (G.selSig === sig && !box.hidden) return;
    G.selSig = sig;
    box.hidden = false; clear(box);
    box.appendChild(h("b", "", n.name));
    box.appendChild(h("span", "ag-gsel-r", " — " + roleSummary(n)));
    box.appendChild(h("span", "muted", " · calls " + n.calls.length + ", called by " + n.callers.length +
      (n.meta && n.meta.ms != null ? " · CPU " + msText(n.meta.ms) : "")));
    if (n.tool) {
      var go = h("button", "ag-ghost", "Show source and tests ↓");
      go.type = "button";
      go.addEventListener("click", function () {
        var dt = $("ag-detail");
        dt.scrollIntoView({ behavior: reduced() ? "auto" : "smooth", block: "start" });
      });
      box.appendChild(go);
    } else {
      box.appendChild(h("span", "muted", n.st === "failed" ? " · never saved" : " · no source yet"));
    }
    var x = h("button", "ag-ghost", "Clear");
    x.type = "button";
    x.addEventListener("click", function () { state.sel = null; drawGraph(); showDetail(); });
    box.appendChild(x);
  }

  function legendIcon(key) {
    var s = el("svg", { viewBox: "0 0 24 24", width: 20, height: 20, "class": "lg-ico", "aria-hidden": "true", focusable: "false" });
    s.appendChild(iconPath(key));
    return s;
  }
  function renderLegend(list) {
    var box = $("ag-glegend");
    var eff = {}, lat = {}, st = {}, inh = false, fix = false, unused = false;
    list.forEach(function (n) {
      if (n.meta && n.st !== "pending" && n.st !== "notbuilt") {
        n.meta.effects.forEach(function (e) { eff[e.key] = 1; if (e.via) inh = true; });
        n.meta.kinds.forEach(function (k) { lat[k] = 1; });
      }
      if (n.st !== "done") st[n.st] = 1;
      if (n.repairs > 0) fix = true;
      if (n.meta && n.meta.unused) unused = true;
    });
    var entries = [];
    FX_ORDER.forEach(function (k) { if (eff[k]) entries.push({ key: "eff:" + k, icon: k, label: FX[k].label, text: FX[k].text }); });
    Object.keys(eff).forEach(function (k) { if (!FX[k]) entries.push({ key: "eff:" + k, icon: "other", label: k, text: "reported by the server" }); });
    ["cpu", "disk", "network"].forEach(function (k) { if (lat[k]) entries.push({ key: "lat:" + k, icon: k, label: "Latency: " + LAT[k].label, text: LAT[k].text }); });
    ["pending", "active", "notbuilt", "failed"].forEach(function (k) { if (st[k]) entries.push({ key: "st:" + k, icon: ST_LEGEND[k].icon, label: ST_LEGEND[k].label, text: ST_LEGEND[k].text }); });
    if (fix) entries.push({ key: "fix", icon: "fix", label: ST_LEGEND.repairs.label, text: ST_LEGEND.repairs.text });
    if (unused) entries.push({ key: "unused", icon: "unused", label: "Unused", text: "dashed outline: nothing calls this function yet" });
    var sig = entries.map(function (e) { return e.key; }).join(",") + "|" + inh;
    if (G.lit && !entries.some(function (e) { return e.key === G.lit; })) G.lit = null;
    if (sig === G.legendSig) return;
    G.legendSig = sig;
    clear(box);
    if (!entries.length) return;
    var ul = h("ul", "ag-lgl");
    entries.forEach(function (e) {
      var li = h("li");
      var b = h("button", "ag-lg");
      b.type = "button";
      b.setAttribute("data-key", e.key);
      b.setAttribute("aria-pressed", G.lit === e.key ? "true" : "false");
      b.title = "Highlight the functions with this: " + e.label;
      b.appendChild(legendIcon(e.icon));
      var tx = h("span", "lg-x");
      tx.appendChild(h("span", "lg-t", e.label));
      tx.appendChild(h("span", "lg-d", e.text));
      b.appendChild(tx);
      b.addEventListener("click", function () {
        G.lit = G.lit === e.key ? null : e.key;
        Array.prototype.forEach.call(box.querySelectorAll(".ag-lg"), function (x) { x.setAttribute("aria-pressed", x.getAttribute("data-key") === G.lit ? "true" : "false"); });
        syncGraph();
      });
      li.appendChild(b);
      ul.appendChild(li);
    });
    box.appendChild(ul);
    var note = "Click an entry to highlight the functions that have it.";
    if (inh) note += " An icon with a dashed outline is inherited: the function gets that effect through a function it calls (hover to see which).";
    box.appendChild(h("p", "muted ag-lg-note", note));
  }

  // ---- everything that follows a change of the tool list
  function drawGraph() {
    syncGraph();
    $("ag-count").textContent = state.tools.length + (state.tools.length === 1 ? " tool" : " tools");
    updateProjectText();
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
    updateCmdBox();
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

  // ---- function detail: inputs, output, ancestors, used-by, definition ----
  function toolByName(name) {
    return state.tools.filter(function (x) { return x.name === name; })[0];
  }
  // Parameter names and docstring, read from "(defun name (a b) "doc" ...)".
  function signatureOf(t) {
    var m = /^\s*\(defun\s+\S+\s*\(([^)]*)\)\s*(?:"((?:[^"\\]|\\.)*)")?/.exec(t.definition || "");
    var params = m ? m[1].split(/\s+/).filter(function (p) { return p; }) : [];
    return { params: params, doc: m && m[2] ? m[2].replace(/\\(.)/g, "$1") : "" };
  }
  // Levels of tools reached by following NEXT from NAME: [[direct], [one step further], ...].
  function levels(name, next) {
    var seen = {}, out = [], frontier = [name];
    seen[name] = true;
    while (frontier.length) {
      var layer = [];
      frontier.forEach(function (n) {
        next(n).forEach(function (d) { if (!seen[d]) { seen[d] = true; layer.push(d); } });
      });
      if (layer.length) out.push(layer);
      frontier = layer;
    }
    return out;
  }
  function callsOf(name) { var t = toolByName(name); return t ? deps(t, state.tools) : []; }
  function callersOf(name) {
    return state.tools.filter(function (x) { return deps(x, state.tools).indexOf(name) >= 0; })
      .map(function (x) { return x.name; });
  }
  // The arguments of a test call "(name a b)" as text, split at the top level.
  function callArgs(call, name) {
    var m = new RegExp("^\\s*\\(\\s*" + name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "(?=[\\s)])", "i").exec(call || "");
    if (!m) return null;
    var out = [], depth = 0, cur = "", str = false, i;
    for (i = m[0].length; i < call.length; i++) {
      var c = call[i];
      if (str) { cur += c; if (c === "\\") { cur += call[++i] || ""; } else if (c === '"') str = false; continue; }
      if (c === '"') { str = true; cur += c; continue; }
      if (c === "(") depth++;
      if (c === ")") { if (depth === 0) break; depth--; }
      if (/\s/.test(c) && depth === 0) { if (cur) { out.push(cur); cur = ""; } continue; }
      cur += c;
    }
    if (cur) out.push(cur);
    return out;
  }
  function chipRow(box, label, layers, emptyText) {
    var sec = h("div", "dt-sec");
    sec.appendChild(h("div", "dt-h", label));
    if (!layers.length) { sec.appendChild(h("div", "muted", emptyText)); box.appendChild(sec); return; }
    layers.forEach(function (layer, depth) {
      var row = h("div", "dt-chips");
      row.appendChild(h("span", "dt-depth", depth === 0 ? "directly" : (depth === 1 ? "one step further" : depth + " steps further")));
      layer.forEach(function (n) {
        var b = h("button", "ag-chip dt-chip", n);
        b.type = "button";
        b.title = (toolByName(n) || {}).description || n;
        b.setAttribute("aria-label", "Show the function " + n);
        b.addEventListener("click", function () { state.sel = n; drawGraph(); showDetail(); });
        row.appendChild(b);
      });
      sec.appendChild(row);
    });
    box.appendChild(sec);
  }

  function showDetail() {
    var box = $("ag-detail");
    var t = toolByName(state.sel);
    if (!t) { box.hidden = true; return; }
    box.hidden = false;
    clear(box);
    var sig = signatureOf(t);
    if (t.meta && t.meta.unused === true) box.appendChild(h("div", "dt-unused", "Nothing calls this function. It was written but is not part of the app yet."));
    var top = h("div", "dt-top");
    top.appendChild(h("b", "", t.name));
    top.appendChild(h("span", "muted", " — " + (t.description || "")));
    box.appendChild(top);
    box.appendChild(h("div", "dt-meta",
      (t.session === "web-kit" ? "supplied by the harness (web kit)" : "written by the model") +
      " · " + (t.tests || []).length + " passing test" + ((t.tests || []).length === 1 ? "" : "s") +
      " · reused " + (t.uses || 0) + "×"));

    // Inputs
    var inp = h("div", "dt-sec");
    inp.appendChild(h("div", "dt-h", "Inputs"));
    if (sig.params.length) {
      var row = h("div", "dt-chips");
      sig.params.forEach(function (p, i) {
        row.appendChild(h("span", "dt-param" + (p.charAt(0) === "&" ? " kw" : ""), p.charAt(0) === "&" ? p : (i + 1) + ". " + p));
      });
      inp.appendChild(row);
    } else {
      inp.appendChild(h("div", "muted", "none: it takes no arguments"));
    }
    box.appendChild(inp);

    // Output
    var outp = h("div", "dt-sec");
    outp.appendChild(h("div", "dt-h", "Output"));
    outp.appendChild(h("div", "", sig.doc || t.description || "not described"));
    box.appendChild(outp);

    // Examples: inputs -> output, from the tests it passed
    var ex = h("div", "dt-sec");
    ex.appendChild(h("div", "dt-h", "Examples it passed (inputs → output)"));
    if ((t.tests || []).length) {
      var tbl = h("table", "dt-ex");
      var hr = h("tr");
      (sig.params.length ? sig.params.filter(function (p) { return p.charAt(0) !== "&"; }) : ["call"]).concat(["output"])
        .forEach(function (c) { hr.appendChild(h("th", "", c)); });
      tbl.appendChild(hr);
      var cols = hr.children.length - 1;
      (t.tests || []).forEach(function (x) {
        var tr = h("tr");
        var args = callArgs(x.call, t.name);
        if (args && args.length === cols && sig.params.length) {
          args.forEach(function (a) { tr.appendChild(h("td", "dt-code", a)); });
        } else {
          var td = h("td", "dt-code", x.call);
          td.colSpan = cols;
          tr.appendChild(td);
        }
        tr.appendChild(h("td", "dt-code good", x.expect));
        tbl.appendChild(tr);
      });
      var wrap = h("div", "dt-scroll");
      wrap.appendChild(tbl);
      ex.appendChild(wrap);
    } else {
      ex.appendChild(h("div", "muted", "no recorded tests"));
    }
    box.appendChild(ex);

    chipRow(box, "Ancestors (functions this one is built from)", levels(t.name, callsOf),
      "none: it uses only built-in Lisp");
    chipRow(box, "Used by (functions built on this one)", levels(t.name, callersOf),
      "nothing calls it yet");

    var def = h("div", "dt-sec");
    def.appendChild(h("div", "dt-h", "Definition"));
    def.appendChild(h("pre", "dt-src", t.definition || ""));
    box.appendChild(def);
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
  // Every log line carries a text label (never colour alone). The label comes from the
  // line's class and wording, so the same event always reads the same way.
  function logKind(cls, t) {
    if (/^Goal:/.test(t)) return ["goal", "GOAL"];
    if (/^Plan:/.test(t)) return ["plan", "PLAN"];
    if (/^Reasoning:/.test(t)) return ["think", "THINK"];
    if (/^Answer:/.test(t)) return ["answer", "ANSWER"];
    if (/^Saved\b/.test(t)) return ["saved", "SAVED"];
    if (/^All tests passed/.test(t)) return ["passed", "PASSED"];
    if (/^Tests FAILED|^Why\b/.test(t)) return ["failed", "FAILED"];
    if (/^Repair round|^Repaired |kept failing|split it into|Stopping code|same code as|test call is broken|^Retired|unreadable/.test(t)) return ["repair", "REPAIR"];
    if (/^Independent check|^Tried the app like a visitor|^Checked that the functions fit together/.test(t)) return cls === "fail" ? ["failed", "FAILED"] : ["check", "CHECK"];
    if (/^Failed: |^The functions do not fit together/.test(t)) return ["failed", "FAILED"];
    if (/rate limiting us|model API is busy/.test(t)) return ["repair", "WAIT"];
    if (/^Screenshot check found|^Could not take a screenshot/.test(t)) return ["failed", "LOOK"];
    if (/^Screenshot|^Taking screenshots|^Fixing what the screenshots/.test(t)) return ["check", "LOOK"];
    if (cls === "fail") return ["error", "ERROR"];
    if (cls === "goal") return ["goal", "GOAL"];
    if (cls === "answer") return ["answer", "ANSWER"];
    if (cls === "pass") return ["passed", "PASSED"];
    if (cls === "use") return ["reuse", "REUSE"];
    if (cls === "build") return ["building", "BUILD"];
    if (cls === "model") return ["model", "MODEL"];
    return ["note", "NOTE"];
  }
  // "[name]" while a log line belongs to one function of a parallel build; set by onEvent only.
  var laneTag = "";
  function say(text, cls) {
    var log = $("ag-log"), t = String(text == null ? "" : text);
    // Follow the newest line only while the user is already at the bottom of the log.
    var follow = log.scrollHeight - log.scrollTop - log.clientHeight < 48;
    var k = logKind(cls || "", t);
    var d = h("div", "ag-say k-" + k[0] + (cls ? " " + cls : ""));
    d.appendChild(h("span", "ag-k", k[1]));
    var tEl = h("span", "ag-t", laneTag ? null : t);
    if (laneTag) { tEl.appendChild(h("span", "ag-lane", laneTag)); tEl.appendChild(document.createTextNode(" " + t)); }
    d.appendChild(tEl);
    if (t.length > 150) {
      d.classList.add("clamp");
      var b = h("button", "ag-more", "show more");
      b.type = "button"; b.setAttribute("aria-expanded", "false");
      b.addEventListener("click", function () {
        var open = d.classList.toggle("open");
        b.textContent = open ? "show less" : "show more";
        b.setAttribute("aria-expanded", open ? "true" : "false");
      });
      d.appendChild(b);
    }
    log.appendChild(d);
    if (follow) log.scrollTop = log.scrollHeight;
    return d;
  }

  // "Asking the model" lines show a live elapsed counter so long calls never look frozen.
  // Parallel builds can have several calls in flight: one indicator covers them all and
  // stays up until the last of them has replied (state.inflight counts them).
  function startWait(text) {
    state.inflight = (state.inflight || 0) + 1;
    if (state.waitEl) { tickWait(); return; }
    state.waitEl = say(text + " (0s)", "model");
    state.waitText = text; state.waitT0 = Date.now(); state.waitLane = laneTag;
  }
  function stopWait() {
    state.inflight = Math.max(0, (state.inflight || 0) - 1);
    if (state.inflight) tickWait(); else state.waitEl = null;
  }
  // Goal, done and error end every call at once: hide the indicator and reset the count.
  function forceWait() { state.inflight = 0; state.waitEl = null; }
  function tickWait() {
    if (!state.waitEl) return;
    var sec = Math.round((Date.now() - state.waitT0) / 1000), n = state.inflight || 0;
    var t = state.waitEl.querySelector(".ag-t") || state.waitEl;
    clear(t);
    if (n > 1) { t.appendChild(document.createTextNode("Asking the model… (" + n + " calls in flight) · " + sec + "s")); return; }
    if (state.waitLane) { t.appendChild(h("span", "ag-lane", state.waitLane)); t.appendChild(document.createTextNode(" ")); }
    t.appendChild(document.createTextNode(state.waitText + " (" + sec + "s" + (sec > 25 ? ", still working: hard retries think longer" : "") + ")"));
  }

  // Shown before the first model call: what the sandbox cannot do for this goal.
  // Text goes in through textContent (h), so goal text is never parsed as HTML.
  function renderCapNotice(gaps) {
    var box = $("ag-capnotice");
    clear(box);
    box.hidden = !(gaps && gaps.length);
    if (box.hidden) return;
    box.appendChild(h("div", "cap-t", "How this goal maps onto pure Lisp functions"));
    gaps.forEach(function (g) { box.appendChild(h("div", "cap-l", g.need + " → " + g.instead)); });
  }

  // "What the goal asks for": one checklist block appended to the build log after the plan.
  // Every line has a text marker (covered: / MISSING:) and a glyph; colour is never the only cue.
  function coverageBlock(ev) {
    var missing = Array.isArray(ev.missing) ? ev.missing : [];
    var feats = Array.isArray(ev.features) && ev.features.length ? ev.features
      : missing.map(function (l) { return { label: l, covered: false }; });
    if (!feats.length) return;
    var log = $("ag-log");
    var follow = log.scrollHeight - log.scrollTop - log.clientHeight < 48;
    var box = h("div", "ag-cov");
    box.appendChild(h("div", "ag-cov-t", "What the goal asks for"));
    feats.forEach(function (f) {
      var ok = !!(f && f.covered), label = String(f && f.label != null ? f.label : (f && f.key) || "feature");
      box.appendChild(h("div", "ag-cov-r " + (ok ? "covered" : "missing"), (ok ? "✓ covered: " : "✗ MISSING: ") + label));
    });
    if (ev.extended === true) box.appendChild(h("div", "ag-cov-x", "Asked the planner to add the missing parts."));
    log.appendChild(box);
    if (follow) log.scrollTop = log.scrollHeight;
  }

  // "a", "a and b", "a, b and c"
  function joinWords(words) {
    if (words.length < 2) return words.join("");
    return words.slice(0, -1).join(", ") + " and " + words[words.length - 1];
  }

  // Screenshots of the finished app: a row of thumbnails; a click opens the full image.
  var shotView = null, shotOpener = null;
  function clearShots() {
    var row = $("ag-shots");
    shotOpener = null; closeShot();
    clear(row); row.hidden = true;
  }
  function addShot(ev) {
    var label = String(ev.label == null ? "page" : ev.label), row = $("ag-shots");
    var fig = h("figure", "ag-shot"), btn = h("button", "ag-shot-btn"), img = h("img", "");
    btn.type = "button";
    img.src = ev.url; img.alt = "Screenshot of " + label; img.loading = "lazy";
    btn.appendChild(img);
    var cap = h("figcaption", "", label);
    cap.title = label;
    if (ev.round === 2) cap.appendChild(h("span", "ag-shot-r", " round 2"));
    btn.addEventListener("click", function () { openShot(ev.url, label, btn); });
    fig.appendChild(btn); fig.appendChild(cap);
    row.appendChild(fig);
    row.hidden = false;
  }
  function shotKey(e) { if (e.key === "Escape") { e.preventDefault(); closeShot(); } }
  function closeShot() {
    if (!shotView) return;
    document.removeEventListener("keydown", shotKey);
    shotView.parentNode.removeChild(shotView);
    shotView = null;
    if (shotOpener && shotOpener.focus) shotOpener.focus();
    shotOpener = null;
  }
  function openShot(src, label, opener) {
    closeShot();
    shotOpener = opener;
    var ov = h("div", "ag-shot-view");
    ov.setAttribute("role", "dialog"); ov.setAttribute("aria-modal", "true"); ov.setAttribute("aria-label", "Screenshot: " + label);
    var box = h("figure", "ag-shot-big"), img = h("img", ""), x = h("button", "ag-shot-x", "Close");
    x.type = "button";
    img.src = src; img.alt = label;
    box.appendChild(img); box.appendChild(h("figcaption", "", label));
    x.addEventListener("click", function () { closeShot(); });
    ov.appendChild(x); ov.appendChild(box);
    ov.addEventListener("click", function (e) { if (e.target === ov) closeShot(); });
    document.body.appendChild(ov);
    shotView = ov;
    document.addEventListener("keydown", shotKey);
    x.focus();
  }

  // Rows for the "How this build was checked" block. A mark means the harness checked that level itself.
  function verificationRows(v) {
    var rows = [], OK = "✓", BAD = "✗", NA = "–";
    function row(mark, cls, text) { rows.push({ mark: mark, cls: cls, text: text }); }
    function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }
    var ft = typeof v.function_tests === "number" ? v.function_tests : 0;
    if (ft > 0) row(OK, "v-ok", plural(ft, "test", "tests") + " of the individual functions passed");
    else row(NA, "v-muted", "no function tests ran");
    if (v.app_answers === true) row(OK, "v-ok", "the finished app answers a request");
    else if (v.app_answers === false) row(BAD, "v-warn", "the finished app did not answer a request");
    else row(NA, "v-muted", "the app as a whole was not started");
    var sc = v.scenarios && typeof v.scenarios === "object" ? v.scenarios : null;
    if (!sc) row(NA, "v-muted", "the app was not tried like a visitor");
    else {
      var scF = Number(sc.failed) || 0, scP = Number(sc.passed) || 0, scS = Number(sc.skipped) || 0;
      var scLabels = Array.isArray(sc.failed_labels) ? sc.failed_labels.filter(Boolean).map(String) : [];
      if (scF > 0) row(BAD, "v-warn", "tried like a visitor: " + plural(scF, "check", "checks") + " failed" + (scLabels.length ? " (" + scLabels.join("; ") + ")" : ""));
      else if (scP > 0) row(OK, "v-ok", "tried like a visitor: " + plural(scP, "check", "checks") + " passed" + (scS > 0 ? ", " + scS + " not applicable" : ""));
      else row(NA, "v-muted", "tried like a visitor: nothing applicable to check");
    }
    if (typeof v.interfaces !== "number") row(NA, "v-muted", "the functions were not compared with each other");
    else if (v.interfaces === 0) row(OK, "v-ok", "the functions fit together (calls, routes, tables)");
    else row(BAD, "v-warn", plural(v.interfaces, "mismatch between functions", "mismatches between functions"));
    if (typeof v.style_missing === "number") {
      if (v.style_missing === 0) row(OK, "v-ok", "every CSS class the pages use has a rule");
      else row(BAD, "v-warn", plural(v.style_missing, "CSS class the pages use has no rule", "CSS classes the pages use have no rule"));
    }
    if (v.screens === true) row(OK, "v-ok", "screenshots match what was asked for");
    else if (v.screens === false) row(BAD, "v-warn", "screenshots still show problems");
    else row(NA, "v-muted", "no screenshots were checked");
    return rows;
  }

  // The clean end-of-run card: what happened, what was built, what it cost.
  function renderSummary(m) {
    var box = $("ag-summary");
    clear(box);
    box.hidden = false;
    var missing = Array.isArray(m.missing_features) ? m.missing_features.filter(Boolean).map(String) : [];
    var unused = Array.isArray(m.unused_functions) ? m.unused_functions.filter(Boolean).map(String) : [];
    var smoke = m.smoke && typeof m.smoke === "object" ? m.smoke : null;
    var vis = m.visual && typeof m.visual === "object" ? m.visual : null;
    var ver = m.verification && typeof m.verification === "object" ? m.verification : null;
    var verRows = ver ? verificationRows(ver) : [];
    var verBad = verRows.some(function (r) { return r.mark === "✗"; });
    // a finished run with missing features, a failed self-check, unfixed screenshot problems or a failed independent check is not a plain success
    var incomplete = m.outcome === "success" && (missing.length > 0 || (!!smoke && smoke.ok === false) || (!!vis && vis.done === false) || verBad);
    box.className = "ag-sumcard " + (incomplete || m.outcome === "cancelled" ? "warn" : m.outcome === "success" ? "ok" : "bad");
    var title;
    if (incomplete) {
      title = "Built, but incomplete";
    } else if (m.outcome === "success") {
      title = m.flow === "cache" ? "\u2713 Answered instantly from a saved tool"
        : m.flow === "reuse" ? "\u2713 Answered by a tool you already had"
        : m.flow === "plan" ? "\u2713 Done: built " + m.built.length + " tools and put them together"
        : m.flow === "build" ? "\u2713 Built and verified a new tool" : "\u2713 Done";
    } else if (m.outcome === "cancelled") {
      title = "Cancelled";
    } else if (m.outcome === "error") {
      title = "\u2717 Stopped by an error";
    } else {
      title = "\u2717 Couldn\u2019t finish this one";
    }
    var head = h("div", "sum-head");
    head.appendChild(h("div", "sum-t", title));
    var more = h("button", "sum-more", "");
    more.type = "button";
    more.hidden = true;
    head.appendChild(more);
    box.appendChild(head);
    if (m.outcome === "cancelled") box.appendChild(h("div", "sum-l", "You stopped this build. What was saved before that is kept; Continue picks it up from there."));
    if (missing.length) box.appendChild(h("div", "sum-l sum-miss", "Not built yet: " + joinWords(missing) + ". Send a follow-up prompt for these."));
    if (ver) {
      box.appendChild(h("div", "sum-v-t", "Checked by the harness itself"));
      var verBox = h("div", "sum-v");
      verRows.forEach(function (r) {
        var rowEl = h("div", "sum-v-r");
        rowEl.appendChild(h("span", "sum-v-m " + r.cls, r.mark));
        rowEl.appendChild(h("span", "sum-v-x", r.text));
        verBox.appendChild(rowEl);
      });
      box.appendChild(verBox);
    }
    if (m.capability_gaps && m.capability_gaps.length) {
      box.appendChild(h("div", "sum-l cap-sum",
        (m.outcome === "success" ? "Built as pure functions. For " : "This goal needs ") +
        joinWords(m.capability_gaps.map(function (g) { return g.need; })) +
        (m.outcome === "success" ? ", see the notice above; use Run server in the project bar to try a web app."
                                 : ", which pure functions only provide as described in the notice above.")));
    }
    if (unused.length) box.appendChild(h("div", "sum-l", "Written but never used: " + unused.join(", ") + "."));
    if (smoke && smoke.ok === true) box.appendChild(h("div", "sum-l smoke-ok", "Checked: the app answered " + (smoke.call || "the check") +
      (smoke.status != null ? " with status " + smoke.status : "") + "."));
    else if (smoke && smoke.ok === false) box.appendChild(h("div", "sum-l smoke-bad", "The finished app failed its own check (" + (smoke.call || "no call") + "): " +
      String(smoke.error || "no reply").slice(0, 200)));
    if (vis && vis.checked && vis.done === true) box.appendChild(h("div", "sum-l smoke-ok", "Screenshots checked: the pages show what the goal asks for."));
    else if (vis && vis.checked && vis.done === false) {
      var visProblems = Array.isArray(vis.problems) ? vis.problems.filter(Boolean).map(String) : [];
      box.appendChild(h("div", "sum-l sum-miss", "Screenshots checked: " + visProblems.length + " problem(s) still visible: " + visProblems.join("; ") + ". Send a follow-up prompt naming what to fix."));
    }
    if (vis && vis.skipped) box.appendChild(h("div", "sum-l muted", "Screenshot check skipped: " + String(vis.skipped)));
    if (ver) box.appendChild(h("div", "sum-l muted", "The marks above are checks the harness ran itself. What the model says about its own work is not counted."));
    if (m.outcome === "success" && m.answer && m.answer.ok) {
      var v = String(m.answer.value == null ? "" : m.answer.value);
      var oneLine = v.indexOf("\n") < 0;
      box.appendChild(h("div", "sum-l", oneLine ? "Answer: " + v.slice(0, 80) + "  (from " + (m.answer.call || "") + ")"
                                              : "The result is shown above (from " + (m.answer.call || "") + ")."));
    }
    if (m.outcome !== "success") {
      if (m.built.length) box.appendChild(h("div", "sum-l", "Saved before stopping (still available): " + m.built.map(function (b) { return b.name; }).join(", ") +
        (m.planned ? " \u2014 " + m.built.length + " of " + m.planned + " planned tools" : "") + "."));
      if (m.outcome !== "cancelled") {   // a cancelled build has no failure to explain
        if (m.stopped && m.stopped.detail) box.appendChild(h("div", "sum-l", "Where it stopped: " + String(m.stopped.detail).split("; ")[0].slice(0, 200)));
        if (m.stopped && m.stopped.hint) box.appendChild(h("div", "sum-l", "Likely cause: " + m.stopped.hint));
        if (m.error) box.appendChild(h("div", "sum-l", /429|rate|traffic|queue/i.test(m.error)
          ? "The model API is rate-limited right now, not a problem with your prompt. Try again in a minute."
          : m.error.slice(0, 200)));
        box.appendChild(h("div", "sum-l muted", "Try a smaller, more specific prompt, or ask for one piece at a time."));
      }
    }
    var chips = h("div", "sum-chips");
    function chip(t, c) { chips.appendChild(h("span", "sc " + (c || ""), t)); }
    if (m.built.length) chip(m.built.length + " tool" + (m.built.length === 1 ? "" : "s") + " saved: " + m.built.map(function (b) { return b.name; }).join(", "), m.outcome === "success" ? "ok" : "");
    if (m.tests_passed) chip(m.tests_passed + " tests passed", "ok");
    if (m.repairs) chip(m.repairs + " repair" + (m.repairs === 1 ? "" : "s"));
    if (m.splits) chip(m.splits + " split into smaller tools");
    if (m.oracle_fixes) chip(m.oracle_fixes + " expected value(s) checked by an independent reference", "ok");
    if (Array.isArray(m.kept) && m.kept.length) chip(m.kept.length + " planned change" + (m.kept.length === 1 ? "" : "s") + " not made (left as saved): " + m.kept.join(", "), "warn");
    if (m.efficiency && m.efficiency.repeated_errors) chip(m.efficiency.repeated_errors + " repeated error(s), about " + fmt(m.efficiency.wasted_tokens) + " tokens wasted");
    var cc = m.concurrency && typeof m.concurrency === "object" ? m.concurrency : null;
    if (cc && cc.parallel && cc.lanes != null) chip("built " + cc.lanes + " at once" + (cc.peak != null ? " (peak " + cc.peak + " calls)" : ""), "ok");
    if (cc && cc.throttles > 0) chip("rate limited " + cc.throttles + " time" + (cc.throttles === 1 ? "" : "s"), "warn");
    var th = m.thinking && typeof m.thinking === "object" ? m.thinking : null;
    if (th && th.calls > 0) chip("thought " + th.calls + " time" + (th.calls === 1 ? "" : "s"));
    var cmp = m.compaction && typeof m.compaction === "object" ? m.compaction : null;
    if (cmp && cmp.saved_chars > 0) {
      var cmpPct = Math.round(100 * cmp.saved_chars / (cmp.prompt_chars + cmp.saved_chars));
      if (cmpPct > 0 && isFinite(cmpPct)) chip("context compacted: " + cmpPct + "% less prompt text");
    }
    if (cmp && cmp.edits > 0) chip(cmp.edits + " repair" + (cmp.edits === 1 ? "" : "s") + " sent as small edits", "ok");
    chip(m.model_calls + " model call" + (m.model_calls === 1 ? "" : "s"));
    chip(fmt(m.tokens) + " tokens" + (m.cost_usd ? " \u00b7 $" + m.cost_usd.toFixed(4) : ""));
    chip(m.seconds + " s");
    box.appendChild(chips);
    // The card stays short beside the log; the switch shows all of it. It appears only
    // when there is more than fits, and keeps its setting from one run to the next.
    function fitSummary() {
      box.classList.toggle("open", !!state.sumOpen);
      var cut = !state.sumOpen && box.scrollHeight > box.clientHeight + 6;
      more.hidden = !(cut || state.sumOpen);
      more.textContent = state.sumOpen ? "Show less" : "Show full report";
      more.setAttribute("aria-expanded", state.sumOpen ? "true" : "false");
    }
    more.onclick = function () { state.sumOpen = !state.sumOpen; fitSummary(); };
    fitSummary();
  }

  var CLASS_TEXT = {
    COMPILER_ERROR: "the code does not compile",
    RUNTIME_ERROR: "the code crashed",
    IMPLEMENTATION_WRONG: "the code gives a different value",
    TEST_WRONG: "the expected value looks like a guess",
    TEST_CALL_INVALID: "a test call is not valid Lisp; the code itself was never the problem",
    REGRESSION: "the change breaks a tool that already worked",
    AMBIGUOUS: "expected values keep changing",
    SPEC_INCONSISTENT: "the tests contradict each other",
    REPEATED_CANDIDATE: "same code as a failed attempt"
  };

  // ------------------------------------------- run meter: wall clock + average tokens per second
  // The clock runs from the goal event to the done event (server times, so a replayed or
  // re-attached run shows the same figure). The gauge is the model's average speed: output
  // tokens over the time it spent answering; the demo model has no timings, so there it is
  // output tokens over wall-clock time.
  var M = { t0: 0, end: 0, running: false, timer: null, out: 0, timedOut: 0, ms: 0, calls: 0, cost: 0, tokens: 0, free: 0 };
  var GAUGE_LEN = Math.PI * 30;
  // Spend of the run so far: under a dollar it needs four decimals to show anything.
  function spendText(usd) { return "$" + (usd >= 1 ? usd.toFixed(2) : usd > 0 ? usd.toFixed(4) : "0.00"); }
  function clockText(sec) {
    sec = Math.max(0, sec);
    var m = Math.floor(sec / 60), s = Math.floor((sec - m * 60) * 10) / 10;
    return m + ":" + (s < 10 ? "0" : "") + s.toFixed(1);
  }
  function gaugeMax(v) { return v <= 2500 ? 2500 : Math.ceil(v / 500) * 500; }
  function meterElapsed() { return Math.max(0, (M.running ? Date.now() / 1000 : M.end) - M.t0); }
  function meterRate() {                       // [tokens per second or null, from real timings?]
    if (M.ms > 0) return [M.timedOut / (M.ms / 1000), true];
    var el = meterElapsed();
    return (!M.calls || el < 0.2) ? [null, false] : [M.out / el, false];
  }
  function renderMeter() {
    if (!$("ag-meter")) return;
    $("ag-clock-t").textContent = clockText(meterElapsed());
    var r = meterRate(), v = r[0], max = gaugeMax(v || 0), f = v == null ? 0 : Math.min(1, v / max);
    $("ag-gauge-val").style.strokeDasharray = (f * GAUGE_LEN).toFixed(1) + " " + (GAUGE_LEN + 1).toFixed(1);
    $("ag-gauge-ndl").style.transform = "rotate(" + (-90 + 180 * f).toFixed(1) + "deg)";
    $("ag-gauge-max").textContent = (max / 1000) + "k";
    $("ag-gauge-n").textContent = v == null ? "\u2013" : Math.round(v).toLocaleString("en-US");
    $("ag-gauge-cap").textContent = (v == null || r[1]) ? "avg tokens/s" : "avg tokens/s (wall clock)";
    $("ag-gauge-box").title = v == null ? "Average tokens per second of this run. No model reply yet."
      : r[1] ? "Average speed of the model in this run: " + M.timedOut.toLocaleString("en-US") + " output tokens over " +
               (M.ms / 1000).toFixed(1) + " s of answering, in " + M.calls + " call" + (M.calls === 1 ? "" : "s") + "."
             : "The demo model has no real timings, so this is output tokens divided by wall-clock time.";
    $("ag-gauge").setAttribute("aria-label", v == null ? "Average tokens per second: no data yet"
      : "Average " + Math.round(v) + " tokens per second");
    // estimated spend: the sum of what each model call of this run cost at the model's list price
    var demo = M.calls > 0 && M.free === M.calls;
    $("ag-spend-n").textContent = spendText(M.cost);
    $("ag-spend-cap").textContent = demo ? "est. spend (demo is free)" : "est. spend";
    $("ag-spend-box").title = !M.calls ? "Estimated cost of this run's model calls. No model reply yet."
      : demo ? "The demo model is scripted and offline, so this run costs nothing."
             : "Estimated from the " + M.tokens.toLocaleString("en-US") + " tokens of this run's " + M.calls + " model call" +
               (M.calls === 1 ? "" : "s") + " at the model's list price. A run compared with no memory spends about the same again.";
  }
  function meterEvent(ev) {
    switch (ev.kind) {
      case "goal":
        if (M.timer) clearInterval(M.timer);
        M = { t0: ev.t || Date.now() / 1000, end: 0, running: true, timer: null, out: 0, timedOut: 0, ms: 0, calls: 0, cost: 0, tokens: 0, free: 0 };
        $("ag-meter").hidden = false;
        M.timer = setInterval(renderMeter, 100);
        renderMeter();
        break;
      case "model_reply":
        M.calls++; M.out += ev.output_tokens || 0;
        M.cost += ev.cost_usd || 0; M.tokens += (ev.input_tokens || 0) + (ev.output_tokens || 0);
        if (ev.estimated && !ev.cost_usd) M.free++;
        if (ev.latency_ms > 0) { M.timedOut += ev.output_tokens || 0; M.ms += ev.latency_ms; }
        renderMeter();
        break;
      case "done":
      case "error":
        if (M.timer) { clearInterval(M.timer); M.timer = null; }
        if (M.running) { M.running = false; M.end = ev.t || Date.now() / 1000; }
        renderMeter();
        break;
    }
  }

  function onEvent(ev) {
    gEvent(ev);
    meterEvent(ev);
    laneTag = ev.lane && state.parallelRun ? "[" + ev.lane + "]" : "";
    switch (ev.kind) {
      case "goal":
        clear(repl); clear($("ag-log")); clear($("ag-stages")); $("ag-summary").hidden = true; clearShots();
        renderCapNotice([]);
        $("ag-answer").textContent = "…"; $("ag-answer-call").textContent = "working";
        forceWait(); state.parallelRun = false;
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
        var ask = (ev.deep ? "Asking the model to think it through" : "Asking the model") + (ev.label ? " [" + ev.label + "]" : "") + "…";
        // Several calls are in flight in a parallel build, so each gets its own plain line
        // (its reply line says how long it took) instead of one shared ticking counter.
        if (laneTag) { say(ask, "model"); break; }
        forceWait();                 // one call at a time: start clean
        startWait(ask);
        break;
      case "model_wait":
        if (ev.rate_limited && typeof ev.limit === "number" && typeof ev.wait_s === "number")
          say("The model API is rate limiting us. Slowing down to " + ev.limit + " call" + (ev.limit === 1 ? "" : "s") +
            " at a time and waiting " + (Math.round(ev.wait_s * 10) / 10) + " s.", "fail");
        else say(ev.message, "fail");
        break;
      case "capability_notice":
        renderCapNotice(ev.gaps);
        break;
      case "coverage":
        coverageBlock(ev);
        break;
      case "summary":
        renderSummary(ev);
        break;
      case "visual_start":
        say(ev.round === 2 ? "Taking screenshots again to check the fixes." : "Taking screenshots of " + ev.pages + " page(s) of the finished app to check the result.", "build");
        break;
      case "screenshot":
        if (ev.ok) { say("Screenshot " + ev.n + ": " + ev.label); addShot(ev); }
        else say("Could not take a screenshot of " + ev.label + ": " + (ev.error || "unknown error"), "fail");
        break;
      case "visual_review":
        var visLeft = Array.isArray(ev.problems) ? ev.problems.filter(Boolean).map(String) : [];
        if (typeof ev.skipped === "string" && ev.skipped) say("Screenshot check skipped: " + ev.skipped);
        else if (ev.done) say("Screenshot check: the pages show what the goal asks for.", "pass");
        else say("Screenshot check found " + visLeft.length + " problem(s): " + visLeft.join("; "), "fail");
        break;
      case "visual_fix":
        say("Fixing what the screenshots showed…", "build");
        break;
      case "model_reply":
        if (!laneTag) stopWait();
        var mr = "Model replied" + (ev.latency_ms > 0 ? " in " + (ev.latency_ms / 1000).toFixed(1) + " s" : "") +
          " · " + ((ev.input_tokens || 0) + (ev.output_tokens || 0)) + " tokens" +
          (ev.estimated ? " (estimated)" : "") + (ev.cost_usd ? " · $" + ev.cost_usd.toFixed(5) : "");
        if (ev.thinking) mr += " · thought first" + (ev.reasoning_tokens != null ? " (" + ev.reasoning_tokens + " reasoning tokens)" : "");
        if (ev.thinking_fallback) mr += " · thinking ran out of budget, answered without it";
        say(mr, "model");
        if (typeof ev.reasoning === "string" && ev.reasoning.length)
          say("Reasoning: " + (ev.reasoning.length > 300 ? ev.reasoning.slice(0, 300) + "…" : ev.reasoning), "reasoning");
        break;
      case "decision":
        if (ev.action === "build") {
          state.ghost = ev.plan && ev.plan.name ? ev.plan.name : "new-tool";
          say("Decision: BUILD “" + state.ghost + "” — " + (ev.plan.description || ""), "build");
          if (ev.plan.definition) { replLine("c", ";; candidate (not saved until its tests pass)"); replLine("def", ev.plan.definition); }
        } else if (ev.action === "cache") {
          say("Decision: CACHED — " + ev.plan.why, "use");
        } else if (ev.action === "plan") {
          say("Decision: PLAN — " + ((ev.plan && ev.plan.why) || "split the goal into small functions"), "build");
        } else {
          say("Decision: REUSE — " + ((ev.plan && ev.plan.why) || "existing tool"), "use");
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
        if (!ev.ok && ev.detail) say("Why" + (ev.class ? " [" + (CLASS_TEXT[ev.class] || ev.class) + "]" : "") + ": " + ev.detail, "fail");
        say((ev.ok ? "All tests passed" : "Tests FAILED") + (ev.risk ? " · risk " + ev.risk : "") + (ev.ok ? "" : " — " + ev.reason), ev.ok ? "pass" : "fail");
        break;
      case "plan":
        say("Plan: " + (ev.steps.length === 1 ? "1 small tool: " : ev.steps.length + " small tools, listed so that each comes after the ones it calls: ") + ev.steps.map(function (x) { return x.name || "?"; }).join(" → "), "build");
        break;
      case "parallel":
        state.parallelRun = true;
        say("Building " + (ev.names || []).length + " functions at the same time" + (ev.limit ? " (up to " + ev.limit + " model calls at once)" : "") +
          ". Functions that call another one wait for it.", "build");
        break;
      case "step":
        say("Step " + ev.i + " of " + ev.n + ": " + (ev.name || "tool") + " — " + ev.spec, "build");
        replLine("c", ";; step " + ev.i + "/" + ev.n + ": " + (ev.name || ""));
        break;
      case "step_wait":
        say((laneTag ? "Waiting" : ev.name + " is waiting") + " for " + (ev.on || []).join(", ") + " to be saved first, because it calls " +
          ((ev.on || []).length === 1 ? "it" : "them") + ".", "build");
        break;
      case "oracle_corrected":
        say("Independent check: for " + ev.call + " the model expected " + ev.was + ", but the " + ev.algo + " reference says " + ev.expected + ", which is exactly what the code returns. The test was wrong, not the code.", "pass");
        break;
      case "oracle_reference":
        say("Independent check: the " + ev.algo + " reference says " + ev.call + " is " + ev.expected + " but the code returned " + ev.got + ". The code is wrong here.", "fail");
        break;
      case "bad_reply":
        say("The model's reply was unreadable even after retries. That attempt is skipped; trying again.", "fail");
        break;
      case "kit_seeded":
        say("Added the web kit to this project (" + ev.tools.length + " tested helper tools such as " + ev.tools.slice(0, 3).join(", ") + "). No tokens used.", "build");
        break;
      case "style_repair":
        say("The pages use " + (ev.missing || []).length + " class" + ((ev.missing || []).length === 1 ? "" : "es") +
          " that the stylesheet " + (ev.sheet || "") + " has no rule for (" + (ev.missing || []).slice(0, 8).join(", ") +
          ((ev.missing || []).length > 8 ? ", …" : "") + "). Adding the missing rules.", "build");
        break;
      case "style_check":
        if (Array.isArray(ev.undefined) && ev.undefined.length)
          say("Checked the pages' HTML against the stylesheet: " + ev.undefined.length + " class" + (ev.undefined.length === 1 ? "" : "es") +
            " used by the pages " + (ev.undefined.length === 1 ? "has" : "have") + " no rule (" + ev.undefined.slice(0, 8).join(", ") +
            (ev.undefined.length > 8 ? ", …" : "") + "). Those parts render unstyled.", "fail");
        else say("Checked the pages' HTML against the stylesheet: every class the pages use has a rule.", "pass");
        break;
      case "step_kept":
        say("Left " + (ev.name || "this function") + " as it was: the model judged that the saved version already does what this step asks." +
          (ev.why ? " Its reason: " + ev.why : ""), "fail");
        break;
      case "step_failed":
        say("Could not build " + (ev.name || "this function") + " after every repair attempt. Skipping it and building the rest so the app still gets wired up." + (ev.detail ? " Last failure: " + ev.detail : ""), "fail");
        break;
      case "smoke":
        say(ev.ok ? "Checked the finished app with " + ev.call + ": it answered with status " + ev.status + ". No model call needed."
                  : "The finished app failed its own check (" + ev.call + "): " + (ev.error || "no answer"), ev.ok ? "pass" : "fail");
        break;
      case "acceptance":
        // the harness drove the finished app like a visitor; a failed check is shown with its detail
        var accRes = Array.isArray(ev.results) ? ev.results.filter(Boolean) : [];
        var accP = typeof ev.passed === "number" ? ev.passed : accRes.filter(function (r) { return r.ok === true; }).length;
        var accF = typeof ev.failed === "number" ? ev.failed : accRes.filter(function (r) { return r.ok === false; }).length;
        if (accF > 0) {
          say("Tried the app like a visitor: " + accF + " of " + (accP + accF) + " checks failed.", "fail");
          accRes.filter(function (r) { return r.ok === false; }).forEach(function (r) {
            say("Failed: " + String(r.label || r.id || "a check") + (r.detail ? " — " + String(r.detail) : ""), "fail");
          });
        } else if (accP > 0) {
          var accNames = accRes.filter(function (r) { return r.ok === true; }).slice(0, 3).map(function (r) { return String(r.label || r.id || ""); }).join("; ");
          say("Tried the app like a visitor: all " + accP + " checks passed" + (accNames ? " (" + accNames + ")" : "") + ".", "pass");
        }
        break;
      case "interface_check":
        var ifProb = Array.isArray(ev.problems) ? ev.problems.filter(Boolean) : [];
        if (!ifProb.length) say("Checked that the functions fit together: no mismatch found.", "pass");
        else {
          say("The functions do not fit together in " + ifProb.length + " place(s):", "fail");
          ifProb.slice(0, 6).forEach(function (p) { say(String(p.detail || p.where || ""), "fail"); });
          if (ifProb.length > 6) say("…and " + (ifProb.length - 6) + " more", "fail");
        }
        break;
      case "visual_rollback":
        say("Undid the last round of visual fixes" + (ev.reason ? ": " + String(ev.reason).replace(/\.\s*$/, "") : "") +
          ". The app is back to the version before it.", "fail");
        break;
      case "tamper":
        var tChanged = Array.isArray(ev.changed) ? ev.changed.filter(Boolean).map(String) : [];
        say("Rejected " + (ev.name || "a candidate function") + ": it changed a function it does not own (" + tChanged.join(", ") +
          "). The Lisp process was restarted from the saved functions.", "fail");
        break;
      case "retired":
        (ev.tools || []).forEach(function (t) {
          say("Retired the leftover function " + t.name + ": it " + t.reason + ". It is kept on disk but no longer offered to the model.", "build");
        });
        reloadTools();
        break;
      case "test_call_repair":
        say("The test call is broken, not the code. Keeping the code and asking the model to fix only the tests.", "build");
        break;
      case "repeat_candidate":
        say("The model sent the same code as a failed attempt; skipping it instead of re-running.", "fail");
        break;
      case "rescue":
        say("Stopping code changes: " + (ev.reason || "the tests look wrong") + ". Keeping the code and switching to property tests.", "build");
        break;
      case "replan":
        say("This step kept failing its tests. Asking the model to split it into smaller tools...", "fail");
        break;
      case "gave_up":
        if (ev.app) { say((ev.detail || "The app is not complete.") + (ev.hint ? " " + ev.hint : ""), "fail"); break; }
        say("Gave up after " + ev.attempts + " attempts. Nothing was saved: the tool never passed its own tests. " +
          (ev.hint ? "Likely cause: " + ev.hint + " " : "") +
          "Try a smaller, more specific prompt (for example one function with concrete inputs); big tasks work better as small tools built one at a time.", "fail");
        break;
      case "repair":
        say("Repair round " + ev.attempt + ": " + ev.reason, "fail");
        break;
      case "edit":
        var editN = ev.edits || 0;
        say("Repaired " + (ev.name || "the function") + " with " + editN + " small edit" + (editN === 1 ? "" : "s") + " instead of rewriting it" +
          (ev.saved_chars > 0 ? " (about " + Math.round(ev.saved_chars / 4) + " tokens not repeated)" : "") + ".", "build");
        break;
      case "edit_failed":
        say("The model's small edit could not be applied" + (ev.reason ? " (" + ev.reason + ")" : "") + ". Asking for the whole function instead.", "fail");
        break;
      case "tests_kept":
        // the reply left its tests out, so the tests it already had are kept: no log line, it would be noise
        break;
      case "promoted":
        say("Saved “" + ev.name + "” to the tool registry", "pass");
        state.ghost = null; state.hot = ev.name;   // gEvent reloads the tools and animates the new node
        break;
      case "result":
        say("Answer: " + (ev.ok ? ev.value : "failed — " + ev.error), ev.ok ? "answer" : "fail");
        var av = ev.ok ? String(ev.value) : "—";
        if (av.charAt(0) === '"' && av.indexOf("\n") >= 0)                        // multi-line string result: show it as text art
          av = av.slice(1, av.charAt(av.length - 1) === '"' ? -1 : undefined).replace(/^\n/, "");
        $("ag-answer").textContent = av;
        $("ag-answer").className = av.indexOf("\n") >= 0 ? "multi" : "";
        $("ag-answer-call").textContent = ev.call;
        break;
      case "error":
        forceWait();
        say("Error: " + (/429|rate|traffic|queue/i.test(ev.message || "") ? "the model API is rate-limited right now (not a problem with your prompt). " : "") + ev.message, "fail");
        if (/demo model only knows/i.test(ev.message || "") && state.liveOk) {
          var sw = h("button", "ag-chip", "Switch to Live and send it again");
          sw.addEventListener("click", function () { $("ag-mode").value = "live"; setModeNote(); send(); });
          $("ag-log").appendChild(sw);
        } state.ghost = null; drawGraph();
        $("ag-answer").textContent = "—"; $("ag-answer-call").textContent = "no answer";
        break;
      case "cancel_requested":
        // The click already logged it; a cancel sent from another tab still needs the button to show it.
        setCancelling();
        break;
      case "cancelled":
        state.cancelled = true;
        var saved = Array.isArray(ev.saved) ? ev.saved.filter(Boolean).map(String) : [];
        say("Cancelled. " + (saved.length ? "Kept what was already saved: " + saved.join(", ") + "." : "Nothing new had been saved yet."));
        forceWait(); state.ghost = null;
        $("ag-answer").textContent = "—"; $("ag-answer-call").textContent = "cancelled, no answer";
        break;
      case "done":
        forceWait();
        state.ghost = null;
        say("Finished (" + ev.state + ") · " + ev.model_calls + " model call(s) · " +
          ((ev.input_tokens || 0) + (ev.output_tokens || 0)) + " tokens", "done");
        break;
    }
    laneTag = "";
  }

  // ------------------------------------------- live build choreography (events -> graph)
  // Per-step run facts live in G.rs (keyed by name) so they survive until the node exists.
  function rs(name) { return G.rs[name] || (G.rs[name] = { repairs: 0, phase: "", flashN: 0 }); }
  function markDirty() { G.dirty = true; }
  function flushSync() { if (G.dirty) { G.dirty = false; syncGraph(); } }
  function plural(n, w) { return n + " " + w + (n === 1 ? "" : "s"); }

  function resetRunState() {
    G.plan = []; G.failed = {}; G.promoted = {}; G.spawn = {}; G.animate = {}; G.split = {}; G.rs = {}; G.lanes = {}; G.parallel = 0;
    G.active = null; G.stepI = 0; G.stepN = 0; G.stepSub = false; G.splitOf = null; G.kitNew = null;
    G.note = ""; G.noteBad = false; G.running = false; G.finished = false; G.lit = null; G.lastSaved = null;
  }
  var PHASE_NOTE = { thinking: "asking the model", testing: "running tests", repairing: "tests failed, repairing", splitting: "splitting into smaller functions", passed: "tests passed", waiting: "waiting for a function it calls" };
  function buildNote() {
    var lanes = Object.keys(G.lanes);
    if (lanes.length >= 2) {
      // several functions at once: name up to four, then count the rest
      var more = lanes.length > 4 ? " and " + (lanes.length - 4) + " more" : "";
      var multi = "Building " + lanes.length + " functions at once: " + lanes.slice(0, 4).join(", ") + more;
      if (G.parallel > 0) multi += " · up to " + G.parallel + " model calls at a time";
      G.note = multi;
      return;
    }
    var name = lanes.length === 1 ? lanes[0] : G.active;
    if (!name) return;
    var r = rs(name);
    var idx = r.stepN != null ? r.stepI : G.stepI, tot = r.stepN != null ? r.stepN : G.stepN;
    var s = "Building " + name + (tot ? " (" + idx + " of " + tot + ")" : "");
    if (G.stepSub && G.splitOf && G.splitOf !== name) s += " — part of " + G.splitOf;
    if (r.repairs) s += " · attempt " + (r.repairs + 1);
    if (PHASE_NOTE[r.phase]) s += " · " + PHASE_NOTE[r.phase];
    G.note = s;
  }

  function finishRun(ev) {
    if (!G.running && G.finished) return;
    var built = Object.keys(G.promoted).length, planned = G.plan.length, failed = Object.keys(G.failed).length;
    G.running = false; G.finished = true; G.active = null; G.lanes = {};
    Object.keys(G.rs).forEach(function (k) { G.rs[k].phase = ""; });
    var ok = !ev || ev.state === "done" || ev.state == null;
    if (ev && ev.state === "cancelled") {
      G.noteBad = true;
      G.note = planned ? "Cancelled — " + built + " of " + planned + " planned functions were saved before stopping."
                       : built ? "Cancelled — " + plural(built, "function") + " saved before stopping." : "Cancelled — nothing new was saved.";
    } else if (ok && !failed) {
      G.noteBad = false;
      G.note = built ? "Finished — saved " + (planned > built ? built + " of " + planned + " planned functions" : plural(built, "function")) + " this run."
                     : "Finished — answered with functions that already exist.";
    } else {
      G.noteBad = true;
      G.note = "Stopped — " + (planned ? built + " of " + planned + " planned functions saved; the dashed ones were not built." : (built ? plural(built, "function") + " saved." : "nothing new was saved."));
    }
    markDirty();
  }

  function gEvent(ev) {
    var name = ev.lane || G.active;
    switch (ev.kind) {
      case "goal":
        resetRunState();
        G.running = true;
        G.note = "Reading your goal…";
        if (!G.dragging) { state.vpTouched = false; G.lastFit = ""; }
        break;
      case "coverage":
        var cmiss = Array.isArray(ev.missing) ? ev.missing : [];
        if (cmiss.length) G.note = "Goal check: " + plural(cmiss.length, "feature") + " not covered yet" + (ev.extended ? "; asking the planner to add " + (cmiss.length === 1 ? "it" : "them") : "") + ".";
        else if (Array.isArray(ev.features) && ev.features.length) G.note = "Goal check: every feature the goal asks for is covered.";
        break;
      case "kit_seeded":
        G.kitNew = (ev.tools || []).slice();
        G.note = "Adding the web kit: " + plural(G.kitNew.length, "helper function") + ", no tokens used.";
        reloadTools();
        break;
      case "plan":
        var steps = ev.steps || [], sub = !!ev.sub;
        if (!sub) { G.plan = []; G.split = {}; }
        var D = Math.min(Math.max(steps.length - 1, 0), 4);
        var pn = ev.lane || G.active;
        var parent = sub && pn ? G.nodes[pn] : null;
        var ph = parent ? Math.max(0, (parent.depth || 0) - 1) : 0;
        steps.forEach(function (s, i) {
          var nm = s.name || ("step-" + (i + 1));
          var have = G.plan.filter(function (p) { return p.name === nm; })[0];
          if (have) { have.spec = s.spec || have.spec; return; }
          G.plan.push({ name: nm, spec: s.spec || "", hint: sub ? ph : (steps.length <= 1 ? 0 : Math.round(i * D / (steps.length - 1))) });
        });
        if (!G.dragging) { state.vpTouched = false; G.lastFit = ""; }
        G.note = sub ? "Splitting " + (G.splitOf || "a step") + " into " + plural(steps.length, "smaller function") + "…"
                     : "Planning " + plural(steps.length, "function") + "…";
        break;
      case "parallel":
        G.parallel = ev.limit || 0;
        break;
      case "step":
        G.active = ev.name || ("step-" + ev.i);
        G.stepI = ev.i || 0; G.stepN = ev.n || 0; G.stepSub = !!ev.sub;
        rs(G.active).phase = "building";
        rs(G.active).stepI = G.stepI; rs(G.active).stepN = G.stepN;
        if (ev.lane) G.lanes[ev.lane] = true;
        if (!G.plan.some(function (p) { return p.name === G.active; }) && !toolByName(G.active)) {
          G.plan.push({ name: G.active, spec: ev.spec || "", hint: ev.n > 1 ? Math.round((ev.i - 1) * Math.min(ev.n - 1, 4) / (ev.n - 1)) : 0 });
        }
        delete G.failed[G.active];
        buildNote();
        break;
      case "step_wait":
        rs(ev.name).phase = "waiting";
        if (G.lanes[ev.name] || G.active === ev.name) buildNote();
        break;
      case "step_done":
        delete G.lanes[ev.name];
        rs(ev.name).phase = "";
        if (ev.ok === false) G.failed[ev.name] = true;
        if (Object.keys(G.lanes).length) buildNote();
        break;
      case "model_call":
        if (name) { rs(name).phase = "thinking"; buildNote(); }
        else if (G.running && !G.plan.length) G.note = "Asking the model…";
        break;
      case "model_reply":
        if (name && rs(name).phase === "thinking") { rs(name).phase = "testing"; buildNote(); }
        break;
      case "verdict":
        if (name) {
          var r = rs(name);
          if (ev.ok) r.phase = "passed";
          else { r.flashN++; r.phase = "repairing"; }
          buildNote();
        }
        break;
      case "repair":
      case "test_call_repair":
        if (name) { var q = rs(name); q.repairs++; q.phase = "thinking"; buildNote(); }
        break;
      case "replan":
        if (name) { rs(name).phase = "splitting"; G.split[name] = true; G.splitOf = name; buildNote(); }
        break;
      case "promoted":
        G.promoted[ev.name] = true; delete G.failed[ev.name]; delete G.split[ev.name]; delete G.lanes[ev.name];
        if (G.rs[ev.name]) G.rs[ev.name].phase = "";
        if (!G.replay) { G.spawn[ev.name] = true; G.animate[ev.name] = { at: nowMs() + 380, i: 0 }; }
        G.lastSaved = ev.name;
        G.note = "Saved " + ev.name;
        if (Object.keys(G.lanes).length) buildNote();   // other functions are still being built
        reloadTools().then(function () {
          var n = G.nodes[ev.name];
          if (G.lastSaved === ev.name && n && n.tool && G.note.indexOf("Saved " + ev.name) === 0) {
            G.note = "Saved " + ev.name + " · " + (n.calls.length ? "connected to " + plural(n.calls.length, "function") : "calls no other function");
            renderStatus();
          }
        });
        break;
      case "step_failed":
        if (ev.name) { G.failed[ev.name] = true; rs(ev.name).phase = ""; }
        break;
      case "gave_up":
        G.noteBad = true;
        if (ev.app) {              // about the finished app as a whole, not about one function
          G.note = ev.detail || "The app is not complete.";
          break;
        }
        if (name) { G.failed[name] = true; rs(name).phase = ""; }
        G.note = "Gave up on " + (name || "this step") + (ev.attempts ? " after " + plural(ev.attempts, "attempt") : "") + " — nothing was saved for it.";
        break;
      case "error":
        if (name) G.failed[name] = true;
        G.noteBad = true;
        G.note = "Stopped by an error.";
        break;
      case "done":
        finishRun(ev);
        break;
      default:
        return;
    }
    markDirty();
  }

  // Reloads the tool list; calls arriving while one is in flight are merged into one follow-up.
  function reloadTools() {
    if (G.reloading) { G.reloadAgain = true; return G.reloadP; }
    var pid = state.project;
    G.reloading = true;
    G.reloadP = api("GET", "/api/agent/tools?mode=" + state.mode + "&" + projectQuery()).then(function (r) {
      if (pid === state.project && r.status === 200 && r.data && r.data.tools) { state.tools = r.data.tools; drawGraph(); showDetail(); }
    }).catch(function () { /* the next event retries */ }).then(function () {
      G.reloading = false;
      if (G.reloadAgain) { G.reloadAgain = false; return reloadTools(); }
    });
    return G.reloadP;
  }

  // ------------------------------------------------ prompt draft, continue, re-attach
  var DRAFT_KEY = "gg.draft.", DISMISS_KEY = "gg.resumeDismissed.";
  function lsGet(k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { if (v) window.localStorage.setItem(k, v); else window.localStorage.removeItem(k); } catch (e) { /* storage blocked: nothing is remembered */ } }
  function saveDraft() { lsSet(DRAFT_KEY + state.project, $("ag-prompt").value); }
  function setPrompt(v) { $("ag-prompt").value = v; saveDraft(); }

  function lastUnfinished() {
    var mains = state.history.filter(function (r) { return r.arm !== "nomem" && !r.oracle && r.prompt; });
    var last = mains[mains.length - 1];
    if (!last || last.state === "done") return null;
    if (state.session && last.session_id === state.session && (state.busy || G.running)) return null;
    return last;
  }
  function updateResume() {
    var box = $("ag-resume");
    // A build in progress (sent from this page, or re-attached after a reload) is never "unfinished".
    var last = state.activeChecked && !state.busy && !G.running && !state.attaching ? lastUnfinished() : null;
    if (last && lsGet(DISMISS_KEY + state.project) === last.session_id) last = null;
    box.hidden = !last;
    if (!last) return;
    state.resumeRow = last;
    var p = $("ag-resume-p");
    clear(p);
    var q = h("q", "", trunc(last.prompt, 90));
    q.title = last.prompt;
    p.appendChild(q);
    p.appendChild(h("span", "muted", " (" + (last.mode === "live" ? "Live" : "Demo") + " model)"));
    var go = $("ag-resume-go"), blocked = last.mode === "live" && !state.liveOk;
    go.disabled = state.busy || blocked;
    go.title = blocked ? "This build used the Live model, which is not available right now." : "";
  }

  function checkActive() {
    if (window.location.protocol === "file:") return Promise.resolve();
    return api("GET", "/api/agent/active").then(function (r) {
      state.activeChecked = true;
      var a = r.status === 200 && r.data && r.data.session_id ? r.data : null;
      renderActiveNote(a);
      if (a && a.project === state.project && !state.busy && state.session !== a.session_id) attach(a);
    }).catch(function () { state.activeChecked = true; }).then(updateResume);
  }
  function renderActiveNote(a) {
    var box = $("ag-active-note");
    clear(box);
    var other = a && a.project !== state.project ? a : null;
    box.hidden = !other;
    if (!other) return;
    var proj = findProject(other.project);
    box.appendChild(h("span", "", "A build is running in " + (proj ? "project “" + proj.name + "”" : "another project") + ": “" + trunc(other.prompt || "", 70) + "”. "));
    if (proj) {
      var b = h("button", "ag-ghost", "Switch to it");
      b.type = "button";
      b.addEventListener("click", function () { switchProject(proj.id); checkActive(); });
      box.appendChild(b);
    }
  }
  // Re-attach to a run already in progress: replay its events from the start so the graph and log rebuild.
  function attach(a) {
    var opt = $("ag-mode");
    if (a.mode && a.mode !== state.mode && Array.prototype.some.call(opt.options, function (o) { return o.value === a.mode && !o.disabled; })) {
      opt.value = a.mode; setModeNote();
    }
    state.view = "session"; state.picked = true; state.pick = null;
    state.session = a.session_id; state.next = 0; state.attaching = true; state.attached = true;
    setBusy(true);
    if (state.timer) clearInterval(state.timer);
    state.timer = setInterval(function () { poll(); }, POLL_MS);
    poll();
  }

  // ---------------------------------------------------------- command box (CLI apps)
  function splitArgs(text) {
    var out = [], cur = "", inq = false, has = false, i, c;
    text = String(text).replace(/[“”]/g, '"');
    for (i = 0; i < text.length; i++) {
      c = text.charAt(i);
      if (c === '"') { inq = !inq; has = true; }
      else if (c === "\\" && inq && text.charAt(i + 1) === '"') { cur += '"'; i++; }
      else if (/\s/.test(c) && !inq) { if (has || cur) out.push(cur); cur = ""; has = false; }
      else cur += c;
    }
    if (has || cur) out.push(cur);
    return out;
  }
  function updateCmdBox() {
    var f = $("ag-cmd");
    var has = state.tools.some(function (t) { return t.name === "handle-command"; });
    if (f.hidden === has) f.hidden = !has;
    if (!has) $("ag-cmd-out").hidden = true;
  }
  function runCommand(e) {
    e.preventDefault();
    var inp = $("ag-cmd-in"), out = $("ag-cmd-out"), btn = $("ag-cmd-run");
    var text = inp.value.trim();
    if (!text) { inp.focus(); return; }
    var pid = state.project;
    btn.disabled = true; out.hidden = false; out.className = "ag-cmd-out"; out.textContent = "running…";
    function fail(msg) { out.className = "ag-cmd-out bad"; out.textContent = msg; }
    api("POST", "/api/agent/projects/" + encodeURIComponent(pid) + "/command", { args: splitArgs(text) }).then(function (r) {
      if (pid !== state.project) return;
      var d = r.data || {};
      if (r.status === 200 && d.ok) out.textContent = d.output == null || d.output === "" ? "(no output)" : String(d.output);
      else fail(d.error ? String(d.error) : "The command did not run (HTTP " + r.status + ").");
    }, function () {
      if (pid === state.project) fail("Could not run the command: this dashboard server may not support commands yet.");
    }).then(function () { btn.disabled = false; });
  }

  // --------------------------------------------------------- run control
  function setBusy(b) {
    state.busy = b;
    $("ag-send").disabled = b;
    $("ag-demo").disabled = b;
    $("ag-cancel").hidden = !b;
    restoreCancel();
    setProjectLock(b);
    updateResume();
  }
  function restoreCancel() { var cx = $("ag-cancel"); cx.disabled = false; cx.textContent = "Cancel build"; }
  function setCancelling() { var cx = $("ag-cancel"); cx.disabled = true; cx.textContent = "Cancelling…"; }
  // Stops the build in progress. The server stops once the model calls already under way return.
  function cancelRun() {
    if (!state.busy) return;
    setCancelling();
    say("Cancel requested. Stopping as soon as the model calls already under way return…");
    api("POST", "/api/agent/cancel").then(function (r) {
      var d = r.data || {};
      if (r.status === 200 && d.cancelled === true) return;   // the run itself reports "cancelled" next
      restoreCancel();
      if (r.status === 200) say("Nothing was running to cancel.");
      else say("Could not cancel the build (HTTP " + r.status + ").", "fail");
    }, function () {
      restoreCancel();
      say("Could not reach the server to cancel.", "fail");
    });
  }

  var POLL_MS = 150;
  function poll(done) {
    if (!state.session || state.pollBusy) return;
    var sid = state.session;
    state.pollBusy = true;
    api("GET", "/api/agent/sessions/" + sid + "?since=" + state.next).then(function (r) {
      state.pollBusy = false;
      if (sid !== state.session || r.status !== 200) return;
      var s = r.data, evs = s.events || [];
      if (evs.length) {
        G.replay = !!state.attaching;   // a re-attached run replays quietly: no animations
        evs.forEach(onEvent);
        G.replay = false;
        flushSync();
      }
      state.attaching = false;
      tickWait();
      state.next = s.next;
      if (s.state !== "running") {
        if (s.compare !== "running") {
          clearInterval(state.timer); state.timer = null;
          if (G.running) { finishRun(null); flushSync(); }
          setTimeout(function () { state.hot = null; drawGraph(); }, 2500);
          loadProjects();
          refresh().then(function () {
            if (done) done(true);
            else if (state.attached) { state.attached = false; setBusy(false); }
          });
        } else {
          $("ag-compare-note").textContent = "Checking against a no-memory run…";
        }
      }
    }, function () { state.pollBusy = false; });
  }

  function refresh() {
    var pid = state.project;   // a reply for a project the user has since left is ignored
    var a = api("GET", "/api/agent/tools?mode=" + state.mode + "&" + projectQuery()).then(function (r) {
      if (pid !== state.project) return;
      state.tools = r.data.tools || []; drawGraph(); showDetail();
    });
    var b = api("GET", "/api/agent/history?" + projectQuery()).then(function (r) {
      if (pid !== state.project) return;
      // Like the functions, the prompt log is the chosen model's: a demo run must never
      // appear among a project's real prompts (rows from before modes were recorded stay).
      var mode = state.mode;
      state.history = (r.data.sessions || []).filter(function (x) { return !x.mode || x.mode === mode; });
    });
    return Promise.all([a, b]).then(function () {
      if (pid !== state.project) return;
      redrawEvidence(); $("ag-compare-note").textContent = ""; updateResume();
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
    var visual = $("ag-visual").checked && !$("ag-visual").disabled;
    return api("POST", "/api/agent/prompt", { prompt: prompt, mode: mode, compare: compare, expected: expected, oracle: oracle, project: state.project, visual: visual }).then(function (r) {
      if (r.status !== 200) { setBusy(false); throw new Error((r.data && r.data.error) || "request failed"); }
      state.session = r.data.session_id; state.next = 0;
      setPrompt(prompt);
      return new Promise(function (resolve) {
        if (state.timer) clearInterval(state.timer);
        state.timer = setInterval(function () { poll(function () { resolve(); }); }, POLL_MS);
      });
    });
  }

  // Bring the build workspace to the top of the viewport once, when a run starts.
  function showBuild() {
    var b = $("ag-build");
    if (b && b.scrollIntoView) b.scrollIntoView({ behavior: reduced() ? "auto" : "smooth", block: "start" });
  }

  function send() {
    var prompt = $("ag-prompt").value.trim();
    if (!prompt) { $("ag-prompt").focus(); return; }
    showBuild();
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
    showBuild();
    // The demo and its evidence belong to the builtin project: leave a custom project first.
    if (!currentProject().builtin) {
      var from = currentProject().name;
      selectProjectLocal(findBuiltin().id);
      $("ag-demo-cost").textContent = "Switched to " + findBuiltin().name + " first: the guided demo runs there, so “" + from + "” is not touched.";
    }
    state.view = "session"; state.picked = true;
    setBusy(true);
    api("POST", "/api/agent/reset", { project: state.project }).then(function () {
      state.tools = []; state.history = []; state.sel = null; state.view = "session"; showDetail();
      drawGraph(); redrawEvidence();
      var chain = Promise.resolve();
      state.cancelled = false;
      DEMO_STEPS.forEach(function (p, i) {
        chain = chain.then(function () {
          if (state.cancelled) return;   // a cancelled build ends the demo: no next prompt
          $("ag-demo").textContent = "Step " + (i + 1) + " of " + DEMO_STEPS.length + "…";
          return runPrompt(p.p, $("ag-mode").value, true, p.e);
        });
      });
      return chain;
    }).catch(function (e) { say("Demo stopped: " + e.message, "fail"); })
      .then(function () {
        setBusy(false);
        $("ag-demo").textContent = "▶ Run guided demo";
        $("ag-demo-cost").textContent = demoCostText();
      });
  }

  // ------------------------------------------------------------- projects
  // A project is its own tool registry, prompt log and history; every call that
  // reads or writes tools sends state.project. Switching, editing and deleting
  // are locked while a run is in progress (setBusy calls setProjectLock).
  var PROJ_KEY = "gg.project";
  var BUILTIN_FALLBACK = { id: "scratch", name: "Scratchpad", description: "", created: 0, updated: 0, tools: 0, builtin: true };
  function findProject(id) { return state.projects.filter(function (p) { return p.id === id; })[0] || null; }
  function findBuiltin() { return state.projects.filter(function (p) { return p.builtin; })[0] || BUILTIN_FALLBACK; }
  function currentProject() { return findProject(state.project) || findBuiltin(); }
  function projectQuery() { return "project=" + encodeURIComponent(state.project); }
  function savedProject() { try { return window.localStorage.getItem(PROJ_KEY); } catch (e) { return null; } }
  function saveProject(id) { try { window.localStorage.setItem(PROJ_KEY, id); } catch (e) { /* storage blocked: the choice lasts this page only */ } }
  function setProjErr(msg) { var e = $("ag-proj-err"); e.textContent = msg || ""; e.hidden = !msg; }
  function focusCurrentChip() { var b = $("ag-proj-chips").querySelector('button[aria-pressed="true"]'); if (b) b.focus(); }

  function demoCostText() {
    // the guided demo runs in Scratchpad, with the model Scratchpad was last set to
    var live = (currentProject().builtin ? $("ag-mode").value : (state.scratchMode || $("ag-mode").value)) === "live";
    var base = live
      ? "Runs the live model: 7 prompts, about 2 min, roughly 1 cent of real tokens"
      : "Runs the scripted demo model: 7 prompts, about 1 min, free, estimated tokens";
    var cur = currentProject();
    return cur.builtin ? base : base + ". It runs in " + findBuiltin().name + ", not in " + cur.name + ".";
  }

  // Disables everything that would change the project while a run owns the pane.
  function setProjectLock(b) {
    Array.prototype.forEach.call($("ag-proj-chips").querySelectorAll("button"), function (x) { x.disabled = b; });
    $("ag-proj-new").disabled = b;
    $("ag-proj-edit").disabled = b;
    $("ag-proj-delete").disabled = b;
    $("ag-proj-save").disabled = b || state.projPending;
    $("ag-proj-delgo").disabled = b || state.projPending;
    $("ag-proj-note").textContent = b ? "A run is in progress. Switching, editing and deleting projects unlock when it finishes." : "";
  }

  // Text that names the current project: Ask heading, helper line, tools heading, forget button.
  function updateProjectText() {
    var n = currentProject().name;
    $("ag-ask-h").textContent = "Ask the agent — in " + n;
    $("ag-proj-hint").textContent = state.tools.length
      ? "Follow-up prompts build on this project's tools. Ask to add a feature, or to change one (for example: make post titles link to the post)."
      : (currentProject().builtin && (currentProject().counts || {})[state.mode === "live" ? "demo" : "live"]
          ? "Scratchpad has " + currentProject().counts[state.mode === "live" ? "demo" : "live"] + " functions built with the other model. Switch the model selector to " +
            (state.mode === "live" ? "Demo" : "Live") + " to see and build on them."
          : "This project is empty. Describe what to build.");
    $("ag-tools-h").textContent = "Tools in " + n;
    $("ag-reset").textContent = "Forget this project's tools";
  }

  // A project other than Scratchpad is built with the Live model only: the scripted demo
  // model knows nothing but its example prompts. So inside a project the selector is held on
  // Live and the Demo option is switched off; back in Scratchpad the earlier choice returns.
  function applyProjectMode() {
    var sel = $("ag-mode"), demo = sel.querySelector('option[value="demo"]');
    var own = !currentProject().builtin;
    if (own && sel.value !== "live") state.scratchMode = sel.value;
    demo.disabled = own;
    demo.textContent = own ? "Demo (Scratchpad only)" : "Demo (free, chips only)";
    var want = own ? "live" : (state.scratchMode || sel.value);
    if (!own && want === "live" && !state.liveOk && state.configLoaded) want = "demo";
    if (sel.value !== want) sel.value = want;
    if (!own) state.scratchMode = null;
    state.mode = sel.value;
    modeTexts();
  }
  // The lines that describe the chosen model (no reloading).
  function modeTexts() {
    var m = state.mode, stuck = !currentProject().builtin && !state.liveOk && state.configLoaded;
    $("ag-mode-note").textContent = stuck
      ? "This project needs the Live model, which is unavailable: " + NOTES.live.replace(/^Live unavailable: /, "")
      : NOTES[m];
    $("ag-compare").checked = m === "demo";
    $("ag-visual").disabled = m !== "live";
    $("ag-visual").title = m === "live" ? "" : "Screenshots are checked by the Live model";
    $("ag-send").textContent = m === "live" ? "Send prompt (about \u00bd\u00a2)" : "Send prompt";
  }
  // How many functions of its own a project has, in the model its view uses.
  function projectCount(p) {
    if (!p.counts) return p.tools || 0;
    // Scratchpad's number is for the model it opens with, also while another project is open
    var scratch = currentProject().builtin ? state.mode : (state.scratchMode || state.mode);
    return p.counts[p.builtin ? scratch : "live"] || 0;
  }

  function renderProjects() {
    var cur = currentProject();
    var box = $("ag-proj-chips");
    var list = state.projects.length ? state.projects : [cur];
    clear(box);
    list.forEach(function (p) {
      var b = h("button", "ag-proj-chip");
      b.type = "button";
      b.setAttribute("aria-pressed", p.id === cur.id ? "true" : "false");
      b.title = p.description || p.name;
      b.appendChild(h("span", "ag-proj-cname", p.name));
      var cn = projectCount(p);
      b.appendChild(h("span", "ag-proj-cn", cn + (cn === 1 ? " function" : " functions")));
      b.addEventListener("click", function () { switchProject(p.id); });
      box.appendChild(b);
    });
    $("ag-proj-name").textContent = cur.name;
    $("ag-proj-desc").textContent = cur.description ? "— " + cur.description : "";
    $("ag-proj-edit").hidden = !!cur.builtin;
    $("ag-proj-delete").hidden = !!cur.builtin;
    $("ag-proj-edit").setAttribute("aria-label", "Edit project " + cur.name);
    $("ag-proj-delete").setAttribute("aria-label", "Delete project " + cur.name);
    setProjectLock(state.busy);
    updateProjectText();
    renderServer();
  }

  // Server row: the harness mounts this project's Lisp (handle-request request state) on a port.
  function renderServer() {
    var cur = currentProject();
    var m = cur.mount;
    var st = $("ag-srv-state"), link = $("ag-srv-link"), btn = $("ag-srv-toggle");
    st.textContent = m ? "running" : "stopped";
    st.className = "ag-srv-state" + (m ? " on" : "");
    link.hidden = !m;
    if (m) { link.href = m.url; link.textContent = m.url; }
    btn.textContent = m ? "Stop server" : "Run server";
    btn.setAttribute("aria-label", (m ? "Stop the server for " : "Run the server for ") + cur.name);
    btn.disabled = !!state.srvPending;
    $("ag-srv-info").textContent = m
      ? "Each request calls this project's Lisp tool handle-request (Live tools); its state is saved in SQLite."
      : "Mounts this project's Lisp tool handle-request on a local port. Build it first with a prompt such as: make this a web app.";
  }

  function toggleServer() {
    var cur = currentProject();
    state.srvPending = true;
    renderServer();
    api(cur.mount ? "DELETE" : "POST", "/api/agent/projects/" + encodeURIComponent(cur.id) + "/mount")
      .then(function (r) {
        state.srvPending = false;
        if (r.status !== 200) { setProjErr((r.data && r.data.error) || "Could not change the server."); }
        return loadProjects();
      }, function () {
        state.srvPending = false;
        setProjErr("Could not reach the dashboard to change the server.");
        renderServer();
      });
  }

  function loadProjects() {
    return api("GET", "/api/agent/projects").then(function (r) {
      if (r.status === 200 && r.data && r.data.projects) {
        state.projects = r.data.projects;
        if (!findProject(state.project)) { state.project = findBuiltin().id; saveProject(state.project); }
      } else {
        setProjErr((r.data && r.data.error) || "Could not load the projects.");
      }
      renderProjects();
    }, function () {
      setProjErr("Could not reach the server to load the projects.");
      renderProjects();
    });
  }

  // Clears everything that belongs to the previous project so none of it stays on screen.
  function selectProjectLocal(id) {
    state.project = id; saveProject(id);
    state.tools = []; state.history = []; state.sel = null; state.ghost = null; state.hot = null;
    state.session = null; state.pick = null; state.vpTouched = false;
    resetRunState(); state.attached = false; state.attaching = false;
    if (!currentProject().builtin) state.view = "session";   // never a recorded run in place of the project's own log
    applyProjectMode();
    $("ag-prompt").value = lsGet(DRAFT_KEY + state.project) || "";
    $("ag-cmd-out").hidden = true; $("ag-cmd-in").value = "";
    closeProjForm(false); closeProjDel(false); setProjErr("");
    $("ag-answer").textContent = "—";
    $("ag-answer-call").textContent = "Run a prompt to see its Lisp, tests and REPL output here.";
    $("ag-capnotice").hidden = true; clear($("ag-capnotice"));
    $("ag-summary").hidden = true; clear($("ag-summary"));
    clear($("ag-stages")); clear($("ag-log")); clear(repl);
    $("ag-barinfo").hidden = true; $("ag-compare-note").textContent = "";
    $("ag-callout").textContent = "";
    $("ag-demo-cost").textContent = demoCostText();
    renderProjects(); drawGraph(); redrawEvidence(); showDetail();
    replLine("c", ";; project: " + currentProject().name);
  }

  function switchProject(id) {
    if (state.busy || id === state.project || !findProject(id)) return;
    selectProjectLocal(id);
    refresh();
  }

  function openProjForm(mode) {
    if (state.busy) return;
    if (mode === "edit" && currentProject().builtin) return;
    closeProjDel(false);
    var creating = mode === "new", cur = currentProject();
    state.projMode = mode;
    $("ag-proj-form-h").textContent = creating ? "New project" : "Edit " + cur.name;
    $("ag-proj-name-in").value = creating ? "" : cur.name;
    $("ag-proj-desc-in").value = creating ? "" : (cur.description || "");
    $("ag-proj-save").textContent = creating ? "Create" : "Save";
    setProjErr("");
    $("ag-proj-form").hidden = false;
    $("ag-proj-name-in").focus();
  }
  function closeProjForm(back) {
    var form = $("ag-proj-form");
    if (form.hidden) return;
    var opener = state.projMode === "edit" ? "ag-proj-edit" : "ag-proj-new";
    form.hidden = true; state.projMode = null; setProjErr("");
    if (back) $(opener).focus();
  }

  function openProjDel() {
    var cur = currentProject();
    if (state.busy || cur.builtin) return;
    closeProjForm(false);
    setProjErr("");
    $("ag-proj-deltext").textContent = "Delete “" + cur.name + "”? Its saved tools and prompt log are moved to a trash folder on disk, not erased. You will switch to " + findBuiltin().name + ".";
    $("ag-proj-delrow").hidden = false;
    $("ag-proj-delcancel").focus();   // Cancel gets focus, so Enter cannot delete by accident
  }
  function closeProjDel(back) {
    if ($("ag-proj-delrow").hidden) return;
    $("ag-proj-delrow").hidden = true;
    if (back) $("ag-proj-delete").focus();
  }

  $("ag-proj-new").addEventListener("click", function () { openProjForm("new"); });
  $("ag-proj-edit").addEventListener("click", function () { openProjForm("edit"); });
  $("ag-proj-delete").addEventListener("click", function () { openProjDel(); });
  $("ag-proj-cancel").addEventListener("click", function () { closeProjForm(true); });
  $("ag-proj-delcancel").addEventListener("click", function () { closeProjDel(true); });
  $("ag-proj-bar").addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    if (!$("ag-proj-form").hidden) { e.preventDefault(); closeProjForm(true); }
    else if (!$("ag-proj-delrow").hidden) { e.preventDefault(); closeProjDel(true); }
  });

  $("ag-proj-form").addEventListener("submit", function (e) {
    e.preventDefault();
    if (state.busy || state.projPending) return;
    var name = $("ag-proj-name-in").value.trim();
    var desc = $("ag-proj-desc-in").value.trim();
    if (!name) { setProjErr("Give the project a name."); $("ag-proj-name-in").focus(); return; }
    if (name.length > 60) { setProjErr("The name can be at most 60 characters."); $("ag-proj-name-in").focus(); return; }
    if (desc.length > 200) { setProjErr("The description can be at most 200 characters."); $("ag-proj-desc-in").focus(); return; }
    var creating = state.projMode === "new";
    var path = creating ? "/api/agent/projects" : "/api/agent/projects/" + encodeURIComponent(currentProject().id);
    state.projPending = true; setProjErr(""); setProjectLock(state.busy);
    api("POST", path, { name: name, description: desc }).then(function (r) {
      if (r.status !== 200 || !r.data || !r.data.project) {
        setProjErr((r.data && r.data.error) || "Could not save the project.");
        return;
      }
      var p = r.data.project;
      closeProjForm(false);
      return loadProjects().then(function () {
        if (creating) { selectProjectLocal(p.id); refresh(); focusCurrentChip(); }
      });
    }, function () {
      setProjErr("Could not reach the server.");
    }).then(function () { state.projPending = false; setProjectLock(state.busy); });
  });

  $("ag-proj-delgo").addEventListener("click", function () {
    var cur = currentProject();
    if (state.busy || state.projPending || cur.builtin) return;
    state.projPending = true; setProjErr(""); setProjectLock(state.busy);
    api("DELETE", "/api/agent/projects/" + encodeURIComponent(cur.id)).then(function (r) {
      if (r.status !== 200 || !(r.data && r.data.ok)) {
        setProjErr((r.data && r.data.error) || "Could not delete the project.");
        return;
      }
      closeProjDel(false);
      return loadProjects().then(function () {
        selectProjectLocal(findBuiltin().id); refresh(); focusCurrentChip();
      });
    }, function () {
      setProjErr("Could not reach the server.");
    }).then(function () { state.projPending = false; setProjectLock(state.busy); });
  });

  // ----------------------------------------------------------------- init
  var NOTES = {
    demo: "Demo model: scripted and offline, free. It only understands the example chips; use Live for anything else.",
    live: "Live model: real Cerebras Qwen calls. The plan of a large goal is thought through first, and functions that do not call each other are built at the same time. Comparing with no memory doubles the tokens."
  };
  function setModeNote() {
    state.mode = $("ag-mode").value;
    renderProjects();            // Scratchpad's count follows the model
    modeTexts();
    $("ag-hero-note").textContent = $("ag-hero-note").textContent || "";
    $("ag-demo-cost").textContent = demoCostText();
    if (state.snapshots) refresh();
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
    return api("GET", "/api/agent/config").then(function (r) {
      var live = (r.data || {}).live || {};
      state.liveOk = !!live.available;
      if (!live.available) {
        $("ag-live-opt").disabled = true;
        $("ag-live-opt").textContent = "Live: unavailable";
        NOTES.live = "Live unavailable: " + (live.reason || "unknown");
        $("ag-mode").value = "demo";
      }
      state.configLoaded = true;
      applyProjectMode();          // a saved project opens on Live, not on the Demo default
      setModeNote();
    });
  }

  function tryCall() {
    var text = $("ag-call").value.trim();
    var out = $("ag-callout");
    if (!text) { $("ag-call").focus(); return; }
    out.className = "ag-callout"; out.textContent = "running…";
    api("POST", "/api/agent/call", { call: text, mode: state.mode, project: state.project }).then(function (r) {
      var d = r.data || {};
      out.className = "ag-callout " + (d.ok ? "ok" : "bad");
      out.textContent = d.ok ? "=> " + d.value + "   (0 tokens, " + Math.round(d.elapsed_ms || 0) + " ms)" : (d.error || "failed");
    });
  }
  $("ag-callbtn").addEventListener("click", tryCall);
  $("ag-srv-toggle").addEventListener("click", toggleServer);
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
  var replbox = $("ag-replbox");
  replbox.open = window.innerHeight >= 900;
  replbox.addEventListener("toggle", function () { if (replbox.open) repl.scrollTop = repl.scrollHeight; });
  $("ag-send").addEventListener("click", send);
  $("ag-cancel").addEventListener("click", cancelRun);
  $("ag-demo").addEventListener("click", guidedDemo);
  $("ag-prompt").addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  Array.prototype.forEach.call(document.querySelectorAll(".ag-chip"), function (c) {
    c.addEventListener("click", function () { if (c.getAttribute("data-p") == null) return; setPrompt(c.getAttribute("data-p")); $("ag-prompt").focus(); });
  });
  $("ag-mode").addEventListener("change", setModeNote);
  $("ag-visual").checked = lsGet("gg-visual") !== "0";
  $("ag-visual").addEventListener("change", function () { lsSet("gg-visual", $("ag-visual").checked ? "1" : "0"); });
  var lastW = window.innerWidth, rt = null;
  window.addEventListener("resize", function () {
    clearTimeout(rt);
    rt = setTimeout(function () {
      if ((lastW < 640) !== (window.innerWidth < 640)) { redrawEvidence(); }
      lastW = window.innerWidth;
    }, 150);
  });
  $("ag-reset").addEventListener("click", function () {
    var pid = state.project;
    if (!window.confirm("Forget the tools in " + currentProject().name + " and its prompt log? Other projects are not touched. This cannot be undone.")) return;
    api("POST", "/api/agent/reset", { project: pid }).then(function () {
      if (pid !== state.project) return;
      state.tools = []; state.history = []; state.sel = null; showDetail(); drawGraph(); redrawEvidence();
      loadProjects();
    });
  });
  document.querySelector('nav button[data-view="agent"]').addEventListener("click", function () { refresh(); checkActive(); });
  document.querySelector('nav button[data-view="thesis"]').addEventListener("click", machinerySection);
  $("ag-prompt").addEventListener("input", saveDraft);
  $("ag-cmd").addEventListener("submit", runCommand);
  $("ag-resume-x").addEventListener("click", function () {
    if (state.resumeRow) lsSet(DISMISS_KEY + state.project, state.resumeRow.session_id);
    updateResume(); $("ag-prompt").focus();
  });
  $("ag-resume-go").addEventListener("click", function () {
    var row = state.resumeRow;
    if (!row || state.busy) return;
    var sel = $("ag-mode");
    if (row.mode && Array.prototype.some.call(sel.options, function (o) { return o.value === row.mode && !o.disabled; })) { sel.value = row.mode; setModeNote(); }
    setPrompt(row.prompt);
    send();
  });
  // Thesis tab buttons: jump to the Agent tab, optionally fill a prompt or start the demo.
  Array.prototype.forEach.call(document.querySelectorAll("[data-goto]"), function (b) {
    b.addEventListener("click", function () {
      document.querySelector('nav button[data-view="' + b.getAttribute("data-goto") + '"]').click();
      var p = b.getAttribute("data-p");
      if (p) { setPrompt(p); $("ag-prompt").focus(); }
      if (b.getAttribute("data-action") === "demo") guidedDemo();
      window.scrollTo(0, 0);
    });
  });

  initGraphView();
  renderProjects();   // draws the project bar even when opened as a file (no server)
  drawGraph(); redrawEvidence();
  replLine("c", ";; REPL output appears here when you send a prompt or run the demo.");
  if (window.location.protocol !== "file:") {
    // Restore the saved project (loadProjects falls back to the builtin one if it is gone), then load its data.
    state.project = savedProject() || "scratch";
    $("ag-prompt").value = lsGet(DRAFT_KEY + state.project) || "";
    loadProjects().then(function () {
      applyProjectMode();          // before the first load, so a project never shows its Demo side
      var cfg = loadConfig();
      refresh(); loadSnapshots();
      return cfg;
    }).then(checkActive, checkActive);
  }
})();
