// CareFlow web app: a patient chat on the left and the clinic's review desk on the right.
// Plain JavaScript, no build step. All text from the server is inserted with textContent,
// never innerHTML, so a message can't inject markup into the page.

const $ = (sel) => document.querySelector(sel);

const els = {
  patientSelect: $("#patient-select"),
  chatLog: $("#chat-log"),
  chatForm: $("#chat-form"),
  chatInput: $("#chat-input"),
  sendButton: $("#send-button"),
  suggestions: $("#suggestions"),
  reviewerName: $("#reviewer-name"),
  reviewerKey: $("#reviewer-key"),
  keyRow: $("#key-row"),
  tabReview: $("#tab-review"),
  tabActivity: $("#tab-activity"),
  reviewCount: $("#review-count"),
  reviewList: $("#review-list"),
  activityList: $("#activity-list"),
  reviewTemplate: $("#tpl-review"),
};

const state = {
  patients: new Map(),
  patientId: null,
  history: [],
  sending: false,
  signatures: { chat: "", reviews: "", activity: "" },
  openAudits: new Set(),
};

// Messages that exercise each path for each demo patient.
const SUGGESTIONS = {
  P001: [
    "Can I move my appointment to next week, afternoon if possible?",
    "I need a refill on my lisinopril",
    "Can you refill my atorvastatin?",
  ],
  P002: [
    "Please refill my oxycodone",
    "Why do I owe money on INV-1042?",
    "I need more metformin",
  ],
  P003: [
    "My new email is aisha.new@example.com",
    "Can you explain the charges on my biopsy bill?",
    "Can I reschedule my dermatology appointment to a Friday morning?",
  ],
  P004: [
    "I need more sertraline",
    "I need to reschedule my appointment to Friday morning",
    "I've had chest pain since this morning",
  ],
  P005: [
    "Can I get a copy of my vaccination records?",
    "Can I move my appointment to Tuesday?",
    "Please update my phone number to 555-010-0177",
  ],
};

const TYPE_LABELS = {
  reschedule_appointment: "Reschedule",
  prescription_refill: "Refill",
  billing_question: "Billing question",
  update_contact: "Contact details",
  other: "General message",
};

const STATUS_LABELS = {
  in_progress: "Working",
  awaiting_review: "Waiting for review",
  completed: "Done",
  handed_off: "Passed to staff",
  rejected: "Declined",
  escalated: "Urgent",
};

// Trail prefixes written by each step of the workflow, and their short names.
const STATIONS = {
  safety: "Safety",
  intake: "Intake",
  plan: "Plan",
  executor: "Act",
  verifier: "Check",
  review: "Review",
  commit: "Commit",
  responder: "Reply",
};

// ------------------------------------------------------------------ helpers

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("data-") || key.startsWith("aria-")) node.setAttribute(key, value);
    else node[key] = value;
  }
  for (const child of children) if (child) node.append(child);
  return node;
}

function clock(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function patientName(id) {
  return state.patients.get(id) || id;
}

function toast(message) {
  const node = el("div", { class: "toast", role: "status", text: message });
  document.body.append(node);
  setTimeout(() => node.remove(), 3200);
}

function reviewerHeaders() {
  const key = els.reviewerKey.value.trim();
  return key ? { "X-Reviewer-Key": key } : {};
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { "content-type": "application/json", ...(options.headers || {}) },
  });
  if (!res.ok) {
    const err = new Error(`${res.status}`);
    err.status = res.status;
    try {
      err.detail = (await res.json()).detail;
    } catch {
      err.detail = res.statusText;
    }
    throw err;
  }
  return res.json();
}

// ------------------------------------------------------------- patient chat

function routeFor(view) {
  const seen = [];
  for (const line of view.trail || []) {
    const key = line.split(":")[0];
    if (STATIONS[key] && !seen.includes(key)) seen.push(key);
  }
  const stops = seen.map((key) => STATIONS[key]);
  if (view.status === "escalated") stops.push("Nurse");
  if (view.status === "awaiting_review") stops.push("Review");
  return stops;
}

function renderRoute(view) {
  const stops = routeFor(view);
  const list = el("ol", {
    class: "route",
    "data-status": view.status,
    "aria-label": `Handled by: ${stops.join(", ")}`,
  });
  stops.forEach((name, i) => {
    const item = el("li", { class: i === stops.length - 1 ? "end" : "" });
    item.append(el("span", { class: "stop" }), el("span", { text: name }));
    list.append(item);
  });
  return list;
}

function clinicBubble(view) {
  const bubble = el("div", { class: "bubble bubble-clinic" });
  bubble.append(el("span", { class: "sender", text: "Clinic" }));
  if (view.status === "awaiting_review") {
    bubble.classList.add("is-waiting");
    bubble.append(
      "Thanks, we have your message. A member of staff needs to check this one before " +
        "we act on it. Their answer will appear here.",
    );
  } else {
    if (view.status === "escalated") bubble.classList.add("is-urgent");
    bubble.append(view.reply || "We've received your message.");
  }
  return bubble;
}

function renderExchange(view) {
  const wrap = el("div", { class: "exchange" });
  const mine = el("div", { class: "bubble bubble-patient" });
  mine.append(el("span", { class: "sender", text: `You, ${clock(view.created_at)}` }));
  mine.querySelector(".sender").style.color = "rgba(255,255,255,0.8)";
  mine.append(view.message || "");
  wrap.append(mine, clinicBubble(view), renderRoute(view));

  if (view.trail && view.trail.length) {
    const details = el("details", { class: "trail" });
    details.append(el("summary", { text: "How the agents handled it" }));
    const steps = el("ol");
    for (const line of view.trail) steps.append(el("li", { text: line }));
    details.append(steps);
    wrap.append(details);
  }
  return wrap;
}

function renderChat() {
  const signature = JSON.stringify(state.history.map((h) => [h.request_id, h.status, h.reply]));
  if (signature === state.signatures.chat && !state.sending) return;
  state.signatures.chat = signature;

  els.chatLog.replaceChildren();
  if (!state.history.length) {
    const empty = el("div", { class: "empty-chat" });
    empty.append(
      el("p", { text: `Write to the clinic as ${patientName(state.patientId)}.` }),
      el("p", { text: "Pick an example below or type your own message." }),
    );
    els.chatLog.append(empty);
    return;
  }
  for (const view of state.history) els.chatLog.append(renderExchange(view));
  els.chatLog.scrollTop = els.chatLog.scrollHeight;
}

function renderSuggestions() {
  els.suggestions.replaceChildren();
  for (const text of SUGGESTIONS[state.patientId] || []) {
    const chip = el("button", { type: "button", class: "suggestion", text });
    chip.addEventListener("click", () => {
      els.chatInput.value = text;
      els.chatInput.focus();
    });
    els.suggestions.append(chip);
  }
}

async function loadHistory() {
  if (!state.patientId) return;
  state.history = await api(`/patients/${state.patientId}/requests`);
  renderChat();
}

async function sendMessage(event) {
  event.preventDefault();
  const message = els.chatInput.value.trim();
  if (!message || state.sending) return;

  state.sending = true;
  els.sendButton.disabled = true;
  els.sendButton.textContent = "Sending";

  // Show the message straight away, with a placeholder while the agents work.
  const pending = el("div", { class: "exchange" });
  const mine = el("div", { class: "bubble bubble-patient" });
  mine.append(message);
  const working = el("div", { class: "bubble bubble-clinic is-working" });
  working.append(el("span", { class: "sender", text: "Clinic" }), "Reading your message");
  pending.append(mine, working);
  els.chatLog.querySelector(".empty-chat")?.remove();
  els.chatLog.append(pending);
  els.chatLog.scrollTop = els.chatLog.scrollHeight;
  els.chatInput.value = "";

  try {
    await api("/requests", {
      method: "POST",
      body: JSON.stringify({ patient_id: state.patientId, message }),
    });
  } catch (err) {
    toast(`Message not sent: ${err.detail || err.message}`);
    els.chatInput.value = message;
  } finally {
    state.sending = false;
    els.sendButton.disabled = false;
    els.sendButton.textContent = "Send";
    state.signatures.chat = "";
    await Promise.all([loadHistory(), refreshStaff()]);
  }
}

// -------------------------------------------------------------- staff side

function selectTab(which) {
  const review = which === "review";
  els.tabReview.setAttribute("aria-selected", String(review));
  els.tabActivity.setAttribute("aria-selected", String(!review));
  els.reviewList.hidden = !review;
  els.activityList.hidden = review;
}

function renderReviews(items) {
  const signature = JSON.stringify(items.map((i) => i.request_id));
  els.reviewCount.textContent = String(items.length);
  els.reviewCount.classList.toggle("has-items", items.length > 0);
  if (signature === state.signatures.reviews) return;
  state.signatures.reviews = signature;

  els.reviewList.replaceChildren();
  if (!items.length) {
    els.reviewList.append(
      el("p", {
        class: "staff-empty",
        text: "Nothing needs review. Requests that need a person's sign-off appear here.",
      }),
    );
    return;
  }

  for (const view of items) {
    const card = els.reviewTemplate.content.firstElementChild.cloneNode(true);
    const p = view.pending_review || {};
    card.querySelector(".review-title").textContent =
      `${patientName(view.patient_id)}: ${TYPE_LABELS[p.request_type] || "Request"}`;
    card.querySelector(".review-meta").textContent = clock(view.created_at);
    card.querySelector(".review-message").textContent = p.message || "";
    card.querySelector(".review-action").textContent = p.if_approved || "";
    const reasons = card.querySelector(".review-reasons");
    for (const reason of [...(p.why_review || []), ...(p.verifier_issues || [])]) {
      reasons.append(el("li", { text: reason }));
    }
    card.querySelector(".approve").addEventListener("click", () => decide(view, true, card));
    card.querySelector(".reject").addEventListener("click", () => decide(view, false, card));
    els.reviewList.append(card);
  }
}

async function decide(view, approved, card) {
  const reviewer = els.reviewerName.value.trim();
  if (!reviewer) {
    toast("Enter your name in Reviewing as first.");
    els.reviewerName.focus();
    return;
  }
  card.querySelectorAll("button").forEach((b) => (b.disabled = true));
  try {
    const result = await api(`/reviews/${view.request_id}`, {
      method: "POST",
      headers: reviewerHeaders(),
      body: JSON.stringify({ approved, reviewer }),
    });
    card.classList.add("is-done");
    toast(
      approved
        ? `Approved. ${patientName(view.patient_id)} has been answered.`
        : `Rejected. ${patientName(view.patient_id)} was told staff will follow up.`,
    );
    state.signatures.reviews = "";
    await refreshStaff();
    if (result.patient_id === state.patientId) {
      state.signatures.chat = "";
      await loadHistory();
    }
  } catch (err) {
    card.querySelectorAll("button").forEach((b) => (b.disabled = false));
    if (err.status === 401) {
      els.keyRow.hidden = false;
      els.reviewerKey.focus();
      toast("This server needs a reviewer key. Enter it, then try again.");
    } else {
      toast(`Couldn't save the decision: ${err.detail || err.message}`);
    }
  }
}

function actorLabel(actor) {
  const [kind, name] = actor.split(":");
  if (kind === "agent") return `${name[0].toUpperCase()}${name.slice(1)} agent`;
  if (kind === "tool") return `Tool ${name}`;
  if (kind === "human") return `${name} (staff)`;
  if (kind === "patient") return "Patient";
  return "Workflow";
}

async function toggleAudit(row, requestId) {
  const open = row.querySelector(".audit");
  if (open) {
    open.remove();
    state.openAudits.delete(requestId);
    return;
  }
  state.openAudits.add(requestId);
  const entries = await api(`/requests/${requestId}/audit`);
  const list = el("ol", { class: "audit" });
  for (const e of entries) {
    const item = el("li");
    item.append(el("span", { class: "who", text: actorLabel(e.actor) }), ` ${e.action.replace(/_/g, " ")}`);
    list.append(item);
  }
  row.append(list);
}

function renderActivity(rows) {
  const signature = JSON.stringify(rows.map((r) => [r.id, r.status]));
  if (signature === state.signatures.activity) return;
  state.signatures.activity = signature;

  els.activityList.replaceChildren();
  if (!rows.length) {
    els.activityList.append(
      el("p", { class: "staff-empty", text: "No requests yet. Send a message from the patient side." }),
    );
    return;
  }
  for (const r of rows) {
    const row = el("div", { class: "activity-row" });
    const what = el("span", { class: "activity-what" });
    what.append(
      el("strong", { text: patientName(r.patient_id) }),
      el("span", { text: `${TYPE_LABELS[r.request_type] || "Urgent symptoms"}. ${r.summary || ""}` }),
    );
    const button = el("button", { type: "button", "aria-expanded": "false" });
    button.append(
      el("span", { class: "activity-time", text: clock(r.created_at) }),
      what,
      el("span", { class: "status", "data-status": r.status, text: STATUS_LABELS[r.status] || r.status }),
    );
    button.addEventListener("click", async () => {
      await toggleAudit(row, r.id);
      button.setAttribute("aria-expanded", String(Boolean(row.querySelector(".audit"))));
    });
    row.append(button);
    els.activityList.append(row);
    if (state.openAudits.has(r.id)) {
      state.openAudits.delete(r.id);
      toggleAudit(row, r.id);
    }
  }
}

async function refreshStaff() {
  try {
    const [reviews, activity] = await Promise.all([
      api("/reviews", { headers: reviewerHeaders() }),
      api("/requests?limit=50"),
    ]);
    renderReviews(reviews);
    renderActivity(activity);
  } catch (err) {
    if (err.status === 401) els.keyRow.hidden = false;
  }
}

// ------------------------------------------------------------------- start

async function start() {
  const patients = await api("/patients");
  for (const p of patients) {
    state.patients.set(p.patient_id, p.name);
    els.patientSelect.append(el("option", { value: p.patient_id, text: `${p.name} (${p.patient_id})` }));
  }
  state.patientId = patients[0]?.patient_id;

  els.patientSelect.addEventListener("change", async () => {
    state.patientId = els.patientSelect.value;
    state.signatures.chat = "";
    renderSuggestions();
    await loadHistory();
  });
  els.chatForm.addEventListener("submit", sendMessage);
  els.chatInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      els.chatForm.requestSubmit();
    }
  });
  els.tabReview.addEventListener("click", () => selectTab("review"));
  els.tabActivity.addEventListener("click", () => selectTab("activity"));
  els.reviewerKey.addEventListener("change", refreshStaff);

  renderSuggestions();
  await Promise.all([loadHistory(), refreshStaff()]);

  // Keep both sides current: a review approved on the right updates the chat on the left.
  setInterval(async () => {
    if (state.sending) return;
    await refreshStaff();
    if (state.history.some((h) => h.status === "awaiting_review")) await loadHistory();
  }, 4000);
}

start().catch((err) => toast(`Couldn't load the app: ${err.message}`));
