"use strict";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const state = {
  bmcIp: "",
  hostIp: "",
  connected: false,
  creds: { username: "", password: "" },
  osCreds: { ip: "", username: "aerial", password: "nvidia" },
  scope: {
    ip: "",
    port: "",
    connected: false,
    idn: "",
    vendor: "",
    live: false,
    vncPort: "5900",
    vncUser: "Tek_Local_Admin",
    vncPassword: "labuser",
    vncConnected: false,
  },
  bmcName: "",
  activeTab: "overview",
  flashJobs: {},
  flashNotified: {},
  flashDismissed: {},
  firmwareCache: null,
  bmcShell: { lines: [], history: [], historyPos: -1, initialized: false },
};

const API = "/api/bmc";

// ---------------------------------------------------------------------------
// DOM helpers
// ---------------------------------------------------------------------------
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function")
      node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c == null) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

function toast(message, kind = "ok") {
  let box = $("#toast");
  if (!box) {
    box = el("div", { id: "toast" });
    document.body.appendChild(box);
  }
  const item = el("div", { class: `toast-item toast-${kind}` }, message);
  box.appendChild(item);
  setTimeout(() => item.remove(), 5000);
}

let notifyBannerTimer = null;

function showNotifyBanner({ kind = "ok", title, message, duration = 12000 }) {
  const banner = $("#notify-banner");
  const icon = $("#notify-banner-icon");
  const titleEl = $("#notify-banner-title");
  const msgEl = $("#notify-banner-msg");
  if (!banner || !titleEl || !msgEl) return;

  banner.className = `notify-banner notify-banner-${kind}`;
  if (icon) icon.textContent = kind === "ok" ? "✓" : kind === "err" ? "✕" : "ℹ";
  titleEl.textContent = title || "";
  msgEl.textContent = message || "";
  banner.classList.remove("hidden");

  if (notifyBannerTimer) clearTimeout(notifyBannerTimer);
  notifyBannerTimer = setTimeout(() => hideNotifyBanner(), duration);
}

function hideNotifyBanner() {
  const banner = $("#notify-banner");
  if (banner) banner.classList.add("hidden");
  if (notifyBannerTimer) {
    clearTimeout(notifyBannerTimer);
    notifyBannerTimer = null;
  }
}

$("#notify-banner-close")?.addEventListener("click", hideNotifyBanner);

function reportTimestamp() {
  return new Date().toLocaleString();
}

function reportFilename(prefix) {
  const host = (state.bmcIp || "bmc").replace(/[^\w.-]+/g, "_");
  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
  return `mgx-arc-${prefix}-${host}-${stamp}.txt`;
}

function downloadTextReport(filename, content) {
  const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: filename });
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  toast(`Downloaded ${filename}.`);
}

async function copyReportText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_) {
    const ta = el("textarea", { value: text });
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  }
}

function shareToTeams(text, title) {
  const href = window.location.href;
  const msgText = `${title}\n\n${text}`.slice(0, 3500);
  const url = `https://teams.microsoft.com/share?href=${encodeURIComponent(href)}&msgText=${encodeURIComponent(msgText)}`;
  window.open(url, "_blank", "noopener,noreferrer");
  toast("Opening Microsoft Teams — paste or send the report.");
}

async function shareToSlack(text, title) {
  const body = `${title}\n\n${text}`;
  const copied = await copyReportText(body);
  window.open("https://app.slack.com/client", "_blank", "noopener,noreferrer");
  toast(copied
    ? "Report copied — paste into Slack (Ctrl+V)."
    : "Opening Slack — copy the report from Download first.");
}

function buildFirmwareReportText(data) {
  const parts = data?.parts || [];
  const lines = [
    "MGX ARC Firmware Report",
    "=".repeat(40),
    `Generated: ${reportTimestamp()}`,
    `BMC: ${state.bmcIp || "—"}`,
    `Hostname: ${state.bmcName || "—"}`,
    `Parts listed: ${parts.length}`,
    `Versions found: ${parts.filter((p) => p.version).length}`,
    "",
    "Firmware Inventory",
    "-".repeat(40),
  ];
  for (const p of parts) {
    lines.push(
      `${p.name || "—"}`,
      `  Inventory ID: ${p.redfish_id || "—"}`,
      `  Version:      ${p.version || "—"}`,
      `  Source:       ${p.source || (p.version ? "redfish" : "not reported")}`,
      "",
    );
  }
  return lines.join("\n");
}

function buildBmcShellReportText() {
  const lines = [
    "MGX ARC BMC Shell Session",
    "=".repeat(40),
    `Generated: ${reportTimestamp()}`,
    `BMC: ${state.bmcIp || "—"}`,
    `Hostname: ${state.bmcName || "—"}`,
    "",
    "Session transcript",
    "-".repeat(40),
    "",
  ];
  for (const line of state.bmcShell.lines) {
    lines.push(line.text);
  }
  return lines.join("\n");
}

function buildFlashJobReportText(job) {
  const lines = [
    "MGX ARC Flash Job Report",
    "=".repeat(40),
    `Generated: ${reportTimestamp()}`,
    `BMC: ${job.bmc_ip || state.bmcIp || "—"}`,
    `Job: ${job.label || "Flash"}`,
    `Status: ${job.status || "—"}`,
    `Phase: ${job.phase || "—"}`,
    `Message: ${job.message || "—"}`,
  ];
  if (job.from_version || job.to_version) {
    lines.push(`Version: ${job.from_version || "—"} → ${job.to_version || "—"}`);
  }
  if (job.error) lines.push(`Error: ${job.error}`);
  lines.push("", "Terminal — Redfish / update task", "-".repeat(40), "");
  for (const entry of job.terminal_log || []) {
    lines.push(entry.line || "");
  }
  if (job.ssh_terminal_log && job.ssh_terminal_log.length) {
    lines.push("", "SSH terminal — aux_cycle / version confirm", "-".repeat(40), "");
    for (const entry of job.ssh_terminal_log) {
      lines.push(entry.line || "");
    }
  }
  return lines.join("\n");
}

function buildReportActionBar({ getContent, filenamePrefix, reportTitle }) {
  const runDownload = () => {
    const content = getContent();
    if (!content?.trim()) {
      toast("Nothing to save yet.", "err");
      return;
    }
    downloadTextReport(reportFilename(filenamePrefix), content);
  };
  const runTeams = async () => {
    const content = getContent();
    if (!content?.trim()) {
      toast("Nothing to share yet.", "err");
      return;
    }
    await copyReportText(content);
    shareToTeams(content, reportTitle);
  };
  const runSlack = async () => {
    const content = getContent();
    if (!content?.trim()) {
      toast("Nothing to share yet.", "err");
      return;
    }
    await shareToSlack(content, reportTitle);
  };
  return el("div", { class: "report-actions" }, [
    el("button", { type: "button", class: "btn btn-ghost report-btn", onclick: runDownload }, "Download report"),
    el("button", { type: "button", class: "btn btn-ghost report-btn report-btn-teams", onclick: runTeams }, "Share to Teams"),
    el("button", { type: "button", class: "btn btn-ghost report-btn report-btn-slack", onclick: runSlack }, "Share to Slack"),
  ]);
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------
async function api(path, options = {}) {
  const opts = { headers: {}, ...options };
  opts.headers["Content-Type"] = "application/json";
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) {
    throw new Error((data && data.error) || `HTTP ${res.status}`);
  }
  return data;
}

function bmcHeaders() {
  return {
    "X-BMC-Host": state.bmcIp,
    "X-Host-IP": state.hostIp || "",
    "X-BMC-User": state.creds.username,
    "X-BMC-Pass": state.creds.password,
  };
}

// NVIDIA eye loader — green lights orbit the eye while a tab loads its data.
function loadingHTML(label = "Loading…") {
  return `
    <div class="loading eye-loader">
      <div class="eye-loader-ring">
        <span class="eye-orbit"><i class="eye-dot"></i></span>
        <span class="eye-orbit eye-orbit-2"><i class="eye-dot"></i></span>
        <img class="eye-loader-eye" src="/static/img/nvidia-eye.svg" alt="" />
      </div>
      <div class="eye-loader-text">${label}</div>
    </div>`;
}

// ---------------------------------------------------------------------------
// Connect / disconnect
// ---------------------------------------------------------------------------
function showLogin() {
  state.connected = false;
  $("#login-view").classList.remove("hidden");
  $("#dashboard-view").classList.add("hidden");
  $("#health-console-view")?.classList.add("hidden");
  setConsoleMode("direct");
  setConnPill("Not connected", "pill-idle");
  playLoginIntro();
}

// Restart the entrance animation on the login boxes. Removing then re-adding
// the class (after a reflow) forces the CSS animations to play again each time
// the login view appears.
function playLoginIntro() {
  const view = $("#login-view");
  if (!view) return;
  view.classList.remove("login-intro");
  void view.offsetWidth;
  view.classList.add("login-intro");
}

function showDashboard() {
  $("#login-view").classList.add("hidden");
  $("#dashboard-view").classList.remove("hidden");
  $("#health-console-view")?.classList.add("hidden");
  setConsoleMode("direct");
}

function setConsoleMode(mode) {
  $("#direct-console-btn")?.classList.toggle("active", mode === "direct");
  $("#health-console-btn")?.classList.toggle("active", mode === "health");
}

function setConnPill(text, cls) {
  const pill = $("#conn-pill");
  pill.textContent = text;
  pill.className = "pill " + cls;
}

function setStatus(text, cls) {
  const s = $("#sv-status");
  s.textContent = text;
  s.className = "pill " + cls;
}

function openBmcGui(bmcIp) {
  const host = (bmcIp || "").trim();
  if (!host) {
    toast("Enter or connect to a BMC address first.", "err");
    return;
  }
  window.open(`https://${host}`, "_blank", "noopener,noreferrer");
}

$("#bmc-gui-login-btn").addEventListener("click", () => {
  openBmcGui($("#bmc-ip").value.trim());
});

$("#bmc-gui-btn").addEventListener("click", () => {
  openBmcGui(state.bmcIp);
});

// ---------------------------------------------------------------------------
// OS IP Detect — open the BMC KVM console, then read the OS IP from ifconfig
// ---------------------------------------------------------------------------
const BMC_KVM_LOGIN = { username: "root", password: "0penBmc" };

// hostusb0 (10.0.1.x) is the BMC-host management link, not the lab OS IP.
const OS_IP_SKIP_IFACE = /^(lo|usb\d|hostusb\d|docker|br-|veth|virbr|tailscale|wg\d)/i;

function kvmOperationsUrl(bmcIp) {
  return `https://${bmcIp}/#/operations/kvm`;
}

/** Parse `ifconfig` or `ip addr` output into [{iface, ip}]. */
function parseIfconfigAddresses(text) {
  const found = [];
  let iface = "";
  for (const raw of String(text || "").replace(/\r/g, "").split("\n")) {
    const line = raw.trim();
    // "enx9c69d3288bf0: flags=4163<UP,...>" or "3: enx9c69d3288bf0: <BROADCAST,...>"
    const header = line.match(/^(?:\d+:\s*)?([A-Za-z0-9._@-]+):\s*(?:flags=|<)/);
    if (header) {
      iface = header[1];
      continue;
    }
    const addr = line.match(/^inet\s+(?:addr:)?(\d{1,3}(?:\.\d{1,3}){3})/);
    if (!addr) continue;
    // `ip addr` repeats the interface name at the end of the inet line.
    let name = iface;
    const trailing = line.match(/\s([A-Za-z0-9._@-]+)$/);
    if (trailing && /^(en|eth|usb|host)/i.test(trailing[1])) name = trailing[1];
    found.push({ iface: name || "?", ip: addr[1] });
  }
  return found;
}

function osIpKind({ iface, ip }) {
  if (OS_IP_SKIP_IFACE.test(iface)) return "link";
  if (/^127\./.test(ip)) return "loopback";
  if (/^169\.254\./.test(ip)) return "linklocal";
  if (/^10\.0\.1\./.test(ip)) return "link";
  return "os";
}

/** Pick the host's real lab IP, preferring physical Ethernet interfaces. */
function pickOsIp(text) {
  const rows = parseIfconfigAddresses(text).map((row) => ({ ...row, kind: osIpKind(row) }));
  const usable = rows.filter((row) => row.kind === "os");
  const physical = usable.find((row) => /^(enx|en|eth)/i.test(row.iface));
  return { rows, chosen: physical || usable[0] || null };
}

function setDetectedOsIp(ip) {
  state.hostIp = ip;
  state.osCreds = { ...state.osCreds, ip };
  const ipEl = $("#sv-ip");
  if (ipEl) {
    ipEl.textContent = [
      state.bmcIp ? `BMC IP ${state.bmcIp}` : "",
      ip ? `OS IP ${ip}` : "",
    ].filter(Boolean).join("  ·  ");
  }
}

function closeOsIpModal() {
  $("#os-ip-modal")?.remove();
  document.removeEventListener("keydown", osIpModalEscHandler);
}

function osIpModalEscHandler(event) {
  if (event.key === "Escape") closeOsIpModal();
}

function openOsIpDetectModal(bmcIp) {
  closeOsIpModal();
  const url = kvmOperationsUrl(bmcIp);

  const pasteBox = el("textarea", {
    class: "os-ip-paste",
    rows: "8",
    spellcheck: "false",
    placeholder:
      "Paste the ifconfig output from the KVM console here, for example:\n\n"
      + "enx9c69d3288bf0: flags=4163<UP,BROADCAST,RUNNING,MULTICAST>  mtu 1500\n"
      + "        inet 10.137.175.29  netmask 255.255.254.0  broadcast 10.137.175.255",
  });
  const resultEl = el("div", { class: "os-ip-result muted" },
    "Paste the output above, then click Read OS IP.");

  const openBtn = el("button", { class: "btn btn-primary" }, "Open KVM console");
  openBtn.addEventListener("click", () => openKvmConsole(bmcIp));

  const copyPassBtn = el("button", { class: "btn btn-ghost" }, "Copy BMC password");
  copyPassBtn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(BMC_KVM_LOGIN.password);
      toast("BMC password copied.");
    } catch (_) {
      toast(`Copy failed — the password is ${BMC_KVM_LOGIN.password}`, "err");
    }
  });

  const readBtn = el("button", { class: "btn btn-primary" }, "Read OS IP");
  const useBtn = el("button", { class: "btn btn-ghost hidden" }, "Use this IP");

  readBtn.addEventListener("click", () => {
    const { rows, chosen } = pickOsIp(pasteBox.value);
    resultEl.innerHTML = "";
    if (!rows.length) {
      resultEl.className = "os-ip-result os-ip-fail";
      resultEl.textContent =
        "No 'inet' lines found. Copy the whole ifconfig output, including the inet lines.";
      useBtn.classList.add("hidden");
      return;
    }
    const detail = el("ul", { class: "os-ip-list" }, rows.map((row) => {
      const label = row.kind === "os"
        ? "candidate OS IP"
        : row.kind === "link" ? "BMC-host / virtual link (skipped)"
        : row.kind === "loopback" ? "loopback (skipped)"
        : "link-local (skipped)";
      return el("li", { class: row.kind === "os" ? "os-ip-hit" : "muted" },
        `${row.iface} — ${row.ip} — ${label}`);
    }));

    if (!chosen) {
      resultEl.className = "os-ip-result os-ip-fail";
      resultEl.append(
        el("strong", {}, "No lab OS IP found."),
        el("div", { class: "muted" },
          "Only loopback / BMC-host link addresses were present. Confirm the host NIC is up."),
        detail,
      );
      useBtn.classList.add("hidden");
      return;
    }

    resultEl.className = "os-ip-result os-ip-pass";
    resultEl.append(
      el("strong", {}, `OS IP: ${chosen.ip}`),
      el("div", { class: "muted" }, `Interface ${chosen.iface}`),
      detail,
    );
    useBtn.classList.remove("hidden");
    useBtn.onclick = () => {
      setDetectedOsIp(chosen.ip);
      toast(`OS IP set to ${chosen.ip} — PCIe and USB tabs will use it.`);
      closeOsIpModal();
    };
  });

  const body = el("div", { class: "os-ip-modal-body" }, [
    el("p", { class: "muted" }, [
      document.createTextNode("The BMC KVM console is a remote screen, so this page cannot type into it. "),
      document.createTextNode("Follow the steps below, then paste the "),
      el("code", {}, "ifconfig"),
      document.createTextNode(" output back here to pull out the OS IP."),
    ]),
    el("ol", { class: "os-ip-steps" }, [
      el("li", {}, [
        document.createTextNode("A new tab opens at "),
        el("code", {}, url),
        document.createTextNode("."),
      ]),
      el("li", {}, [
        el("strong", {}, "Not secure warning"),
        document.createTextNode(" — the BMC uses a self-signed certificate. Click "),
        el("code", {}, "Advanced"),
        document.createTextNode(", then "),
        el("code", {}, `Continue to ${bmcIp} (unsafe)`),
        document.createTextNode("."),
      ]),
      el("li", {}, [
        el("strong", {}, "Log in"),
        document.createTextNode(" — username "),
        el("code", {}, BMC_KVM_LOGIN.username),
        document.createTextNode(", password "),
        el("code", {}, BMC_KVM_LOGIN.password),
        document.createTextNode("."),
      ]),
      el("li", {}, [
        document.createTextNode("In the left menu open "),
        el("strong", {}, "Operations → KVM"),
        document.createTextNode(" (the link above lands there already)."),
      ]),
      el("li", {}, [
        document.createTextNode("Wait until the "),
        el("strong", {}, "KVM console"),
        document.createTextNode(" paints the host screen, then log in to the OS."),
      ]),
      el("li", {}, [
        document.createTextNode("Run "),
        el("code", {}, "ifconfig"),
        document.createTextNode(", then copy the output and paste it below."),
      ]),
    ]),
    el("div", { class: "os-ip-actions" }, [openBtn, copyPassBtn]),
    el("div", { class: "section-title" }, "Paste ifconfig output"),
    pasteBox,
    el("div", { class: "os-ip-actions" }, [readBtn, useBtn]),
    resultEl,
    el("p", { class: "muted os-ip-note" }, [
      el("strong", {}, "Note: "),
      document.createTextNode(
        "hostusb0 / 10.0.1.x is the BMC-host USB link, not the lab OS IP. "
        + "Those entries are skipped automatically."),
    ]),
  ]);

  const closeBtn = el("button", {
    class: "os-ip-modal-close",
    type: "button",
    "aria-label": "Close",
  }, "×");
  closeBtn.addEventListener("click", closeOsIpModal);

  const modal = el("div", { class: "os-ip-modal", id: "os-ip-modal" }, [
    el("div", { class: "os-ip-modal-card" }, [
      el("div", { class: "os-ip-modal-head" }, [
        el("h3", {}, `OS IP Detect — BMC ${bmcIp}`),
        closeBtn,
      ]),
      body,
    ]),
  ]);
  modal.addEventListener("click", (event) => {
    if (event.target === modal) closeOsIpModal();
  });

  document.body.appendChild(modal);
  document.addEventListener("keydown", osIpModalEscHandler);
  pasteBox.focus();
}

function openKvmConsole(bmcIp) {
  const url = kvmOperationsUrl(bmcIp);
  const tab = window.open(url, "_blank", "noopener,noreferrer");
  toast(
    tab ? "Opening BMC KVM console." : `Popup blocked — open ${url} manually.`,
    tab ? "ok" : "err",
  );
  return Boolean(tab);
}

$("#os-ip-detect-btn")?.addEventListener("click", () => {
  const bmcIp = state.bmcIp || $("#bmc-ip")?.value.trim() || "";
  if (!bmcIp) {
    toast("Connect to a BMC first (or enter its IP on the login screen).", "err");
    return;
  }
  // Opened during the click so the browser treats it as user-initiated.
  openKvmConsole(bmcIp);
  openOsIpDetectModal(bmcIp);
});

$("#direct-console-btn")?.addEventListener("click", () => {
  $("#health-console-view")?.classList.add("hidden");
  if (state.connected) {
    $("#login-view").classList.add("hidden");
    $("#dashboard-view").classList.remove("hidden");
  } else {
    $("#dashboard-view").classList.add("hidden");
    $("#login-view").classList.remove("hidden");
    playLoginIntro();
  }
  setConsoleMode("direct");
});

$("#connect-btn").addEventListener("click", async () => {
  const bmcIp = $("#bmc-ip").value.trim();
  const hostIp = "";
  const username = $("#bmc-user").value.trim();
  const password = $("#bmc-pass").value;
  const msg = $("#connect-msg");

  if (!bmcIp || !username || !password) {
    msg.className = "msg msg-err";
    msg.textContent = "BMC address, username, and password are required.";
    msg.classList.remove("hidden");
    return;
  }

  msg.className = "msg";
  msg.textContent = "Connecting…";
  msg.classList.remove("hidden");
  $("#connect-btn").disabled = true;

  try {
    const res = await api(`${API}/connect`, {
      method: "POST",
      body: JSON.stringify({ bmc_ip: bmcIp, host_ip: hostIp, username, password }),
    });
    state.bmcIp = bmcIp;
    state.hostIp = hostIp;
    state.creds = { username, password };
    state.connected = true;
    state.bmcName = res.bmc_name || "BMC";
    state.bmcShell = { lines: [], history: [], historyPos: -1, initialized: false };
    state.firmwareCache = null;
    state.flashNotified = {};
    $("#sv-name").textContent = state.bmcName;
    const verEl = $("#sv-version");
    verEl.textContent = res.bmc_version ? `BMC firmware ${res.bmc_version}` : "";
    const ipEl = $("#sv-ip");
    if (ipEl) ipEl.textContent = bmcIp ? `BMC IP ${bmcIp}` : "";
    setStatus("Connected", "pill-ok");
    setConnPill("Connected", "pill-ok");
    showDashboard();
    switchTab(state.activeTab);
    loadActiveFlashJobs();
    toast("Connected.");
  } catch (err) {
    state.connected = false;
    msg.className = "msg msg-err";
    msg.textContent = err.message;
  } finally {
    $("#connect-btn").disabled = false;
  }
});

$("#disconnect-btn").addEventListener("click", () => {
  stopScopeLive();
  stopScopeVnc();
  state.bmcIp = "";
  state.hostIp = "";
  state.creds = { username: "", password: "" };
  state.osCreds = { ip: "", username: "aerial", password: "nvidia" };
  state.scope = {
    ip: "", port: "", connected: false, idn: "", vendor: "", live: false,
    ...SCOPE_VNC_DEFAULTS,
  };
  state.bmcName = "";
  state.flashJobs = {};
  state.flashNotified = {};
  state.flashDismissed = {};
  state.firmwareCache = null;
  state.bmcShell = { lines: [], history: [], historyPos: -1, initialized: false };
  $("#bmc-pass").value = "";
  $("#connect-msg").classList.add("hidden");
  $("#sv-name").textContent = "—";
  $("#sv-version").textContent = "";
  const ipEl = $("#sv-ip");
  if (ipEl) ipEl.textContent = "";
  renderFlashProgressPanel();
  showLogin();
  toast("Disconnected.");
});

$("#refresh-btn").addEventListener("click", () => switchTab(state.activeTab, true));

$("#fpp-close-btn")?.addEventListener("click", () => {
  const jobs = Object.values(state.flashJobs).filter(
    (j) => (!state.bmcIp || j.bmc_ip === state.bmcIp) && !state.flashDismissed[j.id]
  );
  const hasActive = jobs.some(flashJobIsActive);
  if (hasActive) {
    // Keep active flashes visible; only hide finished ones.
    dismissFinishedFlashJobs();
    toast("Finished flash results hidden. Active flash stays visible.");
    return;
  }
  dismissFinishedFlashJobs();
});

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------
$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => switchTab(tab.dataset.tab));
});

function switchTab(name, force = false) {
  if (!state.connected) return;
  if (state.activeTab === "scope" && name !== "scope") {
    stopScopeLive();
    stopScopeVnc();
  }
  state.activeTab = name;
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".tab-panel").forEach((p) => p.classList.add("hidden"));
  const panel = $(`#panel-${name}`);
  panel.classList.remove("hidden");

  const loaders = {
    overview: loadOverview,
    power: loadPower,
    firmware: loadFirmware,
    flash: loadFlash,
    shell: loadBmcShell,
    spi: loadSpiRead,
    i2c: loadI2c,
    usb: loadUsbEnum,
    pcie: loadPcie,
    kvm: loadKvm,
    scope: loadScope,
  };
  loaders[name](panel);
}

// ---------------------------------------------------------------------------
// Flash jobs — background progress (persists across tab switches)
// ---------------------------------------------------------------------------
let flashPollTimer = null;

function notifyFlashJobResult(prev, job) {
  if (!job?.id) return;
  if (!prev || prev.status === job.status) return;
  if (job.status !== "completed" && job.status !== "failed") return;
  if (state.flashNotified[job.id]) return;
  state.flashNotified[job.id] = true;

  const label = job.label || "Flash";
  if (job.status === "completed") {
    const ver = job.to_version ? ` New version: ${job.to_version}.` : "";
    showNotifyBanner({
      kind: "ok",
      title: `${label} — PASSED`,
      message: (job.message || "Flash completed successfully.") + ver,
    });
    toast(`${label} flash passed.`, "ok");
  } else {
    showNotifyBanner({
      kind: "err",
      title: `${label} — FAILED`,
      message: job.error || job.message || "Flash did not complete. Check the Flash Progress panel.",
    });
    toast(`${label} flash failed.`, "err");
  }
}

function ensureFlashPolling() {
  if (flashPollTimer) return;
  flashPollTimer = setInterval(pollFlashJobs, 1200);
  pollFlashJobs();
}

async function pollFlashJobs() {
  if (!state.bmcIp || !state.connected) {
    if (flashPollTimer) {
      clearInterval(flashPollTimer);
      flashPollTimer = null;
    }
    return;
  }
  const activeIds = Object.keys(state.flashJobs).filter((id) => {
    const j = state.flashJobs[id];
    const s = j.status;
    return j.bmc_ip === state.bmcIp && (s === "queued" || s === "running");
  });
  if (!activeIds.length) {
    if (flashPollTimer) {
      clearInterval(flashPollTimer);
      flashPollTimer = null;
    }
    renderFlashProgressPanel();
    return;
  }
  for (const id of activeIds) {
    try {
      const prev = state.flashJobs[id];
      const job = await api(
        `/api/bmc/flash/jobs/${id}`,
        { headers: bmcHeaders() }
      );
      state.flashJobs[id] = job;
      notifyFlashJobResult(prev, job);
      if (
        job.status === "completed"
        && prev && prev.status !== "completed"
        && (job.label || "").startsWith("BMC")
        && state.activeTab === "flash"
      ) {
        const panel = $("#panel-flash");
        if (panel) loadFlash(panel);
      }
    } catch (_) { /* job may have expired */ }
  }
  renderFlashProgressPanel();
}

async function loadActiveFlashJobs() {
  if (!state.bmcIp || !state.connected) return;
  try {
    const d = await api(`/api/bmc/flash/jobs`, { headers: bmcHeaders() });
    for (const j of d.jobs || []) {
      state.flashJobs[j.id] = j;
    }
    const hasActive = (d.jobs || []).some(
      (j) => j.status === "queued" || j.status === "running"
    );
    if (hasActive) ensureFlashPolling();
    renderFlashProgressPanel();
  } catch (_) {}
}

function flashJobIsActive(job) {
  return job && (job.status === "queued" || job.status === "running");
}

function flashJobResultText(job) {
  if (job.status === "failed") {
    return job.error || job.message || "Flash did not complete.";
  }
  if (job.status === "completed") {
    return job.message || "Flash completed successfully.";
  }
  return job.message || "";
}

function dismissFlashJob(jobId) {
  if (!jobId) return;
  state.flashDismissed[jobId] = true;
  renderFlashProgressPanel();
}

function dismissFinishedFlashJobs() {
  for (const job of Object.values(state.flashJobs)) {
    if (!flashJobIsActive(job)) state.flashDismissed[job.id] = true;
  }
  renderFlashProgressPanel();
}

function renderFlashProgressPanel() {
  const panel = $("#flash-progress-panel");
  const list = $("#flash-jobs-list");
  const titleEl = $("#fpp-title");
  const subtitleEl = $("#fpp-subtitle");
  if (!panel || !list) return;

  const jobs = Object.values(state.flashJobs)
    .filter((j) => !state.bmcIp || j.bmc_ip === state.bmcIp)
    .filter((j) => !state.flashDismissed[j.id])
    .sort(
      (a, b) => (b.created_at || 0) - (a.created_at || 0)
    );
  if (!jobs.length) {
    panel.classList.add("hidden");
    list.innerHTML = "";
    return;
  }

  const hasActive = jobs.some(flashJobIsActive);
  if (titleEl) titleEl.textContent = hasActive ? "Flash Progress" : "Flash Result";
  if (subtitleEl) {
    subtitleEl.textContent = hasActive
      ? "Flashing in progress — switch tabs freely. Close (×) hides finished results only."
      : "Flash finished. Close (×) to hide this panel.";
  }

  panel.classList.remove("hidden");
  list.innerHTML = "";

  for (const job of jobs) {
    const pct = Math.max(0, Math.min(100, job.percent || 0));
    const done = job.status === "completed" || job.status === "failed";
    const statusCls =
      job.status === "completed" ? "fpp-ok"
      : job.status === "failed" ? "fpp-err"
      : "fpp-run";
    const statusLabel =
      job.status === "completed" ? "PASSED"
      : job.status === "failed" ? "FAILED"
      : (job.status || "—").toUpperCase();

    const card = el("div", { class: `fpp-card ${statusCls}` });

    const titleRow = el("div", { class: "fpp-title-row" }, [
      el("div", { class: "fpp-title" }, job.label || "Flash"),
    ]);
    if (done) {
      titleRow.appendChild(el("button", {
        type: "button",
        class: "fpp-card-close",
        title: "Dismiss",
        "aria-label": "Dismiss this result",
        onclick: () => dismissFlashJob(job.id),
      }, "×"));
    }
    card.appendChild(titleRow);

    card.appendChild(el("div", { class: "fpp-meta" }, [
      el("span", { class: `fpp-status fpp-status-${job.status}` }, statusLabel),
      el("span", { class: "fpp-pct" }, `${pct}%`),
    ]));

    if (flashJobIsActive(job)) {
      card.appendChild(el("div", { class: "fpp-bar" }, [
        el("div", { class: "fpp-bar-fill", style: `width:${pct}%` }),
      ]));
      if (job.phase) card.appendChild(el("div", { class: "fpp-phase" }, job.phase));
      if (job.message) card.appendChild(el("div", { class: "fpp-msg muted" }, job.message));
    } else {
      const why = flashJobResultText(job);
      card.appendChild(el("div", {
        class: job.status === "completed" ? "fpp-result fpp-result-ok" : "fpp-result fpp-result-err",
      }, [
        el("div", { class: "fpp-result-label" },
          job.status === "completed" ? "Passed — why" : "Failed — why"),
        el("div", { class: "fpp-result-text" }, why),
      ]));
    }

    if (job.from_version || job.to_version) {
      const verLabel = (job.label || "").toLowerCase().startsWith("sbios")
        ? "SBIOS version: "
        : (job.label || "").toLowerCase().startsWith("bmc")
          ? "BMC version: "
          : "Version: ";
      card.appendChild(el("div", { class: "fpp-version muted" }, [
        verLabel,
        el("code", {}, job.from_version || "—"),
        " → ",
        el("code", {}, job.to_version || (flashJobIsActive(job) ? "…" : "—")),
      ]));
    }

    if (job.curl_command) {
      card.appendChild(el("details", { class: "fpp-curl" }, [
        el("summary", {}, "Equivalent curl / SSH commands"),
        el("pre", {}, job.curl_command),
      ]));
    }

    if (job.terminal_log && job.terminal_log.length) {
      const termWrap = el("div", { class: "fpp-terminal-wrap" }, [
        el("div", { class: "fpp-terminal-head" }, "Terminal — Redfish / update task"),
      ]);
      const termBody = el("pre", { class: "fpp-terminal-body", id: `fpp-term-${job.id}` });
      for (const entry of job.terminal_log) {
        termBody.appendChild(el("span", {
          class: `fpp-t-${entry.style || "out"}`,
        }, entry.line + "\n"));
      }
      termWrap.appendChild(termBody);
      card.appendChild(termWrap);
      requestAnimationFrame(() => {
        if (flashJobIsActive(job)) {
          termBody.scrollTop = termBody.scrollHeight;
        }
      });
    }

    if (job.ssh_terminal_log && job.ssh_terminal_log.length) {
      const sshWrap = el("div", { class: "fpp-terminal-wrap fpp-ssh-terminal" }, [
        el("div", { class: "fpp-terminal-head" },
          `SSH terminal — root@${job.bmc_ip || state.bmcIp || "bmc"} (aux_cycle)`),
      ]);
      const sshBody = el("pre", { class: "fpp-terminal-body", id: `fpp-ssh-${job.id}` });
      for (const entry of job.ssh_terminal_log) {
        sshBody.appendChild(el("span", {
          class: `fpp-t-${entry.style || "out"}`,
        }, entry.line + "\n"));
      }
      sshWrap.appendChild(sshBody);
      card.appendChild(sshWrap);
      requestAnimationFrame(() => {
        if (flashJobIsActive(job)) {
          sshBody.scrollTop = sshBody.scrollHeight;
        }
      });
    }

    if (done) {
      const jobTitle = `MGX ARC Flash — ${job.label || "Flash"} (${statusLabel})`;
      card.appendChild(buildReportActionBar({
        getContent: () => buildFlashJobReportText(job),
        filenamePrefix: `flash-${(job.label || "job").toLowerCase().replace(/\s+/g, "-")}`,
        reportTitle: jobTitle,
      }));
    }

    list.appendChild(card);
  }
}

async function submitFlashJob({ label, url, init }) {
  const res = await fetch(url, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok && res.status !== 202) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  if (data.job_id) {
    delete state.flashDismissed[data.job_id];
    state.flashJobs[data.job_id] = {
      id: data.job_id,
      bmc_ip: state.bmcIp,
      label,
      status: "queued",
      percent: 0,
      phase: "Queued",
      message: data.message || "Starting…",
      curl_command: "",
      terminal_log: [],
      ssh_terminal_log: [],
      created_at: Date.now() / 1000,
    };
    ensureFlashPolling();
    renderFlashProgressPanel();
    return data;
  }
  throw new Error(data.error || "Flash did not return a job id.");
}

// ---------------------------------------------------------------------------
// Tab: Overview
// ---------------------------------------------------------------------------
async function loadOverview(panel) {
  panel.innerHTML = loadingHTML("Loading summary…");
  try {
    const d = await api(`/api/bmc/overview`, { headers: bmcHeaders() });
    const grid = el("div", { class: "kv-grid" });
    for (const item of d.summary || []) {
      grid.appendChild(el("div", { class: "kv" }, [
        el("div", { class: "k" }, item.label),
        el("div", { class: "v" }, item.value ? String(item.value) : "—"),
      ]));
    }
    panel.innerHTML = "";
    panel.appendChild(el("div", { class: "section-title" }, "System Summary"));
    panel.appendChild(grid);
  } catch (err) {
    panel.innerHTML = `<div class="msg msg-err">${err.message}</div>`;
  }
}

// ---------------------------------------------------------------------------
// Tab: Power (all actions use Redfish on the connected BMC)
// ---------------------------------------------------------------------------
const POWER_ACTIONS = [
  { action: "power_on", label: "Power On", cls: "btn-primary" },
  { action: "graceful_shutdown", label: "Graceful Shutdown", cls: "btn-warn" },
  { action: "force_off", label: "Force Off", cls: "btn-danger", confirm: true },
  { action: "power_cycle", label: "Power Cycle", cls: "btn-warn", confirm: true },
  { action: "aux_cycle", label: "AUX Cycle", cls: "btn-danger", confirm: true, reconnect: true },
  { action: "reboot_bmc", label: "Reboot BMC", cls: "btn-warn", confirm: true, reconnect: true },
];

function powerStateClass(value) {
  if (/on/i.test(value) && !/off/i.test(value)) return "power-on";
  if (/off/i.test(value)) return "power-off";
  return "power-unknown";
}

function setPowerState(value) {
  const stateEl = document.getElementById("power-state-value");
  if (!stateEl) return;
  stateEl.textContent = value || "Unknown";
  stateEl.className = `v ${powerStateClass(value || "Unknown")}`;
}

function setPowerButtonsDisabled(panel, disabled) {
  panel?.querySelectorAll(".power-action-btn").forEach((button) => {
    button.disabled = disabled;
  });
}

function appendPowerTerminal(pre, requestLine, output, bmcHost) {
  if (!pre) return;
  const ts = new Date().toLocaleTimeString();
  const block = [
    `[${ts}] Redfish — https://${bmcHost}`,
    requestLine,
    output || "(no response body)",
    "",
  ].join("\n");
  const first = !pre.textContent;
  pre.textContent = pre.textContent ? `${pre.textContent}\n${block}` : block;
  pre.scrollTop = pre.scrollHeight;
  // The log starts collapsed; open it once an action has actually run.
  if (!first) pre.closest("details")?.setAttribute("open", "");
}

async function loadPower(panel) {
  panel.innerHTML = loadingHTML("Loading power state…");
  const bmcHost = state.bmcIp || "bmc";
  let powerState = "Unknown";
  let statusOutput = "Waiting for Redfish response.";
  let statusEndpoint = "/redfish/v1/Systems/System_0";
  let hostUsb = null;
  try {
    const d = await api(`/api/bmc/power`, { headers: bmcHeaders() });
    powerState = d.power_state || "Unknown";
    statusOutput = `PowerState: ${powerState}`;
    statusEndpoint = d.endpoint || statusEndpoint;
    hostUsb = d.host_usb || null;
  } catch (err) {
    toast(`Could not read power state: ${err.message}`, "err");
    statusOutput = err.message;
  }

  panel.innerHTML = "";
  const msg = el("div", { class: "msg hidden" });
  const termBody = el("pre", { class: "fpp-terminal-body", id: "power-terminal" });
  appendPowerTerminal(termBody, `GET ${statusEndpoint}`, statusOutput, bmcHost);
  const hostUsbBox = el("div", {
    class: `power-host-usb ${hostUsb ? "" : "hidden"}`.trim(),
    id: "power-host-usb",
  });
  renderHostUsbInfo(hostUsbBox, hostUsb);

  const powerAction = (action) => POWER_ACTIONS.find((item) => item.action === action);
  const actionButton = (item) => el("button", {
    class: `btn ${item.cls} power-action-btn`,
    onclick: () => sendPower(item, panel, msg, termBody),
  }, item.label);

  // Card 1 — state and the everyday host actions.
  const hostCard = el("div", { class: "card" }, [
    el("div", { class: "power-head" }, [
      el("div", { class: "power-head-text" }, [
        el("h3", {}, "Host Power"),
        el("p", { class: "muted" },
          `Redfish on ${bmcHost} — the host OS IP is not needed for these actions.`),
      ]),
      el("div", { class: "kv power-state-kv" }, [
        el("div", { class: "k" }, "Current power state"),
        el("div", {
          class: `v ${powerStateClass(powerState)}`,
          id: "power-state-value",
        }, powerState),
      ]),
    ]),
    el("div", { class: "power-grid" }, [
      el("button", {
        class: "btn power-action-btn",
        onclick: () => refreshPowerStatus(panel, msg, termBody),
      }, "Refresh"),
      ...["power_on", "graceful_shutdown", "force_off", "power_cycle"]
        .map((action) => actionButton(powerAction(action))),
    ]),
    msg,
  ]);

  // Card 2 — disruptive actions, collapsed so they are hard to hit by accident.
  const advancedCard = el("details", { class: "card power-advanced" }, [
    el("summary", { class: "power-advanced-summary" }, [
      el("span", {}, "Advanced — AUX cycle & BMC reboot"),
      el("span", { class: "muted" }, "Both drop the BMC connection briefly"),
    ]),
    el("div", { class: "power-adv-row" }, [
      el("div", {}, [
        el("strong", {}, "AUX Cycle"),
        el("div", { class: "muted" },
          "Removes auxiliary power and restarts the BMC. The GUI reconnects automatically."),
      ]),
      actionButton(powerAction("aux_cycle")),
    ]),
    el("div", { class: "power-adv-row" }, [
      el("div", {}, [
        el("strong", {}, "Reboot BMC"),
        el("div", { class: "muted" },
          "Restarts only the management controller — the host and tray keep running."),
      ]),
      actionButton(powerAction("reboot_bmc")),
    ]),
  ]);

  // Card 3 — reference detail, collapsed by default to keep the tab clean.
  const detailsCard = el("div", { class: "card power-diagnostics" }, [
    el("details", { class: "power-details", id: "power-terminal-details" }, [
      el("summary", {}, `Redfish activity — ${bmcHost}`),
      el("div", { class: "fpp-terminal-wrap" }, [termBody]),
    ]),
    el("details", {
      class: `power-details ${hostUsb ? "" : "hidden"}`.trim(),
      id: "power-host-usb-details",
    }, [
      el("summary", {}, "BMC-host USB interface"),
      hostUsbBox,
    ]),
  ]);

  panel.append(hostCard, advancedCard, detailsCard);
}

function renderHostUsbInfo(container, hostUsb) {
  if (!container || !hostUsb) return;
  const ethernet = hostUsb.ethernet_interface || {};
  const hostInterface = hostUsb.host_interface || {};
  const addresses = ethernet.IPv4Addresses || ethernet.IPv6Addresses || [];
  const addressText = addresses
    .map((item) => item.Address || item.address)
    .filter(Boolean)
    .join(", ");
  const enabled = ethernet.InterfaceEnabled;
  container.innerHTML = "";
  container.append(
    el("div", { class: "power-host-usb-grid" }, [
      el("span", { class: "muted" }, "Interface"),
      el("strong", {}, ethernet.Name || hostInterface.Name || "hostusb0"),
      el("span", { class: "muted" }, "Address"),
      el("strong", {}, addressText || "Reported without an IP address"),
      el("span", { class: "muted" }, "Enabled"),
      el("strong", {}, enabled == null ? "Unknown" : (enabled ? "Yes" : "No")),
    ]),
    el("p", { class: "muted" },
      "This is the BMC-host management link, not the host's lab OS IP. "
      + "Use OS IP Detect in the header for that."),
  );
  container.classList.remove("hidden");
  document.getElementById("power-host-usb-details")?.classList.remove("hidden");
}

async function refreshPowerStatus(panel, msg, termBody, options = {}) {
  const { quiet = false, includeHostUsb = true } = options;
  if (msg) {
    msg.className = "msg";
    msg.textContent = "Reading power status…";
  }
  if (!quiet) setPowerButtonsDisabled(panel, true);
  try {
    const suffix = includeHostUsb ? "" : "?include_host_usb=0";
    const d = await api(`/api/bmc/power${suffix}`, { headers: bmcHeaders() });
    appendPowerTerminal(
      termBody,
      `GET ${d.endpoint || "/redfish/v1/Systems/System_0"}`,
      `PowerState: ${d.power_state || "Unknown"}`,
      state.bmcIp || "bmc",
    );
    setPowerState(d.power_state);
    if (d.host_usb) {
      renderHostUsbInfo(document.getElementById("power-host-usb"), d.host_usb);
    }
    if (msg) {
      msg.className = "msg msg-ok";
      msg.textContent = d.power_state ? `Power state: ${d.power_state}` : "Status updated.";
    }
    if (!quiet) toast("Power status updated.");
    return d;
  } catch (err) {
    if (msg) {
      msg.className = "msg msg-err";
      msg.textContent = err.message;
    }
    if (!quiet) toast(err.message, "err");
    if (quiet) throw err;
    return null;
  } finally {
    if (!quiet) setPowerButtonsDisabled(panel, false);
  }
}

function powerDelay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForPowerStable(panel, msg, termBody, action) {
  const expected = {
    power_on: "on",
    graceful_shutdown: "off",
    force_off: "off",
    power_cycle: "on",
  }[action];
  await powerDelay(action === "power_cycle" ? 5000 : 2500);
  let previous = "";
  let stableReads = 0;
  for (let attempt = 1; attempt <= 30; attempt += 1) {
    if (msg) msg.textContent = `Waiting for power state (${attempt}/30)…`;
    try {
      const result = await refreshPowerStatus(panel, null, termBody, {
        quiet: true,
        includeHostUsb: false,
      });
      const current = String(result.power_state || "").toLowerCase();
      stableReads = current === previous ? stableReads + 1 : 1;
      previous = current;
      if ((expected && current === expected && stableReads >= 2)
          || (!expected && stableReads >= 2)) {
        if (msg) {
          msg.className = "msg msg-ok";
          msg.textContent = `Power state stabilized at ${result.power_state}.`;
        }
        return;
      }
    } catch (_) {
      // A host cycle may briefly interrupt Redfish; continue until timeout.
    }
    await powerDelay(3000);
  }
  if (msg) {
    msg.className = "msg msg-err";
    msg.textContent = "Power command was accepted, but the state did not stabilize before timeout.";
  }
}

async function waitForBmcReconnect(panel, msg, termBody) {
  let sawOffline = false;
  await powerDelay(4000);
  for (let attempt = 1; attempt <= 60; attempt += 1) {
    if (msg) {
      msg.className = "msg";
      msg.textContent = `BMC is restarting; reconnecting… (${attempt}/60)`;
    }
    try {
      const result = await refreshPowerStatus(panel, null, termBody, {
        quiet: true,
        includeHostUsb: false,
      });
      if (sawOffline || attempt >= 4) {
        if (msg) {
          msg.className = "msg msg-ok";
          msg.textContent = `BMC reconnected. Power state: ${result.power_state || "Unknown"}.`;
        }
        toast("BMC reconnected and power state refreshed.");
        return;
      }
    } catch (_) {
      sawOffline = true;
    }
    await powerDelay(5000);
  }
  if (msg) {
    msg.className = "msg msg-err";
    msg.textContent = "BMC has not reconnected yet. It may still be restarting; try Refresh Power State.";
  }
  toast("BMC reconnect timed out.", "err");
}

async function sendPower(item, panel, msg, termBody) {
  const { action, label, reconnect } = item;
  if (item.confirm) {
    const warning = action === "aux_cycle"
      ? "\n\nThe BMC will temporarily disconnect. This is expected."
      : action === "reboot_bmc"
        ? "\n\nThis restarts only the BMC, not the host OS."
        : "";
    if (!confirm(`Confirm ${label}?${warning}`)) return;
  }
  if (msg) {
    msg.className = "msg";
    msg.textContent = "Sending power command…";
  }
  setPowerButtonsDisabled(panel, true);
  try {
    const res = await api(`/api/bmc/power`, {
      method: "POST",
      headers: bmcHeaders(),
      body: JSON.stringify({ action }),
    });
    appendPowerTerminal(
      termBody,
      `POST ${res.endpoint || "/redfish/v1"} ${JSON.stringify(res.payload || {})}`,
      res.message || `HTTP ${res.http_status || "success"}`,
      state.bmcIp || "bmc",
    );
    if (msg) {
      msg.className = "msg msg-ok";
      msg.textContent = res.message || `${label} accepted.`;
    }
    toast(`${label} sent.`);
    if (reconnect) await waitForBmcReconnect(panel, msg, termBody);
    else await waitForPowerStable(panel, msg, termBody, action);
  } catch (err) {
    appendPowerTerminal(termBody, `POST ${action}`, err.message, state.bmcIp || "bmc");
    if (msg) {
      msg.className = "msg msg-err";
      msg.textContent = err.message;
    }
    toast(err.message, "err");
  } finally {
    setPowerButtonsDisabled(panel, false);
  }
}

// ---------------------------------------------------------------------------
// Tab: BMC Shell (interactive SSH to connected BMC)
// ---------------------------------------------------------------------------
const BMC_SHELL_PRESETS = [
  ["cat /etc/os-release", "BMC version"],
  ["powerctrl.sh power_status", "Power status"],
  ["powerctrl.sh power_on", "Power on"],
  ["powerctrl.sh power_off", "Power off"],
  ["stbypowerctrl.sh aux_cycle", "Aux power cycle"],
  [
    "curl -sku root:0penBmc https://localhost/redfish/v1/UpdateService/FirmwareInventory/FW_CPU_0",
    "SBIOS version",
  ],
  ["journalctl -u pldmd -n 40 --no-pager", "pldmd log"],
  ["hostname", "Hostname"],
];

function bmcShellPrompt() {
  const host = state.bmcName || state.bmcIp || "bmc";
  return `root@${host}# `;
}

function shellLineClass(type) {
  if (type === "cmd") return "bmc-shell-cmd";
  if (type === "err") return "bmc-shell-err";
  if (type === "info") return "bmc-shell-info";
  return "bmc-shell-out";
}

function appendShellLine(type, text) {
  state.bmcShell.lines.push({ type, text });
}

function renderBmcShellOutput(container) {
  if (!container) return;
  container.innerHTML = "";
  for (const line of state.bmcShell.lines) {
    const div = el("div", { class: shellLineClass(line.type) }, line.text);
    container.appendChild(div);
  }
  container.scrollTop = container.scrollHeight;
}

function initBmcShellSession() {
  if (state.bmcShell.initialized) return;
  const bmcIp = state.bmcIp || "bmc";
  appendShellLine("info", `Connected to ${bmcIp} via SSH as root.`);
  appendShellLine("info", "Type a command and press Enter. Use ↑ / ↓ for history.");
  appendShellLine("out", "");
  state.bmcShell.initialized = true;
}

function loadBmcShell(panel) {
  const existing = panel.querySelector("#bmc-shell-root");
  if (existing) {
    const input = panel.querySelector("#bmc-shell-input");
    renderBmcShellOutput(panel.querySelector("#bmc-shell-output"));
    input?.focus();
    return;
  }

  panel.innerHTML = "";
  initBmcShellSession();

  const bmcIp = state.bmcIp || "bmc";
  const output = el("div", { class: "bmc-shell-output", id: "bmc-shell-output" });
  const input = el("input", {
    type: "text",
    id: "bmc-shell-input",
    class: "bmc-shell-input",
    placeholder: "",
    autocomplete: "off",
    autocorrect: "off",
    autocapitalize: "off",
    spellcheck: "false",
  });
  const prompt = el("span", { class: "bmc-shell-prompt", id: "bmc-shell-prompt" }, bmcShellPrompt());
  const promptLine = el("div", { class: "bmc-shell-prompt-line" }, [prompt, input]);
  const status = el("div", { class: "bmc-shell-status hidden", id: "bmc-shell-status" });

  const presetWrap = el("div", { class: "bmc-shell-presets" },
    BMC_SHELL_PRESETS.map(([cmd, label]) =>
      el("button", {
        type: "button",
        class: "btn btn-ghost bmc-shell-preset",
        title: cmd,
        onclick: () => {
          input.value = cmd;
          runBmcShellCommand(input, output, status);
        },
      }, label)));

  const clearBtn = el("button", {
    type: "button",
    class: "btn btn-ghost",
    onclick: () => {
      state.bmcShell.lines = [];
      state.bmcShell.history = [];
      state.bmcShell.historyPos = -1;
      state.bmcShell.initialized = false;
      initBmcShellSession();
      renderBmcShellOutput(output);
      status.classList.add("hidden");
    },
  }, "Clear session");

  input.addEventListener("keydown", (e) => {
    const hist = state.bmcShell.history;
    if (e.key === "ArrowUp") {
      e.preventDefault();
      if (!hist.length) return;
      if (state.bmcShell.historyPos < 0) {
        state.bmcShell.historyPos = hist.length - 1;
      } else if (state.bmcShell.historyPos > 0) {
        state.bmcShell.historyPos -= 1;
      }
      input.value = hist[state.bmcShell.historyPos] || "";
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (state.bmcShell.historyPos < 0) return;
      if (state.bmcShell.historyPos < hist.length - 1) {
        state.bmcShell.historyPos += 1;
        input.value = hist[state.bmcShell.historyPos];
      } else {
        state.bmcShell.historyPos = -1;
        input.value = "";
      }
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      runBmcShellCommand(input, output, status);
    }
  });

  const shellWrap = el("div", { class: "bmc-shell-wrap", id: "bmc-shell-root" }, [
    el("div", { class: "bmc-shell-head" }, `SSH — root@${bmcIp}`),
    output,
    promptLine,
  ]);

  const card = el("div", { class: "card" }, [
    el("h3", {}, "BMC Shell"),
    el("p", { class: "muted" },
      `Interactive shell on the connected BMC (${bmcIp}). Commands run as root over SSH.`),
    buildReportActionBar({
      getContent: buildBmcShellReportText,
      filenamePrefix: "bmc-shell",
      reportTitle: `MGX ARC BMC Shell — ${bmcIp}`,
    }),
    presetWrap,
    el("div", { class: "bmc-shell-actions" }, [clearBtn]),
    status,
    shellWrap,
  ]);
  panel.appendChild(card);
  renderBmcShellOutput(output);
  input.focus();
}

async function runBmcShellCommand(input, output, status) {
  const command = (input.value || "").trim();
  if (!command) return;

  const prompt = bmcShellPrompt();
  appendShellLine("cmd", `${prompt}${command}`);
  renderBmcShellOutput(output);

  const hist = state.bmcShell.history;
  if (!hist.length || hist[hist.length - 1] !== command) {
    hist.push(command);
  }
  state.bmcShell.historyPos = -1;
  input.value = "";
  input.disabled = true;

  if (status) {
    status.className = "bmc-shell-status";
    status.textContent = "Running…";
  }

  try {
    const res = await api(`/api/bmc/command`, {
      method: "POST",
      headers: bmcHeaders(),
      body: JSON.stringify({ command }),
    });
    const text = res.output || res.message || "(no output)";
    for (const line of text.split("\n")) {
      appendShellLine(res.exit_code === 0 ? "out" : "err", line);
    }
    if (res.exit_code !== 0 && !text) {
      appendShellLine("err", `Command exited with code ${res.exit_code}.`);
    }
    if (status) {
      status.className = res.exit_code === 0
        ? "bmc-shell-status bmc-shell-status-ok"
        : "bmc-shell-status bmc-shell-status-err";
      status.textContent = res.exit_code === 0
        ? "Done."
        : `Exit code ${res.exit_code}.`;
    }
  } catch (err) {
    appendShellLine("err", err.message);
    if (status) {
      status.className = "bmc-shell-status bmc-shell-status-err";
      status.textContent = err.message;
    }
    toast(err.message, "err");
  } finally {
    appendShellLine("out", "");
    renderBmcShellOutput(output);
    input.disabled = false;
    input.focus();
  }
}

// ---------------------------------------------------------------------------
// Tab: SPI Read (MGX ARC fpga1 MTD device)
// ---------------------------------------------------------------------------
function renderSpiTerminal(container, steps) {
  if (!container) return;
  container.innerHTML = "";
  for (const step of steps || []) {
    container.appendChild(el("span", { class: "spi-t-cmd" }, `$ ${step.command}\n`));
    const out = step.output || "(no output)";
    for (const line of out.split("\n")) {
      const cls = step.exit_code === 0 ? "spi-t-out" : "spi-t-err";
      container.appendChild(el("span", { class: cls }, `${line}\n`));
    }
    const exitCls = step.exit_code === 0 ? "spi-t-exit-ok" : "spi-t-exit-err";
    container.appendChild(el("span", { class: exitCls }, `[exit ${step.exit_code}]\n\n`));
  }
  container.scrollTop = container.scrollHeight;
}

function setSpiStatus(el, text, kind) {
  if (!el) return;
  el.textContent = text || "";
  el.className = `spi-read-status spi-read-status-${kind || "idle"}`;
}

function loadSpiRead(panel) {
  panel.innerHTML = "";
  const bmcIp = state.bmcIp || "bmc";
  const statusEl = el("div", {
    class: "spi-read-status spi-read-status-idle",
    id: "spi-read-status",
  }, "Ready — press SPI Read to check the chip and read the full flash.");
  const termBody = el("pre", { class: "spi-terminal-body fpp-terminal-body", id: "spi-read-terminal" });
  const msg = el("div", { class: "msg hidden", id: "spi-read-msg" });

  const runBtn = el("button", {
    type: "button",
    class: "btn btn-primary",
    id: "spi-read-btn",
    onclick: () => runSpiRead(statusEl, termBody, msg, runBtn),
  }, "SPI Read");

  const clearBtn = el("button", {
    type: "button",
    class: "btn btn-ghost",
    onclick: () => {
      termBody.innerHTML = "";
      msg.classList.add("hidden");
      setSpiStatus(
        statusEl,
        "Ready — press SPI Read to check the chip and read the full flash.",
        "idle",
      );
    },
  }, "Clear terminal");

  const card = el("div", { class: "card" }, [
    el("h3", {}, "SPI Read"),
    el("p", { class: "muted" },
      `Connect to root@${bmcIp} over SSH, identify the fpga1 NOR using `
      + "mtdinfo /dev/mtd/by-name/fpga1, then read the full MTD device with "
      + "dd into /tmp/u1958_read.bin and show a hexdump sample."),
    statusEl,
    el("div", { class: "spi-read-actions" }, [runBtn, clearBtn]),
    msg,
    el("div", { class: "fpp-terminal-wrap" }, [
      el("div", { class: "fpp-terminal-head" }, `SPI terminal — root@${bmcIp}`),
      termBody,
    ]),
  ]);
  panel.appendChild(card);
}

async function runSpiRead(statusEl, termBody, msg, runBtn) {
  if (!confirm(
    "Run SPI read on the BMC?\n\n"
    + "This reads the full /dev/mtd/by-name/fpga1 device into "
    + "/tmp/u1958_read.bin and may take several minutes."
  )) return;

  termBody.innerHTML = "";
  setSpiStatus(statusEl, "Connecting to BMC and checking the SPI chip…", "run");
  if (msg) {
    msg.className = "msg msg-info";
    msg.textContent = "SPI read running on BMC — please wait…";
    msg.classList.remove("hidden");
  }
  if (runBtn) runBtn.disabled = true;

  try {
    const res = await fetch("/api/bmc/spi-read", {
      method: "POST",
      headers: { ...bmcHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const data = await res.json().catch(() => ({}));
    if (data.steps && data.steps.length) {
      renderSpiTerminal(termBody, data.steps);
    }
    if (!res.ok) {
      const line = data.status_line || data.error || `HTTP ${res.status}`;
      setSpiStatus(statusEl, line, "err");
      if (msg) {
        msg.className = "msg msg-err";
        msg.textContent = line;
      }
      showNotifyBanner({ kind: "err", title: "SPI Read — FAILED", message: line });
      toast(line, "err");
      return;
    }
    const line = data.status_line || data.message || "SPI read completed.";
    setSpiStatus(statusEl, line, "ok");
    if (msg) {
      msg.className = "msg msg-ok";
      msg.textContent = line;
    }
    showNotifyBanner({ kind: "ok", title: "SPI Read — PASSED", message: line });
    toast(line, "ok");
  } catch (err) {
    const failLine = err.message || "SPI read failed.";
    setSpiStatus(statusEl, `SPI read failed — ${failLine}`, "err");
    if (msg) {
      msg.className = "msg msg-err";
      msg.textContent = failLine;
    }
    if (!termBody.textContent) {
      termBody.appendChild(el("span", { class: "spi-t-err" }, `${failLine}\n`));
    }
    showNotifyBanner({ kind: "err", title: "SPI Read — FAILED", message: failLine });
    toast(failLine, "err");
  } finally {
    if (runBtn) runBtn.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Tab: I2C (bus, mux, detect, dump, register read/write)
// ---------------------------------------------------------------------------
function i2cField(label, input) {
  return el("label", { class: "i2c-field" }, [el("span", {}, label), input]);
}

function i2cTextInput(value, placeholder) {
  return el("input", {
    type: "text", value, placeholder, spellcheck: "false", autocomplete: "off",
  });
}

function i2cModeSelect(modes) {
  return el("select", {}, modes.map(([value, label], index) =>
    el("option", index === 0 ? { value, selected: "selected" } : { value }, label)));
}

function i2cResultArea(label) {
  return {
    status: el("div", { class: "i2c-status i2c-status-idle" }, "Ready."),
    details: el("div", { class: "i2c-result-details hidden" }),
    output: el("pre", { class: "i2c-output" }),
    root: el("div", { class: "i2c-result" }, [
      el("div", { class: "i2c-result-label" }, `${label} result`),
    ]),
  };
}

function finishI2cResultArea(area) {
  area.root.appendChild(area.status);
  area.root.appendChild(area.details);
  area.root.appendChild(el("div", { class: "fpp-terminal-wrap" }, [
    el("div", { class: "fpp-terminal-head" }, "Raw command output"),
    area.output,
  ]));
  return area.root;
}

function setI2cStatus(area, text, kind = "idle") {
  area.status.textContent = text;
  area.status.className = `i2c-status i2c-status-${kind}`;
}

function selectedI2cBus(context) {
  const number = context.busSelect.value;
  return context.buses.find((bus) => String(bus.number) === number) || {
    number, name: number ? `I2C Bus ${number}` : "No bus selected",
  };
}

function renderI2cResult(area, data, context) {
  const bus = data.bus || selectedI2cBus(context);
  const mux = data.mux_path || context.selectedMux;
  const validation = data.validation || "";
  area.details.innerHTML = "";
  area.details.classList.remove("hidden");
  const entries = [
    ["BMC IP", data.bmc_ip || state.bmcIp || "—"],
    ["Bus", bus?.number !== undefined && String(bus.number) !== ""
      ? `${bus.name || `I2C Bus ${bus.number}`} (i2c-${bus.number})`
      : "—"],
    ["Mux path", mux?.name || "Not selected"],
    ["Parsed result", data.parsed_result || data.error || "—"],
  ];
  if (validation) entries.push([
    "Validation",
    validation === "pass" ? "PASS — expected devices responded"
      : validation === "fail" ? "FAIL — expected device missing"
        : "NOT CONFIGURED",
  ]);
  area.details.appendChild(el("div", { class: "i2c-result-grid" },
    entries.map(([key, value]) => el("div", { class: "kv" }, [
      el("div", { class: "k" }, key),
      el("div", { class: `v${key === "Validation" ? ` i2c-validation-${validation}` : ""}` }, value),
    ]))));

  if (Array.isArray(data.found_addresses)) {
    area.details.appendChild(el("div", { class: "i2c-addresses" }, [
      el("strong", {}, "Responding addresses: "),
      data.found_addresses.length ? data.found_addresses.join(", ") : "none",
    ]));
  }
  if (Array.isArray(data.expected_devices) && data.expected_devices.length) {
    area.details.appendChild(el("table", { class: "i2c-device-table" }, [
      el("thead", {}, el("tr", {}, [
        el("th", {}, "Expected device"), el("th", {}, "Address"), el("th", {}, "Status"),
      ])),
      el("tbody", {}, data.expected_devices.map((device) => el("tr", {}, [
        el("td", {}, device.name),
        el("td", {}, device.address_hex),
        el("td", { class: device.present ? "health-ok" : "health-crit" },
          device.present ? "PASS — responding" : "FAIL — missing"),
      ]))),
    ]));
  }

  area.output.textContent = "";
  if (data.command) area.output.appendChild(
    el("span", { class: "spi-t-cmd" }, `$ ${data.command}\n`));
  area.output.appendChild(el("span", {
    class: data.ok ? "spi-t-out" : "spi-t-err",
  }, `${data.output || data.error || "(no output)"}\n`));
  if (data.readback) {
    area.output.appendChild(el("span", { class: "spi-t-cmd" },
      `\nReadback: $ ${data.readback.command}\n`));
    area.output.appendChild(el("span", {
      class: data.readback.ok ? "spi-t-out" : "spi-t-err",
    }, `${data.readback.output}\n`));
  }
  if (Number.isInteger(data.exit_code)) area.output.appendChild(el("span", {
    class: data.exit_code === 0 ? "spi-t-exit-ok" : "spi-t-exit-err",
  }, `[exit ${data.exit_code}]\n`));
}

async function i2cRequest(path, options, area, context, button, runningText) {
  button.disabled = true;
  setI2cStatus(area, runningText, "run");
  try {
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({}));
    renderI2cResult(area, data, context);
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    setI2cStatus(area, data.parsed_result || "Command completed.", "ok");
    if (data.config_errors?.length) {
      toast(`I2C config warning: ${data.config_errors.join("; ")}`, "err");
    }
    return data;
  } catch (err) {
    setI2cStatus(area, err.message, "err");
    if (!area.output.textContent) area.output.textContent = err.message;
    toast(err.message, "err");
    return null;
  } finally {
    button.disabled = false;
  }
}

function i2cPostOptions(body) {
  return {
    method: "POST",
    headers: { ...bmcHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

function requireI2cBus(context) {
  const bus = context.busSelect.value;
  if (!bus) toast("Load and select an I2C bus first.", "err");
  return bus;
}

function loadI2c(panel) {
  panel.innerHTML = "";
  const context = {
    buses: [],
    selectedMux: null,
    busSelect: el("select", { disabled: "disabled" },
      el("option", { value: "" }, "Load buses first")),
  };

  const intro = el("div", { class: "card i2c-intro-card" }, [
    el("h3", {}, "I2C Device Path Validation"),
    el("p", { class: "muted" },
      "Load buses from the connected BMC, optionally select a configured mux path, "
      + "then verify that expected devices respond before trusting GPIO or device data."),
    el("div", { class: "msg msg-info i2c-warning" },
      "I2C probing and register access can affect sensitive hardware. Use only known "
      + "MGX ARC buses and devices. All writes require confirmation."),
  ]);

  const busArea = i2cResultArea("Load buses");
  const busTableBody = el("tbody", {}, el("tr", {},
    el("td", { colspan: "5", class: "muted" }, "Click Load I2C Buses.")));
  const busButton = el("button", { type: "button", class: "btn btn-primary" },
    "Load I2C Buses");
  busButton.addEventListener("click", async () => {
    const data = await i2cRequest(
      `${API}/i2c/buses`, { headers: bmcHeaders() }, busArea, context, busButton,
      `Running i2cdetect -l on ${state.bmcIp}…`);
    if (!data) return;
    context.buses = data.buses || [];
    context.busSelect.innerHTML = "";
    context.busSelect.disabled = !context.buses.length;
    if (!context.buses.length) {
      context.busSelect.appendChild(el("option", { value: "" }, "No buses found"));
    } else {
      context.buses.forEach((bus) => context.busSelect.appendChild(el("option", {
        value: String(bus.number),
      }, `${bus.name} — i2c-${bus.number}`)));
    }
    busTableBody.innerHTML = "";
    (context.buses || []).forEach((bus) => busTableBody.appendChild(el("tr", {}, [
      el("td", {}, String(bus.number)),
      el("td", {}, bus.name),
      el("td", {}, bus.type || "—"),
      el("td", {}, bus.adapter || "—"),
      el("td", {}, bus.subsystem || bus.description || "—"),
    ])));
    if (!context.buses.length) busTableBody.appendChild(el("tr", {},
      el("td", { colspan: "5", class: "muted" }, "No I2C buses reported.")));

    context.muxSelect.innerHTML = "";
    const paths = data.mux_paths || [];
    context.muxButton.disabled = !paths.length;
    if (!paths.length) {
      context.muxSelect.disabled = true;
      context.muxSelect.appendChild(el("option", { value: "" },
        "No mux paths configured"));
    } else {
      context.muxSelect.disabled = false;
      paths.forEach((path) => context.muxSelect.appendChild(el("option", {
        value: path.id,
      }, path.name)));
      context.muxPaths = paths;
    }
  });
  const busCard = el("div", { class: "card i2c-section-card" }, [
    el("h3", {}, "1. I2C Bus"),
    el("p", { class: "muted" }, "Discover adapters by running i2cdetect -l on the target BMC."),
    el("div", { class: "i2c-controls" }, [
      i2cField("BMC IP address", el("input", {
        type: "text", value: state.bmcIp, readonly: "readonly",
      })),
      i2cField("Selected bus", context.busSelect),
    ]),
    el("div", { class: "i2c-actions" }, busButton),
    el("div", { class: "i2c-table-scroll" }, el("table", { class: "i2c-bus-table" }, [
      el("thead", {}, el("tr", {}, [
        el("th", {}, "Bus"), el("th", {}, "Friendly name"), el("th", {}, "Type"),
        el("th", {}, "Adapter"), el("th", {}, "Subsystem / description"),
      ])),
      busTableBody,
    ])),
    finishI2cResultArea(busArea),
  ]);

  const muxArea = i2cResultArea("Set mux");
  context.muxSelect = el("select", { disabled: "disabled" },
    el("option", { value: "" }, "Load buses first"));
  context.muxButton = el("button", {
    type: "button", class: "btn btn-primary", disabled: "disabled",
  }, "Set Mux");
  context.muxButton.addEventListener("click", async () => {
    const pathId = context.muxSelect.value;
    if (!pathId) {
      toast("Select a configured mux path first.", "err");
      return;
    }
    const data = await i2cRequest(
      `${API}/i2c/mux`, i2cPostOptions({ path_id: pathId }), muxArea, context,
      context.muxButton, "Setting mux path…");
    if (data) context.selectedMux = data.mux_path;
  });
  const muxCard = el("div", { class: "card i2c-section-card" }, [
    el("h3", {}, "2. Mux Configuration"),
    el("p", { class: "muted" },
      "Select a downstream path using the backend command configured in i2c_bus_map.json."),
    el("div", { class: "i2c-controls i2c-controls-compact" }, [
      i2cField("Mux path", context.muxSelect),
    ]),
    el("div", { class: "i2c-actions" }, context.muxButton),
    finishI2cResultArea(muxArea),
  ]);

  const detectArea = i2cResultArea("Detect");
  const detectButton = el("button", { type: "button", class: "btn btn-primary" }, "Detect");
  detectButton.addEventListener("click", async () => {
    const bus = requireI2cBus(context);
    if (!bus || !confirm(
      `Run i2cdetect on bus ${bus}?\n\nOnly scan a known-safe I2C bus.`
    )) return;
    await i2cRequest(
      `${API}/i2c/detect`, i2cPostOptions({ bus }), detectArea, context,
      detectButton, `Scanning I2C bus ${bus}…`);
  });
  const detectCard = el("div", { class: "card i2c-section-card" }, [
    el("h3", {}, "3. I2C Detect"),
    el("p", { class: "muted" },
      "Scan the selected bus and compare responding addresses with i2c_expected_devices.json."),
    el("div", { class: "i2c-actions" }, detectButton),
    finishI2cResultArea(detectArea),
  ]);

  const dumpArea = i2cResultArea("Dump");
  const dumpAddress = i2cTextInput("0x20", "0x03–0x77");
  const dumpMode = i2cModeSelect([["b", "Byte (b)"], ["w", "Word (w)"]]);
  const dumpButton = el("button", { type: "button", class: "btn btn-primary" }, "Dump");
  dumpButton.addEventListener("click", async () => {
    const bus = requireI2cBus(context);
    if (!bus || !dumpAddress.value.trim()) {
      if (bus) toast("Enter a device address.", "err");
      return;
    }
    if (!confirm(
      `Dump device ${dumpAddress.value} on bus ${bus}?\n\n`
      + "Some devices have read-sensitive registers."
    )) return;
    await i2cRequest(
      `${API}/i2c/dump`, i2cPostOptions({
        bus, address: dumpAddress.value.trim(), mode: dumpMode.value,
      }), dumpArea, context, dumpButton, "Reading device register dump…");
  });
  const dumpCard = el("div", { class: "card i2c-section-card" }, [
    el("h3", {}, "4. I2C Dump"),
    el("p", { class: "muted" }, "Dump registers from one device on the selected bus."),
    el("div", { class: "i2c-controls" }, [
      i2cField("Device address", dumpAddress), i2cField("Mode", dumpMode),
    ]),
    el("div", { class: "i2c-actions" }, dumpButton),
    finishI2cResultArea(dumpArea),
  ]);

  const readArea = i2cResultArea("Read");
  const readAddress = i2cTextInput("0x20", "0x03–0x77");
  const readRegister = i2cTextInput("0x00", "0x00–0xff");
  const readMode = i2cModeSelect([["b", "Byte (b)"], ["w", "Word (w)"]]);
  const readButton = el("button", { type: "button", class: "btn btn-primary" }, "Read");
  readButton.addEventListener("click", async () => {
    const bus = requireI2cBus(context);
    if (!bus) return;
    await i2cRequest(
      `${API}/i2c/read`, i2cPostOptions({
        bus, address: readAddress.value.trim(), register: readRegister.value.trim(),
        mode: readMode.value,
      }), readArea, context, readButton, "Reading I2C register…");
  });

  const writeArea = i2cResultArea("Write");
  const writeAddress = i2cTextInput("0x20", "0x03–0x77");
  const writeRegister = i2cTextInput("0x00", "0x00–0xff");
  const writeValue = i2cTextInput("0x00", "0x00–0xffff");
  const writeMode = i2cModeSelect([["b", "Byte (b)"], ["w", "Word (w)"]]);
  const readback = el("input", { type: "checkbox", checked: "checked" });
  const writeButton = el("button", { type: "button", class: "btn btn-danger" }, "Write");
  writeButton.addEventListener("click", async () => {
    const bus = requireI2cBus(context);
    if (!bus) return;
    if (!confirm(
      `Write ${writeValue.value} to register ${writeRegister.value} at `
      + `${writeAddress.value} on bus ${bus}?\n\nAn incorrect write can change hardware state.`
    )) return;
    await i2cRequest(
      `${API}/i2c/write`, i2cPostOptions({
        bus, address: writeAddress.value.trim(), register: writeRegister.value.trim(),
        value: writeValue.value.trim(), mode: writeMode.value,
        confirm: true, readback: readback.checked,
      }), writeArea, context, writeButton, "Writing I2C register…");
  });

  const readWriteCard = el("div", { class: "card i2c-section-card" }, [
    el("h3", {}, "5. I2C Read / Write"),
    el("div", { class: "i2c-operation-split" }, [
      el("section", { class: "i2c-operation" }, [
        el("h4", {}, "Read one register"),
        el("div", { class: "i2c-controls" }, [
          i2cField("Device address", readAddress),
          i2cField("Register", readRegister),
          i2cField("Read mode", readMode),
        ]),
        el("div", { class: "i2c-actions" }, readButton),
        finishI2cResultArea(readArea),
      ]),
      el("section", { class: "i2c-operation i2c-operation-write" }, [
        el("h4", {}, "Write one register"),
        el("div", { class: "i2c-controls" }, [
          i2cField("Device address", writeAddress),
          i2cField("Register", writeRegister),
          i2cField("Value", writeValue),
          i2cField("Write mode", writeMode),
        ]),
        el("label", { class: "i2c-readback" }, [
          readback, el("span", {}, "Read back the register after writing"),
        ]),
        el("div", { class: "i2c-actions" }, writeButton),
        finishI2cResultArea(writeArea),
      ]),
    ]),
  ]);

  panel.append(intro, busCard, muxCard, detectCard, dumpCard, readWriteCard);
}

// ---------------------------------------------------------------------------
// Tab: USB Enum (lsusb -tv vs schematic golden map)
// ---------------------------------------------------------------------------
function renderUsbTreeNodes(nodes, depth = 0) {
  const frag = document.createDocumentFragment();
  for (const node of nodes || []) {
    const label = node.name || node.raw || "—";
    frag.appendChild(el("div", {
      class: "usb-tree-node",
      style: `padding-left:${depth * 16}px`,
    }, label));
    if (node.children && node.children.length) {
      frag.appendChild(renderUsbTreeNodes(node.children, depth + 1));
    }
  }
  return frag;
}

function usbField(label, input) {
  return el("label", { class: "usb-field" }, [el("span", {}, label), input]);
}

function applyUsbEnumResult({ data, statusEl, compareBody, liveTreeBox, goldenTreeBox, rawBox, toastPrefix }) {
  const cmp = data.compare || {};
  const overall = cmp.overall || "fail";
  statusEl.className = overall === "pass" ? "usb-status usb-pass" : "usb-status usb-fail";
  statusEl.textContent = cmp.summary || overall.toUpperCase();

  compareBody.innerHTML = "";
  (cmp.results || []).forEach((row) => {
    const cls =
      row.status === "pass" ? "usb-row-pass"
      : row.status === "warn" ? "usb-row-warn"
      : "usb-row-fail";
    compareBody.appendChild(el("tr", { class: cls }, [
      el("td", {}, (row.status || "—").toUpperCase()),
      el("td", {}, row.role || row.id || "—"),
      el("td", {}, row.refdes || "—"),
      el("td", {}, row.schematic_page != null ? String(row.schematic_page) : "—"),
      el("td", {}, row.detail || row.notes || "—"),
    ]));
  });
  if (!(cmp.results || []).length) {
    compareBody.appendChild(el("tr", {},
      el("td", { colspan: "5", class: "muted" }, "No golden devices configured.")));
  }

  liveTreeBox.textContent = data.raw_tree || "(empty lsusb -tv output)";
  goldenTreeBox.innerHTML = "";
  const gTree = (data.golden && data.golden.tree) || [];
  if (gTree.length) goldenTreeBox.appendChild(renderUsbTreeNodes(gTree));
  else goldenTreeBox.textContent = "No golden tree in usb_golden_map.json";

  const hostLabel = data.os_ip || data.bmc_ip || data.host_ip || "host";
  rawBox.textContent = [
    `=== host: ${hostLabel} ===`,
    `=== lsusb ===`,
    data.raw_lsusb || "(empty)",
    "",
    `=== lsusb -tv ===`,
    data.raw_tree || "(empty)",
  ].join("\n");

  toast(
    overall === "pass"
      ? `${toastPrefix} PASS.`
      : `${toastPrefix} FAIL — see compare table.`,
    overall === "pass" ? "ok" : "err"
  );
}

function loadUsbEnum(panel) {
  panel.innerHTML = "";

  const intro = el("div", { class: "card usb-intro-card" }, [
    el("h3", {}, "USB Enumeration"),
    el("p", { class: "muted" },
      "PowerCycling BMC_INV / OS_INV USB: lsusb on the BMC vs Hub-0/1/2 + NVIDIA MCTP "
      + "(0955:ffff / 0955:cf11), and lsusb on the host vs FT4232H / ASIX. "
      + "BMC and OS use separate golden views — UART is host-side on EVT."),
    el("p", { class: "muted" },
      `BMC: ${state.bmcIp || "—"} · Command: lsusb && lsusb -tv`),
  ]);

  // --- BMC section ---
  const bmcStatus = el("div", { class: "usb-status muted" }, "Click Run BMC USB Enum to start.");
  const bmcCompareBody = el("tbody", {}, el("tr", {},
    el("td", { colspan: "5", class: "muted" }, "No compare results yet.")));
  const bmcLiveTree = el("pre", { class: "usb-tree-box" }, "—");
  const bmcGoldenTree = el("div", { class: "usb-tree-box usb-golden-tree" }, "—");
  const bmcRaw = el("pre", { class: "usb-raw-box" }, "—");

  const bmcRunBtn = el("button", { type: "button", class: "btn btn-primary" }, "Run BMC USB Enum");
  bmcRunBtn.addEventListener("click", async () => {
    bmcRunBtn.disabled = true;
    bmcStatus.className = "usb-status muted";
    bmcStatus.textContent = `Running lsusb on BMC ${state.bmcIp}…`;
    try {
      const data = await api(`${API}/usb/enumerate`, { headers: bmcHeaders() });
      applyUsbEnumResult({
        data,
        statusEl: bmcStatus,
        compareBody: bmcCompareBody,
        liveTreeBox: bmcLiveTree,
        goldenTreeBox: bmcGoldenTree,
        rawBox: bmcRaw,
        toastPrefix: "BMC USB Enum",
      });
    } catch (err) {
      bmcStatus.className = "usb-status usb-fail";
      bmcStatus.textContent = err.message || String(err);
      toast(err.message || "BMC USB Enum failed.", "err");
    } finally {
      bmcRunBtn.disabled = false;
    }
  });

  const bmcCard = el("div", { class: "card usb-section-card" }, [
    el("h3", {}, "BMC USB Enum"),
    el("p", { class: "muted" },
      "BMC_INV USB: SSH to the BMC and compare against the BMC golden view "
      + "(USB2514 Hub-0/1/2, 0955:ffff MCTP, 0955:cf11 MCU). UART is not required here."),
    el("div", { class: "usb-actions" }, bmcRunBtn),
    bmcStatus,
    el("div", { class: "usb-table-scroll" }, el("table", { class: "usb-compare-table" }, [
      el("thead", {}, el("tr", {}, [
        el("th", {}, "Status"),
        el("th", {}, "Role"),
        el("th", {}, "Refdes"),
        el("th", {}, "Sch page"),
        el("th", {}, "Detail"),
      ])),
      bmcCompareBody,
    ])),
  ]);

  const bmcTrees = el("div", { class: "usb-tree-split" }, [
    el("div", { class: "card usb-section-card" }, [
      el("h3", {}, "Live tree (BMC lsusb -tv)"),
      bmcLiveTree,
    ]),
    el("div", { class: "card usb-section-card" }, [
      el("h3", {}, "Golden tree (schematic roles)"),
      bmcGoldenTree,
    ]),
  ]);

  const bmcRawCard = el("div", { class: "card usb-section-card" }, [
    el("h3", {}, "Raw BMC output"),
    bmcRaw,
  ]);

  // --- OS section ---
  const osIpInput = el("input", {
    type: "text",
    placeholder: "e.g. 10.137.x.x",
    value: state.osCreds.ip || state.hostIp || "",
    autocomplete: "off",
  });
  const osUserInput = el("input", {
    type: "text",
    placeholder: "aerial",
    value: state.osCreds.username || "aerial",
    autocomplete: "username",
  });
  const osPassInput = el("input", {
    type: "password",
    placeholder: "nvidia",
    value: state.osCreds.password || "nvidia",
    autocomplete: "current-password",
  });

  const osStatus = el("div", { class: "usb-status muted" },
    "Enter the OS IP and SSH credentials, then click Run OS USB Enum.");
  const osCompareBody = el("tbody", {}, el("tr", {},
    el("td", { colspan: "5", class: "muted" }, "No compare results yet.")));
  const osLiveTree = el("pre", { class: "usb-tree-box" }, "—");
  const osGoldenTree = el("div", { class: "usb-tree-box usb-golden-tree" }, "—");
  const osRaw = el("pre", { class: "usb-raw-box" }, "—");

  const osRunBtn = el("button", { type: "button", class: "btn btn-primary" }, "Run OS USB Enum");
  osRunBtn.addEventListener("click", async () => {
    const osIp = (osIpInput.value || "").trim();
    const username = (osUserInput.value || "").trim();
    const password = osPassInput.value || "";
    if (!osIp) {
      toast("Enter an OS IP address.", "err");
      return;
    }
    if (!username) {
      toast("Enter the OS SSH username.", "err");
      return;
    }
    if (!password) {
      toast("Enter the OS SSH password.", "err");
      return;
    }

    state.osCreds = { ip: osIp, username, password };
    state.hostIp = osIp;

    osRunBtn.disabled = true;
    osStatus.className = "usb-status muted";
    osStatus.textContent = `Running lsusb on OS ${osIp}…`;
    try {
      const data = await api(`${API}/usb/enumerate-os`, {
        method: "POST",
        headers: { ...bmcHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ os_ip: osIp, username, password }),
      });
      applyUsbEnumResult({
        data,
        statusEl: osStatus,
        compareBody: osCompareBody,
        liveTreeBox: osLiveTree,
        goldenTreeBox: osGoldenTree,
        rawBox: osRaw,
        toastPrefix: "OS USB Enum",
      });
    } catch (err) {
      osStatus.className = "usb-status usb-fail";
      osStatus.textContent = err.message || String(err);
      toast(err.message || "OS USB Enum failed.", "err");
    } finally {
      osRunBtn.disabled = false;
    }
  });

  const osCard = el("div", { class: "card usb-section-card" }, [
    el("h3", {}, "OS USB Enum"),
    el("p", { class: "muted" },
      "OS_INV USB: SSH to the host and compare against the OS golden view "
      + "(FT4232H UART, ASIX AX88179, USB 2.0/3.0 root hubs)."),
    el("div", { class: "usb-controls" }, [
      usbField("OS IP address", osIpInput),
      usbField("SSH username", osUserInput),
      usbField("SSH password", osPassInput),
    ]),
    el("div", { class: "usb-actions" }, osRunBtn),
    osStatus,
    el("div", { class: "usb-table-scroll" }, el("table", { class: "usb-compare-table" }, [
      el("thead", {}, el("tr", {}, [
        el("th", {}, "Status"),
        el("th", {}, "Role"),
        el("th", {}, "Refdes"),
        el("th", {}, "Sch page"),
        el("th", {}, "Detail"),
      ])),
      osCompareBody,
    ])),
  ]);

  const osTrees = el("div", { class: "usb-tree-split" }, [
    el("div", { class: "card usb-section-card" }, [
      el("h3", {}, "Live tree (OS lsusb -tv)"),
      osLiveTree,
    ]),
    el("div", { class: "card usb-section-card" }, [
      el("h3", {}, "Golden tree (schematic roles)"),
      osGoldenTree,
    ]),
  ]);

  const osRawCard = el("div", { class: "card usb-section-card" }, [
    el("h3", {}, "Raw OS output"),
    osRaw,
  ]);

  panel.append(intro, bmcCard, bmcTrees, bmcRawCard, osCard, osTrees, osRawCard);
}

// ---------------------------------------------------------------------------
// Tab: PCIe (PowerCycling OS_INV: lspci + nvme --list)
// ---------------------------------------------------------------------------
function loadPcie(panel) {
  panel.innerHTML = "";

  const osIpInput = el("input", {
    type: "text",
    placeholder: "e.g. 10.137.174.166",
    value: state.osCreds.ip || state.hostIp || "",
    autocomplete: "off",
  });
  const osUserInput = el("input", {
    type: "text",
    placeholder: "aerial",
    value: state.osCreds.username || "aerial",
    autocomplete: "username",
  });
  const osPassInput = el("input", {
    type: "password",
    placeholder: "nvidia",
    value: state.osCreds.password || "nvidia",
    autocomplete: "current-password",
  });

  const statusEl = el("div", { class: "pcie-status muted" },
    "Enter the OS IP, then run PowerCycling OS_INV (lspci + nvme --list).");
  const countEl = el("div", { class: "kv", style: "display:inline-block;margin:8px 0 12px;" }, [
    el("div", { class: "k" }, "Devices found"),
    el("div", { class: "v", id: "pcie-count" }, "—"),
  ]);
  const compareBody = el("tbody", {}, el("tr", {},
    el("td", { colspan: "3", class: "muted" }, "No OS_INV compare yet.")));
  const tbody = el("tbody", {}, el("tr", {},
    el("td", { colspan: "3", class: "muted" }, "No lspci results yet.")));
  const rawBox = el("pre", { class: "pcie-raw-box" }, "—");
  const nvmeBox = el("pre", { class: "pcie-raw-box" }, "—");

  const runBtn = el("button", { type: "button", class: "btn btn-primary" }, "Run OS_INV");
  runBtn.addEventListener("click", async () => {
    const osIp = (osIpInput.value || "").trim();
    const username = (osUserInput.value || "").trim() || "aerial";
    const password = osPassInput.value || "";
    if (!osIp) {
      toast("Enter an OS IP address.", "err");
      return;
    }
    if (!password) {
      toast("Enter the OS SSH password.", "err");
      return;
    }

    state.osCreds = { ip: osIp, username, password };
    state.hostIp = osIp;

    runBtn.disabled = true;
    statusEl.className = "pcie-status muted";
    statusEl.textContent = `Connecting to ${username}@${osIp} and running lspci + nvme --list…`;
    tbody.innerHTML = "";
    tbody.appendChild(el("tr", {},
      el("td", { colspan: "3", class: "muted" }, "Running…")));
    compareBody.innerHTML = "";
    compareBody.appendChild(el("tr", {},
      el("td", { colspan: "3", class: "muted" }, "Running…")));
    rawBox.textContent = "…";
    nvmeBox.textContent = "…";

    try {
      const data = await api(`${API}/pcie/lspci`, {
        method: "POST",
        headers: { ...bmcHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ os_ip: osIp, username, password }),
      });

      const devices = data.devices || [];
      const countNode = $("#pcie-count");
      if (countNode) countNode.textContent = String(data.device_count ?? devices.length);

      compareBody.innerHTML = "";
      const cmpRows = (data.compare && data.compare.results) || [];
      if (!cmpRows.length) {
        compareBody.appendChild(el("tr", {},
          el("td", { colspan: "3", class: "muted" }, "No OS_INV expectations configured.")));
      } else {
        for (const row of cmpRows) {
          const cls =
            row.status === "pass" ? "usb-row-pass"
            : row.status === "fail" ? "usb-row-fail"
            : "usb-row-warn";
          compareBody.appendChild(el("tr", { class: cls }, [
            el("td", {}, (row.status || "—").toUpperCase()),
            el("td", {}, row.role || row.id || "—"),
            el("td", {}, row.detail || "—"),
          ]));
        }
      }

      tbody.innerHTML = "";
      if (!devices.length) {
        tbody.appendChild(el("tr", {},
          el("td", { colspan: "3", class: "muted" }, "No devices reported.")));
      } else {
        for (const d of devices) {
          tbody.appendChild(el("tr", {}, [
            el("td", { class: "pcie-addr" }, d.address || "—"),
            el("td", {}, d.class || "—"),
            el("td", {}, d.description || d.raw || "—"),
          ]));
        }
      }

      rawBox.textContent = data.raw || "(no output)";
      nvmeBox.textContent = data.nvme_raw || "(no nvme --list output)";
      const overall = (data.compare && data.compare.overall) || (data.ok ? "pass" : "fail");
      statusEl.className = overall === "pass" ? "pcie-status pcie-pass" : "pcie-status pcie-fail";
      statusEl.textContent = data.message || `Found ${devices.length} PCIe device(s) on ${osIp}.`;
      toast(data.message || `lspci: ${devices.length} device(s) on ${osIp}.`, overall === "pass" ? "ok" : "err");
    } catch (err) {
      statusEl.className = "pcie-status pcie-fail";
      statusEl.textContent = err.message || String(err);
      tbody.innerHTML = "";
      tbody.appendChild(el("tr", {},
        el("td", { colspan: "3", class: "muted" }, "Failed — see status above.")));
      compareBody.innerHTML = "";
      compareBody.appendChild(el("tr", {},
        el("td", { colspan: "3", class: "muted" }, "Failed — see status above.")));
      rawBox.textContent = err.message || String(err);
      nvmeBox.textContent = err.message || String(err);
      toast(err.message || "OS_INV failed.", "err");
    } finally {
      runBtn.disabled = false;
    }
  });

  panel.append(
    el("div", { class: "card" }, [
      el("h3", {}, "PCIe / OS inventory"),
      el("p", { class: "muted" },
        "PowerCycling OS_INV: SSH to the host and run lspci plus nvme --list, then compare "
        + "against the EVT golden map (3× CX8, GPU 2c3a, SSSTC NVMe, ASPEED, Renesas xHCI)."),
      el("div", { class: "usb-controls" }, [
        usbField("OS IP address", osIpInput),
        usbField("SSH username", osUserInput),
        usbField("SSH password", osPassInput),
      ]),
      el("div", { class: "usb-actions" }, [runBtn]),
      statusEl,
      countEl,
    ]),
    el("div", { class: "card" }, [
      el("h3", {}, "OS_INV compare"),
      el("div", { class: "usb-table-scroll" }, el("table", { class: "pcie-table" }, [
        el("thead", {}, el("tr", {}, [
          el("th", {}, "Status"),
          el("th", {}, "Check"),
          el("th", {}, "Detail"),
        ])),
        compareBody,
      ])),
    ]),
    el("div", { class: "card" }, [
      el("h3", {}, "PCIe devices"),
      el("div", { class: "usb-table-scroll" }, el("table", { class: "pcie-table" }, [
        el("thead", {}, el("tr", {}, [
          el("th", {}, "Address"),
          el("th", {}, "Class"),
          el("th", {}, "Description"),
        ])),
        tbody,
      ])),
    ]),
    el("div", { class: "card" }, [
      el("h3", {}, "Raw lspci output"),
      rawBox,
    ]),
    el("div", { class: "card" }, [
      el("h3", {}, "Raw nvme --list output"),
      nvmeBox,
    ]),
  );
}

// ---------------------------------------------------------------------------
// Tab: KVM (open the BMC's KVM console for watching the OS boot)
// ---------------------------------------------------------------------------
function loadKvm(panel) {
  panel.innerHTML = "";

  const bmcIpInput = el("input", {
    type: "text",
    placeholder: "e.g. 10.137.175.147",
    value: state.bmcIp || "",
    autocomplete: "off",
  });

  const statusEl = el("div", { class: "kvm-status muted" },
    "Enter the BMC IP, then click OS to open the BMC KVM page.");

  const osBtn = el("button", { type: "button", class: "btn btn-primary btn-kvm-boot" },
    "OS");

  osBtn.addEventListener("click", () => {
    const bmcIp = (bmcIpInput.value || "").trim();
    if (!bmcIp) {
      toast("Enter a BMC IP address first.", "err");
      return;
    }
    const url = kvmOperationsUrl(bmcIp);
    const tab = window.open(url, "_blank", "noopener,noreferrer");
    statusEl.className = tab ? "kvm-status kvm-pass" : "kvm-status kvm-warn";
    statusEl.textContent = tab
      ? `Opened ${url}`
      : `Popup blocked — open ${url} manually.`;
    toast(
      tab ? "Opening BMC KVM (OS)." : "Popup blocked — allow popups, then retry.",
      tab ? "ok" : "err"
    );
  });

  const detectBtn = el("button", { type: "button", class: "btn btn-ghost" },
    "OS IP Detect");
  detectBtn.addEventListener("click", () => {
    const bmcIp = (bmcIpInput.value || "").trim();
    if (!bmcIp) {
      toast("Enter a BMC IP address first.", "err");
      return;
    }
    openKvmConsole(bmcIp);
    openOsIpDetectModal(bmcIp);
  });

  panel.append(
    el("div", { class: "card" }, [
      el("h3", {}, "KVM"),
      el("p", { class: "muted" },
        "Opens the BMC Operations → KVM page in a new tab: "
        + "https://<bmc-ip>/#/operations/kvm"),
      el("div", { class: "usb-controls" }, [
        usbField("BMC IP address", bmcIpInput),
      ]),
      el("div", { class: "usb-actions" }, [osBtn, detectBtn]),
      statusEl,
      el("p", { class: "muted" },
        "OS IP Detect walks through the console login and reads the host's lab IP "
        + "from ifconfig, then shares it with the PCIe and USB tabs."),
    ]),
  );
}

// ---------------------------------------------------------------------------
// Tab: Scope (network SCPI live virtual view)
// ---------------------------------------------------------------------------
const SCOPE_CH_COLORS = ["#f0e14a", "#4ad2f0", "#e05cff", "#76b900"];
let scopeLiveTimer = null;
let scopeLiveInFlight = false;
let scopeVncClient = null;

const SCOPE_VNC_DEFAULTS = {
  vncPort: "5900",
  vncUser: "Tek_Local_Admin",
  vncPassword: "labuser",
  vncConnected: false,
};

function stopScopeVnc() {
  if (scopeVncClient) {
    scopeVncClient.disconnect();
    scopeVncClient = null;
  }
  state.scope.vncConnected = false;
}

function stopScopeLive() {
  if (scopeLiveTimer) {
    clearInterval(scopeLiveTimer);
    scopeLiveTimer = null;
  }
  state.scope.live = false;
  scopeLiveInFlight = false;
}

function formatScopeMeas(val) {
  if (val == null || val === "") return "—";
  const n = Number(val);
  if (!Number.isFinite(n)) return String(val);
  const abs = Math.abs(n);
  if (abs === 0) return "0";
  if (abs >= 1e6) return `${(n / 1e6).toFixed(3)} M`;
  if (abs >= 1e3) return `${(n / 1e3).toFixed(3)} k`;
  if (abs >= 1) return n.toFixed(3);
  if (abs >= 1e-3) return `${(n * 1e3).toFixed(3)} m`;
  if (abs >= 1e-6) return `${(n * 1e6).toFixed(3)} µ`;
  if (abs >= 1e-9) return `${(n * 1e9).toFixed(3)} n`;
  return n.toExponential(3);
}

function drawScopeWaveforms(canvas, waveforms) {
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 900;
  const cssH = canvas.clientHeight || 360;
  canvas.width = Math.floor(cssW * dpr);
  canvas.height = Math.floor(cssH * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const w = cssW;
  const h = cssH;
  ctx.fillStyle = "#050806";
  ctx.fillRect(0, 0, w, h);

  // Graticule — 10x8 like a classic scope.
  ctx.strokeStyle = "rgba(118, 185, 0, 0.18)";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 10; i++) {
    const x = (i / 10) * w;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, h);
    ctx.stroke();
  }
  for (let i = 0; i <= 8; i++) {
    const y = (i / 8) * h;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
    ctx.stroke();
  }
  // Center crosshair
  ctx.strokeStyle = "rgba(118, 185, 0, 0.35)";
  ctx.beginPath();
  ctx.moveTo(w / 2, 0);
  ctx.lineTo(w / 2, h);
  ctx.moveTo(0, h / 2);
  ctx.lineTo(w, h / 2);
  ctx.stroke();

  if (!waveforms || !waveforms.length) {
    ctx.fillStyle = "rgba(200, 210, 200, 0.55)";
    ctx.font = "14px Rajdhani, sans-serif";
    ctx.fillText("No waveform data — waiting for scope…", 16, 28);
    return;
  }

  let globalMin = Infinity;
  let globalMax = -Infinity;
  for (const wave of waveforms) {
    for (const v of wave.volts || []) {
      if (v < globalMin) globalMin = v;
      if (v > globalMax) globalMax = v;
    }
  }
  if (!Number.isFinite(globalMin) || !Number.isFinite(globalMax)) return;
  if (globalMax === globalMin) {
    globalMax += 0.5;
    globalMin -= 0.5;
  }
  const pad = (globalMax - globalMin) * 0.08;
  globalMin -= pad;
  globalMax += pad;

  waveforms.forEach((wave, idx) => {
    const volts = wave.volts || [];
    if (volts.length < 2) return;
    const color = SCOPE_CH_COLORS[(wave.channel - 1) % SCOPE_CH_COLORS.length] || SCOPE_CH_COLORS[idx % SCOPE_CH_COLORS.length];
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    volts.forEach((v, i) => {
      const x = (i / (volts.length - 1)) * w;
      const y = h - ((v - globalMin) / (globalMax - globalMin)) * h;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();

    ctx.fillStyle = color;
    ctx.font = "12px JetBrains Mono, monospace";
    ctx.fillText(wave.label || `CH${wave.channel}`, 10 + idx * 64, 18);
  });
}

function renderScopeMeasurements(container, measurements) {
  container.innerHTML = "";
  if (!measurements || !measurements.length) {
    container.appendChild(el("div", { class: "muted" }, "No measurements yet."));
    return;
  }
  for (const m of measurements) {
    const color = SCOPE_CH_COLORS[(m.channel - 1) % SCOPE_CH_COLORS.length];
    container.appendChild(el("div", { class: "scope-meas-card", style: `--ch:${color}` }, [
      el("div", { class: "scope-meas-ch" }, m.label || `CH${m.channel}`),
      el("div", { class: "scope-meas-row" }, [
        el("span", {}, "Vpp"),
        el("strong", {}, `${formatScopeMeas(m.vpp)} V`),
      ]),
      el("div", { class: "scope-meas-row" }, [
        el("span", {}, "Freq"),
        el("strong", {}, `${formatScopeMeas(m.freq)} Hz`),
      ]),
      el("div", { class: "scope-meas-row" }, [
        el("span", {}, "Vrms"),
        el("strong", {}, m.vrms != null ? `${formatScopeMeas(m.vrms)} V` : "—"),
      ]),
    ]));
  }
}

function applyScopeFrame(frame, refs) {
  const {
    statusEl, idnEl, screenImg, screenEmpty, canvas, measBox, metaEl,
  } = refs;

  state.scope.idn = frame.idn || state.scope.idn;
  state.scope.vendor = frame.vendor || state.scope.vendor;
  state.scope.port = String(frame.port || state.scope.port || "");
  idnEl.textContent = frame.idn || "—";
  metaEl.textContent =
    `${frame.vendor || "scope"} · TCP ${frame.port || "—"} · `
    + `${new Date((frame.ts || Date.now() / 1000) * 1000).toLocaleTimeString()}`;

  if (frame.screenshot) {
    const mime = frame.screenshot_mime || "image/png";
    screenImg.src = `data:${mime};base64,${frame.screenshot}`;
    screenImg.classList.remove("hidden");
    screenEmpty.classList.add("hidden");
  } else {
    screenImg.classList.add("hidden");
    screenEmpty.classList.remove("hidden");
    screenEmpty.textContent = frame.screenshot_note
      || "Screenshot not available on this scope — showing waveform plot below.";
  }

  drawScopeWaveforms(canvas, frame.waveforms || []);
  renderScopeMeasurements(measBox, frame.measurements || []);
  statusEl.className = "scope-status scope-ok";
  statusEl.textContent = `Live · ${frame.idn || state.scope.ip}`;
}

async function fetchScopeLive(refs) {
  if (scopeLiveInFlight || !state.scope.connected || !state.scope.ip) return;
  scopeLiveInFlight = true;
  try {
    const body = {
      scope_ip: state.scope.ip,
      port: state.scope.port || undefined,
      screenshot: true,
      waveforms: true,
    };
    const frame = await api(`/api/scope/live`, {
      method: "POST",
      headers: { ...bmcHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    applyScopeFrame(frame, refs);
  } catch (err) {
    refs.statusEl.className = "scope-status scope-err";
    refs.statusEl.textContent = err.message || String(err);
  } finally {
    scopeLiveInFlight = false;
  }
}

function startScopeLive(refs) {
  stopScopeLive();
  state.scope.live = true;
  fetchScopeLive(refs);
  scopeLiveTimer = setInterval(() => fetchScopeLive(refs), 1200);
}

function loadScope(panel) {
  stopScopeVnc();
  panel.innerHTML = "";
  stopScopeLive();

  const ipInput = el("input", {
    type: "text",
    placeholder: "e.g. 10.24.x.x",
    value: state.scope.ip || "",
    autocomplete: "off",
  });
  const portInput = el("input", {
    type: "text",
    placeholder: "auto (5025 / 4000)",
    value: state.scope.port || "",
    autocomplete: "off",
  });
  const vncIpInput = el("input", {
    type: "text",
    placeholder: "e.g. 10.24.x.x",
    value: state.scope.ip || "",
    autocomplete: "off",
  });
  const vncPortInput = el("input", {
    type: "number",
    min: "5900",
    max: "5999",
    value: state.scope.vncPort || "5900",
    autocomplete: "off",
  });
  const vncUserInput = el("input", {
    type: "text",
    class: "input-readonly",
    value: state.scope.vncUser || "Tek_Local_Admin",
    readOnly: true,
    title: "Tektronix lab scope username (fixed)",
  });
  const vncPasswordInput = el("input", {
    type: "password",
    class: "input-readonly",
    value: state.scope.vncPassword || "labuser",
    readOnly: true,
    title: "Tektronix lab scope password (fixed)",
  });
  const vncStatusEl = el("div", { class: "scope-vnc-status muted" },
    "Enter the scope IP, then connect for interactive keyboard and pointer control.");
  const vncViewport = el("div", {
    class: "scope-vnc-viewport",
    tabindex: "0",
    "aria-label": "Interactive live scope display",
  }, el("div", { class: "scope-vnc-placeholder" },
    "The live scope desktop will appear here."));
  const vncConnectBtn = el("button", {
    type: "button",
    class: "btn btn-primary",
  }, "Connect Live VNC");
  const vncDisconnectBtn = el("button", {
    type: "button",
    class: "btn btn-ghost",
    disabled: "disabled",
  }, "Disconnect VNC");

  function setVncStatus(kind, message) {
    const classByKind = {
      connected: "scope-vnc-status scope-ok",
      error: "scope-vnc-status scope-err",
      connecting: "scope-vnc-status scope-vnc-connecting",
      disconnected: "scope-vnc-status muted",
    };
    vncStatusEl.className = classByKind[kind] || "scope-vnc-status muted";
    vncStatusEl.textContent = message;
    state.scope.vncConnected = kind === "connected";
    vncConnectBtn.disabled = kind === "connecting" || kind === "connected";
    if (kind === "connected" || kind === "connecting") {
      vncDisconnectBtn.removeAttribute("disabled");
    } else {
      vncDisconnectBtn.setAttribute("disabled", "disabled");
    }
  }

  vncConnectBtn.addEventListener("click", async () => {
    const scopeIp = (vncIpInput.value || "").trim();
    const port = Number(vncPortInput.value || 5900);
    if (!scopeIp) {
      toast("Enter a scope IP address.", "err");
      vncIpInput.focus();
      return;
    }
    if (!window.ScopeVncClient) {
      setVncStatus("error",
        "The noVNC client could not load. Hard-refresh the page and try again.");
      return;
    }
    state.scope.ip = scopeIp;
    ipInput.value = scopeIp;
    state.scope.vncPort = String(port);
    state.scope.vncUser = vncUserInput.value || "Tek_Local_Admin";
    state.scope.vncPassword = vncPasswordInput.value || "labuser";
    setVncStatus("connecting", `Connecting to ${scopeIp}:${port}…`);
    try {
      const session = await api("/api/bmc/scope/vnc/session", {
        method: "POST",
        headers: { ...bmcHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ scope_ip: scopeIp, port }),
      });
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      const wsUrl =
        `${scheme}://${window.location.host}${session.path}?token=${encodeURIComponent(session.token)}`;
      vncViewport.innerHTML = "";
      scopeVncClient = new window.ScopeVncClient(vncViewport, setVncStatus);
      scopeVncClient.connect(wsUrl, {
        username: state.scope.vncUser,
        password: state.scope.vncPassword,
      });
    } catch (err) {
      setVncStatus("error", err.message || String(err));
      toast(err.message || "VNC connection failed.", "err");
    }
  });

  vncDisconnectBtn.addEventListener("click", () => {
    if (scopeVncClient) {
      scopeVncClient.disconnect();
      scopeVncClient = null;
    }
    vncViewport.innerHTML = "";
    vncViewport.appendChild(el("div", { class: "scope-vnc-placeholder" },
      "The live scope desktop will appear here."));
    setVncStatus("disconnected", "VNC disconnected.");
  });

  const statusEl = el("div", {
    class: state.scope.connected ? "scope-status scope-ok" : "scope-status muted",
  }, state.scope.connected
    ? `Connected · ${state.scope.idn || state.scope.ip}`
    : "Enter the scope IP and click Connect.");

  const idnEl = el("div", { class: "scope-idn" }, state.scope.idn || "—");
  const metaEl = el("div", { class: "muted scope-meta" }, "Not connected");
  const screenImg = el("img", {
    class: "scope-screen-img hidden",
    alt: "Oscilloscope live screen",
  });
  const screenEmpty = el("div", { class: "scope-screen-empty" },
    "Connect to a scope to see the live screen.");
  const canvas = el("canvas", { class: "scope-wave-canvas" });
  const measBox = el("div", { class: "scope-meas-grid" },
    el("div", { class: "muted" }, "Measurements appear after connect."));

  const refs = { statusEl, idnEl, screenImg, screenEmpty, canvas, measBox, metaEl };

  const connectBtn = el("button", { type: "button", class: "btn btn-primary" }, "Connect");
  const liveBtn = el("button", { type: "button", class: "btn btn-ghost" },
    state.scope.live ? "Stop Live" : "Start Live");
  const refreshBtn = el("button", { type: "button", class: "btn btn-ghost" }, "Refresh Once");
  const disconnectBtn = el("button", { type: "button", class: "btn btn-ghost" }, "Disconnect");
  const webBtn = el("button", { type: "button", class: "btn btn-ghost" }, "Open Scope Web UI");
  if (!state.scope.connected) {
    liveBtn.setAttribute("disabled", "disabled");
    refreshBtn.setAttribute("disabled", "disabled");
    disconnectBtn.setAttribute("disabled", "disabled");
  }
  if (!state.scope.ip) webBtn.setAttribute("disabled", "disabled");

  function setConnectedUi(connected) {
    state.scope.connected = connected;
    for (const btn of [liveBtn, refreshBtn, disconnectBtn]) {
      if (connected) btn.removeAttribute("disabled");
      else btn.setAttribute("disabled", "disabled");
    }
    webBtn.removeAttribute("disabled");
  }

  connectBtn.addEventListener("click", async () => {
    const scopeIp = (ipInput.value || "").trim();
    const port = (portInput.value || "").trim();
    if (!scopeIp) {
      toast("Enter a scope IP address.", "err");
      return;
    }
    connectBtn.disabled = true;
    statusEl.className = "scope-status muted";
    statusEl.textContent = `Connecting to ${scopeIp}…`;
    try {
      const body = { scope_ip: scopeIp };
      if (port) body.port = Number(port);
      const res = await api(`/api/scope/connect`, {
        method: "POST",
        headers: { ...bmcHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      state.scope.ip = scopeIp;
      vncIpInput.value = scopeIp;
      state.scope.port = String(res.port || port || "");
      state.scope.idn = res.idn || "";
      state.scope.vendor = res.vendor || "";
      setConnectedUi(true);
      idnEl.textContent = res.idn || "—";
      metaEl.textContent = `${res.vendor || "scope"} · TCP ${res.port}`;
      statusEl.className = "scope-status scope-ok";
      statusEl.textContent = res.message || "Connected.";
      toast(res.message || "Scope connected.", "ok");
      startScopeLive(refs);
      liveBtn.textContent = "Stop Live";
    } catch (err) {
      setConnectedUi(false);
      statusEl.className = "scope-status scope-err";
      statusEl.textContent = err.message || String(err);
      toast(err.message || "Scope connect failed.", "err");
    } finally {
      connectBtn.disabled = false;
    }
  });

  liveBtn.addEventListener("click", () => {
    if (!state.scope.connected) return;
    if (state.scope.live) {
      stopScopeLive();
      liveBtn.textContent = "Start Live";
      statusEl.className = "scope-status muted";
      statusEl.textContent = `Paused · ${state.scope.idn || state.scope.ip}`;
    } else {
      startScopeLive(refs);
      liveBtn.textContent = "Stop Live";
    }
  });

  refreshBtn.addEventListener("click", () => {
    if (!state.scope.connected) return;
    fetchScopeLive(refs);
  });

  disconnectBtn.addEventListener("click", async () => {
    stopScopeLive();
    liveBtn.textContent = "Start Live";
    try {
      if (state.scope.ip) {
        await api(`/api/scope/disconnect`, {
          method: "POST",
          headers: { ...bmcHeaders(), "Content-Type": "application/json" },
          body: JSON.stringify({ scope_ip: state.scope.ip }),
        });
      }
    } catch (_) {
      // ignore — local disconnect still proceeds
    }
    setConnectedUi(false);
    state.scope.idn = "";
    state.scope.vendor = "";
    statusEl.className = "scope-status muted";
    statusEl.textContent = "Disconnected.";
    idnEl.textContent = "—";
    metaEl.textContent = "Not connected";
    screenImg.classList.add("hidden");
    screenEmpty.classList.remove("hidden");
    screenEmpty.textContent = "Connect to a scope to see the live screen.";
    drawScopeWaveforms(canvas, []);
    measBox.innerHTML = "";
    measBox.appendChild(el("div", { class: "muted" }, "Measurements appear after connect."));
    toast("Scope disconnected.");
  });

  webBtn.addEventListener("click", () => {
    const ip = (ipInput.value || state.scope.ip || "").trim();
    if (!ip) {
      toast("Enter a scope IP first.", "err");
      return;
    }
    window.open(`http://${ip}/`, "_blank", "noopener,noreferrer");
  });

  const intro = el("div", { class: "card scope-intro-card" }, [
    el("h3", {}, "Network Scope"),
    el("p", { class: "muted" },
      "Use Live VNC for a RealVNC Viewer-style interactive instrument desktop. "
      + "SCPI diagnostics remain available below for screenshots, waveforms, and measurements."),
  ]);

  const vncCard = el("div", { class: "card scope-section-card scope-vnc-card" }, [
    el("div", { class: "scope-vnc-heading" }, [
      el("div", {}, [
        el("h3", {}, "RealVNCViewer — Live VNC"),
        el("p", { class: "muted" },
          "Browser-based RFB/noVNC equivalent of RealVNC Viewer. Credentials are prefilled "
          + "for Tektronix lab scopes and are sent only during VNC authentication."),
      ]),
      el("span", { class: "scope-vnc-badge" }, "Interactive"),
    ]),
    el("div", { class: "scope-controls scope-vnc-controls" }, [
      usbField("Scope IP address", vncIpInput),
      usbField("VNC port", vncPortInput),
      usbField("Username", vncUserInput),
      usbField("Password", vncPasswordInput),
    ]),
    el("div", { class: "scope-actions" }, [vncConnectBtn, vncDisconnectBtn]),
    vncStatusEl,
    vncViewport,
    el("p", { class: "muted scope-vnc-note" },
      "Click inside the display to send keyboard input. The usual VNC port is 5900."),
  ]);

  const connectCard = el("div", { class: "card scope-section-card" }, [
    el("h3", {}, "SCPI Diagnostics"),
    el("div", { class: "scope-controls" }, [
      usbField("Scope IP address", ipInput),
      usbField("SCPI port (optional)", portInput),
    ]),
    el("div", { class: "scope-actions" }, [
      connectBtn, liveBtn, refreshBtn, disconnectBtn, webBtn,
    ]),
    statusEl,
    el("div", { class: "scope-idn-row" }, [
      el("span", { class: "muted" }, "Identity"),
      idnEl,
    ]),
    metaEl,
  ]);

  const viewCard = el("div", { class: "card scope-section-card" }, [
    el("h3", {}, "Live scope screen"),
    el("p", { class: "muted" },
      "Virtual view of the instrument display (SCPI screenshot). "
      + "If the scope does not support screen capture, the waveform plot below still updates."),
    el("div", { class: "scope-screen-frame" }, [screenImg, screenEmpty]),
  ]);

  const waveCard = el("div", { class: "card scope-section-card" }, [
    el("h3", {}, "Waveform plot"),
    el("div", { class: "scope-wave-wrap" }, canvas),
  ]);

  const measCard = el("div", { class: "card scope-section-card" }, [
    el("h3", {}, "Measurements"),
    measBox,
  ]);

  panel.append(intro, vncCard, connectCard, viewCard, waveCard, measCard);

  // Keep live going if user re-opens the tab while already connected.
  if (state.scope.connected && state.scope.ip) {
    setConnectedUi(true);
    startScopeLive(refs);
    liveBtn.textContent = "Stop Live";
  } else {
    drawScopeWaveforms(canvas, []);
  }
}

// ---------------------------------------------------------------------------
// Tab: Firmware
// ---------------------------------------------------------------------------
async function loadFirmware(panel) {
  panel.innerHTML = loadingHTML("Reading firmware versions from BMC Redfish…");
  try {
    const d = await api(`/api/bmc/firmware`, { headers: bmcHeaders() });
    state.firmwareCache = { ...d, fetchedAt: new Date().toISOString() };
    panel.innerHTML = "";

    const parts = d.parts || [];
    const found = parts.filter((p) => p.version).length;

    const card = el("div", { class: "card" });
    card.appendChild(el("h3", {}, "Firmware Versions"));
    card.appendChild(el("p", { class: "muted" },
      "Versions are read from Redfish FirmwareInventory on the BMC "
      + "(same source as BMC). Commands run in the background — only versions are shown."));
    card.appendChild(buildReportActionBar({
      getContent: () => buildFirmwareReportText(state.firmwareCache),
      filenamePrefix: "firmware",
      reportTitle: `MGX ARC Firmware Report — ${state.bmcIp || "BMC"}`,
    }));

    const summary = el("div", { class: "kv-grid", style: "margin-bottom:18px;" }, [
      el("div", { class: "kv" }, [
        el("div", { class: "k" }, "Parts Listed"),
        el("div", { class: "v" }, String(parts.length)),
      ]),
      el("div", { class: "kv" }, [
        el("div", { class: "k" }, "Versions Found"),
        el("div", { class: "v" }, `${found} / ${parts.length}`),
      ]),
      el("div", { class: "kv" }, [
        el("div", { class: "k" }, "Inventory Objects"),
        el("div", { class: "v" }, String(d.inventory_count ?? "—")),
      ]),
    ]);
    card.appendChild(summary);

    if (!parts.length) {
      card.appendChild(el("p", { class: "muted" }, "No firmware parts configured."));
    } else {
      const table = el("table", { class: "fw-table" });
      table.appendChild(el("thead", {}, el("tr", {}, [
        "Status", "Firmware Part", "Inventory ID", "Version", "Source",
      ].map((h) => el("th", {}, h)))));
      const tbody = el("tbody");
      for (const c of parts) {
        const ok = !!c.version;
        const sourceText = ok ? (c.source || "redfish") : "not reported";
        tbody.appendChild(el("tr", { class: ok ? "fw-row-ok" : "fw-row-miss" }, [
          el("td", {}, el("span", {
            class: "fw-dot" + (ok ? " fw-dot-ok" : ""),
            title: ok ? "Version found" : "Not reported",
          })),
          el("td", { class: "fw-part-name" }, [
            c.name || "—",
            c.extra ? el("span", { class: "fw-badge", title: c.notes || "" }, " extra") : "",
          ]),
          el("td", {}, el("code", {}, c.redfish_id || "—")),
          el("td", {}, ok ? el("code", { class: "fw-version" }, c.version) : el("span", { class: "muted" }, "—")),
          el("td", {}, el("span", { class: "muted" }, sourceText)),
        ]));
      }
      table.appendChild(tbody);
      card.appendChild(table);
    }
    panel.appendChild(card);
  } catch (err) {
    panel.innerHTML = `<div class="msg msg-err">${err.message}</div>`;
  }
}

// ---------------------------------------------------------------------------
// Tab: Flash (BMC + FML + CX8 + SMR + SBIOS + ERoT)
// ---------------------------------------------------------------------------
async function loadFlash(panel) {
  panel.innerHTML = loadingHTML("Loading flash options…");
  try {
    const [bmc, fml, cx8, smr, sbios, erot] = await Promise.all([
      api(`/api/bmc/flash/bmc`, { headers: bmcHeaders() }),
      api(`/api/bmc/flash/fml`, { headers: bmcHeaders() }),
      api(`/api/bmc/flash/cx8`, { headers: bmcHeaders() }),
      api(`/api/bmc/flash/smr`, { headers: bmcHeaders() }),
      api(`/api/bmc/flash/sbios`, { headers: bmcHeaders() }),
      api(`/api/bmc/flash/erot`, { headers: bmcHeaders() }),
    ]);
    panel.innerHTML = "";
    panel.appendChild(el("div", { class: "card flash-note-card" }, [
      el("h3", {}, "How to flash"),
      el("p", {}, [
        document.createTextNode("Upload a .zip or folder — the server unzips and lists every firmware file. "),
        document.createTextNode("Pick which one to flash. Progress appears in the panel on the right."),
      ]),
      el("p", { class: "muted" },
        "Flashes run in the background. Switch tabs freely — jobs do not restart."),
    ]));
    panel.appendChild(buildBmcFlashCard(bmc));
    panel.appendChild(buildFmlBundleFlashCard(fml));
    panel.appendChild(buildCx8FlashCard(cx8));
    panel.appendChild(buildInventoryFlashCard({
      title: "Flash SMR / FPGA",
      subtitle: "Upload a .zip, folder, or firmware file. Pick which one to flash. "
        + "Live progress shows in the right-hand panel.",
      d: smr,
      apiPath: "smr",
      currentLabel: "Current SMR Version",
      flashLabel: "SMR",
      btnText: "Flash SMR",
      toastOk: "SMR flash started.",
      fileAccept: ".bin,.apimage,.image",
    }));
    panel.appendChild(buildInventoryFlashCard({
      title: "Flash SBIOS / UEFI",
      subtitle: "Upload a .zip, folder, or firmware file. Pick which one to flash. "
        + "Live progress shows in the right-hand panel.",
      d: sbios,
      apiPath: "sbios",
      currentLabel: "Current SBIOS Version",
      flashLabel: "SBIOS",
      btnText: "Flash SBIOS",
      toastOk: "SBIOS flash started.",
      fileAccept: ".fwpkg",
    }));
    panel.appendChild(buildInventoryFlashCard({
      title: "Flash ERoT",
      subtitle: "Upload a .zip, folder, or firmware file. Pick which one to flash. "
        + "Live progress shows in the right-hand panel.",
      d: erot,
      apiPath: "erot",
      currentLabel: "Current ERoT Version",
      flashLabel: "ERoT",
      btnText: "Flash ERoT",
      toastOk: "ERoT flash started.",
    }));
    panel.appendChild(buildBmcVersionVerifyCard());
  } catch (err) {
    panel.innerHTML = `<div class="msg msg-err">${err.message}</div>`;
  }
}

function normalizeVersion(v) {
  return (v || "").trim().toLowerCase().replace(/_/g, "-");
}

/** Upload zip/folder/files → server unzips & scans → dropdown to pick which image to flash. */
function buildFlashUploadSection(prefix, accept = ".fwpkg,.image,.img,.bin,.tar") {
  const state = { bundleId: null, files: [] };
  const fileInputId = `${prefix}-file`;
  const folderInputId = `${prefix}-folder`;
  const selectId = `${prefix}-select`;

  const wrap = el("div", { class: "flash-upload-section" });
  const fileInput = el("input", {
    type: "file", id: fileInputId, accept: `${accept},.zip`, multiple: true,
  });
  fileInput.style.display = "none";
  const folderInput = el("input", { type: "file", id: folderInputId });
  folderInput.style.display = "none";
  folderInput.setAttribute("webkitdirectory", "");
  folderInput.setAttribute("directory", "");

  const statusEl = el("div", { class: "flash-stage-status muted" }, "No files selected.");
  const selectWrap = el("div", { class: "flash-file-select hidden" });
  const selectEl = el("select", { id: selectId, class: "flash-file-select-el" });
  selectWrap.appendChild(el("label", { class: "flash-file-select-label" }, [
    "File to flash",
    selectEl,
  ]));

  const pickRow = el("div", { class: "flash-pick-row" }, [
    el("button", {
      type: "button", class: "btn btn-ghost",
      onclick: () => { fileInput.value = ""; fileInput.click(); },
    }, "Choose file(s) or .zip"),
    el("button", {
      type: "button", class: "btn btn-ghost",
      onclick: () => { folderInput.value = ""; folderInput.click(); },
    }, "Choose folder"),
  ]);

  async function processUpload(fileList) {
    if (!fileList || !fileList.length) return;
    statusEl.textContent = "Uploading and scanning for firmware files…";
    selectWrap.classList.add("hidden");
    state.bundleId = null;
    state.files = [];
    selectEl.innerHTML = "";

    const fd = new FormData();
    const files = Array.from(fileList);
    const isZip = files.length === 1 && files[0].name.toLowerCase().endsWith(".zip");
    if (isZip) {
      fd.append("archive", files[0]);
    } else {
      for (const f of files) {
        const path = f.webkitRelativePath || f.name;
        fd.append("files", f, path);
      }
    }

    try {
      const res = await fetch("/api/bmc/flash/stage", {
        method: "POST", headers: bmcHeaders(), body: fd,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      state.bundleId = data.bundle_id;
      state.files = data.files || [];
      for (const f of state.files) {
        const mb = (f.size / (1024 * 1024)).toFixed(1);
        selectEl.appendChild(el("option", { value: f.id }, `${f.path} (${mb} MB)`));
      }
      if (state.files.length === 1) selectEl.selectedIndex = 0;
      selectWrap.classList.remove("hidden");
      statusEl.textContent = data.message || `Found ${state.files.length} firmware file(s).`;
    } catch (err) {
      statusEl.textContent = err.message;
      toast(err.message, "err");
    }
  }

  fileInput.addEventListener("change", () => processUpload(fileInput.files));
  folderInput.addEventListener("change", () => processUpload(folderInput.files));

  wrap.appendChild(pickRow);
  wrap.appendChild(statusEl);
  wrap.appendChild(selectWrap);
  wrap.appendChild(fileInput);
  wrap.appendChild(folderInput);

  return {
    wrap,
    getSelection() {
      if (!state.bundleId || !state.files.length) return null;
      const fileId = selectEl.value;
      const file = state.files.find((f) => f.id === fileId);
      if (!file) return null;
      return { bundle_id: state.bundleId, file_id: fileId, name: file.name, path: file.path };
    },
    appendToForm(body) {
      const sel = this.getSelection();
      if (!sel) return null;
      body.append("bundle_id", sel.bundle_id);
      body.append("file_id", sel.file_id);
      return sel;
    },
    hasSelection() {
      return Boolean(this.getSelection());
    },
  };
}

function buildBmcFlashCard(d) {
  const card = el("div", { class: "card" });
  card.appendChild(el("h3", {}, "Flash BMC"));
  card.appendChild(el("p", { class: "muted" },
    "Upload a .zip, folder, or firmware file(s). The server unzips and lists every "
    + ".fwpkg / .image / .bin found — pick which one to flash. Live progress shows "
    + "in the right-hand panel."));

  card.appendChild(el("div", { class: "kv", style: "display:inline-block;margin:12px 0 18px;" }, [
    el("div", { class: "k" }, "Current BMC Version"),
    el("div", { class: "v" }, d.current_version || "—"),
  ]));

  const picker = buildFlashUploadSection("flash-bmc", ".fwpkg,.image,.img,.bin,.tar");
  const form = el("form", { class: "flash-form", id: "flash-bmc-form" });
  form.appendChild(picker.wrap);
  const msg = el("div", { class: "msg hidden" });
  form.appendChild(msg);
  form.appendChild(el("div", { class: "flash-actions" }, [
    el("button", { type: "submit", class: "btn btn-danger" }, "Flash BMC"),
  ]));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!picker.hasSelection()) {
      return toast("Upload and choose a firmware file first.", "err");
    }
    const sel = picker.getSelection();
    if (!confirm(`Flash BMC with ${sel.path}?\n\nThe BMC may reboot to apply.`)) return;

    const btn = form.querySelector("button");
    btn.disabled = true;
    msg.className = "msg"; msg.textContent = "Starting BMC flash…"; msg.classList.remove("hidden");
    const url = `/api/bmc/flash/bmc`;
    try {
      const body = new FormData();
      picker.appendToForm(body);
      const init = { method: "POST", headers: bmcHeaders(), body };
      const result = await submitFlashJob({
        label: `BMC — ${sel.name}`,
        url,
        init,
      });
      msg.className = "msg msg-ok";
      msg.textContent = result.message || "Flash started — see progress panel.";
      toast("BMC flash started — track progress on the right.");
    } catch (err) {
      msg.className = "msg msg-err"; msg.textContent = err.message;
      toast(err.message, "err");
    } finally { btn.disabled = false; }
  });

  card.appendChild(form);
  return card;
}

function appendFlashTerminal(pre, command, output, bmcHost) {
  if (!pre) return;
  const ts = new Date().toLocaleTimeString();
  const block = [
    `[${ts}] ssh root@${bmcHost}`,
    `$ ${command}`,
    output || "(no output)",
    "",
  ].join("\n");
  pre.textContent = pre.textContent ? `${pre.textContent}\n${block}` : block;
  pre.scrollTop = pre.scrollHeight;
}

function buildBmcVersionVerifyCard() {
  const bmcHost = state.bmcIp || "bmc";
  const card = el("div", { class: "card flash-verify-card" });
  card.appendChild(el("h3", {}, "Verify BMC Version After Flash"));
  card.appendChild(el("p", { class: "muted" },
    "After a BMC flash finishes, the new firmware is not active until chassis power "
    + "is cycled. Use the Power tab first:"));
  card.appendChild(el("ul", { class: "flash-verify-steps muted" }, [
    el("li", {}, [
      "Press ",
      el("strong", {}, "Power Cycle"),
      " (runs ",
      el("code", {}, "stbypowerctrl.sh aux_cycle"),
      "), or",
    ]),
    el("li", {}, [
      "Press ",
      el("strong", {}, "Power Off"),
      " then ",
      el("strong", {}, "Power On"),
      " on the Power tab.",
    ]),
  ]));
  card.appendChild(el("p", { class: "muted" },
    "Then run the command below on the BMC. ",
    el("code", {}, "cat /etc/os-release"),
    " shows the running BMC version (VERSION_ID) — it should match the image you flashed."));

  const msg = el("div", { class: "msg hidden" });
  const versionEl = el("div", { class: "kv", style: "display:inline-block;margin:12px 0;" }, [
    el("div", { class: "k" }, "VERSION_ID"),
    el("div", { class: "v", id: "bmc-verify-version" }, "—"),
  ]);
  const termBody = el("pre", { class: "fpp-terminal-body", id: "bmc-verify-terminal" });

  const actions = el("div", { class: "flash-actions" }, [
    el("button", {
      type: "button",
      class: "btn",
      onclick: () => switchTab("power"),
    }, "Open Power Tab"),
    el("button", {
      type: "button",
      class: "btn btn-primary",
      id: "bmc-verify-btn",
      onclick: () => runBmcOsReleaseVerify(msg, versionEl, termBody),
    }, "Run cat /etc/os-release"),
  ]);

  card.appendChild(versionEl);
  card.appendChild(actions);
  card.appendChild(msg);
  card.appendChild(el("div", { class: "fpp-terminal-wrap", style: "margin-top:12px;" }, [
    el("div", { class: "fpp-terminal-head" }, `BMC terminal — ${bmcHost}`),
    termBody,
  ]));
  return card;
}

async function runBmcOsReleaseVerify(msg, versionEl, termBody) {
  const btn = $("#bmc-verify-btn");
  const bmcHost = state.bmcIp || "bmc";
  const command = "cat /etc/os-release";

  if (msg) {
    msg.className = "msg";
    msg.textContent = "Reading /etc/os-release on the BMC…";
    msg.classList.remove("hidden");
  }
  if (btn) btn.disabled = true;

  try {
    const d = await api(`/api/bmc/os-release`, { headers: bmcHeaders() });
    appendFlashTerminal(termBody, d.command || command, d.output || "", bmcHost);
    if (versionEl) {
      const verNode = versionEl.querySelector(".v") || versionEl;
      verNode.textContent = d.version_id || "—";
    }
    if (msg) {
      msg.className = "msg msg-ok";
      msg.textContent = d.version_id
        ? `BMC VERSION_ID: ${d.version_id}`
        : (d.message || "os-release read successfully.");
    }
    toast(d.version_id ? `BMC version: ${d.version_id}` : "os-release read.");
  } catch (err) {
    appendFlashTerminal(termBody, command, err.message, bmcHost);
    if (msg) {
      msg.className = "msg msg-err";
      msg.textContent = err.message;
    }
    toast(err.message, "err");
  } finally {
    if (btn) btn.disabled = false;
  }
}

function buildInventoryFlashCard(opts) {
  const {
    title, subtitle, d, apiPath, currentLabel, flashLabel, btnText, toastOk,
    fileAccept = ".bin,.apimage,.image",
  } = opts;
  const card = el("div", { class: "card" });
  card.appendChild(el("h3", {}, title));
  card.appendChild(el("p", { class: "muted" }, subtitle));

  card.appendChild(el("div", { class: "kv", style: "display:inline-block;margin:12px 0 18px;" }, [
    el("div", { class: "k" }, currentLabel),
    el("div", { class: "v" }, d.current_version || "—"),
  ]));

  const picker = buildFlashUploadSection(`flash-${apiPath}`, fileAccept);
  const form = el("form", { class: "flash-form" });
  form.appendChild(picker.wrap);
  const msg = el("div", { class: "msg hidden" });
  form.appendChild(msg);
  form.appendChild(el("div", { class: "flash-actions" }, [
    el("button", { type: "submit", class: "btn btn-danger" }, btnText),
  ]));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!picker.hasSelection()) {
      return toast("Upload and choose a firmware file first.", "err");
    }
    const sel = picker.getSelection();
    if (!confirm(`Flash ${flashLabel} with ${sel.path}?`)) return;

    const btn = form.querySelector("button");
    btn.disabled = true;
    msg.className = "msg"; msg.textContent = `Starting ${flashLabel} flash…`; msg.classList.remove("hidden");
    const url = `/api/bmc/flash/${apiPath}`;
    try {
      const body = new FormData();
      body.append("force", "true");
      picker.appendToForm(body);
      const init = { method: "POST", headers: bmcHeaders(), body };
      const result = await submitFlashJob({
        label: `${flashLabel} — ${sel.name}`,
        url,
        init,
      });
      msg.className = "msg msg-ok";
      msg.textContent = result.message || "Flash started — see progress panel.";
      toast(toastOk || `${flashLabel} flash started.`);
    } catch (err) {
      msg.className = "msg msg-err"; msg.textContent = err.message;
      toast(err.message, "err");
    } finally { btn.disabled = false; }
  });

  card.appendChild(form);
  return card;
}

function buildFmlBundleFlashCard(d) {
  const card = el("div", { class: "card" });
  card.appendChild(el("h3", {}, "Flash FML Bundle"));
  card.appendChild(el("p", { class: "muted" },
    "Upload a .zip, folder, or firmware file. Pick which one to flash. "
    + "Updates all components in one operation. Live progress shows in the right-hand panel."));

  const picker = buildFlashUploadSection("flash-fml", ".fwpkg,.bin");
  const form = el("form", { class: "flash-form" });
  form.appendChild(picker.wrap);
  const msg = el("div", { class: "msg hidden" });
  form.appendChild(msg);
  form.appendChild(el("div", { class: "flash-actions" }, [
    el("button", { type: "submit", class: "btn btn-danger" }, "Flash FML Bundle"),
  ]));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!picker.hasSelection()) {
      return toast("Upload and choose an FML bundle file first.", "err");
    }
    const sel = picker.getSelection();
    if (!confirm(
      `Flash FML bundle ${sel.path}?\n\n`
      + "The system may reboot and the connection will drop."
    )) return;

    const btn = form.querySelector("button");
    btn.disabled = true;
    msg.className = "msg"; msg.textContent = "Uploading FML bundle…"; msg.classList.remove("hidden");
    const url = `/api/bmc/flash/fml`;
    try {
      const body = new FormData();
      picker.appendToForm(body);
      const init = { method: "POST", headers: bmcHeaders(), body };
      const result = await submitFlashJob({ label: `FML — ${sel.name}`, url, init });
      msg.className = "msg msg-ok";
      msg.textContent = result.message || "Flash started — see progress panel.";
      toast("FML bundle flash started.");
    } catch (err) {
      msg.className = "msg msg-err"; msg.textContent = err.message;
      toast(err.message, "err");
    } finally { btn.disabled = false; }
  });

  card.appendChild(form);
  return card;
}

function buildCx8FlashCard(d) {
  const card = el("div", { class: "card" });
  card.appendChild(el("h3", {}, "Flash ConnectX8 (CX8)"));
  card.appendChild(el("p", { class: "muted" },
    "Upload a .zip, folder, or firmware file. Choose PK or QP for your board, "
    + "pick which file to flash. Live progress shows in the right-hand panel."));

  const form = el("form", { class: "flash-form" });
  const targetWrap = el("div", { class: "flash-targets" });
  let selected = d.targets[0]?.id || "";

  for (const t of d.targets) {
    const radio = el("input", {
      type: "radio", name: "cx8-target", value: t.id, id: `cx8-${t.id}`,
    });
    if (t.id === selected) radio.checked = true;
    targetWrap.appendChild(el("label", { class: "flash-option", for: `cx8-${t.id}` }, [
      radio,
      el("div", { class: "flash-option-body" }, [
        el("div", { class: "flash-option-title" }, t.label),
      ]),
    ]));
  }
  form.appendChild(targetWrap);

  const picker = buildFlashUploadSection("flash-cx8", ".fwpkg");
  form.appendChild(picker.wrap);

  const msg = el("div", { class: "msg hidden" });
  form.appendChild(msg);
  form.appendChild(el("div", { class: "flash-actions" }, [
    el("button", { type: "submit", class: "btn btn-danger" }, "Flash CX8"),
  ]));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const picked = form.querySelector('input[name="cx8-target"]:checked');
    const target = d.targets.find((t) => t.id === (picked && picked.value));
    if (!target) return toast("Select PK or QP CX8 package.", "err");

    if (!picker.hasSelection()) {
      return toast("Upload and choose a CX8 .fwpkg first.", "err");
    }
    const sel = picker.getSelection();
    if (!confirm(`Flash CX8 (${target.label}) with ${sel.path}?`)) return;

    const btn = form.querySelector("button");
    btn.disabled = true;
    msg.className = "msg"; msg.textContent = "Starting CX8 flash…"; msg.classList.remove("hidden");
    const url = `/api/bmc/flash/cx8`;
    try {
      const body = new FormData();
      body.append("target_id", target.id);
      body.append("force", "true");
      picker.appendToForm(body);
      const init = { method: "POST", headers: bmcHeaders(), body };
      const result = await submitFlashJob({
        label: `CX8 ${target.variant.toUpperCase()} — ${sel.name}`,
        url,
        init,
      });
      msg.className = "msg msg-ok";
      msg.textContent = result.message || "Flash started — see progress panel.";
      toast(`CX8 ${target.variant} flash started.`);
    } catch (err) {
      msg.className = "msg msg-err"; msg.textContent = err.message;
      toast(err.message, "err");
    } finally { btn.disabled = false; }
  });

  card.appendChild(form);
  return card;
}


// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
showLogin();
