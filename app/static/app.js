"use strict";

const state = {
  cases: [],
  inputSource: "Pasted text",
  currentRunId: null,
  currentRun: null,
  polling: null,
  filter: "all",
  folderValid: false,
  editingIndex: null,
  editError: "",
  deletedCase: null,
  undoTimer: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function showWorkflowStep(step) {
  const sections = {
    input: "#setup-section",
    review: "#review-section",
    download: "#run-section",
  };
  Object.values(sections).forEach((selector) => {
    $(selector).classList.add("hidden");
  });
  $(sections[step]).classList.remove("hidden");
  $$(".workflow-step").forEach((button) => {
    button.classList.toggle("active", button.id === `workflow-${step}`);
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
      // Keep the HTTP message if the response is not JSON.
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
  let value = title.trim().replace(/\b(?:v(?:s)?\.?|versus)\b/gi, "v.");
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
  item.parse_error = errors.join("; ");
  item.filename = localFilename(item.title);
  return errors;
}

const icons = {
  edit: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9"></path><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"></path></svg>',
  delete: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18"></path><path d="M8 6V4h8v2M19 6l-1 14H6L5 6"></path><path d="M10 11v5M14 11v5"></path></svg>',
};

function actionButton(label, className, icon) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `icon-button ${className}`;
  button.setAttribute("aria-label", label);
  button.title = label;
  button.innerHTML = icon;
  return button;
}

function reindexCases() {
  state.cases.forEach((item, index) => {
    item.ordinal = index + 1;
  });
}

async function saveCaseEdit(index, value, button) {
  const editedText = value.trim();
  if (!editedText) {
    state.editError = "Enter a case title or syllabus entry.";
    renderReview();
    return;
  }
  button.disabled = true;
  button.textContent = "Saving...";
  state.editError = "";
  try {
    const parsed = await api("/api/parse-text", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: editedText }),
    });
    if (parsed.cases.length !== 1 || !parsed.cases[0].title.trim()) {
      throw new Error("Enter one recognizable case title.");
    }
    const previous = state.cases[index];
    state.cases[index] = {
      ...previous,
      ...parsed.cases[0],
      ordinal: previous.ordinal,
      week: previous.week,
      raw_text: editedText,
      metadata_source: "",
      metadata_source_url: "",
      resolution_status: "ready",
    };
    state.editingIndex = null;
    state.editError = "";
    renderReview();
    updateQueueCount();
  } catch (error) {
    state.editError = error.message;
    renderReview();
  }
}

function deleteCase(index) {
  if (state.undoTimer) window.clearTimeout(state.undoTimer);
  const [item] = state.cases.splice(index, 1);
  state.deletedCase = { item, index };
  if (state.editingIndex === index) state.editingIndex = null;
  else if (state.editingIndex !== null && state.editingIndex > index) state.editingIndex -= 1;
  state.editError = "";
  reindexCases();
  renderReview();
  updateQueueCount();
  $("#undo-toast").classList.remove("hidden");
  state.undoTimer = window.setTimeout(clearUndo, 5000);
}

function clearUndo() {
  if (state.undoTimer) window.clearTimeout(state.undoTimer);
  state.undoTimer = null;
  state.deletedCase = null;
  $("#undo-toast").classList.add("hidden");
}

function undoDelete() {
  if (!state.deletedCase) return;
  const { item, index } = state.deletedCase;
  state.cases.splice(Math.min(index, state.cases.length), 0, item);
  reindexCases();
  clearUndo();
  renderReview();
  updateQueueCount();
}

function renderReview() {
  const body = $("#review-body");
  body.replaceChildren();
  let errors = 0;
  if (!state.cases.length) {
    const empty = document.createElement("div");
    empty.className = "empty-review";
    empty.textContent = "No cases remain. Go back to input or undo the last removal.";
    body.appendChild(empty);
  }
  state.cases.forEach((item, index) => {
    const rowErrors = validateCaseRow(item);
    if (rowErrors.length) errors += 1;
    const row = document.createElement("div");
    row.className = `case-review-item${rowErrors.length ? " has-error" : ""}`;
    row.style.animationDelay = `${Math.min(index, 12) * 25}ms`;
    const number = document.createElement("span");
    number.className = "case-review-number";
    number.textContent = String(index + 1);
    const main = document.createElement("div");
    main.className = "case-review-main";

    if (state.editingIndex === index) {
      const form = document.createElement("form");
      form.className = "case-edit-form";
      const editor = document.createElement("textarea");
      editor.value = item.raw_text || item.title;
      editor.setAttribute("aria-label", `Edit case ${index + 1}`);
      const actions = document.createElement("div");
      actions.className = "case-edit-actions";
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "button ghost small";
      cancel.textContent = "Cancel";
      cancel.addEventListener("click", () => {
        state.editingIndex = null;
        state.editError = "";
        renderReview();
      });
      const save = document.createElement("button");
      save.type = "submit";
      save.className = "button primary small";
      save.textContent = "Save";
      actions.append(cancel, save);
      form.append(editor);
      if (state.editError) {
        const error = document.createElement("span");
        error.className = "edit-error";
        error.textContent = state.editError;
        form.appendChild(error);
      }
      form.appendChild(actions);
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        saveCaseEdit(index, editor.value, save);
      });
      main.appendChild(form);
      window.setTimeout(() => editor.focus(), 0);
    } else {
      const entry = document.createElement("div");
      entry.className = "case-review-entry";
      entry.textContent = item.raw_text || item.title;
      const status = document.createElement("span");
      status.className = `row-check ${rowErrors.length ? "error" : "ok"}`;
      status.textContent = rowErrors.length ? "Could not read a usable case title" : "Ready to search";
      main.append(entry, status);
    }

    row.append(number, main);
    if (state.editingIndex !== index) {
      const actions = document.createElement("div");
      actions.className = "case-review-actions";
      const edit = actionButton("Edit case", "edit-button", icons.edit);
      edit.addEventListener("click", () => {
        state.editingIndex = index;
        state.editError = "";
        renderReview();
      });
      const remove = actionButton("Delete case", "delete-button", icons.delete);
      remove.addEventListener("click", () => deleteCase(index));
      actions.append(edit, remove);
      row.appendChild(actions);
    }
    body.appendChild(row);
  });
  renderReviewCounts(errors);
}

function renderReviewCounts(explicitErrors = null) {
  let errors = explicitErrors;
  if (errors === null) {
    errors = 0;
    state.cases.forEach((item) => {
      const rowErrors = validateCaseRow(item);
      if (rowErrors.length) errors += 1;
    });
  }
  $("#case-count").textContent = `${state.cases.length} ${
    state.cases.length === 1 ? "case" : "cases"
  }`;
  const errorBadge = $("#parse-error-count");
  errorBadge.textContent = errors ? `${errors} need attention` : "";
  errorBadge.classList.toggle("hidden", errors === 0);
  $("#start-button").disabled = errors > 0 || !state.folderValid || !state.cases.length;
}

function showReview(data, source) {
  clearUndo();
  state.cases = data.cases;
  state.inputSource = source;
  state.folderValid = false;
  state.editingIndex = null;
  state.editError = "";
  $("#folder-message").textContent = "";
  renderReview();
  updateQueueCount();
  showWorkflowStep("review");
  $("#resolution-message").textContent = data.folder_name
    ? `Cases will be saved inside “${data.folder_name}”.`
    : "Each syllabus entry will be searched automatically.";
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
  button.textContent = "Reading list...";
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
    button.textContent = "Recognize cases";
  }
}

async function parseDocxInput() {
  hideAlert();
  const file = $("#docx-file").files[0];
  if (!file) return;
  const data = new FormData();
  data.append("file", file);
  data.append("page_range", $("#page-range").value.trim());
  data.append("folder_name", $("#syllabus-folder-name").value.trim());
  const button = $("#parse-docx-button");
  button.disabled = true;
  button.textContent = "Reading document...";
  try {
    const parsed = await api("/api/parse-file", { method: "POST", body: data });
    showReview(parsed, file.name);
  } catch (error) {
    showAlert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Recognize cases";
  }
}

async function validateFolder(showSuccess = true) {
  state.folderValid = false;
  renderReviewCounts();
  const message = $("#folder-message");
  message.className = "folder-message";
  message.textContent = "Checking folder...";
  try {
    const result = await api("/api/validate-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: $("#output-dir").value, create: true }),
    });
    $("#output-dir").value = result.path;
    state.folderValid = true;
    message.classList.add("valid");
    message.textContent = "Folder is ready and writable.";
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
  button.textContent = "Starting...";
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
    showWorkflowStep("download");
    beginPolling();
  } catch (error) {
    showAlert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Continue to download";
  }
}

const terminalStatuses = new Set(["Completed", "Cancelled", "Failed", "Interrupted"]);
const activeCaseStatuses = new Set([
  "Queued",
  "Searching case details",
  "Searching web",
  "Searching Google",
  "Verifying match",
  "Searching Lawphil",
  "Searching E-Library",
  "Searching ChanRobles",
  "Saving",
]);

function statusKind(status) {
  if (status === "Saved") return "saved";
  if (status === "Needs review") return "review";
  if (["Not saved", "Duplicate input", "Cancelled"].includes(status)) return "failed";
  if (activeCaseStatuses.has(status)) return "active";
  return "neutral";
}

function filterCase(item) {
  if (state.filter === "all") return true;
  if (state.filter === "active") return activeCaseStatuses.has(item.status);
  if (state.filter === "saved") return ["Saved", "Already saved"].includes(item.status);
  if (state.filter === "failed") {
    return ["Not saved", "Duplicate input", "Cancelled", "Needs review"].includes(item.status);
  }
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
    if (["Not saved", "Duplicate input", "Cancelled", "Needs review"].includes(item.status)) counts.failed += 1;
    if (!activeCaseStatuses.has(item.status)) counts.completed += 1;
  });
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
    ? `${run.verified_count || 0} of ${counts.total} cases verified; ${counts.saved + counts.existing} files available in the destination folder.`
    : "Checking official sources, then Google for unresolved cases.";
  $("#cancel-button").classList.toggle("hidden", terminal);
  $("#retry-button").classList.toggle("hidden", !terminal || counts.failed === 0);

  const body = $("#results-body");
  body.replaceChildren();
  run.cases.filter(filterCase).forEach((item) => {
    const row = document.createElement("tr");
    const number = document.createElement("td");
    number.textContent = item.ordinal;
    row.appendChild(number);

    const caseCell = document.createElement("td");
    addText(caseCell, item.raw_text || item.title, "case-title");
    row.appendChild(caseCell);

    const statusCell = document.createElement("td");
    const pill = document.createElement("span");
    pill.className = `status-pill ${statusKind(item.status)}`;
    pill.textContent = item.status;
    statusCell.appendChild(pill);
    row.appendChild(statusCell);

    const detail = document.createElement("td");
    detail.className = "source-detail";
    if (item.error_code && !item.error_code.startsWith("GOOGLE_SEARCH_")) {
      addText(detail, item.error_code, "error-code");
    }
    if (item.error_message) addText(detail, item.error_message);
    else if (item.source) addText(detail, item.source);
    else if (activeCaseStatuses.has(item.status)) addText(detail, "Working...");
    else addText(detail, "Waiting");
    if (item.verification?.message) addText(detail, item.verification.message);
    const identityProof = item.verification?.identity_verification;
    if (identityProof?.reporter_page_verified === false) {
      addText(detail, "Identity confirmed by independent sources; exact reporter page unavailable.");
    }
    if (item.failure_stage) addText(detail, `Stage: ${item.failure_stage}`);
    if (item.source_url) {
      const link = document.createElement("a");
      link.href = item.source_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "Open source";
      detail.appendChild(document.createElement("br"));
      detail.appendChild(link);
    }
    const manualSearchUrl = item.manual_search_url || (
      item.status === "Needs review"
        ? `https://www.google.com/search?q=${encodeURIComponent(item.raw_text || item.title || "")}&num=10&hl=en&gl=ph&pws=0`
        : ""
    );
    if (manualSearchUrl) {
      try {
        const manualUrl = new URL(manualSearchUrl);
        if (manualUrl.protocol === "https:" && manualUrl.hostname === "www.google.com") {
          const manualLink = document.createElement("a");
          manualLink.href = manualUrl.href;
          manualLink.target = "_blank";
          manualLink.rel = "noopener noreferrer";
          manualLink.className = "button outline small manual-search-link";
          manualLink.textContent = "None match? Open Google search";
          detail.appendChild(manualLink);
        }
      } catch {
        // Ignore malformed links from historical local data.
      }
    }
    if (Array.isArray(item.candidates) && item.candidates.length) {
      const list = document.createElement("div");
      list.className = "candidate-list";
      item.candidates.forEach((candidate) => {
        const option = document.createElement("div");
        option.className = "candidate-option";
        const copy = document.createElement("div");
        const title = document.createElement("strong");
        title.textContent = candidate.title || candidate.docket || "Possible decision";
        const metadata = document.createElement("small");
        metadata.textContent = [
          candidate.source,
          candidate.docket,
          candidate.decision_date,
        ].filter(Boolean).join(" · ");
        copy.append(title, metadata);
        const choose = document.createElement("button");
        choose.type = "button";
        choose.className = "button outline small";
        choose.textContent = "Verify this match";
        choose.addEventListener("click", () => selectCandidate(item.id, candidate.id, choose));
        option.append(copy, choose);
        list.appendChild(option);
      });
      detail.appendChild(list);
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
    $("#cancel-button").textContent = "Cancelling...";
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
    $("#cancel-button").textContent = "Cancel";
    beginPolling();
  } catch (error) {
    showAlert(error.message);
  }
}

async function selectCandidate(caseId, candidateId, button) {
  button.disabled = true;
  button.textContent = "Starting...";
  try {
    await api(`/api/runs/${state.currentRunId}/cases/${caseId}/select-candidate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ candidate_id: candidateId }),
    });
    beginPolling();
  } catch (error) {
    button.disabled = false;
    button.textContent = "Verify this match";
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

function setTheme(theme) {
  const dark = theme === "dark";
  document.documentElement.classList.toggle("dark", dark);
  const toggle = $("#theme-toggle");
  toggle.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
  try {
    window.localStorage.setItem("case-downloader-theme", theme);
  } catch {
    // Theme persistence is optional when browser storage is unavailable.
  }
}

function initializeTheme() {
  let saved = "";
  try {
    saved = window.localStorage.getItem("case-downloader-theme") || "";
  } catch {
    // Use the operating-system preference when browser storage is unavailable.
  }
  const preferred = window.matchMedia?.("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
  setTheme(saved === "dark" || saved === "light" ? saved : preferred);
}

function updateSyllabusFileControls() {
  const file = $("#docx-file").files[0];
  const isPdf = Boolean(file && file.name.toLowerCase().endsWith(".pdf"));
  $("#docx-name").textContent = file
    ? file.name
    : "Drop a DOCX or PDF syllabus, or click to browse";
  $("#parse-docx-button").disabled = !file;
  $("#page-range").disabled = !isPdf;
  $("#page-range-help").textContent = isPdf
    ? "Leave blank to read all pages. Example: 4, 7-9."
    : "PDF only. Select a PDF to choose pages; DOCX files use the whole document.";
  if (!isPdf) $("#page-range").value = "";
  if (file && !$("#syllabus-folder-name").value.trim()) {
    $("#syllabus-folder-name").value = file.name.replace(/\.[^.]+$/, "");
  }
}

initializeTheme();
$("#paste-tab").addEventListener("click", () => setTab("paste"));
$("#docx-tab").addEventListener("click", () => setTab("docx"));
$("#parse-text-button").addEventListener("click", parseTextInput);
$("#parse-docx-button").addEventListener("click", parseDocxInput);
$("#docx-file").addEventListener("change", updateSyllabusFileControls);
const dropZone = $(".drop-zone");
dropZone.addEventListener("dragover", (event) => event.preventDefault());
dropZone.addEventListener("drop", (event) => {
  event.preventDefault();
  const file = event.dataTransfer.files[0];
  if (!file) return;
  const transfer = new DataTransfer();
  transfer.items.add(file);
  $("#docx-file").files = transfer.files;
  updateSyllabusFileControls();
});
$("#theme-toggle").addEventListener("click", () => {
  setTheme(document.documentElement.classList.contains("dark") ? "light" : "dark");
});
$("#undo-delete-button").addEventListener("click", undoDelete);
$("#validate-folder-button").addEventListener("click", () => validateFolder());
$("#output-dir").addEventListener("input", () => {
  state.folderValid = false;
  $("#folder-message").textContent = "Folder changed; check it before starting.";
  $("#folder-message").className = "folder-message";
  renderReviewCounts();
});
$("#start-button").addEventListener("click", startRun);
$("#back-button").addEventListener("click", () => showWorkflowStep("input"));
$("#cancel-button").addEventListener("click", cancelRun);
$("#retry-button").addEventListener("click", retryRun);
$("#workflow-input").addEventListener("click", () => showWorkflowStep("input"));
$("#workflow-review").addEventListener("click", () => showWorkflowStep("review"));
$("#workflow-download").addEventListener("click", () => showWorkflowStep("download"));
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
