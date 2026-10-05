/* GrayGoo dashboard frontend. Vanilla JS, no libraries, no CDN.
   Works best served from server.py; static layout also renders via file://. */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var serverMode = window.location.protocol !== "file:";
  var currentJob = null;
  var pollTimer = null;

  // Client-side redaction (defense in depth; server already redacts).
  function redact(s) {
    return String(s)
      .replace(/(cerebras[_\- ]?(?:api[_\- ]?key)?["']?\s*[:=]\s*["']?)([^\s"',}]+)/gi,
        "$1[REDACTED]")
      .replace(/sk-[A-Za-z0-9\-_]{8,}/g, "[REDACTED]");
  }

  function setConn(ok) {
    var el = $("conn");
    el.textContent = ok ? "connected" : "server unreachable";
    el.className = "conn" + (ok ? " ok" : "");
  }

  function get(path, cb) {
    fetch(path).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then(function (data) { setConn(true); cb(null, data); })
      .catch(function (e) { setConn(false); cb(e); });
  }

  function post(path, body, cb) {
    fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().then(function (data) { return { status: r.status, data: data }; });
    }).then(function (res) { setConn(true); cb(null, res); })
      .catch(function (e) { setConn(false); cb(e); });
  }

  // ---- tabs ----
  var tabs = document.querySelectorAll("nav button");
  tabs.forEach(function (btn) {
    btn.addEventListener("click", function () {
      tabs.forEach(function (b) { b.classList.remove("active"); });
      btn.classList.add("active");
      document.querySelectorAll(".view").forEach(function (v) {
        v.classList.remove("active");
      });
      $("view-" + btn.getAttribute("data-view")).classList.add("active");
      if (btn.getAttribute("data-view") === "overview") loadStatus();
      if (btn.getAttribute("data-view") === "pipeline") loadPipeline();
    });
  });

  // ---- overview ----
  function loadStatus() {
    if (!serverMode) return;
    get("/api/status", function (err, s) {
      if (err) return;
      var t = s.todos || {};
      var pct = t.total ? Math.round(100 * t.checked / t.total) : 0;
      $("todo-bar").style.width = pct + "%";
      $("todo-text").textContent =
        (t.checked || 0) + " / " + (t.total || 0) + " done (" + pct + "%)";
      var sp = s.spend || {};
      if (sp.known) {
        var frac = Math.min(1, sp.spent_usd / sp.budget_usd);
        $("spend-bar").style.width = Math.round(100 * frac) + "%";
        $("spend-bar").className = "fill spend" + (frac > 0.8 ? " hot" : "");
        $("spend-text").textContent =
          "$" + sp.spent_usd.toFixed(4) + " / $" + sp.budget_usd.toFixed(2);
      } else {
        $("spend-bar").style.width = "0";
        $("spend-text").textContent =
          "unknown (no usage files) / $" + (sp.budget_usd || 50).toFixed(2);
      }
      var p = s.presence || {};
      var rows = [
        ["SBCL", !!p.sbcl],
        ["Cerebras key", p.cerebras_key === "present"],
        ["venv", !!p.venv]
      ];
      $("checks").innerHTML = rows.map(function (r) {
        return "<li><span class='" + (r[1] ? "ok-t'>✓ " : "bad-t'>✗ ") +
          "</span>" + r[0] + (r[0] === "Cerebras key"
            ? " (" + (r[1] ? "present" : "absent") + ")" : "") + "</li>";
      }).join("");
      var g = s.git || {};
      $("gitlog").textContent = (g.commits && g.commits.length)
        ? redact(g.commits.join("\n")) : "(git log unavailable)";
    });
  }

  // ---- pipeline ----
  var STAGES = [
    { id: "parse", label: "Parse", desc: "Parse candidate Lisp; reject malformed output." },
    { id: "risk", label: "Risk", desc: "Classify mutation risk R0-R6 before execution." },
    { id: "worker", label: "Worker", desc: "Compile + run in an isolated rehearsal worker." },
    { id: "repair", label: "Repair", desc: "Attempt automated repair of failing candidates." },
    { id: "patch", label: "Patch", desc: "Assemble promoted patch with lineage." },
    { id: "transfer", label: "Transfer", desc: "Transfer lessons/patches to memory." }
  ];

  function drawStages(serverStages) {
    var stages = (serverStages && serverStages.length) ? serverStages : STAGES;
    var svg = $("stages");
    var NS = "http://www.w3.org/2000/svg";
    svg.innerHTML = "";
    var w = 96, h = 44, gap = 12, y = 20;
    stages.forEach(function (st, i) {
      var x = gap + i * (w + gap);
      var rect = document.createElementNS(NS, "rect");
      rect.setAttribute("x", x); rect.setAttribute("y", y);
      rect.setAttribute("width", w); rect.setAttribute("height", h);
      rect.setAttribute("rx", 8); rect.setAttribute("class", "node");
      rect.addEventListener("click", function () {
        svg.querySelectorAll(".node").forEach(function (n) {
          n.classList.remove("sel");
        });
        rect.classList.add("sel");
        $("stage-desc").textContent = st.label + ": " + (st.desc || "");
      });
      svg.appendChild(rect);
      var text = document.createElementNS(NS, "text");
      text.setAttribute("x", x + w / 2); text.setAttribute("y", y + h / 2 + 5);
      text.textContent = st.label || st.id;
      text.style.pointerEvents = "none";
      svg.appendChild(text);
      if (i < stages.length - 1) {
        var line = document.createElementNS(NS, "line");
        line.setAttribute("x1", x + w); line.setAttribute("y1", y + h / 2);
        line.setAttribute("x2", x + w + gap); line.setAttribute("y2", y + h / 2);
        line.setAttribute("class", "arrow"); line.setAttribute("stroke-width", 2);
        svg.appendChild(line);
      }
    });
    svg.setAttribute("viewBox",
      "0 0 " + (gap + stages.length * (w + gap)) + " 90");
  }

  function loadPipeline() {
    if (!serverMode) { drawStages(null); return; }
    get("/api/pipeline", function (err, p) {
      if (err) { drawStages(null); return; }
      drawStages(p.stages);
      var d = (p.last_demo && p.last_demo.summary) || null;
      $("demo").textContent = (p.last_demo && p.last_demo.found)
        ? redact(JSON.stringify(d, null, 2))
        : "(no artifacts/demo-e2e/summary.json yet)";
    });
  }

  // ---- control ----
  function setRunningUI(jobId, kind) {
    currentJob = jobId;
    $("run-stop").disabled = !jobId;
    ["run-smoke", "run-tests", "run-baseline"].forEach(function (id) {
      $(id).disabled = !!jobId;
    });
    if (jobId) {
      $("job-meta").textContent = "Running " + kind + " (" + jobId + ")…";
    }
  }

  function pollJob() {
    if (!currentJob) return;
    get("/api/jobs/" + currentJob, function (err, j) {
      if (err || !j) return;
      $("job-tail").textContent = redact((j.tail || []).join("\n")) || "(no output yet)";
      $("job-tail").scrollTop = $("job-tail").scrollHeight;
      if (j.state === "running") {
        $("job-meta").textContent =
          "Running " + j.kind + " (" + j.job_id + ")…";
      } else {
        $("job-meta").textContent = "Finished " + j.kind + " (" + j.job_id +
          "): " + j.state + " exit=" + j.exit_code + " — " + (j.output || "");
        setRunningUI(null);
        clearInterval(pollTimer); pollTimer = null;
        loadStatus();
      }
    });
  }

  function startJob(kind) {
    post("/api/run", { job: kind }, function (err, res) {
      if (err) { $("job-tail").textContent = "Request failed: " + err; return; }
      if (res.status === 409) {
        $("job-tail").textContent = "Busy: another job is already running.";
        return;
      }
      if (res.status !== 200 || !res.data.job_id) {
        $("job-tail").textContent =
          "Failed: " + redact(JSON.stringify(res.data));
        return;
      }
      setRunningUI(res.data.job_id, kind);
      $("job-tail").textContent = "(starting…)";
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(pollJob, 1000);
      pollJob();
    });
  }

  $("run-smoke").addEventListener("click", function () { startJob("smoke"); });
  $("run-tests").addEventListener("click", function () { startJob("tests"); });
  $("run-baseline").addEventListener("click", function () { startJob("baseline-stub"); });
  $("run-stop").addEventListener("click", function () {
    if (!currentJob) return;
    post("/api/jobs/" + currentJob + "/stop", {}, function () { pollJob(); });
  });

  // ---- init ----
  if (!serverMode) $("file-hint").hidden = false;
  drawStages(null);  // static graph renders even on file://
  loadStatus();
  loadPipeline();
  setInterval(function () {
    if ($("view-overview").classList.contains("active")) loadStatus();
  }, 10000);
})();
