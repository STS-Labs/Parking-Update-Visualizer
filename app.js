(function () {
  "use strict";

  const REFRESH_MS = 2 * 60 * 1000;
  const ROUTE_COLOR = "#2563eb";   // processed route: one color for every session/checkpoint
  const PENDING_COLOR = "#8a91a0"; // recorded but not yet processed

  const map = L.map("map", { zoomControl: false }).setView([41.7151, 44.8271], 12);
  L.control.zoom({ position: "topright" }).addTo(map);
  const streets = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: "&copy; OpenStreetMap contributors"
  });
  const satellite = L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 20, maxNativeZoom: 19, attribution: "Imagery &copy; Esri" });
  streets.addTo(map);
  L.control.layers({ "Streets": streets, "Satellite": satellite }, null, { position: "topright" }).addTo(map);
  L.control.scale({ position: "bottomright", imperial: false }).addTo(map);

  const cluster = L.markerClusterGroup({ disableClusteringAtZoom: 16, maxClusterRadius: 30 });
  map.addLayer(cluster);

  // session id -> { meta, color, track: L.Layer, markers: [L.CircleMarker], visible }
  const sessions = new Map();
  let lastIndexStamp = null;
  let fittedOnce = false;
  let minConf = 0;
  // sign-type filter: "all" | "parking" | "custom" (custom uses selectedCodes)
  let typeMode = "all";
  const selectedCodes = new Set();
  let knownCodes = new Set();

  function codeVisible(code) {
    if (typeMode === "all") return true;
    if (typeMode === "parking") return window.isParkingCode(code);
    return selectedCodes.has(code);
  }

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function confColor(c) {
    if (c == null) return "#6b7385";
    return c >= 0.8 ? "#16a34a" : c >= 0.6 ? "#f59e0b" : "#dc2626";
  }

  function fmtTs(ns) {
    if (!ns) return "–";
    const d = new Date(Number(BigInt(ns) / 1000000n));
    return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function ago(iso) {
    if (!iso) return "never";
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 90) return "just now";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    return new Date(iso).toLocaleString();
  }

  function popupHtml(p, lat, lon, meta) {
    const img = p.photo_url
      ? `<a href="${esc(p.photo_view || p.photo_url)}" target="_blank" rel="noopener"><img src="${esc(p.photo_url)}" alt="detected sign" loading="lazy" referrerpolicy="no-referrer" onerror="this.parentNode.outerHTML='<div class=noimg>Photo not available yet</div>'"></a>`
      : `<div class="noimg">No photo</div>`;
    const labels = (p.labels || []).map((l) => {
      const n = window.signName(l);
      return `<span class="lab"><b>${esc(l)}</b>${n ? " · " + esc(n) : ""}</span>`;
    }).join("");
    const stats = p.label_stats || {};
    const statRows = Object.keys(stats).map((l) =>
      `<tr><td>${esc(l)} detections</td><td>${stats[l].detections} · mean ${stats[l].mean_conf.toFixed(2)} · max ${stats[l].max_conf.toFixed(2)}</td></tr>`).join("");
    const unverified = p.gnss_gap
      ? `<tr><td>Position</td><td class="warn">No GNSS here: placed by camera tracking only, may be tens of metres off</td></tr>`
      : "";
    const gm = `https://www.google.com/maps/search/?api=1&query=${lat},${lon}`;
    const sv = `https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=${lat},${lon}`;
    return `<div class="pop">
      ${img}
      <div class="labels">${labels}</div>
      <table>
        <tr><td>Confidence</td><td class="conf" style="color:${confColor(p.conf)}">${p.conf != null ? (p.conf * 100).toFixed(1) + "%" : "–"}</td></tr>
        <tr><td>Frames seen</td><td>${esc(p.n_frames ?? "–")}</td></tr>
        ${statRows}
        <tr><td>First seen</td><td>${fmtTs(p.first_ts)}</td></tr>
        <tr><td>Last seen</td><td>${fmtTs(p.last_ts)}</td></tr>
        <tr><td>Location</td><td>${lat.toFixed(6)}, ${lon.toFixed(6)}${p.alt != null ? ` · ${p.alt} m` : ""}</td></tr>
        ${unverified}
        <tr><td>Pole ID</td><td>${esc(p.pole_id)}</td></tr>
        <tr><td>Session</td><td>${esc(meta.name)} · ${esc(meta.checkpoint)}</td></tr>
      </table>
      <div class="links">
        <a href="${gm}" target="_blank" rel="noopener">Google Maps</a>
        <a href="${sv}" target="_blank" rel="noopener">Street View</a>
        ${p.photo_view ? `<a href="${esc(p.photo_view)}" target="_blank" rel="noopener">Full photo</a>` : ""}
      </div>
    </div>`;
  }

  function applyFilter() {
    cluster.clearLayers();
    const visible = [];
    for (const s of sessions.values()) {
      if (!s.visible) continue;
      for (const m of s.markers) {
        if ((m.options.conf ?? 1) < minConf) continue;
        if (!m.options.labels.some(codeVisible)) continue;
        visible.push(m);
      }
    }
    cluster.addLayers(visible);
    const total = [...sessions.values()].reduce((n, s) => n + (s.visible ? s.markers.length : 0), 0);
    $("shown").textContent = total ? `Showing ${visible.length} of ${total} signs` : "";
  }

  async function loadSession(meta, color) {
    const res = await fetch(`${meta.file}?v=${encodeURIComponent(meta.stamp)}`);
    if (!res.ok) throw new Error(`${meta.file}: ${res.status}`);
    const fc = await res.json();
    const markers = [];
    const tracks = [];
    for (const f of fc.features) {
      const p = f.properties || {};
      if (p.kind === "track_pending") {
        tracks.unshift(L.geoJSON(f, { style: { color: PENDING_COLOR, weight: 3, opacity: 0.7, dashArray: "4 6" }, interactive: false }));
      } else if (p.kind === "track") {
        tracks.push(L.geoJSON(f, { style: { color, weight: 4, opacity: 0.85 }, interactive: false }));
      } else if (f.geometry.type === "Point") {
        const [lon, lat] = f.geometry.coordinates;
        const style = p.gnss_gap  // no GNSS to check the position against: hollow, dashed ring
          ? { radius: 7, weight: 2, color: confColor(p.conf), dashArray: "3 3", fillColor: "#fff", fillOpacity: 0.7 }
          : { radius: 7, weight: 2, color: "#fff", fillColor: confColor(p.conf), fillOpacity: 0.95 };
        const m = L.circleMarker([lat, lon], {
          ...style, conf: p.conf, labels: (p.labels && p.labels.length) ? p.labels : ["?"]
        });
        m.bindPopup(() => popupHtml(p, lat, lon, meta), { maxWidth: 320, autoPanPadding: [20, 20] });
        m.bindTooltip((p.labels || []).join(", ") + (p.conf != null ? ` (${(p.conf * 100).toFixed(0)}%)` : ""), { direction: "top", offset: [0, -6] });
        markers.push(m);
      }
    }
    return { track: L.layerGroup(tracks), markers };
  }

  function renderCodes() {
    const counts = new Map();
    for (const s of sessions.values())
      for (const m of s.markers) for (const l of m.options.labels) counts.set(l, (counts.get(l) || 0) + 1);
    // codes seen for the first time start selected in custom mode
    for (const c of counts.keys()) if (!knownCodes.has(c)) { knownCodes.add(c); selectedCodes.add(c); }
    const codes = [...counts.keys()].sort((x, y) => x.localeCompare(y, undefined, { numeric: true }));
    const box = $("codes");
    box.innerHTML = "";
    for (const c of codes) {
      const name = window.signName(c);
      const el = document.createElement("label");
      el.innerHTML = `<input type="checkbox" ${codeVisible(c) ? "checked" : ""}>
        <b>${esc(c)}</b><span>${esc(name)}</span>${window.isParkingCode(c) ? '<span class="p">P</span>' : ""}<span class="n">${counts.get(c)}</span>`;
      el.querySelector("input").addEventListener("change", (e) => {
        if (typeMode !== "custom") {  // editing a box switches to custom, starting from what is visible now
          selectedCodes.clear();
          for (const k of codes) if (codeVisible(k)) selectedCodes.add(k);
          setMode("custom", false);
        }
        e.target.checked ? selectedCodes.add(c) : selectedCodes.delete(c);
        applyFilter();
      });
      box.appendChild(el);
    }
  }

  function setMode(mode, rerender = true) {
    typeMode = mode;
    for (const b of document.querySelectorAll("#typemode button")) b.classList.toggle("on", b.dataset.mode === mode);
    if (rerender) renderCodes();
    applyFilter();
    try { localStorage.setItem("typeMode", mode); } catch (e) { /* ignore */ }
  }

  function renderPanel(index) {
    const list = index.sessions;
    $("t-points").textContent = list.reduce((a, s) => a + (s.points || 0), 0);
    $("t-km").title = "processed so far"; $("t-km").textContent = list.reduce((a, s) => a + (s.track_km || 0), 0).toFixed(1);
    $("t-sessions").textContent = list.length;
    $("updated").textContent = "Data updated " + ago(index.updated_at);
    $("updated").title = index.updated_at || "";

    const box = $("sessions");
    if (!list.length) { box.innerHTML = '<p class="muted">Waiting for first upload…</p>'; return; }
    box.innerHTML = "";
    for (const meta of list.slice().reverse()) {
      const s = sessions.get(meta.id);
      const pr = meta.progress || {};
      const pct = pr.final ? 100 : pr.frames_expected ? Math.min(100, 100 * pr.frames_done / pr.frames_expected) : 0;
      const el = document.createElement("div");
      el.className = "sess";
      el.innerHTML = `
        <div class="sess-head">
          <input type="checkbox" ${s && s.visible ? "checked" : ""} aria-label="Show session">
          <span class="swatch" style="background:${s ? s.color : "#999"}"></span>
          <span class="name" title="${esc(meta.name)}">${esc(meta.name.replace(/^session_/, ""))}</span>
          ${pr.final ? '<span class="badge">final</span>' : '<span class="badge live">in progress</span>'}
          <button type="button">Zoom</button>
        </div>
        <div class="bar ${pr.final ? "done" : ""}"><div style="width:${pct.toFixed(1)}%"></div></div>
        <div class="meta">
          ${esc(meta.checkpoint)} · ${pct.toFixed(0)}% · ${meta.points} signs · ${meta.track_km}${meta.recorded_km > meta.track_km ? ` / ${meta.recorded_km}` : ""} km<br>
          video ${esc(pr.video_time_reached || "–")} / ${esc(pr.video_duration || "–")} · frames ${pr.frames_done ?? "–"} / ${pr.frames_expected ?? "–"}<br>
          ${pr.raw_detections != null ? `${pr.raw_detections} raw detections · ` : ""}written ${esc(pr.written_at || "–")}
        </div>`;
      el.querySelector("input").addEventListener("change", (e) => {
        if (!s) return;
        s.visible = e.target.checked;
        s.visible ? s.track.addTo(map) : map.removeLayer(s.track);
        applyFilter();
      });
      el.querySelector("button").addEventListener("click", () => {
        if (meta.bbox) map.fitBounds([[meta.bbox[1], meta.bbox[0]], [meta.bbox[3], meta.bbox[2]]], { padding: [40, 40] });
      });
      box.appendChild(el);
    }
  }

  async function refresh() {
    let index;
    try {
      const res = await fetch(`data/index.json?t=${Date.now()}`, { cache: "no-store" });
      if (!res.ok) throw new Error(res.status);
      index = await res.json();
    } catch (e) {
      $("updated").textContent = "No data published yet";
      return;
    }
    index.sessions = index.sessions || [];
    const stamp = JSON.stringify(index.sessions.map((s) => [s.id, s.stamp]));
    if (stamp !== lastIndexStamp) {
      lastIndexStamp = stamp;
      const seen = new Set();
      for (const [i, meta] of index.sessions.entries()) {
        seen.add(meta.id);
        const old = sessions.get(meta.id);
        if (old && old.meta.stamp === meta.stamp) { old.meta = meta; continue; }
        const color = ROUTE_COLOR;
        try {
          const layers = await loadSession(meta, color);
          if (old) map.removeLayer(old.track);
          const visible = old ? old.visible : true;
          if (visible) layers.track.addTo(map);
          sessions.set(meta.id, { meta, color, visible, ...layers });
        } catch (e) {
          console.warn("failed to load session", meta.id, e);
        }
      }
      for (const [id, s] of sessions) {
        if (!seen.has(id)) { map.removeLayer(s.track); sessions.delete(id); }
      }
      applyFilter();
      if (!fittedOnce) {
        const boxes = index.sessions.map((s) => s.bbox).filter(Boolean);
        if (boxes.length) {
          map.fitBounds([[Math.min(...boxes.map((b) => b[1])), Math.min(...boxes.map((b) => b[0]))],
                         [Math.max(...boxes.map((b) => b[3])), Math.max(...boxes.map((b) => b[2]))]], { padding: [40, 40] });
          fittedOnce = true;
        }
      }
    }
    renderPanel(index);
    renderCodes();
  }

  $("minconf").addEventListener("input", (e) => {
    minConf = parseFloat(e.target.value);
    $("minconf-val").textContent = minConf.toFixed(2);
    applyFilter();
  });
  for (const b of document.querySelectorAll("#typemode button"))
    b.addEventListener("click", () => setMode(b.dataset.mode));
  try {
    const saved = localStorage.getItem("typeMode");
    if (saved === "parking") { typeMode = saved; document.querySelectorAll("#typemode button").forEach((b) => b.classList.toggle("on", b.dataset.mode === saved)); }
  } catch (e) { /* ignore */ }
  $("toggle").addEventListener("click", () => $("panel").classList.toggle("collapsed"));
  if (window.innerWidth < 600) $("panel").classList.add("collapsed");

  refresh();
  setInterval(refresh, REFRESH_MS);
})();
