"use strict";

const TECHNICAL_CANDIDATE = "technical_candidate";
const TRANSPORT_SMOKE_OUTPUT = "transport_smoke_output";
const CREATIVE_CANDIDATE = "creative_candidate";
const PACKAGED_STAGES = new Set([TRANSPORT_SMOKE_OUTPUT, TECHNICAL_CANDIDATE]);
const REVIEW_SCOPES = new Set(["technical", "creative"]);
const APPROVAL_DECISIONS = new Set(["accept", "shortlist"]);
const SAFE_ASSET_URL = /^\/assets\/[a-z0-9]+(?:-[a-z0-9]+)*$/;

const state = {
  candidates: [],
  categories: [],
  hardFailCategories: [],
  reviewScopes: [],
  decisions: [],
  selected: new Set(),
  verification: { key: null, status: "idle", digest: null },
};

const $ = (id) => document.getElementById(id);

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

function sortedUnique(values) {
  return [...new Set(values)].sort();
}

function checkedValues(selector) {
  return sortedUnique(
    [...document.querySelectorAll(selector)]
      .filter((input) => input.checked)
      .map((input) => input.value),
  );
}

function uniqueValues(key) {
  return sortedUnique(state.candidates.map((item) => item[key]).filter(Boolean));
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
    ["quality-filter", "quality_stage"],
    ["review-filter", "review_state"],
  ];
  return state.candidates.filter((candidate) =>
    checks.every(([control, key]) => !$(control).value || candidate[key] === $(control).value),
  );
}

function safeAssetUrl(candidate) {
  return typeof candidate?.image_url === "string" && SAFE_ASSET_URL.test(candidate.image_url)
    ? candidate.image_url
    : null;
}

function stageLabel(stage) {
  if (stage === TRANSPORT_SMOKE_OUTPUT) return "transport smoke（候補外）";
  if (stage === TECHNICAL_CANDIDATE) return "technical candidate（未承認）";
  if (stage === CREATIVE_CANDIDATE) return "creative candidate（レビュー結果）";
  return "legacy / stage未登録";
}

function metadataRows(candidate) {
  return [
    ["ID", candidate.id],
    ["役割", candidate.role],
    ["表情", candidate.expression],
    ["ポーズ", candidate.pose],
    ["ツール", candidate.tool_id],
    ["モデル", candidate.model_id],
    ["技術状態", candidate.technical_status ?? candidate.candidate_status],
    ["入力品質段階", candidate.quality_stage ?? "未登録"],
    ["最新判定", candidate.review_state],
    ["最新scope", candidate.review_scope ?? "legacy / 未レビュー"],
    ["最新結果段階", candidate.review_resulting_quality_stage ?? "—"],
    ["最新hard fail", (candidate.review_hard_fail_categories || []).join(", ") || "なし"],
    ["ライセンス", candidate.license_status],
    ["サイズ", `${candidate.width}×${candidate.height}`],
    ["SHA-256", candidate.sha256],
    ["由来", JSON.stringify(candidate.provenance || {})],
  ];
}

function statusBadge(text, modifier) {
  const badge = element("span", text, `badge ${modifier}`);
  return badge;
}

function candidateCard(candidate, compact = false) {
  const card = element("article", undefined, "candidate-card");
  card.dataset.candidateId = candidate.id;
  card.dataset.qualityStage = candidate.quality_stage || "legacy";

  const visual = element("div", undefined, "visual");
  const assetUrl = safeAssetUrl(candidate);
  if (candidate.image_available && assetUrl) {
    const image = document.createElement("img");
    image.src = assetUrl;
    image.alt = `${candidate.character_id} ${candidate.expression} ${candidate.pose}`;
    image.loading = "lazy";
    visual.append(image);
  } else {
    visual.append(element("div", "画像未登録・検証不可\nメタデータのみ", "placeholder"));
  }
  card.append(visual);
  card.append(element("h3", candidate.id));
  card.append(element("p", `${candidate.character_id} / ${candidate.expression} / ${candidate.pose}`, "summary"));

  const badges = element("div", undefined, "badges");
  badges.append(statusBadge(
    candidate.technical_status ?? candidate.candidate_status ?? "unknown",
    candidate.technical_status === "technically_valid" ? "badge-technical" : "badge-muted",
  ));
  const stageModifier = candidate.quality_stage === TECHNICAL_CANDIDATE
    ? "badge-candidate"
    : candidate.quality_stage === TRANSPORT_SMOKE_OUTPUT
      ? "badge-smoke"
      : "badge-warning";
  badges.append(statusBadge(stageLabel(candidate.quality_stage), stageModifier));
  if (candidate.review_resulting_quality_stage === CREATIVE_CANDIDATE) {
    badges.append(statusBadge("creative承認済み", "badge-creative"));
  }
  card.append(badges);

  if (!compact) {
    const toggle = element("button", state.selected.has(candidate.id) ? "比較から外す" : "比較する");
    toggle.type = "button";
    toggle.addEventListener("click", () => toggleSelection(candidate.id));
    card.append(toggle);
  }

  const details = document.createElement("details");
  details.append(element("summary", "メタデータと品質状態"));
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

function candidateKey(candidate) {
  return candidate ? `${candidate.id}:${candidate.sha256}` : null;
}

function webCryptoAvailable() {
  return Boolean(globalThis.crypto?.subtle && globalThis.TextEncoder);
}

function verificationIsCurrent(candidate) {
  return state.verification.key === candidateKey(candidate)
    && state.verification.status === "verified"
    && state.verification.digest === candidate?.sha256;
}

function canAttemptVerification(candidate) {
  return Boolean(
    candidate
    && candidate.technical_status === "technically_valid"
    && candidate.image_available === true
    && safeAssetUrl(candidate)
    && webCryptoAvailable(),
  );
}

function creativeVocabularyReady() {
  return state.reviewScopes.includes("creative") && state.hardFailCategories.length > 0;
}

function canCreativeReview(candidate) {
  return Boolean(
    candidate
    && candidate.quality_stage === TECHNICAL_CANDIDATE
    && verificationIsCurrent(candidate)
    && creativeVocabularyReady(),
  );
}

function selectedScope() {
  return $("review-scope").value || "technical";
}

function selectedDecision() {
  return $("decision").value;
}

function selectedHardFails() {
  return checkedValues("#hard-fail-list input:checked");
}

function resultingStage(candidate, scope = selectedScope(), decision = selectedDecision(), hardFails = selectedHardFails()) {
  if (!candidate || !PACKAGED_STAGES.has(candidate.quality_stage)) return null;
  if (scope === "creative" && decision === "accept" && hardFails.length === 0) {
    return CREATIVE_CANDIDATE;
  }
  return candidate.quality_stage;
}

function liveRequired(scope, decision) {
  return scope === "creative" || APPROVAL_DECISIONS.has(decision);
}

function reviewGate(candidate) {
  const scope = selectedScope();
  const decision = selectedDecision();
  const hardFails = selectedHardFails();
  const reviewer = $("reviewer").value.trim();
  const result = resultingStage(candidate, scope, decision, hardFails);

  if (!candidate) return { allowed: false, result, message: "比較ボードから対象候補を選択してください。" };
  if (!PACKAGED_STAGES.has(candidate.quality_stage)) {
    return { allowed: false, result, message: "この候補は品質段階が未登録のlegacy recordです。再パッケージ後にレビューしてください。" };
  }
  if (!webCryptoAvailable()) {
    return { allowed: false, result, message: "Web Cryptoが利用できないため、検証済みの決定IDを作成できません。" };
  }
  if (!REVIEW_SCOPES.has(scope) || !state.reviewScopes.includes(scope)) {
    return { allowed: false, result, message: "APIが許可していないレビュー範囲です。" };
  }
  if (!state.decisions.includes(decision)) {
    return { allowed: false, result, message: "APIが許可していない判定です。" };
  }
  if (!reviewer) return { allowed: false, result, message: "レビュアー名を入力してください。" };

  if (scope === "creative") {
    if (candidate.quality_stage !== TECHNICAL_CANDIDATE) {
      return { allowed: false, result, message: "creative reviewはtechnical_candidateにのみ実行できます。transport smokeは候補ではありません。" };
    }
    if (!creativeVocabularyReady()) {
      return { allowed: false, result, message: "APIのcreative scopeまたはhard-fail語彙が不足しているため、creative reviewを閉じています。" };
    }
    if (!verificationIsCurrent(candidate)) {
      return { allowed: false, result, message: "creative reviewには現在取得できるPNGのSHA-256再検証が必要です。" };
    }
    if (decision === "accept" && hardFails.length > 0) {
      return { allowed: false, result, message: "hard failが選択されたcreative acceptは作成できません。rejectまたはneeds_revisionに変更してください。" };
    }
  }

  if (APPROVAL_DECISIONS.has(decision) && !verificationIsCurrent(candidate)) {
    return { allowed: false, result, message: "acceptまたはshortlistには現在取得できるPNGのSHA-256再検証が必要です。" };
  }
  if (liveRequired(scope, decision) && !verificationIsCurrent(candidate)) {
    return { allowed: false, result, message: "このレビュー範囲・判定にはlive image verificationが必要です。" };
  }
  return { allowed: true, result, message: "レビューJSONを作成できます。ダウンロード直前にもPNGを再検証します。" };
}

function syncScopeOptions(candidate) {
  const creative = [...$("review-scope").options].find((item) => item.value === "creative");
  if (creative) creative.disabled = !canCreativeReview(candidate);
  if (selectedScope() === "creative" && creative?.disabled) $("review-scope").value = "technical";
}

function syncDecisionOptions(candidate) {
  const scope = selectedScope();
  const hardFails = selectedHardFails();
  [...$("decision").options].forEach((item) => {
    const needsLive = APPROVAL_DECISIONS.has(item.value) || scope === "creative";
    const creativeHardFail = scope === "creative" && item.value === "accept" && hardFails.length > 0;
    item.disabled = (needsLive && !verificationIsCurrent(candidate)) || creativeHardFail;
  });
  if ($("decision").selectedOptions[0]?.disabled) {
    $("decision").value = state.decisions.includes("needs_revision") ? "needs_revision" : "reject";
  }
}

function syncReviewControls() {
  const candidate = selectedReviewCandidate();
  syncScopeOptions(candidate);
  syncDecisionOptions(candidate);
  const gate = reviewGate(candidate);
  $("technical-status").textContent = candidate?.technical_status ?? candidate?.candidate_status ?? "—";
  $("input-quality-stage").textContent = candidate ? stageLabel(candidate.quality_stage) : "—";
  $("resulting-stage").textContent = gate.result ? stageLabel(gate.result) : "—";
  $("review-gate-status").textContent = gate.message;
  $("review-gate-status").className = gate.allowed ? "gate-ok" : "gate-closed";
  $("download-review").disabled = !gate.allowed;
  $("hard-fail-fieldset").disabled = !candidate || !PACKAGED_STAGES.has(candidate.quality_stage);
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

  const candidate = selectedReviewCandidate();
  if (state.verification.key !== candidateKey(candidate)) void verifySelectedCandidate();
  else syncReviewControls();
}

function render() {
  renderList();
  renderComparison();
}

function utcTimestamp() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

function digestHex(buffer) {
  return [...new Uint8Array(buffer)].map((value) => value.toString(16).padStart(2, "0")).join("");
}

async function sha256Text(text) {
  if (!webCryptoAvailable()) throw new Error("Web Cryptoが利用できません。");
  const bytes = new TextEncoder().encode(text);
  return digestHex(await crypto.subtle.digest("SHA-256", bytes));
}

async function liveVerifiedChecksum(candidate) {
  const assetUrl = safeAssetUrl(candidate);
  if (!canAttemptVerification(candidate) || !assetUrl) return null;
  const response = await fetch(assetUrl, { cache: "no-store", credentials: "omit", redirect: "error" });
  const contentType = (response.headers.get("Content-Type") || "").split(";", 1)[0].trim().toLowerCase();
  if (!response.ok || contentType !== "image/png") return null;
  const bytes = await response.arrayBuffer();
  const digest = digestHex(await crypto.subtle.digest("SHA-256", bytes));
  return digest === candidate.sha256 ? digest : null;
}

async function verifySelectedCandidate() {
  const candidate = selectedReviewCandidate();
  const key = candidateKey(candidate);
  state.verification = { key, status: "idle", digest: null };
  syncReviewControls();
  if (!candidate) return;
  if (!candidate.image_available || !safeAssetUrl(candidate)) {
    state.verification = { key, status: "unavailable", digest: null };
    syncReviewControls();
    return;
  }
  if (candidate.technical_status !== "technically_valid") {
    state.verification = { key, status: "not-technical", digest: null };
    syncReviewControls();
    return;
  }
  if (!webCryptoAvailable()) {
    state.verification = { key, status: "unsupported", digest: null };
    syncReviewControls();
    return;
  }
  state.verification = { key, status: "verifying", digest: null };
  $("review-gate-status").textContent = "PNGを再取得してSHA-256を検証しています…";
  $("download-review").disabled = true;
  try {
    const digest = await liveVerifiedChecksum(candidate);
    if (candidateKey(selectedReviewCandidate()) !== key) return;
    state.verification = {
      key,
      status: digest ? "verified" : "failed",
      digest,
    };
  } catch (_error) {
    if (candidateKey(selectedReviewCandidate()) !== key) return;
    state.verification = { key, status: "failed", digest: null };
  }
  syncReviewControls();
}

function semanticIdentity({ candidate, reviewer, decision, scope, result, timestamp, categories, hardFails }) {
  return [
    candidate.id,
    candidate.request_id,
    candidate.sha256,
    reviewer,
    decision,
    scope,
    result,
    timestamp,
    categories.join(","),
    hardFails.join(","),
  ].join("\n");
}

async function reviewDocument() {
  const candidate = selectedReviewCandidate();
  let gate = reviewGate(candidate);
  if (!gate.allowed) throw new Error(gate.message);

  const scope = selectedScope();
  const decision = selectedDecision();
  if (liveRequired(scope, decision)) {
    const verified = await liveVerifiedChecksum(candidate);
    const key = candidateKey(candidate);
    state.verification = { key, status: verified ? "verified" : "failed", digest: verified };
    syncReviewControls();
    if (!verified) throw new Error("ダウンロード直前のPNG再検証に失敗しました。レビューJSONは作成しません。");
  }

  gate = reviewGate(candidate);
  if (!gate.allowed || !gate.result) throw new Error(gate.message);

  const reviewer = $("reviewer").value.trim();
  const categories = checkedValues("#category-list input:checked");
  const hardFails = selectedHardFails();
  const timestamp = utcTimestamp();
  const identity = semanticIdentity({
    candidate,
    reviewer,
    decision,
    scope,
    result: gate.result,
    timestamp,
    categories,
    hardFails,
  });
  const suffix = (await sha256Text(identity)).slice(0, 12);
  const documentValue = {
    kind: "review-decision",
    schema_version: "1.0",
    id: `review-${candidate.id}-${suffix}`,
    candidate_ref: candidate.id,
    candidate_request_ref: candidate.request_id,
    candidate_sha256: candidate.sha256,
    decision,
    reviewer,
    timestamp,
    categories,
    review_scope: scope,
    resulting_quality_stage: gate.result,
    hard_fail_categories: hardFails,
  };
  const notes = $("notes").value.trim();
  if (notes) documentValue.notes = notes;
  return documentValue;
}

async function downloadReview() {
  try {
    const documentValue = await reviewDocument();
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
    $("review-preview").textContent = error instanceof Error ? error.message : String(error);
  }
}

function appendCheckboxes(containerId, values, className) {
  const container = $(containerId);
  container.replaceChildren();
  values.forEach((value) => {
    const label = element("label", undefined, className);
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = value;
    label.append(input, document.createTextNode(value));
    container.append(label);
  });
}

function initializeScopeSelect() {
  const select = $("review-scope");
  select.replaceChildren();
  const scopes = ["technical", ...state.reviewScopes.filter((value) => value !== "technical")]
    .filter((value) => REVIEW_SCOPES.has(value));
  sortedUnique(scopes).sort((a, b) => (a === "technical" ? -1 : b === "technical" ? 1 : a.localeCompare(b)))
    .forEach((value) => select.append(option(value, value === "technical" ? "technical（技術レビュー）" : "creative（所有者の絵柄・人物承認）")));
  select.value = "technical";
}

async function initialize() {
  const response = await fetch("/api/candidates", { cache: "no-store", credentials: "omit", redirect: "error" });
  if (!response.ok) throw new Error(`候補APIの取得に失敗しました: ${response.status}`);
  const payload = await response.json();
  state.candidates = Array.isArray(payload.candidates) ? payload.candidates : [];
  state.categories = Array.isArray(payload.categories) ? sortedUnique(payload.categories.filter((value) => typeof value === "string")) : [];
  state.hardFailCategories = Array.isArray(payload.hard_fail_categories)
    ? sortedUnique(payload.hard_fail_categories.filter((value) => typeof value === "string"))
    : [];
  state.reviewScopes = Array.isArray(payload.review_scopes)
    ? sortedUnique(payload.review_scopes.filter((value) => REVIEW_SCOPES.has(value)))
    : ["technical"];
  if (!state.reviewScopes.includes("technical")) state.reviewScopes.push("technical");
  state.decisions = Array.isArray(payload.decisions) ? sortedUnique(payload.decisions.filter((value) => typeof value === "string")) : [];

  populateSelect("character-filter", uniqueValues("character_id"));
  populateSelect("expression-filter", uniqueValues("expression"));
  populateSelect("pose-filter", uniqueValues("pose"));
  populateSelect("quality-filter", uniqueValues("quality_stage"));
  populateSelect("review-filter", uniqueValues("review_state"));
  state.decisions.forEach((value) => $("decision").append(option(value)));
  initializeScopeSelect();
  appendCheckboxes("category-list", state.categories, "category");
  appendCheckboxes("hard-fail-list", state.hardFailCategories, "category hard-fail");

  ["character-filter", "role-filter", "expression-filter", "pose-filter", "quality-filter", "review-filter"]
    .forEach((id) => $(id).addEventListener("change", render));
  $("review-candidate").addEventListener("change", () => void verifySelectedCandidate());
  $("review-scope").addEventListener("change", syncReviewControls);
  $("decision").addEventListener("change", syncReviewControls);
  $("reviewer").addEventListener("input", syncReviewControls);
  $("hard-fail-list").addEventListener("change", syncReviewControls);
  $("clear-selection").addEventListener("click", () => {
    state.selected.clear();
    state.verification = { key: null, status: "idle", digest: null };
    render();
  });
  $("download-review").addEventListener("click", downloadReview);
  render();
}

initialize().catch((error) => {
  $("status").textContent = error instanceof Error ? error.message : String(error);
  $("download-review").disabled = true;
});
