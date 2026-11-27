"use strict";

const el = (id) => document.getElementById(id);
const pageSize = 10;
const statusLabels = {
  queued: "Queued", sending: "Sending", accepted: "Accepted", failed: "Failed",
  review: "Needs review", skipped: "Skipped",
};
let csv = "";
let contacts = [];
let skipped = [];
let resume = null;
let contactIndex = 0;
let skipPage = 0;
let resultsPage = 0;
let appStatus = { state: "empty", configured: false };
let busy = false;
let pendingStart = null;
let previewRevision = 0;
let previewTimer;
let importRevision = 0;
let resumeRevision = 0;
let statusRevision = 0;
let lastResults;

function notice(id, text = "", error = false) {
  const node = el(id);
  if (node.textContent !== text) node.textContent = text;
  node.classList.toggle("error", error);
}

async function api(path, data) {
  const response = await fetch(path, data === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "The app could not finish the request.");
  return result;
}

function draft() {
  return { subject: el("subject").value, body: el("body").value };
}

function updateButtons() {
  const canSend = appStatus.configured && appStatus.state !== "running";
  const hasDraft = Boolean(draft().subject.trim() && draft().body.trim() && resume);
  const dry = el("dry").checked;
  el("test").disabled = busy || !canSend || !hasDraft;
  el("run").disabled = busy || !hasDraft || !contacts.length
    || (!dry && (!canSend || appStatus.state === "paused"));
  el("run").textContent = dry ? "Check batch" : "Start sending";
  el("pause").disabled = busy;
  el("resume-batch").disabled = busy || !appStatus.configured;
  el("google-logout").disabled = busy;
}

function updatePreview() {
  const revision = ++previewRevision;
  clearTimeout(previewTimer);
  const contact = contacts[contactIndex];
  const currentDraft = draft();
  el("contact-index").value = contactIndex + 1;
  el("contact-index").max = Math.max(contacts.length, 1);
  el("contact-index").disabled = !contacts.length;
  el("preview-total").textContent = "of " + contacts.length.toLocaleString();
  el("preview-prev").disabled = contactIndex === 0;
  el("preview-next").disabled = contactIndex + 1 >= contacts.length;
  el("preview-address").textContent = contact ? contact.email : "Example recipient";
  el("preview-attachment").textContent = resume ? resume.name : "Add your résumé PDF";
  if (!currentDraft.body.trim() || !currentDraft.subject.trim()) {
    el("preview-subject").textContent = currentDraft.subject || "Add a subject";
    el("preview-body").textContent = "Add a subject and message to preview your email.";
    return;
  }
  el("preview-subject").textContent = "Updating preview…";
  el("preview-body").textContent = "";
  previewTimer = setTimeout(async () => {
    try {
      const preview = await api("/api/preview", { contact, draft: currentDraft });
      if (revision !== previewRevision) return;
      el("preview-subject").textContent = preview.subject;
      el("preview-body").textContent = preview.body;
    } catch (error) {
      if (revision !== previewRevision) return;
      el("preview-subject").textContent = currentDraft.subject;
      el("preview-body").textContent = error.message;
    }
  }, 160);
}

function pagination(prefix, page, count) {
  const pages = Math.max(1, Math.ceil(count / pageSize));
  el(prefix + "-prev").disabled = page === 0;
  el(prefix + "-next").disabled = page + 1 >= pages;
  el(prefix + "-page").textContent = "Page " + (page + 1) + " of " + pages;
}

function paintSkipped() {
  el("skipped").hidden = !skipped.length;
  el("skipped-summary").textContent = "Review " + skipped.length.toLocaleString() + " skipped rows";
  const items = skipped.slice(skipPage * pageSize, (skipPage + 1) * pageSize).map((row) => {
    const item = document.createElement("li");
    item.textContent = "Row " + row.row + " · " + (row.email || "Blank address") + " · " + row.detail;
    return item;
  });
  el("skipped-list").replaceChildren(...items);
  el("skip-pagination").hidden = skipped.length <= pageSize;
  pagination("skip", skipPage, skipped.length);
}

el("csv").addEventListener("change", async () => {
  const revision = ++importRevision;
  const file = el("csv").files[0];
  csv = "";
  contacts = [];
  skipped = [];
  contactIndex = 0;
  skipPage = 0;
  paintSkipped();
  updatePreview();
  updateButtons();
  notice("import-status");
  if (!file) return;
  try {
    if (file.size > 5 * 1024 * 1024) throw new Error("Choose a CSV file up to 5 MB.");
    notice("import-status", "Checking your list…");
    const raw = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
    const { rows } = await api("/api/import", { csv: raw });
    if (revision !== importRevision) return;
    csv = raw;
    contacts = rows.filter((row) => row.status === "queued");
    skipped = rows.filter((row) => row.status === "skipped");
    const duplicate = skipped.filter((row) => row.detail === "Duplicate email address").length;
    notice("import-status", contacts.length.toLocaleString() + " ready · "
      + (skipped.length - duplicate).toLocaleString() + " invalid · " + duplicate.toLocaleString() + " duplicate");
    paintSkipped();
    updatePreview();
    updateButtons();
  } catch (error) {
    if (revision !== importRevision) return;
    notice("import-status", error instanceof TypeError
      ? "Could not read the list. Save it as CSV UTF-8 and try again."
      : error.message, true);
  }
});

el("skip-prev").addEventListener("click", () => { skipPage--; paintSkipped(); });
el("skip-next").addEventListener("click", () => { skipPage++; paintSkipped(); });
el("preview-prev").addEventListener("click", () => { contactIndex--; updatePreview(); });
el("preview-next").addEventListener("click", () => { contactIndex++; updatePreview(); });
el("contact-index").addEventListener("change", () => {
  const number = el("contact-index").valueAsNumber;
  if (Number.isInteger(number)) contactIndex = Math.max(0, Math.min(contacts.length - 1, number - 1));
  updatePreview();
});

el("profile").addEventListener("submit", (event) => {
  event.preventDefault();
  const details = Object.fromEntries([...new FormData(event.currentTarget)].map(([key, value]) => [key, value.trim()]));
  const { name, course, university, field, availability, skills, phone, portfolio } = details;
  if (![name, course, university, field].every(Boolean)) return;
  if (el("body").value.trim() && !window.confirm("Replace the current email draft?")) return;
  const available = availability ? " and am available " + availability : "";
  const lines = [
    "Hi {{greeting}},",
    "I’m " + name + ", a " + course + " student at " + university + ". I’m looking for an internship in "
      + field + available + ". Does {{company}} have any internship opportunities?",
  ];
  if (skills) lines.push("My relevant skills and experience include " + skills.replace(/[.!?]+$/, "") + ".");
  lines.push(
    "I’ve attached my résumé for your review. I’d appreciate the chance to speak with you or learn how to apply.",
    "Thank you for your time.",
    ["Best regards,", name, phone, portfolio].filter(Boolean).join("\n"),
  );
  el("subject").value = ("Internship inquiry – " + field + " – " + name).slice(0, 240);
  el("body").value = lines.join("\n\n");
  el("profile-details").open = false;
  el("body").focus();
  updatePreview();
  updateButtons();
});

for (const id of ["subject", "body"]) {
  el(id).addEventListener("input", () => { updatePreview(); updateButtons(); });
}

function encodeFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve(reader.result.split(",")[1]));
    reader.addEventListener("error", () => reject(new Error("The résumé could not be read.")));
    reader.readAsDataURL(file);
  });
}

el("resume").addEventListener("change", async () => {
  const revision = ++resumeRevision;
  const file = el("resume").files[0];
  resume = null;
  updatePreview();
  updateButtons();
  notice("resume-status");
  if (!file) return;
  try {
    if (!file.name.toLowerCase().endsWith(".pdf") || file.size > 10 * 1024 * 1024) {
      throw new Error("Choose a PDF file up to 10 MB.");
    }
    if (await file.slice(0, 5).text() !== "%PDF-"
        || !(await file.slice(Math.max(0, file.size - 1024)).text()).includes("%%EOF")) {
      throw new Error("This file does not look like a valid PDF.");
    }
    const data = await encodeFile(file);
    if (revision !== resumeRevision) return;
    resume = { name: file.name, data };
    notice("resume-status", file.name + " is ready to attach.");
  } catch (error) {
    if (revision !== resumeRevision) return;
    notice("resume-status", error.message, true);
  }
  updatePreview();
  updateButtons();
});

function payload() {
  return { csv, draft: draft(), resume, delay: el("delay").valueAsNumber, index: contactIndex };
}

async function action(callback, feedback = "send-status") {
  if (busy) return;
  busy = true;
  notice(feedback);
  updateButtons();
  try {
    await callback();
  } catch (error) {
    notice(feedback, error.message, true);
  } finally {
    busy = false;
    updateButtons();
  }
}

el("dry").addEventListener("change", () => { notice("send-status"); updateButtons(); });

el("google-start").addEventListener("click", () => { el("google-setup").open = true; });

el("google-login").addEventListener("click", (event) => {
  event.preventDefault();
  if (busy) return;
  const continuation = el("google-continue");
  continuation.hidden = true;
  const signInTab = window.open("about:blank", "_blank");
  if (signInTab) signInTab.opener = null;
  action(async () => {
    try {
      const result = await api("/api/google/start", {});
      if (signInTab && !signInTab.closed) {
        signInTab.location.replace(result.url);
      } else {
        continuation.href = result.url;
        continuation.hidden = false;
        continuation.focus();
      }
    } catch (error) {
      if (signInTab && !signInTab.closed) signInTab.close();
      throw error;
    }
  }, "google-login-feedback");
});

el("google-continue").addEventListener("click", (event) => {
  const continuation = event.currentTarget;
  continuation.hidden = true;
});

el("google-logout").addEventListener("click", () => {
  action(async () => {
    await api("/api/google/logout", {});
    el("google-continue").hidden = true;
    await refreshStatus();
    notice("google-login-feedback", "Signed out of Gmail in Send Email.");
    el("google-login").focus();
  }, "google-login-feedback");
});

el("google-credentials").addEventListener("change", async () => {
  const input = el("google-credentials");
  const file = input.files[0];
  if (!file) return;
  input.disabled = true;
  notice("google-feedback");
  try {
    if (file.size > 16_384) throw new Error("Choose the Desktop app JSON file downloaded from Google Cloud.");
    await api("/api/google/setup", { credentials: await file.text() });
    await refreshStatus();
    el("google-setup").open = false;
    el("google-login").focus();
  } catch (error) {
    notice("google-feedback", error.message, true);
  } finally {
    input.disabled = false;
    input.value = "";
  }
});

el("test").addEventListener("click", () => {
  const input = el("test-to");
  input.required = true;
  if (!input.reportValidity()) return;
  action(async () => {
    notice("send-status", "Sending your test email…");
    await api("/api/test", { ...payload(), to: input.value.trim() });
    notice("send-status", "The provider accepted your test email. Open it to check the message and résumé.");
  });
});

el("run").addEventListener("click", () => {
  if (!el("delay").reportValidity()) return;
  if (el("dry").checked) {
    action(async () => {
      const { count, skipped, filename } = await api("/api/dry-run", payload());
      notice("send-status", "Dry run complete. " + count.toLocaleString() + " emails ready, "
        + skipped.toLocaleString() + " rows skipped. Attachment: " + filename + ". No emails sent.");
    });
  } else {
    pendingStart = { ...payload(), count: contacts.length };
    const label = contacts.length === 1 ? "email" : "emails";
    el("confirm-copy").textContent = "Send " + contacts.length.toLocaleString() + " separate " + label
      + " with " + resume.name + " attached?"
      + (appStatus.state === "complete" ? " This replaces the results of the previous batch." : "");
    el("confirm").showModal();
  }
});

el("confirm-cancel").addEventListener("click", () => { el("confirm").close(); });
el("confirm").addEventListener("close", () => { pendingStart = null; });
el("confirm-start").addEventListener("click", () => {
  const data = pendingStart;
  el("confirm").close();
  if (!data) return;
  action(async () => {
    await api("/api/start", data);
    resultsPage = 0;
    await refreshStatus();
    notice("send-status", "Batch started. Keep the app running to send your emails.");
    el("progress").scrollIntoView({ block: "start" });
  });
});

for (const [id, verb] of [["pause", "pause"], ["resume-batch", "resume"]]) {
  el(id).addEventListener("click", () => action(async () => {
    await api("/api/" + verb, {});
    await refreshStatus();
  }, "global-notice"));
}

function showResults(data) {
  const key = JSON.stringify(data.rows);
  if (lastResults !== key) {
    lastResults = key;
    el("result-rows").replaceChildren(...data.rows.map((row) => {
      const tr = document.createElement("tr");
      for (const value of [row.row, row.email || "—", statusLabels[row.status], row.detail || "—"]) {
        const td = document.createElement("td");
        td.textContent = value;
        tr.append(td);
      }
      return tr;
    }));
  }
  const total = Object.values(data.counts).reduce((sum, count) => sum + count, 0);
  pagination("results", resultsPage, total);
}

async function refreshStatus() {
  const revision = ++statusRevision;
  const data = await api("/api/status?page=" + resultsPage);
  if (revision !== statusRevision) return;
  const previousSender = appStatus.google_sender;
  appStatus = data;
  const stateText = {
    empty: "No batch started.", running: "Sending batch",
    paused: "Batch paused" + (data.counts.sending ? " · Finishing the current email" : ""),
    complete: "Batch complete",
  };
  el("connection").hidden = false;
  el("connection").textContent = data.google_sender ? "Gmail connected" : data.configured ? "SMTP configured"
    : data.google_ready ? "Google sign-in needed" : "Email setup needed";
  el("connection").classList.toggle("ready", data.configured);
  el("gmail").hidden = false;
  const changingAccount = data.state !== "running";
  el("google-start").hidden = !changingAccount || data.google_ready;
  el("google-api").hidden = !changingAccount || !data.google_ready || Boolean(data.google_sender);
  const apiPage = new URL("https://console.cloud.google.com/apis/library/gmail.googleapis.com");
  if (data.google_project) apiPage.searchParams.set("project", data.google_project);
  el("google-api").href = apiPage.href;
  el("google-login").hidden = !changingAccount || !data.google_ready;
  el("google-login").textContent = data.google_sender ? "Switch Google account" : "Sign in with Google";
  el("google-logout").hidden = !changingAccount || !data.google_sender;
  el("google-setup").hidden = !changingAccount;
  el("google-setup-title").textContent = data.google_ready ? "Replace Google setup file" : "Import Google setup file";
  el("google-setup-instructions").hidden = data.google_ready;
  el("google-replace-instructions").hidden = !data.google_ready;
  const gmailStatus = data.google_sender ? "Sending from " + data.google_sender
    : data.google_ready ? "Setup file imported. Enable the Gmail API for your project, then sign in with Google. If Google shows Manage, it’s already enabled."
    : data.google_error || "Connect your own Gmail account with Google’s one-time setup.";
  notice("gmail-status", gmailStatus);
  if (data.google_sender && data.google_sender !== previousSender && !el("test-to").value) {
    el("test-to").value = data.google_sender;
  }
  notice("current-state", stateText[data.state] + (data.notice ? ". " + data.notice : ""));
  const active = data.state !== "empty";
  el("batch-progress").hidden = !active;
  el("campaign-details").hidden = !active;
  el("campaign-details").textContent = active ? data.filename + " · " + data.delay + " seconds between sends" : "";
  el("export").hidden = !active;
  el("pause").hidden = data.state !== "running";
  el("resume-batch").hidden = data.state !== "paused";
  for (const key of Object.keys(statusLabels)) {
    el("count-" + key).textContent = (data.counts[key] || 0).toLocaleString();
  }
  showResults(data);
  updateButtons();
}

function refreshOrReport() {
  refreshStatus().catch(() => notice("current-state", "The local app is unavailable. Restart it to continue.", true));
}

el("results-prev").addEventListener("click", () => { resultsPage--; refreshOrReport(); });
el("results-next").addEventListener("click", () => { resultsPage++; refreshOrReport(); });
if (location.protocol === "file:") {
  for (const section of document.querySelectorAll("[data-app-only]")) section.hidden = true;
  el("launch-help").hidden = false;
} else {
  updatePreview();
  refreshOrReport();
  setInterval(refreshOrReport, 1700);
}
