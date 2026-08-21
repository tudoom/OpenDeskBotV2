(function () {
  "use strict";

  const API_LIST = "/app/api/devices";
  const API_SELECT = "/app/api/devices/select";

  const channel =
    typeof BroadcastChannel === "function"
      ? new BroadcastChannel("deskbot-console-device")
      : null;

  function requestedDeviceId() {
    return new URL(window.location.href).searchParams.get("device_id") || "";
  }

  function pinPageTarget(deviceId) {
    const url = new URL(window.location.href);
    const id = String(deviceId || "").trim();
    if (id) url.searchParams.set("device_id", id);
    else url.searchParams.delete("device_id");
    window.history.replaceState(window.history.state, "", url);
  }

  function $(sel, root) {
    return (root || document).querySelector(sel);
  }

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function statusLabel(d) {
    return d.online ? "在线" : "离线";
  }

  function dispatchDeviceChanged(deviceId, generation) {
    window.__CURRENT_DEVICE_ID__ = deviceId || "";
    window.dispatchEvent(
      new CustomEvent("deskbot:device-changed", {
        detail: {
          device_id: deviceId || "",
          generation: Number(generation || 0),
        },
      })
    );
  }

  async function apiJson(url, opts) {
    const res = await fetch(url, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    return data;
  }

  function initDeviceSelector(root) {
    const trigger = $(".app-device-trigger", root);
    const menu = $(".app-device-menu", root);
    const listEl = $(".app-device-list", root);
    const triggerLabel = $(".app-device-trigger-label", root);
    const triggerMeta = $(".app-device-trigger-meta", root);
    const manageBtn = $(".app-device-manage-btn", root);
    const modal = document.getElementById("deviceManageModal");
    const modalBody = modal ? $(".device-manage-body", modal) : null;
    const modalClose = modal ? $(".device-modal-close", modal) : null;

    let devices = [];
    let currentId = requestedDeviceId() || window.__CURRENT_DEVICE_ID__ || "";
    let sessionId = window.__CURRENT_DEVICE_ID__ || "";
    let otherTabDeviceId = "";
    let refreshGeneration = 0;
    let selectionGeneration = 0;
    let selectionQueue = Promise.resolve();
    let open = false;

    function currentDevice() {
      return devices.find((d) => d.device_id === currentId) || null;
    }

    function updateTrigger() {
      const cur = currentDevice();
      if (!devices.length) {
        triggerLabel.textContent = "USB 设备";
        triggerMeta.textContent = "等待连接";
        return;
      }
      if (cur) {
        triggerLabel.textContent = cur.display_name || cur.device_id;
        const other = devices.find((d) => d.device_id === otherTabDeviceId);
        triggerMeta.textContent =
          other && other.device_id !== currentId
            ? `本页锁定 · 默认已切至 ${other.display_name || other.device_id}`
            : `${statusLabel(cur)} · ${cur.last_seen || "—"}`;
      } else {
        triggerLabel.textContent = "当前设备";
        triggerMeta.textContent = requestedDeviceId()
          ? "URL 中的目标设备不可用"
          : "请选择设备";
      }
    }

    function updatePinnedLinks() {
      document
        .querySelectorAll(".app-brand[href], .app-sidebar a[href]")
        .forEach((anchor) => {
          const original =
            anchor.getAttribute("data-device-link") ||
            anchor.getAttribute("href");
          if (!original) return;
          if (!anchor.hasAttribute("data-device-link")) {
            anchor.setAttribute("data-device-link", original);
          }
          const url = new URL(original, window.location.origin);
          if (url.origin !== window.location.origin) return;
          if (currentId) url.searchParams.set("device_id", currentId);
          else url.searchParams.delete("device_id");
          anchor.setAttribute("href", url.pathname + url.search + url.hash);
        });
    }

    function renderList() {
      if (!devices.length) {
        listEl.innerHTML = '<p class="app-device-empty muted">尚未检测到机器人，请连接 USB 数据线</p>';
        return;
      }
      listEl.innerHTML = devices
        .map((d) => {
          const active = d.device_id === currentId ? " active" : "";
          const onlineCls = d.online ? "online" : "offline";
          return (
            `<button type="button" class="app-device-item${active}" data-id="${escapeHtml(d.device_id)}">` +
            `<span class="app-device-item-id mono">${escapeHtml(d.device_id)}</span>` +
            `<span class="app-device-item-status ${onlineCls}">${escapeHtml(statusLabel(d))}</span>` +
            `<span class="app-device-item-seen muted">最近 ${escapeHtml(d.last_seen || "—")}</span>` +
            `</button>`
          );
        })
        .join("");
    }

    function renderManageTable() {
      if (!modalBody) return;
      if (!devices.length) {
        modalBody.innerHTML = '<p class="muted">尚未检测到 USB 设备</p>';
        return;
      }
      const rows = devices
        .map((d) => {
          const onlineCls = d.online ? "online" : "offline";
          const isCurrent = d.device_id === currentId;
          return (
            "<tr>" +
            `<td class="mono">${escapeHtml(d.device_id)}</td>` +
            `<td><span class="status-pill ${onlineCls}">${escapeHtml(statusLabel(d))}</span>` +
            `<br><span class="muted sm">最近 ${escapeHtml(d.last_seen || "—")}</span></td>` +
            "<td>" +
            (isCurrent
              ? '<span class="agent-badge green sm">当前</span> '
              : `<button type="button" class="agent-btn secondary sm dm-select" data-id="${escapeHtml(d.device_id)}">选为当前</button> `) +

            "</td></tr>"
          );
        })
        .join("");
      modalBody.innerHTML =
        '<table class="agent-table device-manage-table">' +
        "<thead><tr><th>设备 ID</th><th>状态</th><th>操作</th></tr></thead>" +
        `<tbody>${rows}</tbody></table>`;
    }

    function setOpen(next) {
      open = next;
      root.classList.toggle("open", open);
      trigger.setAttribute("aria-expanded", open ? "true" : "false");
    }

    function openModal(el) {
      if (!el) return;
      el.hidden = false;
      document.body.classList.add("device-modal-open");
    }

    function closeModal(el) {
      if (!el) return;
      el.hidden = true;
      if (!document.querySelector(".device-modal:not([hidden])")) {
        document.body.classList.remove("device-modal-open");
      }
    }

    async function refresh() {
      const generation = ++refreshGeneration;
      const selectedGeneration = selectionGeneration;
      const requested = requestedDeviceId();
      const data = await apiJson(API_LIST, { cache: "no-store" });
      if (
        generation !== refreshGeneration ||
        selectedGeneration !== selectionGeneration ||
        requested !== requestedDeviceId()
      ) {
        return;
      }
      devices = Array.isArray(data.devices) ? data.devices : [];
      sessionId = data.current_device_id || "";
      const knownIds = new Set(devices.map((d) => d.device_id));
      const nextId = requested
        ? (knownIds.has(requested) ? requested : "")
        : (knownIds.has(currentId)
            ? currentId
            : (knownIds.has(sessionId) ? sessionId : ""));
      const changed = nextId !== currentId;
      currentId = nextId;
      if (!requested && currentId) pinPageTarget(currentId);
      if (
        sessionId &&
        currentId &&
        sessionId !== currentId &&
        !otherTabDeviceId
      ) {
        otherTabDeviceId = sessionId;
      }
      window.__CURRENT_DEVICE_ID__ = currentId;
      renderList();
      updateTrigger();
      renderManageTable();
      updatePinnedLinks();
      if (changed) dispatchDeviceChanged(currentId, generation);
    }

    async function commitSelection(deviceId, generation) {
      if (generation !== selectionGeneration) return;
      await apiJson(API_SELECT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device_id: deviceId || "" }),
      });
      if (generation !== selectionGeneration) return;
      currentId = deviceId || "";
      sessionId = currentId;
      otherTabDeviceId = "";
      devices.forEach((d) => {
        d.is_current = d.device_id === currentId;
      });
      pinPageTarget(currentId);
      updateTrigger();
      renderList();
      renderManageTable();
      updatePinnedLinks();
      dispatchDeviceChanged(currentId, generation);
      broadcastSelection(currentId);
      setOpen(false);
    }

    function selectDevice(deviceId) {
      const id = String(deviceId || "").trim();
      const generation = ++selectionGeneration;
      // Serialize changes so two rapid clicks cannot complete on the server in
      // the opposite order and leave session state different from the page.
      selectionQueue = selectionQueue
        .catch(() => {})
        .then(() => commitSelection(id, generation));
      return selectionQueue;
    }

    function broadcastSelection(deviceId) {
      const message = {
        type: "device-selected",
        device_id: deviceId || "",
        at: Date.now(),
      };
      if (channel) channel.postMessage(message);
      try {
        localStorage.setItem(
          "deskbot-console-device-event",
          JSON.stringify(message)
        );
      } catch (_error) {
        // Storage may be unavailable in private browsing.
      }
    }

    function handleExternalSelection(message) {
      if (!message || message.type !== "device-selected") return;
      otherTabDeviceId = String(message.device_id || "");
      updateTrigger();
    }

    trigger.addEventListener("click", (e) => {
      e.stopPropagation();
      setOpen(!open);
    });

    listEl.addEventListener("click", (e) => {
      const btn = e.target.closest(".app-device-item");
      if (!btn) return;
      const id = btn.dataset.id || "";
      if (id && id !== currentId) void selectDevice(id);
      else setOpen(false);
    });

    manageBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      setOpen(false);
      renderManageTable();
      openModal(modal);
    });

    document.addEventListener("click", (e) => {
      if (!root.contains(e.target)) setOpen(false);
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        setOpen(false);
        closeModal(modal);
      }
    });

    modal?.addEventListener("click", (e) => {
      if (e.target === modal) closeModal(modal);
      const sel = e.target.closest(".dm-select");
      if (sel) void selectDevice(sel.dataset.id || "").then(() => closeModal(modal));
    });

    modalClose?.addEventListener("click", () => closeModal(modal));

    if (channel) {
      channel.addEventListener("message", (event) =>
        handleExternalSelection(event.data)
      );
    }
    window.addEventListener("storage", (event) => {
      if (
        event.key !== "deskbot-console-device-event" ||
        !event.newValue
      ) {
        return;
      }
      try {
        handleExternalSelection(JSON.parse(event.newValue));
      } catch (_error) {
        // Ignore malformed cross-tab events.
      }
    });

    void refresh();
    setInterval(() => {
      void refresh().catch(() => {});
    }, 30000);
  }

  document.addEventListener("DOMContentLoaded", () => {
    const root = document.getElementById("appDeviceBar");
    if (root) initDeviceSelector(root);
  });
})();
