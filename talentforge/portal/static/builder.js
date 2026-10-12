/* Resume builder: edits a structured resume, renders a live preview and autosaves (debounced).
   The server recalculates the score on every save and returns it with the top fixes. */
(function () {
  "use strict";
  var data = JSON.parse(document.getElementById("resume-data").textContent);
  var meta = JSON.parse(document.getElementById("resume-meta").textContent);
  var builder = document.getElementById("builder");
  var preview = document.getElementById("preview");
  var stateEl = document.getElementById("savestate");
  var titleEl = document.getElementById("rtitle");
  var tplEl = document.getElementById("rtemplate");
  var saveTimer = null;
  var dirty = false;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function lines(s) { return s.split(/\r?\n/).map(function (x) { return x.trim(); }).filter(Boolean); }
  function csv(s) { return s.split(/[,\n]/).map(function (x) { return x.trim(); }).filter(Boolean); }

  /* ---------- field helpers ---------- */
  function field(label, value, onInput, opts) {
    opts = opts || {};
    var wrap = document.createElement("div");
    var lab = document.createElement("label");
    lab.textContent = label;
    var el = document.createElement(opts.multiline ? "textarea" : "input");
    if (!opts.multiline) el.type = "text";
    if (opts.rows) el.rows = opts.rows;
    if (opts.placeholder) el.placeholder = opts.placeholder;
    el.value = value || "";
    el.addEventListener("input", function () { onInput(el.value); changed(); });
    wrap.appendChild(lab);
    wrap.appendChild(el);
    if (opts.hint) { var h = document.createElement("div"); h.className = "hint"; h.textContent = opts.hint; wrap.appendChild(h); }
    return wrap;
  }
  function grid(children, cols) {
    var g = document.createElement("div");
    g.className = "grid " + (cols === 3 ? "g3" : "g2");
    g.style.gap = "0 14px";
    children.forEach(function (c) { g.appendChild(c); });
    return g;
  }
  function card(title, hint) {
    var c = document.createElement("div");
    c.className = "sec-card";
    var h = document.createElement("div");
    h.className = "sec-head";
    var t = document.createElement("h3");
    t.textContent = title;
    t.style.margin = 0;
    h.appendChild(t);
    c.appendChild(h);
    if (hint) { var p = document.createElement("div"); p.className = "hint"; p.textContent = hint; c.appendChild(p); }
    c._head = h;
    return c;
  }
  function addBtn(label, fn) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "btn ghost sm";
    b.textContent = label;
    b.addEventListener("click", function () { fn(); changed(); renderForm(); });
    return b;
  }
  function itemShell(title, onRemove) {
    var it = document.createElement("div");
    it.className = "item";
    var head = document.createElement("div");
    head.className = "item-head";
    var s = document.createElement("span");
    s.textContent = title;
    var rm = document.createElement("button");
    rm.type = "button";
    rm.className = "linkbtn";
    rm.textContent = "Remove";
    rm.addEventListener("click", function () { onRemove(); changed(); renderForm(); });
    head.appendChild(s);
    head.appendChild(rm);
    it.appendChild(head);
    return it;
  }

  /* ---------- form ---------- */
  function renderForm() {
    builder.innerHTML = "";
    var b = data.basics;

    var c = card("Contact");
    c.appendChild(grid([
      field("Full name", b.name, function (v) { b.name = v; }),
      field("Headline", b.headline, function (v) { b.headline = v; }, { placeholder: "e.g. Backend Developer | Python" }),
      field("Email", b.email, function (v) { b.email = v; }),
      field("Phone", b.phone, function (v) { b.phone = v; }),
      field("Location", b.location, function (v) { b.location = v; }, { placeholder: "City, Country" }),
      field("Links", (b.links || []).join(", "), function (v) { b.links = csv(v); }, { placeholder: "linkedin.com/in/you, github.com/you" })
    ]));
    builder.appendChild(c);

    c = card("Summary", "2-3 sentences: who you are, your strongest skills, the role you want.");
    c.appendChild(field("", data.summary, function (v) { data.summary = v; }, { multiline: true, rows: 4 }));
    builder.appendChild(c);

    c = card("Skills", "Separate with commas. Use standard names (JavaScript, PostgreSQL, Docker).");
    c.appendChild(field("", data.skills.join(", "), function (v) { data.skills = csv(v); }, { multiline: true, rows: 3 }));
    builder.appendChild(c);

    c = card("Experience", "Start each bullet with an action verb and include a result or number.");
    data.experience.forEach(function (e, i) {
      var it = itemShell("Role " + (i + 1), function () { data.experience.splice(i, 1); });
      it.appendChild(grid([
        field("Job title", e.role, function (v) { e.role = v; }),
        field("Company", e.company, function (v) { e.company = v; }),
        field("Start", e.start, function (v) { e.start = v; }, { placeholder: "Jun 2025" }),
        field("End", e.end, function (v) { e.end = v; }, { placeholder: "Present" })
      ]));
      it.appendChild(field("Bullets (one per line)", e.bullets.join("\n"), function (v) { e.bullets = lines(v); },
        { multiline: true, rows: 4 }));
      c.appendChild(it);
    });
    c.appendChild(addBtn("+ Add experience", function () {
      data.experience.push({ role: "", company: "", location: "", start: "", end: "", bullets: [] });
    }));
    builder.appendChild(c);

    c = card("Projects", "Especially important if you are early in your career.");
    data.projects.forEach(function (p, i) {
      var it = itemShell("Project " + (i + 1), function () { data.projects.splice(i, 1); });
      it.appendChild(grid([
        field("Name", p.name, function (v) { p.name = v; }),
        field("One-line description", p.description, function (v) { p.description = v; })
      ]));
      it.appendChild(field("Bullets (one per line)", p.bullets.join("\n"), function (v) { p.bullets = lines(v); },
        { multiline: true, rows: 3 }));
      c.appendChild(it);
    });
    c.appendChild(addBtn("+ Add project", function () { data.projects.push({ name: "", description: "", bullets: [] }); }));
    builder.appendChild(c);

    c = card("Education");
    data.education.forEach(function (e, i) {
      var it = itemShell("Education " + (i + 1), function () { data.education.splice(i, 1); });
      it.appendChild(grid([
        field("Degree", e.degree, function (v) { e.degree = v; }, { placeholder: "B.Tech in Computer Science" }),
        field("Institution", e.institution, function (v) { e.institution = v; }),
        field("Year", e.year, function (v) { e.year = v; }, { placeholder: "2026" }),
        field("Details", e.details, function (v) { e.details = v; })
      ]));
      c.appendChild(it);
    });
    c.appendChild(addBtn("+ Add education", function () {
      data.education.push({ degree: "", institution: "", year: "", details: "" });
    }));
    builder.appendChild(c);

    c = card("Certifications");
    c.appendChild(field("One per line", data.certifications.join("\n"), function (v) { data.certifications = lines(v); },
      { multiline: true, rows: 3 }));
    builder.appendChild(c);
  }

  /* ---------- preview ---------- */
  function renderPreview() {
    var b = data.basics, h = "";
    var contact = [b.email, b.phone, b.location].concat(b.links || []).filter(Boolean).map(esc).join("&nbsp;&nbsp;|&nbsp;&nbsp;");
    h += '<div class="paper ' + esc(tplEl.value) + '">';
    h += '<h1 class="name">' + (b.name ? esc(b.name) : '<span class="empty">Your name</span>') + "</h1>";
    if (b.headline) h += '<div class="headline">' + esc(b.headline) + "</div>";
    h += '<div class="contact">' + contact + "</div>";
    if (data.summary) h += '<h2 class="sec">Summary</h2><div>' + esc(data.summary) + "</div>";
    if (data.skills.length) h += '<h2 class="sec">Skills</h2><div>' + data.skills.map(esc).join(", ") + "</div>";
    function bullets(a) { return a.length ? "<ul>" + a.map(function (t) { return "<li>" + esc(t) + "</li>"; }).join("") + "</ul>" : ""; }
    if (data.experience.length) {
      h += '<h2 class="sec">Experience</h2>';
      data.experience.forEach(function (e) {
        h += '<div class="entry"><div class="t"><span>' + [e.role, e.company].filter(Boolean).map(esc).join(" | ") +
          '</span><span class="d">' + [e.start, e.end].filter(Boolean).map(esc).join(" - ") + "</span></div>" + bullets(e.bullets) + "</div>";
      });
    }
    if (data.projects.length) {
      h += '<h2 class="sec">Projects</h2>';
      data.projects.forEach(function (p) {
        h += '<div class="entry"><div class="t"><span>' + esc(p.name) +
          (p.description ? '<span class="sub" style="font-weight:400"> - ' + esc(p.description) + "</span>" : "") +
          "</span></div>" + bullets(p.bullets) + "</div>";
      });
    }
    if (data.education.length) {
      h += '<h2 class="sec">Education</h2>';
      data.education.forEach(function (e) {
        h += '<div class="entry"><div class="t"><span>' + [e.degree, e.institution].filter(Boolean).map(esc).join(" | ") +
          '</span><span class="d">' + esc(e.year) + "</span></div>" + (e.details ? '<div class="sub">' + esc(e.details) + "</div>" : "") + "</div>";
      });
    }
    if (data.certifications.length) h += '<h2 class="sec">Certifications</h2>' + bullets(data.certifications);
    h += "</div>";
    preview.innerHTML = h;
  }

  /* ---------- score + save ---------- */
  function renderScore(score, grade, fixes) {
    var ring = document.querySelector("#scorering .ring");
    if (ring) {
      ring.style.setProperty("--p", score);
      ring.querySelector("span").textContent = Math.round(score);
      ring.classList.remove("good", "low");
      if (score >= 70) ring.classList.add("good"); else if (score < 50) ring.classList.add("low");
    }
    document.getElementById("scoregrade").textContent = grade;
    var f = document.getElementById("fixes");
    f.innerHTML = (fixes || []).slice(0, 4).map(function (x) { return '<div class="fix">' + esc(x) + "</div>"; }).join("");
  }

  function save() {
    stateEl.textContent = "Saving...";
    fetch("/api/resumes/" + meta.id, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data: data, title: titleEl.value, template: tplEl.value })
    }).then(function (r) {
      if (!r.ok) throw new Error("save failed");
      return r.json();
    }).then(function (res) {
      dirty = false;
      stateEl.textContent = "All changes saved";
      renderScore(res.score, res.grade, res.top_fixes);
    }).catch(function () {
      stateEl.textContent = "Could not save - check your connection and press Save";
    });
  }

  function changed() {
    dirty = true;
    stateEl.textContent = "Unsaved changes...";
    renderPreview();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, 1200);
  }

  titleEl.addEventListener("input", changed);
  tplEl.addEventListener("change", changed);
  document.getElementById("savebtn").addEventListener("click", function () { clearTimeout(saveTimer); save(); });
  window.addEventListener("beforeunload", function (e) { if (dirty) { e.preventDefault(); e.returnValue = ""; } });

  renderForm();
  renderPreview();
  renderScore(meta.score, meta.grade, meta.fixes);
})();
