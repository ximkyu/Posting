/* YouTube AutoPoster - Dashboard-JavaScript (kein Framework, keine externen Quellen)
 *
 * 1. Alle Formular-Buttons laufen per fetch() und zeigen ein Toast-Ergebnis.
 * 2. VEROEFFENTLICHEN oeffnet einen Sicherheitsdialog. Der Fokus liegt dabei
 *    ausdruecklich auf ABBRECHEN - ein versehentliches Bestaetigen per Enter
 *    ist damit ausgeschlossen.
 * 3. Doppel-Klicks werden gesperrt (Button disabled + serverseitige
 *    Status-Pruefung), damit nichts zweimal publiziert wird.
 * 4. Die Statistik-Karten aktualisieren sich per Polling aus der lokalen
 *    Datenbank - ohne YouTube-API-Aufrufe.
 */
(function () {
  "use strict";

  var config = window.APP_CONFIG || { autoRefreshSeconds: 5 };
  var toastArea = document.getElementById("toastArea");

  /* ------------------------------------------------------------------ */
  /* Toasts                                                             */
  /* ------------------------------------------------------------------ */

  function toast(message, level) {
    if (!toastArea || !message) return;
    var element = document.createElement("div");
    element.className = "toast toast-" + (level || "info");
    element.textContent = message;
    toastArea.appendChild(element);
    window.setTimeout(function () {
      element.style.transition = "opacity .4s ease";
      element.style.opacity = "0";
      window.setTimeout(function () { element.remove(); }, 420);
    }, level === "error" ? 12000 : 6000);
  }

  /* ------------------------------------------------------------------ */
  /* Formular-Aktionen                                                  */
  /* ------------------------------------------------------------------ */

  function serialize(form) {
    var data = new FormData(form);
    data.set("format", "json");
    return data;
  }

  function setBusy(button, busy) {
    if (!button) return;
    if (busy) {
      button.dataset.originalLabel = button.textContent;
      button.disabled = true;
      button.textContent = "Arbeitet ...";
    } else if (button.dataset.originalLabel) {
      button.disabled = false;
      button.textContent = button.dataset.originalLabel;
    }
  }

  function submitForm(form, trigger) {
    var message = form.dataset.confirm;
    if (message && !window.confirm(message)) {
      return Promise.resolve(null);
    }
    var action = form.getAttribute("action") || window.location.pathname;
    setBusy(trigger, true);
    return window
      .fetch(action, {
        method: "POST",
        body: serialize(form),
        headers: { "X-Requested-With": "fetch", "Accept": "application/json" },
        credentials: "same-origin"
      })
      .then(function (response) {
        return response.json().catch(function () {
          return { ok: response.ok, message: "Antwort konnte nicht gelesen werden (HTTP " + response.status + ")" };
        });
      })
      .then(function (payload) {
        setBusy(trigger, false);
        if (!payload) return null;
        toast(payload.message || (payload.ok ? "OK" : "Fehler"), payload.level || (payload.ok ? "success" : "error"));
        return payload;
      })
      .catch(function (error) {
        setBusy(trigger, false);
        toast("Netzwerkfehler: " + error.message, "error");
        return { ok: false };
      });
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.hasAttribute("data-form")) return;
    event.preventDefault();
    var trigger = event.submitter || form.querySelector("button[type=submit]");
    submitForm(form, trigger).then(function (payload) {
      if (payload) window.setTimeout(function () { window.location.reload(); }, 450);
    });
  });

  /* ------------------------------------------------------------------ */
  /* Sicherheitsdialog "VEROEFFENTLICHEN"                               */
  /* ------------------------------------------------------------------ */

  var modal = document.getElementById("publishModal");
  var modalInfo = document.getElementById("publishModalInfo");
  var cancelButton = document.getElementById("publishCancel");
  var confirmButton = document.getElementById("publishConfirm");
  var pending = null;

  function closeModal() {
    if (!modal) return;
    modal.hidden = true;
    pending = null;
    if (confirmButton) setBusy(confirmButton, false);
  }

  function openModal(button) {
    if (!modal) return;
    pending = {
      id: button.dataset.videoId,
      title: button.dataset.title || "",
      youtubeId: button.dataset.videoYt || "",
      expectedStatus: button.dataset.expectedStatus || "UPLOADED_PRIVATE"
    };
    if (modalInfo) {
      modalInfo.textContent =
        pending.title +
        (pending.youtubeId ? "  |  YouTube-ID: " + pending.youtubeId : "") +
        "  |  Status: " + pending.expectedStatus +
        "  ->  privacyStatus: public";
    }
    modal.hidden = false;
    /* Sicherheitsregel: Fokus liegt NIE auf "VEROEFFENTLICHEN" */
    if (cancelButton) cancelButton.focus();
  }

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest ? event.target.closest("[data-publish]") : null;
    if (trigger) {
      event.preventDefault();
      openModal(trigger);
      return;
    }
    if (modal && event.target === modal) closeModal();
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && modal && !modal.hidden) closeModal();
  });

  if (cancelButton) cancelButton.addEventListener("click", closeModal);

  if (confirmButton) {
    confirmButton.addEventListener("click", function () {
      if (!pending) return;
      var body = new FormData();
      body.set("confirm", "yes");
      body.set("expected_status", pending.expectedStatus);
      body.set("csrf_token", config.csrfToken || "");
      body.set("format", "json");
      setBusy(confirmButton, true);
      window
        .fetch("/videos/" + encodeURIComponent(pending.id) + "/publish", {
          method: "POST",
          body: body,
          headers: { "X-Requested-With": "fetch", "Accept": "application/json" },
          credentials: "same-origin"
        })
        .then(function (response) {
          return response.json().catch(function () {
            return { ok: false, message: "Ungueltige Antwort (HTTP " + response.status + ")" };
          });
        })
        .then(function (payload) {
          toast(payload.message || "Fertig", payload.ok ? "success" : "error");
          closeModal();
          window.setTimeout(function () { window.location.reload(); }, 600);
        })
        .catch(function (error) {
          setBusy(confirmButton, false);
          toast("Netzwerkfehler: " + error.message, "error");
        });
    });
  }

  /* ------------------------------------------------------------------ */
  /* Auto-Refresh der Statistik                                         */
  /* ------------------------------------------------------------------ */

  function updateStats(payload) {
    if (!payload) return;
    var stats = payload.stats || {};
    var mapping = {
      ready: stats.ready,
      uploading: stats.uploading,
      private: stats.private,
      publish_ready: stats.private,
      published: stats.published,
      failed: stats.failed
    };
    Object.keys(mapping).forEach(function (key) {
      var node = document.querySelector('[data-stat="' + key + '"]');
      if (node && mapping[key] !== undefined && node.textContent !== String(mapping[key])) {
        node.textContent = String(mapping[key]);
        node.animate
          ? node.animate([{ opacity: 0.35 }, { opacity: 1 }], { duration: 400 })
          : null;
      }
    });

    var worker = payload.worker || {};
    var hint = document.querySelector("[data-worker-hint]");
    if (hint) {
      hint.textContent = worker.current_episode
        ? "gerade: " + worker.current_episode
        : worker.paused
          ? "pausiert"
          : worker.last_message
            ? "zuletzt: " + worker.last_message.slice(0, 60)
            : "inaktiv";
    }
  }

  function poll() {
    if (!config.statusUrl) return;
    if (document.hidden) return;
    if (modal && !modal.hidden) return;
    window
      .fetch(config.statusUrl, { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(updateStats)
      .catch(function () { /* offline ist egal - naechster Versuch kommt */ });
  }

  if (config.autoRefreshSeconds && config.autoRefreshSeconds > 0) {
    window.setInterval(poll, Math.max(2000, config.autoRefreshSeconds * 1000));
  }
})();
