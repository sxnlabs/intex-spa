/* Form-based scheduler editor (no manual JSON). Reads /api/schedule, renders rules
   as day-chip + time + temp rows, and POSTs the assembled config back. */
(function () {
  "use strict";
  var DAYS = ["L", "M", "M", "J", "V", "S", "D"];
  var CONTAINER = { heat: "rules-heat", filter: "rules-filter", ready: "rules-ready" };
  var DEFAULTS = {
    heat: { days: [0, 1, 2, 3, 4, 5, 6], time: "18:00", temp: 36 },
    filter: { days: [0, 1, 2, 3, 4, 5, 6], start: "07:00", end: "10:00" },
    ready: { days: [5, 6], time: "10:00", temp: 36 },
  };
  var $ = function (s) { return document.querySelector(s); };

  function chips(days) {
    return '<div class="days">' + DAYS.map(function (l, i) {
      return '<button type="button" class="day' + (days.indexOf(i) >= 0 ? " on" : "") +
        '" data-d="' + i + '">' + l + "</button>";
    }).join("") + "</div>";
  }

  function rowEl(kind, r) {
    var el = document.createElement("div");
    el.className = "rule";
    el.dataset.kind = kind;
    var fields;
    if (kind === "filter") {
      fields = '<input type="time" class="t" value="' + r.start + '">' +
        '<span class="dash">→</span><input type="time" class="t" value="' + r.end + '">';
    } else {
      fields = '<input type="time" class="t" value="' + r.time + '">' +
        '<span class="num"><input type="number" class="temp" min="20" max="40" value="' + r.temp + '">°</span>';
    }
    el.innerHTML = chips(r.days) +
      '<div class="rfields">' + fields + '<button type="button" class="del" aria-label="Supprimer">✕</button></div>';
    return el;
  }

  function addRow(kind, r) {
    document.getElementById(CONTAINER[kind]).appendChild(rowEl(kind, r || DEFAULTS[kind]));
  }

  function daysOf(row) {
    return [].slice.call(row.querySelectorAll(".day.on")).map(function (b) { return +b.dataset.d; });
  }

  function collectRules(kind) {
    return [].slice.call(document.getElementById(CONTAINER[kind]).children).map(function (row) {
      var t = row.querySelectorAll(".t");
      if (kind === "filter") return { days: daysOf(row), start: t[0].value, end: t[1].value };
      return { days: daysOf(row), time: t[0].value, temp: +row.querySelector(".temp").value };
    });
  }

  function collect() {
    return {
      enabled: $("#sched-enabled").checked,
      eco_temp: +$("#eco-temp").value,
      heat_rules: collectRules("heat"),
      filter_windows: collectRules("filter"),
      ready_by: collectRules("ready"),
    };
  }

  function populate(cfg) {
    $("#sched-enabled").checked = !!cfg.enabled;
    $("#eco-temp").value = cfg.eco_temp;
    ["heat", "filter", "ready"].forEach(function (k) {
      document.getElementById(CONTAINER[k]).innerHTML = "";
    });
    (cfg.heat_rules || []).forEach(function (r) { addRow("heat", r); });
    (cfg.filter_windows || []).forEach(function (r) { addRow("filter", r); });
    (cfg.ready_by || []).forEach(function (r) { addRow("ready", r); });
  }

  function renderPlan(p) {
    var el = $("#sched-plan");
    if (!p || !p.enabled) { el.innerHTML = ""; return; }
    var bits = [];
    if (p.setpoint != null) bits.push("🎯 " + p.setpoint + "°");
    bits.push(p.heater ? "🔥 chauffe" : "⏸️ repos");
    if (p.filter != null) bits.push(p.filter ? "🌀 filtre on" : "filtre off");
    if (p.heat_rate != null) bits.push("↗ " + p.heat_rate + "°/h");
    el.innerHTML = '<div class="plan-now">' + bits.join(" · ") + "</div>";
  }

  document.addEventListener("click", function (e) {
    var add = e.target.closest && e.target.closest(".add");
    if (add) { addRow(add.dataset.add); return; }
    if (e.target.classList && e.target.classList.contains("del")) {
      e.target.closest(".rule").remove(); return;
    }
    if (e.target.classList && e.target.classList.contains("day")) {
      e.target.classList.toggle("on"); return;
    }
  });

  $("#sched-save").addEventListener("click", async function () {
    var msg = $("#sched-msg");
    var r = await fetch("/api/schedule", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify(collect()),
    });
    var j = await r.json().catch(function () { return {}; });
    if (r.ok) { msg.textContent = "✓ Enregistré"; msg.className = "sched-msg ok"; renderPlan(j.plan); }
    else { msg.textContent = "✗ " + (j.detail || ("Erreur " + r.status)); msg.className = "sched-msg err"; }
  });

  // -- weather + algorithm explainer ----------------------------------------
  function fmtAge(s) {
    if (s == null) return "";
    if (s < 90) return "à l'instant";
    var m = Math.round(s / 60);
    return m < 60 ? "il y a " + m + " min" : "il y a " + Math.round(m / 60) + " h";
  }

  function renderWeather(w) {
    var card = $("#weather-card");
    if (!w || !w.enabled) { if (card) card.hidden = true; return; }
    card.hidden = false;
    var now = [];
    if (w.air != null) now.push("🌡️ " + w.air + "°");
    if (w.feels != null) now.push("ressenti " + w.feels + "°");
    if (w.wind != null) now.push("💨 " + Math.round(w.wind) + " km/h");
    if (w.low_12h != null) now.push("min 12 h " + w.low_12h + "°");
    $("#wx-now").textContent = now.join(" · ") || "—";
    $("#wx-age").textContent = fmtAge(w.age_s);

    var ex = w.rate_explain, rate = $("#wx-rate");
    if (ex) {
      if (ex.source === "calibrated") {
        rate.innerHTML = "↗ Chauffe estimée <b>" + ex.effective + "°/h</b> — calibrée sur l'historique " +
          "(perte " + ex.k_loss + "°/h par °C d'écart eau/air ; eau " + ex.water + "°, ext " + ex.air + "°)";
      } else if (ex.source === "weather-derate") {
        rate.innerHTML = "↗ Chauffe <b>" + ex.effective + "°/h</b> — base " + ex.base +
          "°/h × " + ex.factor + " (météo, ext " + ex.air + "°)";
      } else {
        rate.innerHTML = "↗ Chauffe <b>" + ex.effective + "°/h</b> — mesurée";
      }
    } else { rate.textContent = "—"; }

    var ph = w.preheat, pe = $("#wx-preheat");
    if (ph) {
      pe.innerHTML = (ph.active ? "🟢 " : "⚪ ") + "Pré-chauffe " + ph.temp + "° pour " + ph.time +
        " — départ ~" + ph.start + " (avance " + ph.lead_h + " h)";
    } else { pe.textContent = ""; }
    $("#wx-note").textContent =
      "Plus il fait froid/venté dehors, plus la montée est lente — l'algo avance l'heure de départ en conséquence.";
  }

  async function loadAll() {
    try {
      var r = await fetch("/api/schedule"); if (!r.ok) return;
      var j = await r.json(); populate(j.config); renderPlan(j.plan);
    } catch (e) { /* ignore */ }
  }
  async function pollPlan() {
    try {
      var r = await fetch("/api/schedule"); if (!r.ok) return;
      renderPlan((await r.json()).plan);
    } catch (e) { /* ignore */ }
  }
  async function pollWeather() {
    try {
      var r = await fetch("/weather"); if (!r.ok) return;
      renderWeather(await r.json());
    } catch (e) { /* ignore */ }
  }

  loadAll();
  pollWeather();
  setInterval(function () { pollPlan(); pollWeather(); }, 30000);
})();
