"use strict";

const state = { candidates: [], categories: [], decisions: [], selected: new Set() };
const $ = (id) => document.getElementById(id);
const APPROVAL_DECISIONS = new Set(["accept", "shortlist"]);

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function option(value, text = value) {
  const node = element("option", text);
  node.value = value;
  return node;
}

function uniqueValues(key) {
  return [...new Set(state.candidates.map((item) => item[key]).filter(Boolean))].sort();
}

function populateSelect(id, values) {
  const select = $(id);
  values.forEach((value) => select.append(option(value)));
}

function filteredCandidates() {
  const checks = [
    ["character-filter", "character_id"],
    ["role-filter", "role"],
    ["expression-filter", "expression"],
    ["pose-filter", "pose"],
    ["review-filter", "review_state"],
  ];
  return state.candidates.filter((candidate) =>
    checks.every(([control, key]) => !$(control).value || candidate[key] === $(control).value)
  );
}

function metadataRows(candidate) {
  return [
    ["ID", candidate.id], ["役割", candidate.role], ["表情", candidate.expression],
    ["ポーズ", candidate.pose], ["ツール", candidate.tool_id], ["モデル", candidate.model_id],
    ["状態", candidate.candidate_status], ["レビュー", candidate.review_state],
    ["ライセンス", candidate.license_status], ["サイズ", `${candidate.width}×${candidate.height}`],
    ["SHA-256", candidate.sha256], ["由来", JSON.stringify(candidate.provenance || {})],
  ];
}

function candidateCard(candidate, compact = false) {
  const card = element("article", undefined, "candidate-card");
  card.dataset.candidateId = candidate.id;
  const visual = element("div", undefined, "visual");
  if (candidate.image_available && candidate.image_url) {
    const image = document.createElement("img");
    image.src = candidate.image_url;
    image.alt = `${candidate.character_id} ${candidate.expression} ${candidate.pose}`;
    image.loading = "lazy";
    visual.append(image);
  } else {
    visual.append(element("div", "画像未登録\nメタデータのみ", "placeholder"));
  }
  card.append(visual);
  card.append(element("h3", candidate.id));
  card.append(element("p", `${candidate.character_id} / ${candidate.expression} / ${candidate.pose}`, "summary"));
  if (!compact) {
    const toggle = element("button", state.selected.has(candidate.id) ? "比較から外す" : "比較する");
    toggle.type = "button";
    toggle.addEventListener("click", () => toggleSelection(candidate.id));
    card.append(toggle);
  }
  const details = document.createElement("details");
  details.append(element("summary", "メタデータ"));
  const table = element("dl", undefined, "metadata");
  metadataRows(candidate).forEach(([key, value]) => {
    table.append(element("dt", key));
    table.append(element("dd", value ?? "—"));
  });
  details.append(table);
  card.append(details);
  return card;
}

function toggleSelection(id) {
  if (state.selected.has(id)) state.selected.delete(id);
  else if (state.selected.size < 4) state.selected.add(id);
  else $("status").textContent = "比較できる候補は最大4件です。";
  render();
}

function renderList() {
  const list = $("candidate-list");
  list.replaceChildren();
  const candidates = filteredCandidates();
  candidates.forEach((candidate) => list.append(candidateCard(candidate)));
  $("status").textContent = `${candidates.length}件を表示中（全${state.candidates.length}件）`;
}

function selectedReviewCandidate() {
  return state.candidates.find((item) => item.id === $("review-candidate").value);
}

function canApprove(candidate) {
  return candidate?.candidate_status === "technically_valid" && candidate?.image_available === true;
}

function syncDecisionOptions() {
  const candidate = selectedReviewCandidate();
  const reviewable = canApprove(candidate);
  [...$("decision").options].forEach((item) => {
    item.disabled = !reviewable && APPROVAL_DECISIONS.has(item.value);
  });
  if ($("decision").selectedOptions[0]?.disabled) {
    $("decision").value = state.decisions.includes("needs_revision") ? "needs_revision" : "reject";
  }
}

function renderComparison() {
  const grid = $("comparison-grid");
  grid.replaceChildren();
  const selected = state.candidates.filter((item) => state.selected.has(item.id));
  selected.forEach((candidate) => grid.append(candidateCard(candidate, true)));
  if (!selected.length) grid.append(element("p", "候補カードの「比較する」を選択してください。", "empty"));
  $("selected-count").textContent = `${selected.length}/4`;

  const reviewCandidate = $("review-candidate");
  const current = reviewCandidate.value;
  reviewCandidate.replaceChildren();
  selected.forEach((candidate) => reviewCandidate.append(option(candidate.id)));
  if (selected.some((item) => item.id === current)) reviewCandidate.value = current;
  syncDecisionOptions();
}

function render() {
  renderList();
  renderComparison();
}

function utcTimestamp() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

function reviewDocument() {
  const candidate = selectedReviewCandidate();
  if (!candidate) throw new Error("比較ボードから対象候補を選んでください。");
  const reviewer = $("reviewer").value.trim();
  if (!reviewer) throw new Error("レビュアー名を入力してください。");
  const decision = $("decision").value;
  if (APPROVAL_DECISIONS.has(decision) && !canApprove(candidate)) {
    throw new Error("採用または候補入りには technically_valid かつ検証済み画像の候補が必要です。");
  }
  const timestamp = utcTimestamp();
  const categories = [...document.querySelectorAll("#category-list input:checked")]
    .map((input) => input.value).sort();
  const stamp = timestamp.replace(/\D/g, "");
  const documentValue = {
    kind: "review-decision",
    schema_version: "1.0",
    id: `review-${candidate.id}-${decision.replace("_", "-")}-${stamp}`,
    candidate_ref: candidate.id,
    candidate_request_ref: candidate.request_id,
    candidate_sha256: candidate.sha256,
    decision,
    reviewer,
    timestamp,
    categories,
  };
  const notes = $("notes").value.trim();
  if (notes) documentValue.notes = notes;
  return documentValue;
}

function downloadReview() {
  try {
    const documentValue = reviewDocument();
    const text = `${JSON.stringify(documentValue, null, 2)}\n`;
    $("review-preview").textContent = text;
    const blob = new Blob([text], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${documentValue.id}.json`;
    link.click();
    URL.revokeObjectURL(url);
  } catch (error) {
    $("review-preview").textContent = error.message;
  }
}

async function initialize() {
  const response = await fetch("/api/candidates", { cache: "no-store" });
  if (!response.ok) throw new Error(`候補APIの取得に失敗しました: ${response.status}`);
  const payload = await response.json();
  state.candidates = payload.candidates || [];
  state.categories = payload.categories || [];
  state.decisions = payload.decisions || [];
  populateSelect("character-filter", uniqueValues("character_id"));
  populateSelect("expression-filter", uniqueValues("expression"));
  populateSelect("pose-filter", uniqueValues("pose"));
  populateSelect("review-filter", uniqueValues("review_state"));
  state.decisions.forEach((value) => $("decision").append(option(value)));
  state.categories.forEach((value) => {
    const label = element("label", undefined, "category");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = value;
    label.append(input, document.createTextNode(value));
    $("category-list").append(label);
  });
  ["character-filter", "role-filter", "expression-filter", "pose-filter", "review-filter"]
    .forEach((id) => $(id).addEventListener("change", render));
  $("review-candidate").addEventListener("change", syncDecisionOptions);
  $("clear-selection").addEventListener("click", () => { state.selected.clear(); render(); });
  $("download-review").addEventListener("click", downloadReview);
  render();
}

initialize().catch((error) => { $("status").textContent = error.message; });
