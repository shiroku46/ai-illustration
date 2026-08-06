"use strict";

const state = {
  candidates: [], categories: [], hardFailCategories: [], reviewScopes: [], decisions: [], selected: new Set(),
};
const $ = (id) => document.getElementById(id);
const APPROVAL_DECISIONS = new Set(["accept", "shortlist"]);

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}
function option(value, text = value) { const node = element("option", text); node.value = value; return node; }
function uniqueValues(key) { return [...new Set(state.candidates.map((item) => item[key]).filter(Boolean))].sort(); }
function populateSelect(id, values) { values.forEach((value) => $(id).append(option(value))); }
function checkedValues(selector) { return [...document.querySelectorAll(selector)].filter((i) => i.checked).map((i) => i.value).sort(); }

function filteredCandidates() {
  const checks = [["character-filter", "character_id"], ["role-filter", "role"], ["expression-filter", "expression"], ["pose-filter", "pose"], ["review-filter", "review_state"]];
  return state.candidates.filter((candidate) => checks.every(([control, key]) => !$(control).value || candidate[key] === $(control).value));
}

function metadataRows(candidate) {
  return [
    ["ID", candidate.id], ["役割", candidate.role], ["表情", candidate.expression], ["ポーズ", candidate.pose],
    ["ツール", candidate.tool_id], ["モデル", candidate.model_id], ["技術状態", candidate.technical_status],
    ["包装品質段階", candidate.quality_stage], ["最新レビュー範囲", candidate.review_scope],
    ["最新結果品質段階", candidate.review_resulting_quality_stage],
    ["最新ハード失敗", (candidate.review_hard_fail_categories || []).join(", ") || "—"],
    ["レビュー判定", candidate.review_state], ["ライセンス", candidate.license_status],
    ["サイズ", `${candidate.width}×${candidate.height}`], ["SHA-256", candidate.sha256],
    ["由来", JSON.stringify(candidate.provenance || {})],
  ];
}

function candidateCard(candidate, compact = false) {
  const card = element("article", undefined, "candidate-card");
  card.dataset.candidateId = candidate.id;
  const visual = element("div", undefined, "visual");
  if (candidate.image_available && candidate.image_url) {
    const image = document.createElement("img"); image.src = candidate.image_url;
    image.alt = `${candidate.character_id} ${candidate.expression} ${candidate.pose}`; image.loading = "lazy"; visual.append(image);
  } else visual.append(element("div", "画像未登録\nメタデータのみ", "placeholder"));
  card.append(visual, element("h3", candidate.id), element("p", `${candidate.character_id} / ${candidate.expression} / ${candidate.pose}`, "summary"));
  if (!compact) {
    const toggle = element("button", state.selected.has(candidate.id) ? "比較から外す" : "比較する");
    toggle.type = "button"; toggle.addEventListener("click", () => toggleSelection(candidate.id)); card.append(toggle);
  }
  const details = document.createElement("details"); details.append(element("summary", "メタデータ"));
  const table = element("dl", undefined, "metadata"); metadataRows(candidate).forEach(([key, value]) => table.append(element("dt", key), element("dd", value ?? "—")));
  details.append(table); card.append(details); return card;
}

function toggleSelection(id) {
  if (state.selected.has(id)) state.selected.delete(id); else if (state.selected.size < 4) state.selected.add(id);
  else $("status").textContent = "比較できる候補は最大4件です。"; render();
}
function renderList() {
  const list = $("candidate-list"); list.replaceChildren(); const candidates = filteredCandidates();
  candidates.forEach((candidate) => list.append(candidateCard(candidate)));
  $("status").textContent = `${candidates.length}件を表示中（全${state.candidates.length}件）`;
}
function selectedReviewCandidate() { return state.candidates.find((item) => item.id === $("review-candidate").value); }
function canLiveVerify(candidate) { return candidate?.technical_status === "technically_valid" && candidate?.image_available === true && Boolean(globalThis.crypto?.subtle); }
function creativeEligible(candidate) { return canLiveVerify(candidate) && candidate?.quality_stage === "technical_candidate"; }
function resultingStage(candidate, scope, decision) { return scope === "creative" && decision === "accept" ? "creative_candidate" : candidate?.quality_stage; }

function syncReviewControls() {
  const candidate = selectedReviewCandidate(); const scope = $("review-scope").value || "technical";
  [...$("review-scope").options].forEach((item) => { item.disabled = item.value === "creative" && !creativeEligible(candidate); });
  if ($("review-scope").selectedOptions[0]?.disabled) $("review-scope").value = "technical";
  const activeScope = $("review-scope").value || "technical";
  const hardFails = checkedValues("#hard-fail-list input");
  [...$("decision").options].forEach((item) => {
    const needsImage = APPROVAL_DECISIONS.has(item.value) || activeScope === "creative";
    item.disabled = needsImage && !canLiveVerify(candidate);
    if (activeScope === "creative" && item.value === "accept" && hardFails.length) item.disabled = true;
  });
  if ($("decision").selectedOptions[0]?.disabled) $("decision").value = state.decisions.includes("needs_revision") ? "needs_revision" : "reject";
  $("resulting-quality-stage").textContent = resultingStage(candidate, activeScope, $("decision").value) || "—";
}

function renderComparison() {
  const grid = $("comparison-grid"); grid.replaceChildren(); const selected = state.candidates.filter((item) => state.selected.has(item.id));
  selected.forEach((candidate) => grid.append(candidateCard(candidate, true)));
  if (!selected.length) grid.append(element("p", "候補カードの「比較する」を選択してください。", "empty"));
  $("selected-count").textContent = `${selected.length}/4`;
  const reviewCandidate = $("review-candidate"); const current = reviewCandidate.value; reviewCandidate.replaceChildren();
  selected.forEach((candidate) => reviewCandidate.append(option(candidate.id))); if (selected.some((item) => item.id === current)) reviewCandidate.value = current;
  syncReviewControls();
}
function render() { renderList(); renderComparison(); }
function utcTimestamp() { return new Date().toISOString().replace(/\.\d{3}Z$/, "Z"); }
function digestHex(buffer) { return [...new Uint8Array(buffer)].map((value) => value.toString(16).padStart(2, "0")).join(""); }
async function liveVerifiedChecksum(candidate) {
  if (!canLiveVerify(candidate) || !candidate.image_url) return null;
  const response = await fetch(candidate.image_url, { cache: "no-store" });
  if (!response.ok || response.headers.get("Content-Type") !== "image/png") return null;
  const bytes = await response.arrayBuffer(); const digest = digestHex(await crypto.subtle.digest("SHA-256", bytes));
  return digest === candidate.sha256 ? digest : null;
}
async function deterministicReviewId(identity) {
  if (!globalThis.crypto?.subtle) throw new Error("Web Crypto SHA-256 が利用できないためレビューを書き出せません。");
  return `review-${digestHex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(identity))).slice(0, 12)}`;
}

async function reviewDocument() {
  const candidate = selectedReviewCandidate(); if (!candidate) throw new Error("比較ボードから対象候補を選んでください。");
  if (!candidate.quality_stage) throw new Error("包装品質段階のない旧形式候補は新規レビューできません。");
  const reviewer = $("reviewer").value.trim(); if (!reviewer) throw new Error("レビュアー名を入力してください。");
  const decision = $("decision").value; const reviewScope = $("review-scope").value || "technical";
  const categories = checkedValues("#category-list input"); const hardFailCategories = checkedValues("#hard-fail-list input");
  if (reviewScope === "creative" && !creativeEligible(candidate)) throw new Error("創作レビューにはライブ検証可能な technical_candidate が必要です。");
  if (reviewScope === "creative" && decision === "accept" && hardFailCategories.length) throw new Error("ハード失敗がある候補を creative_candidate として採用できません。");
  if (APPROVAL_DECISIONS.has(decision) || reviewScope === "creative") {
    if (!await liveVerifiedChecksum(candidate)) throw new Error("この判定には、現在取得できるSHA-256一致済みPNGが必要です。");
  }
  const timestamp = utcTimestamp(); const resultingQualityStage = resultingStage(candidate, reviewScope, decision);
  const identity = [candidate.id, candidate.request_id, candidate.sha256, reviewer, decision, reviewScope, resultingQualityStage, timestamp, ...categories, ...hardFailCategories].join("\n");
  const documentValue = {
    kind: "review-decision", schema_version: "1.0", id: await deterministicReviewId(identity),
    candidate_ref: candidate.id, candidate_request_ref: candidate.request_id, candidate_sha256: candidate.sha256,
    decision, reviewer, timestamp, categories, review_scope: reviewScope,
    resulting_quality_stage: resultingQualityStage, hard_fail_categories: hardFailCategories,
  };
  const notes = $("notes").value.trim(); if (notes) documentValue.notes = notes; return documentValue;
}
async function downloadReview() {
  try {
    const documentValue = await reviewDocument(); const text = `${JSON.stringify(documentValue, null, 2)}\n`; $("review-preview").textContent = text;
    const blob = new Blob([text], { type: "application/json" }); const url = URL.createObjectURL(blob); const link = document.createElement("a");
    link.href = url; link.download = `${documentValue.id}.json`; link.click(); URL.revokeObjectURL(url);
  } catch (error) { $("review-preview").textContent = error.message; }
}
function appendChecks(containerId, values) {
  values.forEach((value) => { const label = element("label", undefined, "category"); const input = document.createElement("input"); input.type = "checkbox"; input.value = value; input.addEventListener("change", syncReviewControls); label.append(input, document.createTextNode(value)); $(containerId).append(label); });
}
async function initialize() {
  const response = await fetch("/api/candidates", { cache: "no-store" }); if (!response.ok) throw new Error(`候補APIの取得に失敗しました: ${response.status}`);
  const payload = await response.json(); state.candidates = payload.candidates || []; state.categories = payload.categories || [];
  state.hardFailCategories = payload.hard_fail_categories || []; state.reviewScopes = payload.review_scopes || []; state.decisions = payload.decisions || [];
  populateSelect("character-filter", uniqueValues("character_id")); populateSelect("expression-filter", uniqueValues("expression")); populateSelect("pose-filter", uniqueValues("pose")); populateSelect("review-filter", uniqueValues("review_state"));
  state.reviewScopes.forEach((value) => $("review-scope").append(option(value))); $("review-scope").value = "technical";
  state.decisions.forEach((value) => $("decision").append(option(value))); appendChecks("category-list", state.categories); appendChecks("hard-fail-list", state.hardFailCategories);
  ["character-filter", "role-filter", "expression-filter", "pose-filter", "review-filter"].forEach((id) => $(id).addEventListener("change", render));
  ["review-candidate", "review-scope", "decision"].forEach((id) => $(id).addEventListener("change", syncReviewControls));
  $("clear-selection").addEventListener("click", () => { state.selected.clear(); render(); }); $("download-review").addEventListener("click", downloadReview); render();
}
initialize().catch((error) => { $("status").textContent = error.message; });
