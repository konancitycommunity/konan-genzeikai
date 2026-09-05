(function () {
  "use strict";

  var config = window.SITE_CONFIG || {};
  var sns = config.sns || {};

  function applyMailto() {
    document.querySelectorAll("[data-mailto]").forEach(function (el) {
      if (config.mailto) {
        el.setAttribute("href", config.mailto);
      }
    });
  }

  function applyGoogleForm() {
    var url = config.googleFormUrl || "";
    document.querySelectorAll("[data-google-form]").forEach(function (el) {
      if (url) {
        el.setAttribute("href", url);
        el.setAttribute("target", "_blank");
        el.setAttribute("rel", "noopener noreferrer");
        el.classList.remove("is-disabled");
        el.removeAttribute("aria-disabled");
        el.style.display = "";
      } else {
        el.setAttribute("href", "#");
        el.removeAttribute("target");
        el.setAttribute("aria-disabled", "true");
        el.classList.add("is-disabled");
        el.style.display = "none";
        el.addEventListener("click", function (e) {
          e.preventDefault();
        });
      }
    });

    document.querySelectorAll("[data-google-form-pending]").forEach(function (el) {
      el.hidden = !!url;
      el.style.display = url ? "none" : "";
    });
  }

  function applySns() {
    var keys = {};
    document.querySelectorAll("[data-sns]").forEach(function (el) {
      var key = el.getAttribute("data-sns");
      if (key) keys[key] = true;
    });
    Object.keys(sns).forEach(function (key) {
      keys[key] = true;
    });

    Object.keys(keys).forEach(function (key) {
      var url = Object.prototype.hasOwnProperty.call(sns, key) ? sns[key] : null;

      document.querySelectorAll('[data-sns="' + key + '"]').forEach(function (el) {
        if (url === null || url === undefined) {
          el.style.display = "none";
          el.setAttribute("hidden", "");
          el.setAttribute("aria-hidden", "true");
          el.classList.remove("is-soon");
          return;
        }

        el.style.display = "";
        el.removeAttribute("hidden");
        el.removeAttribute("aria-hidden");

        if (url) {
          el.setAttribute("href", url);
          el.removeAttribute("aria-disabled");
          el.classList.remove("is-soon");
          if (!el.getAttribute("target") && el.classList.contains("sns-card")) {
            el.setAttribute("target", "_blank");
            el.setAttribute("rel", "noopener noreferrer");
          }
        } else {
          el.setAttribute("href", "#");
          el.removeAttribute("target");
          el.setAttribute("aria-disabled", "true");
          el.classList.add("is-soon");
          el.addEventListener("click", function (e) {
            e.preventDefault();
          });
          var caption = el.querySelector("span");
          if (caption && el.classList.contains("sns-card")) {
            caption.textContent = "今後公開";
          }
        }
      });
    });
  }

  function applyConfig() {
    applyMailto();
    applyGoogleForm();
    applySns();
  }

  function setupNav() {
    var toggle = document.querySelector(".nav-toggle");
    var nav = document.querySelector(".site-nav");
    if (!toggle || !nav) return;

    toggle.addEventListener("click", function () {
      var open = nav.classList.toggle("is-open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      document.body.classList.toggle("nav-open", open);
    });

    nav.querySelectorAll("a").forEach(function (link) {
      link.addEventListener("click", function () {
        nav.classList.remove("is-open");
        toggle.setAttribute("aria-expanded", "false");
        document.body.classList.remove("nav-open");
      });
    });
  }

  function setActiveNav() {
    var path = (location.pathname.split("/").pop() || "index.html").toLowerCase();
    if (!path || path === "") path = "index.html";
    document.querySelectorAll(".site-nav a").forEach(function (a) {
      var href = (a.getAttribute("href") || "").toLowerCase();
      if (href === path || (path === "" && href === "index.html")) {
        a.setAttribute("aria-current", "page");
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    applyConfig();
    setupNav();
    setActiveNav();
  });
})();
