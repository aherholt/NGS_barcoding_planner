/* Small helpers for drag & drop pages. All rules live on the server; this file only
   sends what was dropped where, shows the answer, and reloads the page. */
(function () {
  function cookie(name) {
    const m = document.cookie.match("(^|;)\\s*" + name + "=([^;]*)");
    return m ? decodeURIComponent(m[2]) : "";
  }

  function flash(text, kind) {
    let box = document.getElementById("dnd-flash");
    if (!box) {
      box = document.createElement("div");
      box.id = "dnd-flash";
      document.querySelector("main").prepend(box);
    }
    box.className = "msg " + (kind || "success");
    box.innerHTML = "";
    (Array.isArray(text) ? text : [text]).forEach(function (t) {
      const d = document.createElement("div");
      d.textContent = t;
      box.appendChild(d);
    });
    box.scrollIntoView({block: "nearest"});
  }

  async function post(url, data) {
    const r = await fetch(url, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-CSRFToken": cookie("csrftoken")},
      body: JSON.stringify(data || {}),
      credentials: "same-origin",
    });
    let body = {};
    try { body = await r.json(); } catch (e) { body = {ok: false, errors: ["Server error (" + r.status + ")."]}; }
    return body;
  }

  /* Send a change; on success remember the message + scroll position and reload. */
  async function change(url, data) {
    document.body.classList.add("busy");
    const res = await post(url, data);
    document.body.classList.remove("busy");
    if (res.ok) {
      try {
        sessionStorage.setItem("dnd-msg", res.message || "Saved.");
        sessionStorage.setItem("dnd-scroll", String(window.scrollY));
      } catch (e) { /* storage unavailable — fine */ }
      location.reload();
    } else {
      flash(res.errors || ["Not possible."], "error");
    }
    return res;
  }

  /* Make every element matching `sel` draggable with a JSON payload from data-drag. */
  function draggables(sel) {
    document.querySelectorAll(sel).forEach(function (el) {
      el.setAttribute("draggable", "true");
      el.addEventListener("dragstart", function (ev) {
        ev.dataTransfer.setData("application/json", el.dataset.drag);
        ev.dataTransfer.setData("text/plain", el.dataset.drag);
        ev.dataTransfer.effectAllowed = "move";
        el.classList.add("dragging");
        document.body.classList.add("is-dragging");
      });
      el.addEventListener("dragend", function () {
        el.classList.remove("dragging");
        document.body.classList.remove("is-dragging");
      });
    });
  }

  /* Drop zones: handler(payload, zoneElement) decides what to do; return false to refuse. */
  function dropzones(sel, accepts, handler) {
    document.querySelectorAll(sel).forEach(function (zone) {
      zone.addEventListener("dragover", function (ev) {
        ev.preventDefault();
        zone.classList.add("over");
      });
      zone.addEventListener("dragleave", function () { zone.classList.remove("over"); });
      zone.addEventListener("drop", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        zone.classList.remove("over");
        let payload;
        try { payload = JSON.parse(ev.dataTransfer.getData("application/json") || ev.dataTransfer.getData("text/plain")); }
        catch (e) { return; }
        if (accepts.indexOf(payload.kind) === -1) return;
        handler(payload, zone);
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    try {
      const msg = sessionStorage.getItem("dnd-msg");
      const y = sessionStorage.getItem("dnd-scroll");
      sessionStorage.removeItem("dnd-msg");
      sessionStorage.removeItem("dnd-scroll");
      if (y) window.scrollTo(0, parseInt(y, 10));
      if (msg) flash(msg, "success");
    } catch (e) { /* ignore */ }
  });

  window.DnD = {post: post, change: change, flash: flash, draggables: draggables, dropzones: dropzones};
})();
