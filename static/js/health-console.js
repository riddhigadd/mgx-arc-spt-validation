"use strict";

(() => {
  const ARC = "/api/arc";
  const TARGET_KEY = "mgxArcHealthTarget";
  const RECOVERY_KEY = "mgxArcRecoveryJob";
  const TERMINAL_JOB_STATES = new Set(["complete", "failed", "cancelled"]);
  const SENSITIVE_KEY = /(password|passwd|secret|token|authorization|credential)/i;

  const hc = {
    activeTab: "targets",
    targets: [],
    selectedTargetId: localStorage.getItem(TARGET_KEY) || "",
    snapshots: [],
    issues: [],
    results: {},
    recovery: readStoredRecovery(),
    recoveryTimer: null,
  };

  const one = (selector, root = document) => root.querySelector(selector);
  const all = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  function node(tag, attrs = {}, children = []) {
    const element = document.createElement(tag);
    Object.entries(attrs).forEach(([key, value]) => {
      if (value == null) return;
      if (key === "class") element.className = value;
      else if (key === "text") element.textContent = String(value);
      else if (key.startsWith("on") && typeof value === "function") {
        element.addEventListener(key.slice(2), value);
      } else if (key === "disabled") {
        element.disabled = Boolean(value);
      } else {
        element.setAttribute(key, String(value));
      }
    });
    [].concat(children).forEach((child) => {
      if (child == null) return;
      element.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
    });
    return element;
  }

  function replace(element, children = []) {
    element.replaceChildren(...[].concat(children).filter(Boolean));
  }

  function selectedTarget() {
    return hc.targets.find((target) => target.id === hc.selectedTargetId) || null;
  }

  function display(value, fallback = "—") {
    return value === undefined || value === null || value === "" ? fallback : String(value);
  }

  function pretty(value) {
    return JSON.stringify(redact(value), null, 2);
  }

  function redact(value) {
    if (Array.isArray(value)) return value.map(redact);
    if (!value || typeof value !== "object") return value;
    const clean = {};
    Object.entries(value).forEach(([key, item]) => {
      clean[key] = SENSITIVE_KEY.test(key) ? "***" : redact(item);
    });
    return clean;
  }

  async function arcApi(path, options = {}) {
    const init = { ...options, headers: { Accept: "application/json", ...(options.headers || {}) } };
    if (options.body !== undefined) init.headers["Content-Type"] = "application/json";
    const response = await fetch(`${ARC}${path}`, init);
    let payload = null;
    try {
      payload = await response.json();
    } catch (_) {
      payload = {};
    }
    if (!response.ok) {
      const error = new Error(payload.error || `HTTP ${response.status}`);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  function isNotConfigured(error) {
    return error?.status === 424
      || /not_configured|config_error|source_not_supported/i.test(error?.message || "");
  }

  function badge(status, label) {
    const normalized = ["pass", "fail", "not-configured", "running", "info"].includes(status)
      ? status
      : "info";
    return node("span", { class: `hc-badge hc-${normalized}` }, label || status);
  }

  function setApiBadge(status, text) {
    const target = one("#health-api-status");
    if (!target) return;
    target.className = `hc-badge hc-${status}`;
    target.textContent = text;
  }

  function message(text, type = "info") {
    return node("div", { class: `hc-message hc-message-${type}` }, text);
  }

  function errorMessage(error) {
    if (isNotConfigured(error)) {
      return message("Not configured. Replace the TODO placeholders and enable this capability in server configuration.", "warn");
    }
    return message(error?.message || "Request failed.", "error");
  }

  function field(label, value) {
    return node("div", { class: "hc-field" }, [
      node("span", { class: "hc-field-label" }, label),
      node("strong", { class: "hc-field-value" }, display(value)),
    ]);
  }

  function button(label, onClick, options = {}) {
    return node("button", {
      type: "button",
      class: `btn ${options.primary ? "btn-primary" : "btn-ghost"} ${options.class || ""}`.trim(),
      disabled: options.disabled,
      onclick: onClick,
    }, label);
  }

  function select(options, value, attrs = {}) {
    const element = node("select", attrs);
    options.forEach((option) => {
      const optionNode = node("option", { value: option.value }, option.label);
      optionNode.selected = String(option.value) === String(value);
      element.appendChild(optionNode);
    });
    return element;
  }

  function resultActions(title, value) {
    const safeValue = redact(value);
    const base = `mgx-arc-${title.toLowerCase().replace(/[^a-z0-9]+/g, "-")}-${new Date()
      .toISOString().slice(0, 19).replace(/[:T]/g, "-")}`;
    return node("div", { class: "hc-report-actions" }, [
      button("Download JSON", () => download(`${base}.json`, pretty(safeValue), "application/json")),
      button("Download text", () => download(`${base}.txt`, toTextReport(title, safeValue), "text/plain")),
    ]);
  }

  function download(filename, content, type) {
    if (type === "text/plain" && typeof downloadTextReport === "function") {
      downloadTextReport(filename, content);
      return;
    }
    const url = URL.createObjectURL(new Blob([content], { type: `${type};charset=utf-8` }));
    const anchor = node("a", { href: url, download: filename });
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  }

  function toTextReport(title, value) {
    return [
      title,
      "=".repeat(title.length),
      `Generated: ${new Date().toLocaleString()}`,
      `Target: ${hc.selectedTargetId || "all"}`,
      "",
      pretty(value),
    ].join("\n");
  }

  function loading(label = "Loading…") {
    return message(label, "info");
  }

  function rawAndParsed(snapshot) {
    const normalized = snapshot?.normalized || {};
    const collectorStatus = normalized.errors?.length ? "fail" : "pass";
    const wrapper = node("div", { class: "hc-output" });
    wrapper.appendChild(node("div", { class: "hc-output-head" }, [
      badge(collectorStatus, collectorStatus === "pass" ? "Pass" : "Partial / Fail"),
      node("span", { class: "muted" }, `${display(snapshot?.kind, "collector")} · ${display(snapshot?.created_at)}`),
    ]));
    wrapper.appendChild(renderRows(normalized.items || []));
    wrapper.appendChild(node("details", { class: "hc-details" }, [
      node("summary", {}, "Raw output"),
      node("pre", { class: "hc-raw" }, pretty(snapshot?.raw || {})),
    ]));
    wrapper.appendChild(resultActions(`${snapshot?.kind || "collector"} report`, snapshot));
    return wrapper;
  }

  function renderRows(rows) {
    if (!Array.isArray(rows) || !rows.length) return message("No parsed rows were reported.", "warn");
    const keys = Array.from(rows.reduce((set, row) => {
      if (row && typeof row === "object") Object.keys(row).slice(0, 8).forEach((key) => set.add(key));
      return set;
    }, new Set())).slice(0, 8);
    const table = node("table", { class: "hc-table" });
    table.appendChild(node("thead", {}, node("tr", {}, keys.map((key) => node("th", {}, key)))));
    const body = node("tbody");
    rows.forEach((row) => {
      body.appendChild(node("tr", {}, keys.map((key) => {
        const value = row?.[key];
        return node("td", {}, typeof value === "object" ? pretty(value) : display(value));
      })));
    });
    table.appendChild(body);
    return node("div", { class: "hc-table-scroll" }, table);
  }

  function emptyTarget() {
    return message("No configured target is available. The legacy Direct Connect console remains usable.", "warn");
  }

  async function initialize() {
    bindNavigation();
    try {
      const status = await arcApi("/");
      if (!status.ok && status.config_error) throw Object.assign(new Error(status.config_error), { status: 503 });
      setApiBadge("pass", `${status.targets || 0} targets`);
      await loadTargets();
      if (hc.recovery?.jobId) beginRecoveryPolling();
    } catch (error) {
      hc.targets = [];
      updateTargetSelector();
      setApiBadge("not-configured", "Not configured");
      renderCurrentPanel(error);
    }
  }

  function bindNavigation() {
    one("#health-console-btn")?.addEventListener("click", () => {
      one("#login-view")?.classList.add("hidden");
      one("#dashboard-view")?.classList.add("hidden");
      one("#health-console-view")?.classList.remove("hidden");
      one("#health-console-btn")?.classList.add("active");
      one("#direct-console-btn")?.classList.remove("active");
      renderCurrentPanel();
    });
    all("[data-health-tab]").forEach((tab) => {
      tab.addEventListener("click", () => switchHealthTab(tab.dataset.healthTab));
    });
    one("#health-refresh-btn")?.addEventListener("click", async () => {
      try {
        await loadTargets();
        renderCurrentPanel();
      } catch (error) {
        setApiBadge(isNotConfigured(error) ? "not-configured" : "fail", "Unavailable");
        renderCurrentPanel(error);
      }
    });
    one("#health-target-select")?.addEventListener("change", (event) => {
      hc.selectedTargetId = event.target.value;
      localStorage.setItem(TARGET_KEY, hc.selectedTargetId);
      renderCurrentPanel();
    });
  }

  async function loadTargets() {
    const response = await arcApi("/targets");
    hc.targets = Array.isArray(response.items) ? response.items : [];
    if (!hc.targets.some((target) => target.id === hc.selectedTargetId)) {
      hc.selectedTargetId = hc.targets[0]?.id || "";
    }
    if (hc.selectedTargetId) localStorage.setItem(TARGET_KEY, hc.selectedTargetId);
    else localStorage.removeItem(TARGET_KEY);
    updateTargetSelector();
    setApiBadge(hc.targets.length ? "pass" : "not-configured", `${hc.targets.length} targets`);
  }

  function updateTargetSelector() {
    const targetSelect = one("#health-target-select");
    if (!targetSelect) return;
    replace(targetSelect, hc.targets.length
      ? hc.targets.map((target) => {
        const option = node("option", { value: target.id }, `${target.label || target.id} (${target.id})`);
        option.selected = target.id === hc.selectedTargetId;
        return option;
      })
      : node("option", { value: "" }, "No targets"));
    targetSelect.disabled = !hc.targets.length;
  }

  function switchHealthTab(name) {
    hc.activeTab = name;
    all("[data-health-tab]").forEach((tab) => tab.classList.toggle("active", tab.dataset.healthTab === name));
    all(".health-tab-panel").forEach((panel) => panel.classList.add("hidden"));
    one(`#health-panel-${name}`)?.classList.remove("hidden");
    renderCurrentPanel();
  }

  function renderCurrentPanel(initialError) {
    const panel = one(`#health-panel-${hc.activeTab}`);
    if (!panel) return;
    if (initialError) {
      replace(panel, [
        node("div", { class: "card" }, [
          node("h3", {}, "Fleet health is unavailable"),
          errorMessage(initialError),
          node("p", { class: "muted" }, "This does not affect the Direct Connect console."),
        ]),
      ]);
      return;
    }
    const renderers = {
      targets: renderTargets,
      mctp: () => renderCollectorPanel("mctp", "MCTP endpoint health", `/targets/${encodeURIComponent(hc.selectedTargetId)}/mctp`),
      inventory: () => renderCollectorPanel("inventory", "FRU / Redfish inventory", `/targets/${encodeURIComponent(hc.selectedTargetId)}/inventory`),
      "firmware-compare": renderFirmwareCompare,
      recovery: renderRecovery,
      "snapshots-issues": renderSnapshotsIssues,
    };
    renderers[hc.activeTab]?.(panel);
  }

  function renderTargets(panel) {
    const targetGrid = node("div", { class: "hc-target-grid" });
    hc.targets.forEach((target) => {
      const configured = target.configured || {};
      const capabilities = target.capabilities || {};
      const card = node("article", {
        class: `card hc-target-card ${target.id === hc.selectedTargetId ? "selected" : ""}`,
      });
      card.appendChild(node("div", { class: "hc-card-heading" }, [
        node("div", {}, [
          node("h3", {}, target.label || target.id),
          node("p", { class: "muted" }, target.id),
        ]),
        badge(configured.bmc ? "pass" : "not-configured", configured.bmc ? "BMC configured" : "Not configured"),
      ]));
      card.appendChild(node("div", { class: "hc-field-grid" }, [
        field("BMC", target.bmc_host),
        field("Host", target.os_host),
        field("Revision", target.revision),
        field("Profile", target.capability_profile),
      ]));
      const statusArea = node("div", { class: "hc-inline-result" });
      card.appendChild(node("div", { class: "hc-actions" }, [
        button("Select", () => {
          hc.selectedTargetId = target.id;
          localStorage.setItem(TARGET_KEY, target.id);
          updateTargetSelector();
          renderTargets(panel);
        }, { primary: target.id !== hc.selectedTargetId }),
        button("Get status", () => runTargetStatus(target, statusArea, false), { disabled: !configured.bmc }),
        button("Test connections", () => runTargetStatus(target, statusArea, true)),
      ]));
      card.appendChild(statusArea);
      if (!configured.bmc && !configured.host) {
        card.appendChild(message("TODO placeholders detected; connection-dependent actions are disabled.", "warn"));
      } else if (!capabilities) {
        card.appendChild(message("No capability profile is assigned.", "warn"));
      }
      targetGrid.appendChild(card);
    });

    const actionCard = buildSelectedTargetActions();
    replace(panel, [
      node("div", { class: "hc-section-heading" }, [
        node("div", {}, [node("h2", {}, "Targets"), node("p", { class: "muted" }, "Select a configured system and test its managed connections.")]),
      ]),
      hc.targets.length ? targetGrid : emptyTarget(),
      actionCard,
    ]);
  }

  async function runTargetStatus(target, area, test) {
    replace(area, loading(test ? "Testing BMC and host connections…" : "Checking reachability…"));
    try {
      const path = `/targets/${encodeURIComponent(target.id)}/${test ? "test" : "status"}`;
      const result = await arcApi(path, { method: test ? "POST" : "GET" });
      const connection = result.connection || result;
      const states = [connection.bmc, connection.host].filter(Boolean);
      const failed = states.some((value) => !["connected", "reachable"].includes(value));
      replace(area, [
        badge(failed ? (states.every((value) => value === "not_configured") ? "not-configured" : "fail") : "pass",
          failed ? "Attention" : "Pass"),
        node("pre", { class: "hc-raw hc-raw-compact" }, pretty(result)),
      ]);
    } catch (error) {
      replace(area, [badge(isNotConfigured(error) ? "not-configured" : "fail"), errorMessage(error)]);
    }
  }

  function buildSelectedTargetActions() {
    const target = selectedTarget();
    const card = node("section", { class: "card" }, [
      node("h3", {}, "Selected target actions"),
      node("p", { class: "muted" }, "Power and collectors below use only the selected configuration target ID."),
    ]);
    if (!target) {
      card.appendChild(emptyTarget());
      return card;
    }
    const result = node("div", { class: "hc-action-result" });
    const powerActions = ["on", "off", "graceful_shutdown", "restart", "graceful_restart", "power_cycle"];
    const actionSelect = select(powerActions.map((value) => ({ value, label: value.replaceAll("_", " ") })), "restart", {
      "aria-label": "Power action",
    });
    const usbSources = target.capabilities?.usb_sources || [];
    const sourceSelect = select(usbSources.map((value) => ({ value, label: `USB from ${value}` })), usbSources[0] || "", {
      "aria-label": "USB source",
      disabled: !usbSources.length,
    });
    card.appendChild(node("div", { class: "hc-actions hc-actions-wrap" }, [
      button("Power status", () => runPower(result), { disabled: !target.configured?.bmc }),
      actionSelect,
      button("Run power action", () => runPower(result, actionSelect.value), { disabled: !target.capabilities?.redfish_power }),
      sourceSelect,
      button("Collect USB", () => runQuickCollector(result, "usb", { source: sourceSelect.value }), {
        disabled: !usbSources.length,
      }),
      button("Collect I2C health", () => runQuickCollector(result, "i2c"), {
        disabled: !target.capabilities?.i2c,
      }),
    ]));
    card.appendChild(result);
    return card;
  }

  async function runPower(area, action) {
    const id = encodeURIComponent(hc.selectedTargetId);
    replace(area, loading(action ? `Running ${action}…` : "Reading power status…"));
    try {
      const result = await arcApi(`/targets/${id}/power`, action
        ? { method: "POST", body: JSON.stringify({ action }) }
        : {});
      replace(area, [
        badge("pass", action ? "Action accepted" : display(result.power_state)),
        node("pre", { class: "hc-raw" }, pretty(result)),
        resultActions("power", result),
      ]);
    } catch (error) {
      replace(area, [badge(isNotConfigured(error) ? "not-configured" : "fail"), errorMessage(error)]);
    }
  }

  async function runQuickCollector(area, kind, data) {
    const id = encodeURIComponent(hc.selectedTargetId);
    const endpoint = kind === "usb" ? `/targets/${id}/usb` : `/targets/${id}/i2c/health`;
    replace(area, loading(`Collecting ${kind.toUpperCase()}…`));
    try {
      const result = await arcApi(endpoint, {
        method: "POST",
        body: JSON.stringify(data || {}),
      });
      replace(area, rawAndParsed(result.snapshot));
    } catch (error) {
      replace(area, [badge(isNotConfigured(error) ? "not-configured" : "fail"), errorMessage(error)]);
    }
  }

  function renderCollectorPanel(key, title, endpoint) {
    const panel = one(`#health-panel-${key}`);
    const target = selectedTarget();
    const output = node("div", { class: "hc-action-result" });
    const configured = key === "mctp" ? target?.capabilities?.mctp : target?.capabilities?.fru_redfish;
    const card = node("section", { class: "card" }, [
      node("div", { class: "hc-card-heading" }, [
        node("div", {}, [node("h3", {}, title), node("p", { class: "muted" }, `Target: ${target?.label || "none"}`)]),
        configured ? badge("info", "Ready") : badge("not-configured", "Not configured"),
      ]),
    ]);
    card.appendChild(button("Run collector", async () => {
      replace(output, loading(`Collecting ${title.toLowerCase()}…`));
      try {
        const response = await arcApi(endpoint, { method: "POST", body: JSON.stringify({}) });
        hc.results[key] = response.snapshot;
        replace(output, rawAndParsed(response.snapshot));
      } catch (error) {
        replace(output, [badge(isNotConfigured(error) ? "not-configured" : "fail"), errorMessage(error)]);
      }
    }, { primary: true, disabled: !target || !configured }));
    card.appendChild(output);
    if (hc.results[key]) replace(output, rawAndParsed(hc.results[key]));
    replace(panel, card);
  }

  async function loadSnapshots() {
    const response = await arcApi("/snapshots?limit=500");
    hc.snapshots = Array.isArray(response.items) ? response.items : [];
  }

  function snapshotOptions(kind) {
    return hc.snapshots
      .filter((snapshot) => !kind || snapshot.kind === kind)
      .map((snapshot) => ({
        value: snapshot.id,
        label: `${snapshot.target_id} · ${snapshot.kind} · ${new Date(snapshot.created_at).toLocaleString()}`,
      }));
  }

  async function renderFirmwareCompare(panel) {
    replace(panel, loading("Loading firmware snapshots…"));
    try {
      await loadSnapshots();
    } catch (error) {
      replace(panel, errorMessage(error));
      return;
    }
    const output = node("div", { class: "hc-action-result" });
    const options = snapshotOptions("firmware");
    const fromSelect = select([{ value: "", label: "From snapshot…" }, ...options], "");
    const toSelect = select([{ value: "", label: "To snapshot…" }, ...options], "");
    const target = selectedTarget();
    const card = node("section", { class: "card" }, [
      node("h3", {}, "Firmware collection and comparison"),
      node("p", { class: "muted" }, "Collect firmware for the selected target, then compare two firmware snapshots."),
      node("div", { class: "hc-actions hc-actions-wrap" }, [
        button("Collect firmware", async () => {
          replace(output, loading("Collecting firmware inventory…"));
          try {
            const response = await arcApi(`/targets/${encodeURIComponent(hc.selectedTargetId)}/firmware`, {
              method: "POST",
              body: JSON.stringify({}),
            });
            hc.results.firmware = response.snapshot;
            await loadSnapshots();
            renderFirmwareCompare(panel);
          } catch (error) {
            replace(output, [badge(isNotConfigured(error) ? "not-configured" : "fail"), errorMessage(error)]);
          }
        }, { primary: true, disabled: !target?.configured?.bmc }),
        fromSelect,
        toSelect,
        button("Compare", async () => {
          if (!fromSelect.value || !toSelect.value) {
            replace(output, message("Choose both firmware snapshots.", "warn"));
            return;
          }
          replace(output, loading("Comparing firmware…"));
          try {
            const result = await arcApi("/firmware/compare", {
              method: "POST",
              body: JSON.stringify({ from: fromSelect.value, to: toSelect.value }),
            });
            replace(output, renderComparison(result, "firmware comparison"));
          } catch (error) {
            replace(output, [badge("fail"), errorMessage(error)]);
          }
        }),
      ]),
      output,
    ]);
    if (hc.results.firmware) replace(output, rawAndParsed(hc.results.firmware));
    replace(panel, card);
  }

  function renderComparison(result, title) {
    const summary = result.summary || {};
    return node("div", { class: "hc-output" }, [
      node("div", { class: "hc-output-head" }, [
        badge(result.changed === false || (!summary.added && !summary.removed && !summary.changed) ? "pass" : "info",
          result.changed === false ? "No changes" : "Compared"),
        node("span", { class: "muted" }, `Added ${summary.added || 0} · Removed ${summary.removed || 0} · Changed ${summary.changed || 0}`),
      ]),
      node("pre", { class: "hc-raw" }, pretty(result)),
      resultActions(title, result),
    ]);
  }

  function readStoredRecovery() {
    try {
      const value = JSON.parse(localStorage.getItem(RECOVERY_KEY) || "null");
      return value && value.jobId ? value : null;
    } catch (_) {
      return null;
    }
  }

  function renderRecovery(panel) {
    const target = selectedTarget();
    const enabled = Boolean(target?.configured?.recovery && target?.capabilities?.mcu_recovery);
    const moduleSelect = select(
      enabled
        ? [{ value: "configured", label: "Configured MCU recovery module" }]
        : [{ value: "", label: "No recovery module configured" }],
      enabled ? "configured" : "",
      { disabled: !enabled, "aria-label": "Recovery module" },
    );
    const output = node("div", { id: "hc-recovery-output", class: "hc-action-result" });
    const card = node("section", { class: "card" }, [
      node("div", { class: "hc-card-heading" }, [
        node("div", {}, [
          node("h3", {}, "MCU recovery"),
          node("p", { class: "muted" }, "Runs only the trusted recovery command configured on the server."),
        ]),
        badge(enabled ? "info" : "not-configured", enabled ? "Configured" : "Not configured"),
      ]),
      node("div", { class: "hc-actions hc-actions-wrap" }, [
        moduleSelect,
        button("Start recovery", async () => {
          if (!confirm(`Start configured MCU recovery for ${target.label || target.id}?`)) return;
          replace(output, loading("Starting recovery…"));
          try {
            const response = await arcApi(`/targets/${encodeURIComponent(target.id)}/recovery`, {
              method: "POST",
              body: JSON.stringify({}),
            });
            hc.recovery = { jobId: response.job.id, targetId: target.id };
            localStorage.setItem(RECOVERY_KEY, JSON.stringify(hc.recovery));
            beginRecoveryPolling();
          } catch (error) {
            replace(output, [badge(isNotConfigured(error) ? "not-configured" : "fail"), errorMessage(error)]);
          }
        }, { primary: true, disabled: !enabled }),
      ]),
      output,
    ]);
    if (!enabled) card.appendChild(message("Recovery remains explicitly disabled until both the capability and command are configured.", "warn"));
    replace(panel, card);
    if (hc.recovery?.jobId) pollRecovery();
  }

  function beginRecoveryPolling() {
    if (hc.recoveryTimer) clearInterval(hc.recoveryTimer);
    pollRecovery();
    hc.recoveryTimer = setInterval(pollRecovery, 1500);
  }

  async function pollRecovery() {
    if (!hc.recovery?.jobId) return;
    try {
      const [job, logResponse] = await Promise.all([
        arcApi(`/recovery/jobs/${encodeURIComponent(hc.recovery.jobId)}`),
        arcApi(`/recovery/jobs/${encodeURIComponent(hc.recovery.jobId)}/log`),
      ]);
      hc.recovery.job = job;
      hc.recovery.logs = logResponse.items || [];
      localStorage.setItem(RECOVERY_KEY, JSON.stringify({
        jobId: hc.recovery.jobId,
        targetId: hc.recovery.targetId,
      }));
      const output = one("#hc-recovery-output");
      if (output) {
        const status = job.status === "complete" ? "pass"
          : job.status === "failed" || job.status === "cancelled" ? "fail" : "running";
        replace(output, [
          node("div", { class: "hc-output-head" }, [
            badge(status, job.status),
            node("span", { class: "muted" }, `${job.percent || 0}% · ${display(job.message)}`),
          ]),
          node("progress", { max: "100", value: job.percent || 0, class: "hc-progress" }),
          node("pre", { class: "hc-raw hc-log" }, (hc.recovery.logs || []).map((entry) => entry.line).join("\n")),
          resultActions("recovery job", { job, logs: hc.recovery.logs }),
        ]);
      }
      if (TERMINAL_JOB_STATES.has(job.status) && hc.recoveryTimer) {
        clearInterval(hc.recoveryTimer);
        hc.recoveryTimer = null;
      }
    } catch (error) {
      const output = one("#hc-recovery-output");
      if (output) replace(output, errorMessage(error));
      if (error.status === 404) {
        localStorage.removeItem(RECOVERY_KEY);
        hc.recovery = null;
      }
    }
  }

  async function renderSnapshotsIssues(panel) {
    replace(panel, loading("Loading snapshots and issues…"));
    try {
      await Promise.all([loadSnapshots(), loadIssues()]);
    } catch (error) {
      replace(panel, errorMessage(error));
      return;
    }
    replace(panel, [buildSnapshotsCard(), buildIssuesCard()]);
  }

  function buildSnapshotsCard() {
    const output = node("div", { class: "hc-action-result" });
    const options = snapshotOptions();
    const first = select([{ value: "", label: "First snapshot…" }, ...options], "");
    const second = select([{ value: "", label: "Second snapshot…" }, ...options], "");
    const kind = select([
      "aggregate", "usb", "mctp", "i2c", "inventory", "firmware",
    ].map((value) => ({ value, label: value })), "aggregate");
    return node("section", { class: "card" }, [
      node("h3", {}, "Snapshots"),
      node("p", { class: "muted" }, "Select any snapshots. Same-target/same-kind pairs use the API; cross-system pairs use a read-only client comparison."),
      node("div", { class: "hc-actions hc-actions-wrap" }, [
        kind,
        button("Capture selected target", async () => {
          replace(output, loading(`Creating ${kind.value} snapshot…`));
          try {
            const result = await arcApi("/snapshots", {
              method: "POST",
              body: JSON.stringify({ target_id: hc.selectedTargetId, kind: kind.value }),
            });
            replace(output, [
              badge(result.job ? "running" : "pass", result.job ? "Queued" : "Captured"),
              node("pre", { class: "hc-raw" }, pretty(result)),
              resultActions("snapshot capture", result),
            ]);
          } catch (error) {
            replace(output, [badge(isNotConfigured(error) ? "not-configured" : "fail"), errorMessage(error)]);
          }
        }, { primary: true, disabled: !selectedTarget() }),
        first,
        second,
        button("Compare snapshots", () => compareSnapshots(first.value, second.value, output)),
      ]),
      output,
      renderSnapshotList(),
    ]);
  }

  function renderSnapshotList() {
    if (!hc.snapshots.length) return message("No snapshots have been captured.", "warn");
    const rows = hc.snapshots.slice(0, 100).map((snapshot) => ({
      target: snapshot.target_id,
      kind: snapshot.kind,
      created: new Date(snapshot.created_at).toLocaleString(),
      id: snapshot.id,
    }));
    return node("details", { class: "hc-details" }, [
      node("summary", {}, `Recent snapshots (${hc.snapshots.length})`),
      renderRows(rows),
    ]);
  }

  async function compareSnapshots(firstId, secondId, output) {
    if (!firstId || !secondId) {
      replace(output, message("Choose two snapshots.", "warn"));
      return;
    }
    replace(output, loading("Comparing snapshots…"));
    const first = hc.snapshots.find((item) => item.id === firstId);
    const second = hc.snapshots.find((item) => item.id === secondId);
    try {
      let result;
      if (first?.kind === second?.kind && first?.target_id === second?.target_id) {
        result = await arcApi("/snapshots/compare", {
          method: "POST",
          body: JSON.stringify({ from: firstId, to: secondId }),
        });
      } else {
        const [left, right] = await Promise.all([
          arcApi(`/snapshots/${encodeURIComponent(firstId)}`),
          arcApi(`/snapshots/${encodeURIComponent(secondId)}`),
        ]);
        result = {
          comparison: "client_structural",
          reason: "The backend compare route requires matching target and kind.",
          from: { id: left.id, target_id: left.target_id, kind: left.kind },
          to: { id: right.id, target_id: right.target_id, kind: right.kind },
          changed: pretty(left.normalized) !== pretty(right.normalized),
          before: left.normalized,
          after: right.normalized,
        };
      }
      replace(output, renderComparison(result, "snapshot comparison"));
    } catch (error) {
      replace(output, [badge("fail"), errorMessage(error)]);
    }
  }

  async function loadIssues(filters = {}) {
    const params = new URLSearchParams({ limit: "500" });
    if (filters.target) params.set("target_id", filters.target);
    if (filters.status) params.set("status", filters.status);
    const response = await arcApi(`/issues?${params.toString()}`);
    hc.issues = Array.isArray(response.items) ? response.items : [];
  }

  function issueCategory(issue) {
    return issue.extra?.category || String(issue.code || "uncategorized").split(/[._:-]/)[0] || "uncategorized";
  }

  function buildIssuesCard() {
    const list = node("div", { class: "hc-issue-list" });
    const targetFilter = select([
      { value: "", label: "All targets" },
      ...hc.targets.map((target) => ({ value: target.id, label: target.label || target.id })),
    ], "");
    const severityFilter = select([
      { value: "", label: "All severities" }, ...["info", "warning", "error", "critical"].map((value) => ({ value, label: value })),
    ], "");
    const categories = Array.from(new Set(hc.issues.map(issueCategory))).sort();
    const categoryFilter = select([
      { value: "", label: "All categories" }, ...categories.map((value) => ({ value, label: value })),
    ], "");
    const statusFilter = select([
      { value: "", label: "All statuses" }, ...["open", "acknowledged", "resolved"].map((value) => ({ value, label: value })),
    ], "");
    const timeFilter = select([
      { value: "", label: "Any time" },
      { value: "24", label: "Last 24 hours" },
      { value: "168", label: "Last 7 days" },
      { value: "720", label: "Last 30 days" },
    ], "");

    const apply = async () => {
      try {
        await loadIssues({ target: targetFilter.value, status: statusFilter.value });
        const cutoff = timeFilter.value
          ? Date.now() - Number(timeFilter.value) * 60 * 60 * 1000
          : 0;
        const filtered = hc.issues.filter((issue) => (
          (!severityFilter.value || issue.severity === severityFilter.value)
          && (!categoryFilter.value || issueCategory(issue) === categoryFilter.value)
          && (!cutoff || new Date(issue.created_at).getTime() >= cutoff)
        ));
        renderIssueList(list, filtered, apply);
      } catch (error) {
        replace(list, errorMessage(error));
      }
    };

    const card = node("section", { class: "card" }, [
      node("h3", {}, "Issues"),
      node("p", { class: "muted" }, "Target and status are server filters; severity, category, and time are applied to returned records."),
      node("div", { class: "hc-filters" }, [
        targetFilter, severityFilter, categoryFilter, statusFilter, timeFilter,
        button("Apply filters", apply, { primary: true }),
      ]),
      list,
    ]);
    renderIssueList(list, hc.issues, apply);
    return card;
  }

  function renderIssueList(container, issues, refresh) {
    if (!issues.length) {
      replace(container, message("No issues match these filters.", "info"));
      return;
    }
    replace(container, issues.map((issue) => {
      const actions = node("div", { class: "hc-actions" });
      if (issue.status !== "acknowledged") {
        actions.appendChild(button("Acknowledge", () => updateIssue(issue.id, "acknowledged", refresh)));
      }
      if (issue.status !== "resolved") {
        actions.appendChild(button("Resolve", () => updateIssue(issue.id, "resolved", refresh), { primary: true }));
      }
      return node("article", { class: "hc-issue" }, [
        node("div", { class: "hc-card-heading" }, [
          node("div", {}, [
            node("h4", {}, issue.title || issue.code),
            node("p", { class: "muted" }, `${issue.target_id} · ${new Date(issue.created_at).toLocaleString()}`),
          ]),
          node("div", { class: "hc-badge-row" }, [
            badge(issue.severity === "critical" || issue.severity === "error" ? "fail" : "info", issue.severity),
            badge(issue.status === "resolved" ? "pass" : issue.status === "acknowledged" ? "info" : "running", issue.status),
            badge("not-configured", issueCategory(issue)),
          ]),
        ]),
        node("p", {}, issue.detail || "No detail provided."),
        actions,
      ]);
    }));
  }

  async function updateIssue(issueId, status, refresh) {
    try {
      await arcApi(`/issues/${encodeURIComponent(issueId)}`, {
        method: "PATCH",
        body: JSON.stringify({ status }),
      });
      await refresh();
    } catch (error) {
      if (typeof toast === "function") toast(error.message, "err");
    }
  }

  initialize();
})();
