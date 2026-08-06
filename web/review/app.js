"use strict";

const PACKAGED_QUALITY_STAGES = new Set(["transport_smoke_output", "technical_candidate"]);
const APPROVAL_DECISIONS = new Set(["accept", "shortlist"]);
const state = {
  candidates: [],
  categories: [],
  hardFailCategories: [],
  reviewScopes: [],
  decisions: [],
  selected: new Set(),
  liveVerification: new Map(),
  controlEpoch: 0,
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

function uniqueValues(key) {
  return [...new Set(state.candidates.map((item) => item[key]).filter(Boolean))].sort();
}

function populateSelect(id, values) {
  values.forEach((value) => $(id).append(option(value)));
}

function checkedValues(selector) {
  return [...document.querySelectorAll(selector)]
    .filter((input) => input.checked)
    .map((input) => input.value)
    .sort();
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
    ["ID", candidate.id],
    ["役割", candidate.role],
    ["表情", candidate.expression],
    ["ポーズ", candidate.pose],
    ["ツール", candidate.tool_id],
    ["モデル", candidate.model_id],
    ["技術状態", candidate.technical_status],
    ["包装品質段階", candidate.quality_stage],
    ["最新レビュー範囲", candidate.review_scope],
    ["最新結果品質段階", candidate.review_resulting_quality_stage],
    ["最新ハード失敗", (candidate.review_hard_fail_categories || []).join(", ") || "—"],
    ["レビュー判定", candidate.review_state],
    ["ライセンス", candidate.license_status],
    ["サイズ", `${candidate.width}×${candidate.height}`],
    ["SHA-256", candidate.sha256],
    ["由来", JSON.stringify(candidate.provenance || {})],
  ];
}

function qualitySummary(candidate) {
  const summary = element("dl", undefined, "quality-summary");
  const rows = [
    ["technical_status", candidate.technical_status],
    ["quality_stage", candidate.quality_stage],
    ["latest review_scope", candidate.review_scope],
    ["latest resulting stage", candidate.review_resulting_quality_stage],
    ["latest hard fails", (candidate.review_hard_fail_categories || []).join(", ") || "—"],
  ];
  rows.forEach(([key, value]) => {
    summary.append(element("dt", key), element("dd", value ?? "—"));
  });
  return summary;
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

  card.append(
    visual,
    element("h3", candidate.id),
    element("p", `${candidate.character_id} / ${candidate.expression} / ${candidate.pose}`, "summary"),
    qualitySummary(candidate),
  );

  if (!compact) {
    const toggle = element("button", state.selected.has(candidate.id) ? "比較から外す" : "比較する");
    toggle.type = "button";
    toggle.addEventListener("click", () => toggleSelection(candidate.id));
    card.append(toggle);
  }

  const details = document.createElement("details");
  details.append(element("summary", "全メタデータ"));
  const table = element("dl", undefined, "metadata");
  metadataRows(candidate).forEach(([key, value]) => {
    table.append(element("dt", key), element("dd", value ?? "—"));
  });
  details.append(table);
  card.append(details);
  return card;
}

function toggleSelection(id) {
  if (state.selected.has(id)) {
    state.selected.delete(id);
  } else if (state.selected.size < 4) {
    state.selected.add(id);
  } else {
    $("status").textContent = "比較できる候補は最大4件です。";
  }
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

function hasPackagedQualityStage(candidate) {
  return PACKAGED_QUALITY_STAGES.has(candidate?.quality_stage);
}

function canAttemptLiveVerification(candidate) {
  return Boolean(
    candidate?.technical_status === "technically_valid" &&
    candidate?.image_available === true &&
    candidate?.image_url &&
    globalThis.crypto?.subtle
  );
}

function liveVerificationKey(candidate) {
  return candidate ? `${candidate.id}:${candidate.sha256}` : "";
}

function hasKnownLiveVerification(candidate) {
  return state.liveVerification.get(liveVerificationKey(candidate)) === true;
}

function creativeMetadataEligible(candidate) {
  return candidate?.quality_stage === "technical_candidate" && canAttemptLiveVerification(candidate);
}

function resultingStage(candidate, scope, decision) {
  if (scope === "creative" && decision === "accept") return "creative_candidate";
  return candidate?.quality_stage;
}

function digestHex(buffer) {
  return [...new Uint8Array(buffer)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

async function liveVerifiedChecksum(candidate) {
  if (!canAttemptLiveVerification(candidate)) return null;
  try {
    const response = await fetch(candidate.image_url, { cache: "no-store" });
    const mediaType = (response.headers.get("Content-Type") || "").split(";", 1)[0].trim();
    if (!response.ok || mediaType !== "image/png") return null;
    const bytes = await response.arrayBuffer();
    const digest = digestHex(await crypto.subtle.digest("SHA-256", bytes));
    return digest === candidate.sha256 ? digest : null;
  } catch (_error) {
    return null;
  }
}

async function refreshLiveVerification(candidate) {
  const key = liveVerificationKey(candidate);
  if (!key) return false;
  const verified = Boolean(await liveVerifiedChecksum(candidate));
  state.liveVerification.set(key, verified);
  return verified;
}

function applyReviewControls(candidate, liveVerified) {
  const packaged = hasPackagedQualityStage(candidate);
  const scopeSelect = $("review-scope");

  [...scopeSelect.options].forEach((item) => {
    item.disabled = item.value === "creative" && !(creativeMetadataEligible(candidate) && liveVerified);
  });
  if (!packaged || scopeSelect.selectedOptions[0]?.disabled) scopeSelect.value = "technical";

  const activeScope = scopeSelect.value || "technical";
  const hardFails = checkedValues("#hard-fail-list input");
  [...$("decision").options].forEach((item) => {
    const needsLiveImage = APPROVAL_DECISIONS.has(item.value) || activeScope === "creative";
    item.disabled = !packaged || (needsLiveImage && !liveVerified);
    if (activeScope === "creative" && item.value === "accept" && hardFails.length) {
      item.disabled = true;
    }
  });
  if ($("decision").selectedOptions[0]?.disabled) {
    const fallback = state.decisions.includes("needs_revision") ? "needs_revision" : "reject";
    $("decision").value = fallback;
  }

  const webCryptoAvailable = Boolean(globalThis.crypto?.subtle);
  $("download-review").disabled = !candidate || !packaged || !webCryptoAvailable;
  $("resulting-quality-stage").textContent = resultingStage(
    candidate,
    activeScope,
    $("decision").value,
  ) || "—";

  let gateMessage = "技術レビューです。創作品質の承認にはなりません。";
  if (!candidate) gateMessage = "比較ボードから対象候補を選んでください。";
  else if (!packaged) gateMessage = "包装品質段階のない旧形式候補は新規レビューできません。";
  else if (!webCryptoAvailable) gateMessage = "Web Crypto SHA-256 が利用できないため書き出しできません。";
  else if (activeScope === "creative" && hardFails.length) gateMessage = "ハード失敗があるため creative accept は禁止されています。";
  else if (activeScope === "creative") gateMessage = "創作レビューです。書き出し時にもPNGのSHA-256を再検証します。";
  else if (canAttemptLiveVerification(candidate) && !liveVerified) gateMessage = "ライブPNGのSHA-256検証に失敗したため採用判定を閉じています。";
  $("review-gate-status").textContent = gateMessage;
}

async function syncReviewControls(refreshVerification = false) {
  const candidate = selectedReviewCandidate();
  const epoch = ++state.controlEpoch;
  applyReviewControls(candidate, hasKnownLiveVerification(candidate));

  if (refreshVerification && canAttemptLiveVerification(candidate)) {
    const verified = await refreshLiveVerification(candidate);
    if (epoch === state.controlEpoch && selectedReviewCandidate()?.id === candidate.id) {
      applyReviewControls(candidate, verified);
    }
  }
}

function renderComparison() {
  const grid = $("comparison-grid");
  grid.replaceChildren();
  const selected = state.candidates.filter((item) => state.selected.has(item.id));
  selected.forEach((candidate) => grid.append(candidateCard(candidate, true)));
  if (!selected.length) {
    grid.append(element("p", "候補カードの「比較する」を選択してください。", "empty"));
  }
  $("selected-count").textContent = `${selected.length}/4`;

  const reviewCandidate = $("review-candidate");
  const current = reviewCandidate.value;
  reviewCandidate.replaceChildren();
  selected.forEach((candidate) => reviewCandidate.append(option(candidate.id)));
  if (selected.some((item) => item.id === current)) reviewCandidate.value = current;
  void syncReviewControls(true);
}

function render() {
  renderList();
  renderComparison();
}

function utcTimestamp() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

async function deterministicReviewId(candidateId, identity) {
  if (!globalThis.crypto?.subtle) {
    throw new Error("Web Crypto SHA-256 が利用できないためレビューを書き出せません。");
  }
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(identity));
  return `review-${candidateId}-${digestHex(digest).slice(0, 12)}`;
}

async function reviewDocument() {
  const candidate = selectedReviewCandidate();
  if (!candidate) throw new Error("比較ボードから対象候補を選んでください。");
  if (!hasPackagedQualityStage(candidate)) {
    throw new Error("明示的な包装品質段階がない候補は新規レビューできません。");
  }

  const reviewer = $("reviewer").value.trim();
  if (!reviewer) throw new Error("レビュアー名を入力してください。");

  const decision = $("decision").value;
  const reviewScope = $("review-scope").value || "technical";
  const categories = checkedValues("#category-list input");
  const hardFailCategories = checkedValues("#hard-fail-list input");

  if (!state.reviewScopes.includes(reviewScope)) throw new Error("API未定義のレビュー範囲です。");
  if (!state.decisions.includes(decision)) throw new Error("API未定義の判定です。");
  if (reviewScope === "creative" && candidate.quality_stage !== "technical_candidate") {
    throw new Error("創作レビューは technical_candidate にのみ使用できます。");
  }
  if (reviewScope === "creative" && decision === "accept" && hardFailCategories.length) {
    throw new Error("ハード失敗がある候補を creative_candidate として採用できません。");
  }

  const needsLiveImage = APPROVAL_DECISIONS.has(decision) || reviewScope === "creative";
  if (needsLiveImage && !await refreshLiveVerification(candidate)) {
    throw new Error("この判定には、現在取得できるSHA-256一致済みPNGが必要です。");
  }

  const timestamp = utcTimestamp();
  const resultingQualityStage = resultingStage(candidate, reviewScope, decision);
  const identity = [
    candidate.id,
    candidate.request_id,
    candidate.sha256,
    reviewer,
    decision,
    reviewScope,
    resultingQualityStage,
    timestamp,
    categories.join(","),
    hardFailCategories.join(","),
  ].join("\n");

  const documentValue = {
    kind: "review-decision",
    schema_version: "1.0",
    id: await deterministicReviewId(candidate.id, identity),
    candidate_ref: candidate.id,
    candidate_request_ref: candidate.request_id,
    candidate_sha256: candidate.sha256,
    decision,
    reviewer,
    timestamp,
    categories,
    review_scope: reviewScope,
    resulting_quality_stage: resultingQualityStage,
    hard_fail_categories: hardFailCategories,
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
    setTimeout(() => URL.revokeObjectURL(url), 0);
  } catch (error) {
    $("review-preview").textContent = error instanceof Error ? error.message : String(error);
  }
}

function appendChecks(containerId, values, onChange) {
  values.forEach((value) => {
    const label = element("label", undefined, "category");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = value;
    input.addEventListener("change", onChange);
    label.append(input, document.createTextNode(value));
    $(containerId).append(label);
  });
}

async function initialize() {
  const response = await fetch("/api/candidates", { cache: "no-store" });
  if (!response.ok) throw new Error(`候補APIの取得に失敗しました: ${response.status}`);
  const payload = await response.json();

  state.candidates = payload.candidates || [];
  state.categories = payload.categories || [];
  state.hardFailCategories = payload.hard_fail_categories || [];
  state.reviewScopes = payload.review_scopes || [];
  state.decisions = payload.decisions || [];

  if (!state.reviewScopes.includes("technical") || !state.reviewScopes.includes("creative")) {
    throw new Error("候補APIに必要なレビュー範囲語彙がありません。");
  }

  populateSelect("character-filter", uniqueValues("character_id"));
  populateSelect("expression-filter", uniqueValues("expression"));
  populateSelect("pose-filter", uniqueValues("pose"));
  populateSelect("review-filter", uniqueValues("review_state"));
  state.reviewScopes.forEach((value) => $("review-scope").append(option(value)));
  $("review-scope").value = "technical";
  state.decisions.forEach((value) => $("decision").append(option(value)));
  appendChecks("category-list", state.categories, () => void syncReviewControls(false));
  appendChecks("hard-fail-list", state.hardFailCategories, () => void syncReviewControls(false));

  ["character-filter", "role-filter", "expression-filter", "pose-filter", "review-filter"]
    .forEach((id) => $(id).addEventListener("change", render));
  $("review-candidate").addEventListener("change", () => void syncReviewControls(true));
  $("review-scope").addEventListener("change", () => void syncReviewControls(true));
  $("decision").addEventListener("change", () => void syncReviewControls(APPROVAL_DECISIONS.has($("decision").value)));
  $("clear-selection").addEventListener("click", () => {
    state.selected.clear();
    render();
  });
  $("download-review").addEventListener("click", downloadReview);
  render();
}

initialize().catch((error) => {
  $("status").textContent = error instanceof Error ? error.message : String(error);
  $("download-review").disabled = true;
});
