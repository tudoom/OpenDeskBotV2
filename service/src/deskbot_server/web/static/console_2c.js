(function () {
  "use strict";

  const DEVICE_API = "/app/api/devices";
  const POLL_MS = 10000;
  const channel =
    typeof BroadcastChannel === "function"
      ? new BroadcastChannel("deskbot-console-device")
      : null;
  const listeners = new Set();
  let pollTimer = 0;
  let refreshSeq = 0;
  let otherTabDeviceId = "";
  let state = {
    phase: "loading",
    devices: [],
    availableUsbDevices: [],
    currentId: "",
    targetId: "",
    target: null,
    observedAt: "",
    controlPlane: null,
    usbDiscovery: null,
    error: "",
    generation: 0,
  };

  class ApiError extends Error {
    constructor(message, status, payload) {
      super(message);
      this.name = "ApiError";
      this.status = status || 0;
      this.payload = payload || {};
    }
  }

  function requestedDeviceId() {
    return new URL(location.href).searchParams.get("device_id") || "";
  }

  function withDevice(url, deviceId) {
    const next = new URL(url, location.origin);
    const id = deviceId === undefined ? state.targetId : String(deviceId || "");
    if (id) next.searchParams.set("device_id", id);
    else next.searchParams.delete("device_id");
    return next.origin === location.origin
      ? next.pathname + next.search + next.hash
      : next.toString();
  }

  function debugWsProtocols(token) {
    const value = String(token || "").trim();
    return value
      ? ["deskbot.debug.v1", `deskbot.debug.auth.${value}`]
      : ["deskbot.debug.v1"];
  }

  async function apiJson(url, options) {
    const opts = Object.assign({}, options || {});
    const timeoutMs = Number(opts.timeoutMs || 8000);
    const timeoutMessage = String(
      opts.timeoutMessage || "请求超时，请检查服务连接后重试"
    );
    delete opts.timeoutMs;
    delete opts.timeoutMessage;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    opts.signal = controller.signal;
    opts.credentials = opts.credentials || "same-origin";
    opts.cache = opts.cache || "no-store";
    opts.headers = Object.assign({ Accept: "application/json" }, opts.headers || {});
    try {
      const response = await fetch(url, opts);
      const payload = await response.json().catch(() => ({}));
      if (response.status === 401) {
        throw new ApiError("控制台会话已失效，请刷新页面后重试", 401, payload);
      }
      if (!response.ok || payload.ok === false) {
        const nestedError =
          payload.operation &&
          payload.operation.error &&
          payload.operation.error.message;
        throw new ApiError(
          payload.message ||
            nestedError ||
            (typeof payload.error === "string" && payload.error) ||
            `请求失败（HTTP ${response.status}）`,
          response.status,
          payload
        );
      }
      return payload;
    } catch (error) {
      if (error && error.name === "AbortError") {
        throw new ApiError(timeoutMessage, 0, {});
      }
      if (error instanceof ApiError) throw error;
      throw new ApiError("无法连接控制台服务，请稍后重试", 0, {});
    } finally {
      clearTimeout(timer);
    }
  }

  function newControlOperationId(prefix) {
    const safePrefix = String(prefix || "control")
      .replace(/[^A-Za-z0-9._:-]/g, "-")
      .slice(0, 32);
    const random =
      globalThis.crypto && typeof globalThis.crypto.randomUUID === "function"
        ? globalThis.crypto.randomUUID()
        : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    return `${safePrefix}:${random}`.slice(0, 128);
  }

  function controlOperationError(payload) {
    const operation = (payload && payload.operation) || {};
    const detail = operation.error || (payload && payload.error) || {};
    if (payload && payload.message) return payload.message;
    if (typeof detail === "string" && detail) return detail;
    if (detail && detail.message) return detail.message;
    const status = operation.status || (payload && payload.status) || "failed";
    return `设备控制未完成（${status}）`;
  }

  function isTransientControlError(error) {
    const status = Number(error && error.status);
    return status === 0 || status >= 500;
  }

  async function waitControlOperation(options) {
    const opts = options || {};
    const deviceId = String(opts.deviceId || "").trim();
    const operationId = String(opts.operationId || "").trim();
    if (!deviceId || !operationId) {
      throw new ApiError("缺少设备控制 operation_id", 0, {});
    }
    const baseUrl = String(opts.baseUrl || "/proxy/deskbot").replace(/\/$/, "");
    const timeoutMs = Math.max(1000, Number(opts.timeoutMs || 960000));
    const pollMs = Math.max(200, Number(opts.pollMs || 750));
    const deadline = Date.now() + timeoutMs;
    const tolerateNotFoundUntil =
      Date.now() + Math.max(0, Number(opts.tolerateNotFoundMs || 0));
    let transientFailures = 0;
    while (Date.now() < deadline) {
      const url = new URL(`${baseUrl}/api/control_operation`, location.origin);
      url.searchParams.set("device_id", deviceId);
      url.searchParams.set("operation_id", operationId);
      let payload;
      try {
        payload = await apiJson(url.toString(), {
          timeoutMs: Math.min(8000, Math.max(1000, deadline - Date.now())),
        });
        transientFailures = 0;
      } catch (error) {
        const notFoundDuringSubmitRecovery =
          Number(error && error.status) === 404 &&
          Date.now() < tolerateNotFoundUntil;
        if (!isTransientControlError(error) && !notFoundDuringSubmitRecovery) {
          throw error;
        }
        transientFailures += 1;
        const retryMs = Math.min(
          5000,
          pollMs * Math.pow(2, Math.min(4, transientFailures - 1))
        );
        await new Promise((resolve) => setTimeout(resolve, retryMs));
        continue;
      }
      const operation = payload.operation || {};
      if (operation.terminal || payload.terminal) {
        if (operation.status !== "completed") {
          throw new ApiError(
            controlOperationError(payload),
            200,
            payload
          );
        }
        return payload;
      }
      await new Promise((resolve) => setTimeout(resolve, pollMs));
    }
    throw new ApiError(
      `设备执行状态未知；请使用 operation_id=${operationId} 继续查询，勿直接重复操作`,
      0,
      { operation_id: operationId }
    );
  }

  async function submitControlOperation(options) {
    const opts = options || {};
    const deviceId = String(opts.deviceId || "").trim();
    const operationId = String(opts.operationId || "").trim();
    if (!deviceId || !operationId || !opts.url) {
      throw new ApiError("缺少设备控制提交参数", 0, {
        operation_id: operationId,
      });
    }
    let accepted = null;
    let submitUncertain = false;
    try {
      accepted = await apiJson(opts.url, opts.requestOptions || {});
    } catch (error) {
      if (!isTransientControlError(error)) throw error;
      submitUncertain = true;
    }
    const acceptedOperationId = String(
      (accepted && accepted.operation_id) || operationId
    );
    try {
      const terminal = await waitControlOperation({
        baseUrl: opts.baseUrl,
        deviceId,
        operationId: acceptedOperationId,
        timeoutMs: opts.timeoutMs,
        pollMs: opts.pollMs,
        tolerateNotFoundMs: submitUncertain ? 8000 : 0,
      });
      return {
        accepted: accepted || {},
        terminal,
        operationId: acceptedOperationId,
      };
    } catch (error) {
      if (!submitUncertain) throw error;
      throw new ApiError(
        `设备执行状态未知；请使用 operation_id=${acceptedOperationId} 继续查询，勿直接重复操作`,
        0,
        { operation_id: acceptedOperationId }
      );
    }
  }

  function copyState() {
    return {
      phase: state.phase,
      devices: state.devices.slice(),
      availableUsbDevices: state.availableUsbDevices.slice(),
      currentId: state.currentId,
      targetId: state.targetId,
      target: state.target ? Object.assign({}, state.target) : null,
      observedAt: state.observedAt,
      controlPlane: state.controlPlane
        ? Object.assign({}, state.controlPlane)
        : null,
      usbDiscovery: state.usbDiscovery
        ? Object.assign({}, state.usbDiscovery)
        : null,
      error: state.error,
      generation: state.generation,
    };
  }

  function notify() {
    const snapshot = copyState();
    listeners.forEach((listener) => {
      try {
        listener(snapshot);
      } catch (_error) {
        // A page listener must not break the shared status loop.
      }
    });
    window.dispatchEvent(
      new CustomEvent("deskbot:device-state", { detail: snapshot })
    );
  }

  function formatObserved(value) {
    const date = new Date(value || "");
    if (!Number.isFinite(date.getTime())) return "更新时间未知";
    return `更新于 ${date.toLocaleTimeString("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    })}`;
  }

  function presence(device) {
    if (!device) return "no-device";
    if (device.presence_state === "control_plane_down") return "control-down";
    if (device.online === true) return "online";
    if (device.online === false) return "offline";
    return "unknown";
  }

  function presenceLabel(device) {
    const value = presence(device);
    if (value === "online") return "在线";
    if (value === "offline") return "离线";
    if (value === "control-down") return "实时服务不可达";
    if (value === "unknown") return "状态未知";
    return "未选择设备";
  }

  function updatePinnedLinks() {
    const id = state.targetId;
    document.querySelectorAll(
      ".navi a[href], [data-device-link], main a[href^='/home'], main a[href^='/voice'], " +
        "main a[href^='/expr'], main a[href^='/lab'], main a[href^='/advanced'], " +
        "main a[href^='/memories'], main a[href^='/reminders'], main a[href^='/sessions'], " +
        "main a[href^='/preferences'], main a[href^='/people'], main a[href^='/devices'], " +
        "main a[href^='/miot']"
    ).forEach((anchor) => {
      const original =
        anchor.getAttribute("data-device-link") || anchor.getAttribute("href");
      if (!original) return;
      if (!anchor.hasAttribute("data-device-link")) {
        anchor.setAttribute("data-device-link", original);
      }
      anchor.setAttribute("href", withDevice(original, id));
    });
  }

  function renderStatus() {
    const link = document.getElementById("tbLink");
    const text = document.getElementById("tbLinkText");
    const observed = document.getElementById("tbLinkObserved");
    if (link && text) {
      const led = link.querySelector(".led");
      let status = "unknown";
      let label = "正在检查设备状态";
      if (state.phase === "error") {
        status = "control-down";
        label = "控制台状态不可用";
      } else if (state.phase === "ready") {
        if (!state.devices.length) {
          status = "no-device";
          label = "未检测到 USB 设备";
        } else if (!state.target) {
          status = "no-device";
          label = requestedDeviceId() ? "目标设备不可用" : "未选择设备";
        } else {
          status = presence(state.target);
          const name = state.target.display_name || state.target.device_id;
          label = `${name} · ${presenceLabel(state.target)}`;
        }
      }
      if (led) led.className = `led state-${status}`;
      text.textContent = label;
      link.setAttribute("data-state", status);
      link.setAttribute(
        "aria-label",
        `${label}，${formatObserved(state.observedAt)}`
      );
      link.title = formatObserved(state.observedAt);
      if (observed) observed.textContent = formatObserved(state.observedAt);
    }
    renderOtherTabNotice();
    updatePinnedLinks();
  }

  function renderOtherTabNotice() {
    const box = document.getElementById("globalDeviceNotice");
    if (!box) return;
    box.classList.remove("is-error");
    box.setAttribute("role", "status");
    box.setAttribute("aria-live", "polite");
    const microphoneHealth =
      state.target &&
      state.target.microphone_health &&
      typeof state.target.microphone_health === "object"
        ? state.target.microphone_health
        : null;
    const microphoneAlarm =
      state.target &&
      state.target.online === true &&
      microphoneHealth &&
      microphoneHealth.status === "mic_no_acoustic_signal";
    if (microphoneAlarm) {
      // The home page renders the same warning in its own device status area.
      if (document.querySelector("[data-page-microphone-health-alert]")) {
        box.hidden = true;
        box.innerHTML = "";
        return;
      }
      const link = withDevice("/devices", state.targetId);
      box.classList.add("is-error");
      box.setAttribute("role", "alert");
      box.setAttribute("aria-live", "assertive");
      box.innerHTML =
        "<div><b>麦克风没有收到声音，请检查扩展板或麦克风</b>" +
        "<small>设备仍在上传音频，但长期没有检测到可用的声学变化；检测到有效声音后会自动恢复。</small></div>" +
        `<a class="btn ghost" href="${escapeHtml(link)}">查看设备</a>`;
      box.hidden = false;
      return;
    }
    if (
      !otherTabDeviceId ||
      !state.targetId ||
      otherTabDeviceId === state.targetId
    ) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    const other = state.devices.find((row) => row.device_id === otherTabDeviceId);
    if (!other) {
      box.hidden = true;
      return;
    }
    const currentName =
      (state.target && (state.target.display_name || state.target.device_id)) ||
      state.targetId;
    const otherName = other.display_name || other.device_id;
    const link = withDevice(location.href, other.device_id);
    box.innerHTML =
      `<div><b>默认设备已在其他标签切换为 ${escapeHtml(otherName)}</b>` +
      `<small>本页仍锁定 ${escapeHtml(currentName)}，不会误操作其他设备。</small></div>` +
      `<a class="btn ghost" href="${escapeHtml(link)}">本页也切换</a>`;
    box.hidden = false;
  }

  function escapeHtml(value) {
    return String(value || "").replace(/[&<>"']/g, (char) => {
      return {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      }[char];
    });
  }

  function chooseTarget(devices, currentId, requested) {
    requested =
      requested === undefined ? requestedDeviceId() : String(requested || "");
    const targetId = requested || currentId || "";
    return {
      targetId,
      target: devices.find((row) => row.device_id === targetId) || null,
    };
  }

  async function refresh() {
    const seq = ++refreshSeq;
    const requested = requestedDeviceId();
    try {
      const payload = await apiJson(DEVICE_API, { timeoutMs: 5000 });
      if (seq !== refreshSeq || requested !== requestedDeviceId()) {
        return copyState();
      }
      const devices = Array.isArray(payload.devices) ? payload.devices : [];
      const availableUsbDevices = Array.isArray(
        payload.available_usb_devices
      )
        ? payload.available_usb_devices
        : [];
      const selected = chooseTarget(
        devices,
        payload.current_device_id || "",
        requested
      );
      state = {
        phase: "ready",
        devices,
        availableUsbDevices,
        currentId: payload.current_device_id || "",
        targetId: selected.targetId,
        target: selected.target,
        observedAt:
          payload.observed_at ||
          (payload.control_plane && payload.control_plane.observed_at) ||
          new Date().toISOString(),
        controlPlane: payload.control_plane || null,
        usbDiscovery: payload.usb_discovery || null,
        error: "",
        generation: seq,
      };
    } catch (error) {
      if (seq !== refreshSeq || requested !== requestedDeviceId()) {
        return copyState();
      }
      state = Object.assign({}, state, {
        phase: "error",
        observedAt: new Date().toISOString(),
        error: error.message || "设备状态加载失败",
        generation: seq,
      });
    }
    renderStatus();
    notify();
    return copyState();
  }

  function broadcastSelection(deviceId) {
    const message = {
      type: "device-selected",
      device_id: deviceId || "",
      at: Date.now(),
    };
    if (channel) channel.postMessage(message);
    try {
      localStorage.setItem("deskbot-console-device-event", JSON.stringify(message));
    } catch (_error) {
      // Storage can be unavailable in privacy modes; BroadcastChannel is preferred.
    }
  }

  async function selectDevice(deviceId, options) {
    const id = String(deviceId || "").trim();
    await apiJson("/app/api/devices/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_id: id || null }),
    });
    if (!options || options.pinPage !== false) {
      const url = new URL(location.href);
      if (id) url.searchParams.set("device_id", id);
      else url.searchParams.delete("device_id");
      history.replaceState(history.state, "", url);
    }
    otherTabDeviceId = "";
    broadcastSelection(id);
    return refresh();
  }

  function onChange(listener, immediate) {
    if (typeof listener !== "function") return function () {};
    listeners.add(listener);
    if (immediate !== false) listener(copyState());
    return function () {
      listeners.delete(listener);
    };
  }

  function handleExternalSelection(message) {
    if (!message || message.type !== "device-selected") return;
    otherTabDeviceId = String(message.device_id || "");
    void refresh();
  }

  function initMobileNavigation() {
    const toggle = document.getElementById("mobileNavToggle");
    const side = document.getElementById("sideNav");
    const scrim = document.getElementById("mobileNavScrim");
    if (!toggle || !side || !scrim) return;
    const mobile = window.matchMedia("(max-width: 900px)");
    let lastFocus = null;

    function setOpen(open) {
      const active = Boolean(open && mobile.matches);
      document.body.classList.toggle("mobile-nav-open", active);
      toggle.setAttribute("aria-expanded", active ? "true" : "false");
      side.setAttribute("aria-hidden", mobile.matches && !active ? "true" : "false");
      scrim.hidden = !active;
      if (active) {
        lastFocus = document.activeElement;
        const first = side.querySelector("a,button,[tabindex]:not([tabindex='-1'])");
        if (first) first.focus();
      } else if (
        lastFocus &&
        document.contains(lastFocus) &&
        document.activeElement &&
        side.contains(document.activeElement)
      ) {
        lastFocus.focus();
      }
    }

    toggle.addEventListener("click", () => {
      setOpen(toggle.getAttribute("aria-expanded") !== "true");
    });
    scrim.addEventListener("click", () => setOpen(false));
    side.addEventListener("click", (event) => {
      if (event.target.closest("a")) setOpen(false);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") setOpen(false);
      if (
        event.key === "Tab" &&
        document.body.classList.contains("mobile-nav-open")
      ) {
        const focusable = Array.from(
          side.querySelectorAll("a,button,[tabindex]:not([tabindex='-1'])")
        ).filter((element) => !element.disabled);
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    });
    const onMedia = () => setOpen(false);
    if (typeof mobile.addEventListener === "function") {
      mobile.addEventListener("change", onMedia);
    } else {
      mobile.addListener(onMedia);
    }
    setOpen(false);
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      if (!document.hidden) await refresh();
      schedulePoll();
    }, POLL_MS);
  }

  window.DeskbotConsole = {
    ApiError,
    apiJson,
    controlOperationError,
    debugWsProtocols,
    formatObserved,
    getSnapshot: copyState,
    newControlOperationId,
    onChange,
    presence,
    presenceLabel,
    refresh,
    selectDevice,
    submitControlOperation,
    waitControlOperation,
    withDevice,
  };

  if (channel) channel.addEventListener("message", (event) => handleExternalSelection(event.data));
  window.addEventListener("storage", (event) => {
    if (event.key !== "deskbot-console-device-event" || !event.newValue) return;
    try {
      handleExternalSelection(JSON.parse(event.newValue));
    } catch (_error) {
      // Ignore malformed storage events.
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) void refresh();
  });
  document.addEventListener("DOMContentLoaded", () => {
    initMobileNavigation();
    void refresh();
    schedulePoll();
  });
})();
