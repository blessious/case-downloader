"use strict";

const state = {
  cases: [],
  inputSource: "Pasted text",
  currentRunId: null,
  currentRun: null,
  polling: null,
  filter: "all",
  folderValid: false,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function showWorkflowStep(step) {
  const sections = {
    input: "#setup-section",
    review: "#review-section",
    download: "#run-section",
    history: "#history-section",
  };
  Object.values(sections).forEach((selector) => {
    $(selector).classList.add("hidden");
  });
  $(sections[step]).classList.remove("hidden");
  $$(".workflow-step").forEach((button) => {
    button.classList.toggle(
      "active",
      button.id === (step === "history" ? "history-button" : `workflow-${step}`),
    );
  });
  $(sections[step]).scrollIntoView({ behavior: "smooth", block: "start" });
}

function updateQueueCount() {
  const count = state.cases.length;
  $("#queue-count").textContent = count
    ? `${count} ${count === 1 ? "case" : "cases"} in queue`
    : "No cases loaded";
  $("#workflow-review").disabled = count === 0;
  $("#workflow-download").disabled = !state.currentRunId;
}

function showAlert(message, success = false) {
  const alert = $("#alert");
  alert.textContent = message;
  alert.classList.remove("hidden", "success");
  if (success) alert.classList.add("success");
  alert.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function hideAlert() {
  $("#alert").classList.add("hidden");
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      message = body.detail || message;
    } catch {
      // Retain the HTTP message when the response is not JSON.
    }
    throw new Error(message);
  }
  return response.json();
}

function setTab(tab) {
  const paste = tab === "paste";
  $("#paste-tab").classList.toggle("active", paste);
  $("#docx-tab").classList.toggle("active", !paste);
  $("#paste-panel").classList.toggle("hidden", !paste);
  $("#docx-panel").classList.toggle("hidden", paste);
}

function localFilename(title) {
  let value = title.trim().replace(/\b(?:v(?:s)?\.?)\b/gi, "v.");
  value = value
    .replace(/\s+/g, " ")
    .replace(/\s+([,.;])/g, "$1")
    .replace(/(?<!\s)v\.(?!\s)/g, "v. ")
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, "-")
    .replace(/[. ]+$/g, "");
  if (!value) value = "Untitled case";
  if (value.length > 176) value = value.slice(0, 176).replace(/[. ]+$/g, "");
  return `${value}.pdf`;
}

function validateCaseRow(item) {
  const errors = [];
  if (!item.title.trim()) errors.push("Missing title");
  if (!item.docket.trim() || !/\d{3,}/.test(item.docket)) errors.push("Missing docket");
  if (!item.decision_date) errors.push("Missing date");
  item.parse_error = errors.join("; ");
  item.filename = localFilename(item.title);
  return errors;
}

function renderReview() {
  const body = $("#review-body");
  body.replaceChildren();
  let errors = 0;
  state.cases.forEach((item, index) => {
    const rowErrors = validateCaseRow(item);
    if (rowErrors.length) errors += 1;
    const row = document.createElement("tr");

    const number = document.createElement("td");
    number.textContent = String(index + 1);
    row.appendChild(number);

    const weekCell = document.createElement("td");
    const weekInput = document.createElement("input");
    weekInput.type = "text";
    weekInput.value = item.week;
    weekInput.setAttribute("aria-label", `Week for case ${index + 1}`);
    weekInput.addEventListener("input", () => {
      item.week = weekInput.value;
    });
    weekCell.appendChild(weekInput);
    row.appendChild(weekCell);

    const titleCell = document.createElement("td");
    const titleInput = document.createElement("input");
    titleInput.type = "text";
    titleInput.value = item.title;
    titleInput.classList.toggle("error", !item.title.trim());
    titleInput.setAttribute("aria-label", `Assigned title for case ${index + 1}`);
    const filename = document.createElement("span");
    filename.className = "filename-preview";
    filename.textContent = item.filename;
    titleInput.addEventListener("input", () => {
      item.title = titleInput.value;
      validateCaseRow(item);
      filename.textContent = item.filename;
      renderReviewCounts();
      titleInput.classList.toggle("error", !item.title.trim());
    });
    titleCell.append(titleInput, filename);
    row.appendChild(titleCell);

    const docketCell = document.createElement("td");
    const docketInput = document.createElement("input");
    docketInput.type = "text";
    docketInput.value = item.docket;
    docketInput.classList.toggle("error", !item.docket.trim());
    docketInput.setAttribute("aria-label", `Docket for case ${index + 1}`);
    docketInput.addEventListener("input", () => {
      item.docket = docketInput.value;
      validateCaseRow(item);
      docketInput.classList.toggle("error", !item.docket.trim());
      renderReviewCounts();
    });
    docketCell.appendChild(docketInput);
    row.appendChild(docketCell);

    const dateCell = document.createElement("td");
    const dateInput = document.createElement("input");
    dateInput.type = "date";
    dateInput.value = item.decision_date;
    dateInput.classList.toggle("error", !item.decision_date);
    dateInput.setAttribute("aria-label", `Decision date for case ${index + 1}`);
    dateInput.addEventListener("input", () => {
      item.decision_date = dateInput.value;
      validateCaseRow(item);
      dateInput.classList.toggle("error", !item.decision_date);
      renderReviewCounts();
    });
    dateCell.appendChild(dateInput);
    row.appendChild(dateCell);

    const checkCell = document.createElement("td");
    const check = document.createElement("span");
    check.className = `row-check ${rowErrors.length ? "error" : "ok"}`;
    check.textContent = rowErrors.length ? rowErrors.join(", ") : "Ready";
    check.dataset.rowCheck = String(index);
    checkCell.appendChild(check);
    row.appendChild(checkCell);
    body.appendChild(row);
  });
  renderReviewCounts(errors);
}

function renderReviewCounts(explicitErrors = null) {
  let errors = explicitErrors;
  if (errors === null) {
    errors = 0;
    state.cases.forEach((item, index) => {
      const rowErrors = validateCaseRow(item);
      if (rowErrors.length) errors += 1;
      const check = document.querySelector(`[data-row-check="${index}"]`);
      if (check) {
        check.className = `row-check ${rowErrors.length ? "error" : "ok"}`;
        check.textContent = rowErrors.length ? rowErrors.join(", ") : "Ready";
      }
    });
  }
  $("#case-count").textContent = `${state.cases.length} ${state.cases.length === 1 ? "case" : "cases"}`;
  $("#parse-error-count").textContent = errors ? `${errors} need attention` : "";
  $("#start-button").disabled = errors > 0 || !state.folderValid;
}

function showReview(data, source) {
  state.cases = data.cases;
  state.inputSource = source;
  state.folderValid = false;
  $("#folder-message").textContent = "";
  renderReview();
  updateQueueCount();
  showWorkflowStep("review");
  window.setTimeout(() => validateFolder(false), 0);
}

async function parseTextInput() {
  hideAlert();
  const text = $("#case-text").value.trim();
  if (!text) {
    showAlert("Paste at least one case before continuing.");
    return;
  }
  const button = $("#parse-text-button");
  button.disabled = true;
  button.textContent = "Reading list…";
  try {
    const data = await api("/api/parse-text", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    showReview(data, "Pasted text");
  } catch (error) {
    showAlert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Continue";
  }
}

async function parseDocxInput() {
  hideAlert();
  const file = $("#docx-file").files[0];
  if (!file) return;
  const data = new FormData();
  data.append("file", file);
  const button = $("#parse-docx-button");
  button.disabled = true;
  button.textContent = "Reading document…";
  try {
    const parsed = await api("/api/parse-docx", { method: "POST", body: data });
    showReview(parsed, file.name);
  } catch (error) {
    showAlert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Continue";
  }
}

async function validateFolder(showSuccess = true) {
  state.folderValid = false;
  renderReviewCounts();
  const message = $("#folder-message");
  message.className = "folder-message";
  message.textContent = "Checking folder…";
  try {
    const result = await api("/api/validate-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: $("#output-dir").value, create: true }),
    });
    $("#output-dir").value = result.path;
    state.folderValid = true;
    message.classList.add("valid");
    message.textContent = "✓ Folder is ready and writable.";
    renderReviewCounts();
    if (showSuccess) hideAlert();
    return true;
  } catch (error) {
    message.classList.add("invalid");
    message.textContent = error.message;
    renderReviewCounts();
    return false;
  }
}

async function startRun() {
  hideAlert();
  renderReviewCounts();
  if (state.cases.some((item) => validateCaseRow(item).length)) {
    showAlert("Correct the highlighted cases before starting.");
    return;
  }
  if (!(await validateFolder(false))) {
    showAlert("Choose a valid, writable destination folder.");
    return;
  }
  const button = $("#start-button");
  button.disabled = true;
  button.textContent = "Starting…";
  try {
    const result = await api("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        input_source: state.inputSource,
        output_dir: $("#output-dir").value,
        cases: state.cases,
      }),
    });
    state.currentRunId = result.run_id;
    updateQueueCount();
    $("#cancel-button").classList.remove("hidden");
    $("#retry-button").classList.add("hidden");
    $("#report-button").classList.add("hidden");
    showWorkflowStep("download");
    beginPolling();
  } catch (error) {
    showAlert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Find and save PDFs";
  }
}

const terminalStatuses = new Set(["Completed", "Cancelled", "Failed", "Interrupted"]);
const activeCaseStatuses = new Set([
  "Queued",
  "Searching Lawphil",
  "Searching E-Library",
  "Searching ChanRobles",
  "Saving",
]);

function statusKind(status) {
  if (status === "Saved") return "saved";
  if (["Not saved", "Duplicate input", "Cancelled"].includes(status)) return "failed";
  if (activeCaseStatuses.has(status)) return "active";
  return "neutral";
}

function filterCase(item) {
  if (state.filter === "all") return true;
  if (state.filter === "active") return activeCaseStatuses.has(item.status);
  if (state.filter === "saved") return ["Saved", "Already saved"].includes(item.status);
  if (state.filter === "failed")
    return ["Not saved", "Duplicate input", "Cancelled"].includes(item.status);
  return true;
}

function addText(element, text, className = "") {
  const child = document.createElement("div");
  if (className) child.className = className;
  child.textContent = text;
  element.appendChild(child);
}

function renderRun(run) {
  state.currentRun = run;
  const counts = {
    total: run.cases.length,
    saved: 0,
    existing: 0,
    failed: 0,
    completed: 0,
  };
  run.cases.forEach((item) => {
    if (item.status === "Saved") counts.saved += 1;
    if (item.status === "Already saved") counts.existing += 1;
    if (["Not saved", "Duplicate input", "Cancelled"].includes(item.status)) counts.failed += 1;
    if (!activeCaseStatuses.has(item.status)) counts.completed += 1;
  });
  $("#stat-total").textContent = counts.total;
  $("#stat-saved").textContent = counts.saved;
  $("#stat-existing").textContent = counts.existing;
  $("#stat-failed").textContent = counts.failed;
  $("#progress-bar").style.width = counts.total
    ? `${Math.round((counts.completed / counts.total) * 100)}%`
    : "0%";

  const terminal = terminalStatuses.has(run.status);
  $("#run-title").textContent = terminal
    ? run.status === "Completed"
      ? "Run complete"
      : `Run ${run.status.toLowerCase()}`
    : "Downloading cases";
  $("#run-subtitle").textContent = terminal
    ? `${counts.saved + counts.existing} of ${counts.total} cases are available in the destination folder.`
    : "Checking Lawphil, the Supreme Court E-Library, then ChanRobles.";
  $("#cancel-button").classList.toggle("hidden", terminal);
  $("#retry-button").classList.toggle("hidden", !terminal || counts.failed === 0);
  const hasXlsx = run.report_path && !run.report_path.includes(";");
  $("#report-button").classList.toggle("hidden", !terminal || !hasXlsx);
  $("#report-button").href = `/api/runs/${run.id}/report`;
  $("#not-saved-panel").classList.toggle("hidden", counts.failed === 0);

  const body = $("#results-body");
  body.replaceChildren();
  run.cases.filter(filterCase).forEach((item) => {
    const row = document.createElement("tr");
    const number = document.createElement("td");
    number.textContent = item.ordinal;
    row.appendChild(number);

    const caseCell = document.createElement("td");
    addText(caseCell, item.title, "case-title");
    addText(
      caseCell,
      `${item.docket || "No docket"} · ${item.decision_date || "No date"}`,
      "case-meta",
    );
    row.appendChild(caseCell);

    const week = document.createElement("td");
    week.textContent = item.week;
    row.appendChild(week);

    const statusCell = document.createElement("td");
    const pill = document.createElement("span");
    pill.className = `status-pill ${statusKind(item.status)}`;
    pill.textContent = item.status;
    statusCell.appendChild(pill);
    row.appendChild(statusCell);

    const detail = document.createElement("td");
    detail.className = "source-detail";
    if (item.error_code) addText(detail, item.error_code, "error-code");
    if (item.error_message) addText(detail, item.error_message);
    else if (item.source) addText(detail, item.source);
    else if (activeCaseStatuses.has(item.status)) addText(detail, "Working…");
    else addText(detail, "Waiting");
    if (item.source_url) {
      const link = document.createElement("a");
      link.href = item.source_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "Open source";
      detail.appendChild(document.createElement("br"));
      detail.appendChild(link);
    }
    row.appendChild(detail);
    body.appendChild(row);
  });
  $("#last-updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
}

async function refreshRun() {
  if (!state.currentRunId) return;
  try {
    const run = await api(`/api/runs/${state.currentRunId}`);
    renderRun(run);
    if (terminalStatuses.has(run.status)) stopPolling();
  } catch (error) {
    showAlert(`Could not update the current run: ${error.message}`);
  }
}

function beginPolling() {
  stopPolling();
  refreshRun();
  state.polling = window.setInterval(refreshRun, 1000);
}

function stopPolling() {
  if (state.polling) window.clearInterval(state.polling);
  state.polling = null;
}

async function cancelRun() {
  if (!state.currentRunId) return;
  try {
    await api(`/api/runs/${state.currentRunId}/cancel`, { method: "POST" });
    $("#cancel-button").disabled = true;
    $("#cancel-button").textContent = "Cancelling…";
  } catch (error) {
    showAlert(error.message);
  }
}

async function retryRun() {
  if (!state.currentRunId) return;
  try {
    const result = await api(`/api/runs/${state.currentRunId}/retry`, { method: "POST" });
    if (!result.started) throw new Error("This run is already active.");
    $("#retry-button").classList.add("hidden");
    $("#cancel-button").disabled = false;
    $("#cancel-button").textContent = "Cancel run";
    beginPolling();
  } catch (error) {
    showAlert(error.message);
  }
}

async function openHistory() {
  hideAlert();
  try {
    const data = await api("/api/runs");
    const list = $("#history-list");
    list.replaceChildren();
    if (!data.runs.length) {
      const empty = document.createElement("div");
      empty.className = "history-item";
      empty.textContent = "No previous runs yet.";
      list.appendChild(empty);
    }
    data.runs.forEach((run) => {
      const item = document.createElement("div");
      item.className = "history-item";
      const summary = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = `${run.total} ${run.total === 1 ? "case" : "cases"} · ${run.status}`;
      const meta = document.createElement("small");
      meta.textContent = `${new Date(run.created_at).toLocaleString()} · ${run.output_dir}`;
      summary.append(title, meta);
      const counts = document.createElement("div");
      counts.className = "history-counts";
      counts.textContent = `${run.saved || 0} saved · ${run.already_saved || 0} existing · ${run.not_saved || 0} not saved`;
      const open = document.createElement("button");
      open.type = "button";
      open.className = "button secondary compact";
      open.textContent = "Open run";
      open.addEventListener("click", async () => {
        state.currentRunId = run.id;
        const detail = await api(`/api/runs/${run.id}`);
        updateQueueCount();
        renderRun(detail);
        showWorkflowStep("download");
        if (!terminalStatuses.has(detail.status)) beginPolling();
      });
      item.append(summary, counts, open);
      list.appendChild(item);
    });
    showWorkflowStep("history");
  } catch (error) {
    showAlert(error.message);
  }
}

async function initialize() {
  try {
    const config = await api("/api/config");
    $("#output-dir").value = config.default_output_dir;
    $("#app-version").textContent = `v${config.version}`;
  } catch (error) {
    showAlert(`Application setup failed: ${error.message}`);
  }
}

$("#paste-tab").addEventListener("click", () => setTab("paste"));
$("#docx-tab").addEventListener("click", () => setTab("docx"));
$("#parse-text-button").addEventListener("click", parseTextInput);
$("#parse-docx-button").addEventListener("click", parseDocxInput);
$("#docx-file").addEventListener("change", () => {
  const file = $("#docx-file").files[0];
  $("#docx-name").textContent = file ? file.name : "No document selected";
  $("#parse-docx-button").disabled = !file;
});
$("#validate-folder-button").addEventListener("click", () => validateFolder());
$("#output-dir").addEventListener("input", () => {
  state.folderValid = false;
  $("#folder-message").textContent = "Folder changed—check it before starting.";
  $("#folder-message").className = "folder-message";
  renderReviewCounts();
});
$("#start-button").addEventListener("click", startRun);
$("#back-button").addEventListener("click", () => {
  showWorkflowStep("input");
});
$("#cancel-button").addEventListener("click", cancelRun);
$("#retry-button").addEventListener("click", retryRun);
$("#workflow-input").addEventListener("click", () => showWorkflowStep("input"));
$("#workflow-review").addEventListener("click", () => showWorkflowStep("review"));
$("#workflow-download").addEventListener("click", () => showWorkflowStep("download"));
$("#history-button").addEventListener("click", openHistory);
$("#close-history-button").addEventListener("click", () => {
  showWorkflowStep(state.currentRunId ? "download" : state.cases.length ? "review" : "input");
});
$$(".filter").forEach((button) => {
  button.addEventListener("click", () => {
    $$(".filter").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    state.filter = button.dataset.filter;
    if (state.currentRun) renderRun(state.currentRun);
  });
});

updateQueueCount();
initialize();
