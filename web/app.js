const $ = (id) => document.getElementById(id);
const NODE_IDS = ["28", "29", "30"];
const MAX_BODY_BYTES = 9_500_000;
const DEFAULT_DURATIONS = [3, 5, 8, 10, 12, 15];
const FINAL_STATES = new Set(["COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT", "SUBMIT_FAILED", "EXPIRED"]);
const STATUS_LABELS = {
  SUBMITTING: "Đang gửi",
  IN_QUEUE: "Trong hàng đợi",
  IN_PROGRESS: "Đang tạo",
  COMPLETED: "Hoàn tất",
  FAILED: "Lỗi",
  CANCELLED: "Đã huỷ",
  TIMED_OUT: "Quá giờ",
  SUBMIT_FAILED: "Gửi lỗi",
  EXPIRED: "Hết hạn",
};
// Frame anchor: an extra LoadImage that is both reference <Picture 4> (so the
// prompt can name it) and a MiniMaxH3AddGuide keyframe at frame 0 in each stage.
const ANCHOR = { load: "300", guide1: "301", guide2: "302", label: "<Picture 4>" };
const IMAGE_LABELS = ["Nhân vật 1", "Nhân vật 2", "Bối cảnh", "Khung neo"];
const PROGRESS = {
  SUBMITTING: [10, "Đang gửi request đến Runpod…"],
  IN_QUEUE: [35, "Job đang trong hàng đợi. Worker sẽ khởi động khi đến lượt (lần đầu có thể mất vài phút)."],
  IN_PROGRESS: [70, "Worker đang xử lý workflow. Render video có thể mất nhiều phút."],
};

const state = {
  workflow: null,
  defaultPrompt: "",
  promptTouched: false,
  savedKey: "",
  previewUrls: [null, null, null],
  listTimer: null,
  listDebounce: null,
  listSeq: 0,
  detailTimer: null,
  detail: null,
  menuItem: null,
  formSource: null,
  route: null,
  durations: DEFAULT_DURATIONS,
  timeoutMin: 60,
  history: [],
  anchorFile: null,
  anchorUrl: null,
  anchorSeq: 0,
  fillSeq: 0,
  continuesFrom: null,
  chainFor: null,
  chain: [],
  film: null,
  filmScenes: [],
  filmImages: [null, null, null],
  filmTemplate: null,
  filmTimer: null,
};

/* ---------- helpers ---------- */

async function localJson(url, options = {}) {
  const response = await fetch(url, options);
  const raw = await response.text();
  let data = {};
  try { data = raw ? JSON.parse(raw) : {}; } catch { throw new Error(`Máy chủ trả dữ liệu không phải JSON (HTTP ${response.status}).`); }
  if (!response.ok) {
    const detail = data.error || data.message || raw.slice(0, 300);
    throw new Error(`HTTP ${response.status}: ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
  }
  return data;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function sizeLabel(bytes) {
  return bytes < 1024 * 1024 ? `${Math.ceil(bytes / 1024)} KB` : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTime(iso) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString("vi-VN", { dateStyle: "short", timeStyle: "short" });
}

function formatMs(ms) {
  if (ms == null) return "—";
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds} giây`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest ? `${minutes} phút ${rest} giây` : `${minutes} phút`;
}

function settingsLabel(item) {
  const parts = [];
  if (item.duration != null) parts.push(`${Number(item.duration)} giây`);
  if (item.resolution != null) parts.push(Number(item.resolution) === 0.2 ? "0,2 MP" : "1 MP");
  if (item.crf != null) parts.push(Number(item.crf) === 15 ? "Chất lượng gốc" : "Gọn");
  return parts.join(" · ");
}

function statusBadge(status) {
  const badge = element("span", "status-badge", STATUS_LABELS[status] || status);
  if (status === "COMPLETED") badge.classList.add("ok");
  else if (FINAL_STATES.has(status)) badge.classList.add("bad");
  else badge.classList.add("live");
  return badge;
}

function showBox(id, message) {
  $(id).textContent = message || "";
  $(id).hidden = !message;
}

/** The final frame of a request's stored video, extracted by ffmpeg on the server. */
async function fetchLastFrame(requestId) {
  const response = await fetch(`/api/requests/${requestId}/last-frame`);
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || `Không lấy được khung cuối (HTTP ${response.status}).`);
  }
  return new File([await response.blob()], `last_frame_${requestId.slice(0, 8)}.png`, { type: "image/png" });
}

/**
 * Add the official Ref2VA wording for a first-frame anchor: a standalone
 * <Picture 4> entry in subject_definitions, the `keyframe completion` task type
 * in summary, and a retention line. Each part is added only when missing.
 */
function applyAnchorPrompt(text) {
  let out = text;
  if (!out.includes(ANCHOR.label)) {
    const line = `${ANCHOR.label} is the first frame of [Shot 1]: the final frame of the previous scene. ` +
      "The target video starts exactly from this frame, preserving its composition, character positions and poses, " +
      "lighting, and camera framing, then continues the action naturally from that state.";
    const end = out.search(/\n\s*\nsummary:/);
    out = end >= 0 ? `${out.slice(0, end)}\n${line}${out.slice(end)}` : `${line}\n\n${out}`;
  }
  out = out.replace(/(summary:\s*)\[([^\]\n]*)\]/, (match, head, types) =>
    types.includes("keyframe completion") ? match : `${head}[${types} + keyframe completion]`);
  if (!out.includes(`${ANCHOR.label} ([Shot 1] first frame)`)) {
    out = out.replace(/(retention_analysis:[^\n]*\n)/, `$1${ANCHOR.label} ([Shot 1] first frame): fully_preserved - ` +
      "the composition, character positions, poses, lighting, and framing of the previous scene's final frame are kept at 0.00 seconds.\n");
  }
  return out;
}

function anchorPromptMissing(text) {
  const missing = [];
  if (!text.includes(ANCHOR.label)) missing.push(`câu “${ANCHOR.label} is the first frame of [Shot 1]”`);
  if (!/summary:\s*\[[^\]\n]*keyframe completion/.test(text)) missing.push("nhãn “keyframe completion” trong summary");
  return missing;
}

/* ---------- API key ---------- */

function updateKeyBadge() {
  const badge = $("key-badge");
  badge.textContent = state.savedKey ? `API key …${state.savedKey.slice(-4)}` : "Chưa có API key";
  badge.classList.toggle("ok", Boolean(state.savedKey));
  $("key-missing").hidden = Boolean(state.savedKey);
}

async function loadSavedKey() {
  try {
    const data = await localJson("/api/settings");
    state.savedKey = data.api_key || "";
    if (Array.isArray(data.durations) && data.durations.length) state.durations = data.durations;
    if (data.execution_timeout_min) state.timeoutMin = data.execution_timeout_min;
  } catch {
    state.savedKey = "";
  }
  $("api-key").value = state.savedKey;
  updateKeyBadge();
}

function settingsStatus(message, tone = "") {
  const box = $("settings-status");
  box.textContent = message;
  box.className = `settings-status ${tone}`;
}

async function saveKey(event) {
  event.preventDefault();
  const key = $("api-key").value.trim();
  if (!key) return settingsStatus("Hãy dán Runpod API key.", "bad");
  try {
    await localJson("/api/settings/key", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: key }),
    });
    state.savedKey = key;
    updateKeyBadge();
    settingsStatus(`Đã lưu API key …${key.slice(-4)}.`, "ok");
  } catch (error) {
    settingsStatus(error.message, "bad");
  }
}

async function deleteKey() {
  if (!state.savedKey) return settingsStatus("Chưa có key nào được lưu.");
  if (!confirm("Xoá Runpod API key đã lưu trên máy này?")) return;
  try {
    await localJson("/api/settings/key", { method: "DELETE" });
    state.savedKey = "";
    $("api-key").value = "";
    updateKeyBadge();
    settingsStatus("Đã xoá API key. Server sẽ không tự tải video cho tới khi bạn lưu key mới.", "ok");
  } catch (error) {
    settingsStatus(error.message, "bad");
  }
}

async function testKey() {
  const key = $("api-key").value.trim();
  if (!key) return settingsStatus("Hãy dán Runpod API key.", "bad");
  settingsStatus("Đang kiểm tra endpoint…");
  try {
    const response = await fetch("/api/health", { headers: { "X-Runpod-Key": key } });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `Runpod từ chối key (HTTP ${response.status}).`);
    const workers = data.workers || {};
    settingsStatus(`Kết nối được. Worker: ${workers.ready ?? 0} sẵn sàng, ${workers.running ?? 0} đang chạy. Job trong hàng đợi: ${data.jobs?.inQueue ?? 0}.`, "ok");
  } catch (error) {
    settingsStatus(error.message || "Không kiểm tra được.", "bad");
  }
}

/* ---------- request list ---------- */

async function loadList() {
  clearTimeout(state.listTimer);
  const query = $("filter-title").value.trim();
  const seq = ++state.listSeq;
  try {
    const items = await localJson(`/api/requests${query ? `?q=${encodeURIComponent(query)}` : ""}`);
    // Ignore responses that arrive after a newer filter or after leaving the list.
    if (seq !== state.listSeq || state.route?.name !== "requests") return;
    $("request-rows").replaceChildren(...items.map(renderRow));
    $("list-count").textContent = `${items.length} request`;
    const empty = $("list-empty");
    empty.hidden = items.length > 0;
    empty.replaceChildren();
    if (!items.length && query) empty.textContent = `Không có request nào có tiêu đề chứa “${query}”.`;
    else if (!items.length) {
      empty.append(element("p", "", "Chưa có request nào."));
      const link = element("a", "primary-button", "＋ Tạo request đầu tiên");
      link.href = "#/new";
      empty.append(link);
    }
    if (items.some((item) => !FINAL_STATES.has(item.status))) state.listTimer = setTimeout(loadList, 10000);
  } catch (error) {
    if (seq !== state.listSeq) return;
    $("request-rows").replaceChildren();
    $("list-empty").hidden = false;
    $("list-empty").textContent = `Không tải được danh sách: ${error.message}`;
  }
}

function renderRow(item) {
  const row = element("tr");
  row.tabIndex = 0;
  row.addEventListener("click", (event) => {
    if (!event.target.closest("button, a")) location.hash = `#/requests/${item.id}`;
  });
  row.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.target === row) location.hash = `#/requests/${item.id}`;
  });

  const titleCell = element("td", "title-cell");
  const link = element("a", "row-title", item.continues_from ? `↳ ${item.title}` : item.title);
  link.href = `#/requests/${item.id}`;
  if (item.continues_from) link.title = "Cảnh nối tiếp từ một request trước";
  titleCell.append(link, element("span", "row-prompt", item.prompt));

  const statusCell = element("td");
  statusCell.append(statusBadge(item.status));

  const videoCell = element("td", "video-cell", item.video ? "▶ Có" : "—");
  videoCell.classList.toggle("has-video", Boolean(item.video));

  const actionCell = element("td", "action-cell");
  const more = element("button", "more-button", "⋯");
  more.type = "button";
  more.setAttribute("aria-label", `Thao tác cho ${item.title}`);
  more.setAttribute("aria-haspopup", "menu");
  more.setAttribute("aria-expanded", "false");
  more.addEventListener("click", (event) => {
    event.stopPropagation();
    openMenu(more, item);
  });
  actionCell.append(more);

  const settingsCell = element("td", "muted nowrap", settingsLabel(item));
  if (item.execution_ms != null) settingsCell.append(element("span", "row-prompt", `Chạy ${formatMs(item.execution_ms)}`));

  row.append(titleCell, statusCell, settingsCell, videoCell, element("td", "muted nowrap", formatTime(item.created_at)), actionCell);
  return row;
}

function openMenu(button, item) {
  const menu = $("row-menu");
  const wasOpen = !menu.hidden && state.menuItem?.id === item.id;
  closeMenu();
  if (wasOpen) return;
  state.menuItem = item;
  button.setAttribute("aria-expanded", "true");
  button.classList.add("open");
  menu.hidden = false;
  const rect = button.getBoundingClientRect();
  const top = rect.bottom + 6 + menu.offsetHeight > window.innerHeight ? rect.top - menu.offsetHeight - 6 : rect.bottom + 6;
  menu.style.top = `${Math.max(8, top)}px`;
  menu.style.left = `${Math.max(8, rect.right - menu.offsetWidth)}px`;
  menu.querySelector("button").focus();
}

function closeMenu() {
  $("row-menu").hidden = true;
  document.querySelectorAll(".more-button.open").forEach((button) => {
    button.classList.remove("open");
    button.setAttribute("aria-expanded", "false");
  });
  state.menuItem = null;
}

async function deleteRequest(item) {
  if (!confirm(`Xoá “${item.title}” cùng ảnh và video đã lưu trên máy?\nJob trên Runpod không bị ảnh hưởng.`)) return false;
  try {
    await localJson(`/api/requests/${item.id}`, { method: "DELETE" });
    return true;
  } catch (error) {
    alert(error.message || "Không xoá được request.");
    return false;
  }
}

/* ---------- create request ---------- */

function validateWorkflow(graph) {
  if (!graph || Array.isArray(graph) || typeof graph !== "object") throw new Error("Workflow API cài sẵn không hợp lệ.");
  for (const id of NODE_IDS) {
    if (graph[id]?.class_type !== "LoadImage" || !graph[id].inputs) throw new Error(`Không tìm thấy nút LoadImage ${id} trong workflow.`);
  }
  if (graph["190"]?.class_type !== "PrimitiveStringMultiline") throw new Error("Không tìm thấy nút prompt 190 trong workflow.");
  for (const id of ["111", "154", "112", "102"]) {
    if (!graph[id]?.inputs) throw new Error(`Không tìm thấy nút ${id} trong workflow.`);
  }
  // Nodes the frame anchor wires into (see addFrameAnchor).
  const anchorNodes = { "4": "VAELoader", "121": "MiniMaxH3ReferenceToVideo", "145": "BasicGuider", "146": "BasicGuider", "157": "LTXVConcatAVLatent" };
  for (const [id, type] of Object.entries(anchorNodes)) {
    if (graph[id]?.class_type !== type) throw new Error(`Nút ${id} phải là ${type} để dùng khung neo.`);
  }
}

/**
 * Anchor frame 0 of the video to the image in `name`. Stage 1 (guider 145)
 * guides the base latent; stage 2 (guider 146) needs its own guide because
 * MiniMaxH3AddGuide encodes the image at the size of the latent it receives,
 * and stage 2 samples the upscaled latent from node 157.
 */
function addFrameAnchor(graph, name) {
  graph[ANCHOR.load] = { class_type: "LoadImage", inputs: { image: name }, _meta: { title: "Frame anchor" } };
  graph["121"].inputs["ref_images.ref_image_3"] = [ANCHOR.load, 0];
  const guide = (latent) => ({
    class_type: "MiniMaxH3AddGuide",
    inputs: { positive: ["121", 0], vae: ["4", 0], latent, image: [ANCHOR.load, 0], frame_idx: 0 },
  });
  graph[ANCHOR.guide1] = { ...guide(["121", 1]), _meta: { title: "Anchor frame 0 · stage 1" } };
  graph[ANCHOR.guide2] = { ...guide(["157", 0]), _meta: { title: "Anchor frame 0 · stage 2" } };
  graph["145"].inputs.conditioning = [ANCHOR.guide1, 0];
  graph["146"].inputs.conditioning = [ANCHOR.guide2, 0];
}

async function loadWorkflow() {
  try {
    const graph = await localJson("/api/workflow");
    validateWorkflow(graph);
    state.workflow = graph;
    $("workflow-detail").textContent = `Cinematic · ${Object.keys(graph).length} nút · ảnh gắn vào nút 28, 29, 30`;
  } catch (error) {
    $("workflow-detail").textContent = `Chưa tải được workflow: ${error.message}`;
  }
  try {
    const data = await localJson("/api/default-prompt");
    state.defaultPrompt = data.prompt || "";
  } catch {
    state.defaultPrompt = state.workflow?.["190"]?.inputs?.value || "";
  }
  if (!state.promptTouched) setPrompt(state.defaultPrompt, false);
  $("prompt-text").placeholder = "Nhập prompt video của bạn…";
}

function setPrompt(value, touched) {
  $("prompt-text").value = value;
  state.promptTouched = touched;
  updatePromptCount();
}

function updatePromptCount() {
  $("prompt-count").textContent = `${$("prompt-text").value.length.toLocaleString("vi-VN")} ký tự`;
}

function previewImage(index, file) {
  const img = $(`preview-${index}`);
  if (state.previewUrls[index - 1]) URL.revokeObjectURL(state.previewUrls[index - 1]);
  state.previewUrls[index - 1] = null;
  img.hidden = true;
  img.removeAttribute("src");
  $(`image-label-${index}`).hidden = false;
  $(`image-meta-${index}`).textContent = "PNG, JPG hoặc WebP";
  if (!file) return;
  if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
    showBox("form-error", `Ảnh ${index} phải là PNG, JPG hoặc WebP.`);
    $(`image-${index}`).value = "";
    return;
  }
  state.previewUrls[index - 1] = URL.createObjectURL(file);
  img.src = state.previewUrls[index - 1];
  img.hidden = false;
  $(`image-label-${index}`).hidden = true;
  $(`image-meta-${index}`).textContent = `${file.name} · ${sizeLabel(file.size)}`;
  showBox("form-error", "");
}

/* ---------- frame anchor ---------- */

function setAnchorFile(file, continuesFrom) {
  if (state.anchorUrl) URL.revokeObjectURL(state.anchorUrl);
  state.anchorFile = file;
  state.anchorUrl = file ? URL.createObjectURL(file) : null;
  state.continuesFrom = file ? continuesFrom : null;
  $("anchor-preview").hidden = !file;
  if (file) $("anchor-preview").src = state.anchorUrl;
  else $("anchor-preview").removeAttribute("src");
  $("anchor-meta").textContent = file ? `${file.name} · ${sizeLabel(file.size)}` : "Chưa có ảnh neo";
}

function anchorCandidates() {
  return state.history.filter((item) => item.status === "COMPLETED" && item.video_name);
}

function populateAnchorOptions() {
  const select = $("anchor-request");
  const current = select.value;
  const options = anchorCandidates().map((item) => {
    const option = element("option", "", `${item.title} · ${formatTime(item.created_at)}`);
    option.value = item.id;
    return option;
  });
  const placeholder = element("option", "", anchorCandidates().length ? "— Chọn cảnh trước —" : "Chưa có request nào có video lưu trên máy");
  placeholder.value = "";
  select.replaceChildren(placeholder, ...options);
  if ([...select.options].some((option) => option.value === current)) select.value = current;
}

function ensureAnchorOption(item) {
  const select = $("anchor-request");
  if (![...select.options].some((option) => option.value === item.id)) {
    const option = element("option", "", `${item.title} · ${formatTime(item.created_at)}`);
    option.value = item.id;
    select.append(option);
  }
  select.value = item.id;
}

function setAnchorEnabled(enabled) {
  $("anchor-enabled").checked = enabled;
  $("anchor-options").hidden = !enabled;
  updateAnchorPromptState();
}

function updateAnchorPromptState() {
  const box = $("anchor-prompt-state");
  if (!$("anchor-enabled").checked) {
    box.textContent = "";
    return;
  }
  const missing = anchorPromptMissing($("prompt-text").value);
  box.textContent = missing.length ? `Prompt còn thiếu: ${missing.join(", ")}.` : "Prompt đã có câu neo và nhãn keyframe completion.";
  box.className = missing.length ? "anchor-warn" : "anchor-ok";
}

/** Use the final frame of a saved request's video as the anchor. */
async function useRequestAsAnchor(id) {
  const seq = ++state.anchorSeq;
  setAnchorFile(null, null);
  if (!id) return;
  const item = state.history.find((entry) => entry.id === id) || await localJson(`/api/requests/${encodeURIComponent(id)}`);
  if (!item.video_name) throw new Error("Request này không có video lưu trên máy nên không lấy được khung cuối.");
  $("anchor-meta").textContent = "Đang lấy khung cuối của video…";
  const file = await fetchLastFrame(item.id);
  if (seq !== state.anchorSeq) return;
  setAnchorFile(file, item.id);
  $("anchor-file").value = "";
}

function resetForm() {
  $("title-text").value = "";
  setPrompt(state.defaultPrompt, false);
  $("duration-seconds").value = "5";
  $("video-size").value = "28";
  $("resolution").value = "0.2";
  updateTimingHint();
  for (let index = 1; index <= 3; index++) {
    $(`image-${index}`).value = "";
    previewImage(index, null);
  }
  state.anchorSeq++;
  setAnchorFile(null, null);
  $("anchor-request").value = "";
  $("anchor-file").value = "";
  setAnchorEnabled(false);
  showBox("form-error", "");
  $("copy-banner").hidden = true;
  $("new-heading").textContent = "Tạo request mới";
  state.formSource = null;
}

/** Copy a request into the form; with `continueScene`, anchor it to that request's last frame. */
async function fillFromRequest(id, { continueScene = false } = {}) {
  resetForm();
  const seq = ++state.fillSeq;
  const stale = () => seq !== state.fillSeq;
  $("new-heading").textContent = continueScene ? "Tiếp nối cảnh" : "Sao chép request";
  showBox("copy-banner", "Đang nạp request cũ…");
  try {
    const item = await localJson(`/api/requests/${encodeURIComponent(id)}`);
    if (stale()) return;
    if (continueScene && !item.video_name) {
      throw new Error("Chỉ tiếp nối được request đã hoàn tất và có video lưu trên máy.");
    }
    // The anchor slot (4) belongs to the previous shot; a new anchor is chosen below.
    for (const image of item.images.filter((entry) => entry.slot <= 3)) {
      const response = await fetch(image.url);
      if (!response.ok) throw new Error(`Không đọc được ảnh ${image.slot} đã lưu.`);
      const blob = await response.blob();
      if (stale()) return;
      const file = new File([blob], image.name, { type: image.mime });
      const transfer = new DataTransfer();
      transfer.items.add(file);
      $(`image-${image.slot}`).files = transfer.files;
      previewImage(image.slot, file);
    }
    $("title-text").value = `${item.title} (${continueScene ? "cảnh tiếp" : "bản sao"})`.slice(0, 200);
    setPrompt(item.prompt, true);
    let durationNote = "";
    if (item.duration != null) {
      const wanted = Number(item.duration);
      const nearest = state.durations.reduce((best, value) => (Math.abs(value - wanted) < Math.abs(best - wanted) ? value : best));
      $("duration-seconds").value = String(nearest);
      if (nearest !== wanted) durationNote = ` Thời lượng ${wanted} giây không còn được hỗ trợ nên đã đổi sang ${nearest} giây.`;
    }
    if (item.crf != null) $("video-size").value = String(Number(item.crf));
    if (item.resolution != null) $("resolution").value = String(Number(item.resolution));
    state.formSource = id;
    if (continueScene) {
      setAnchorEnabled(true);
      ensureAnchorOption(item);
      await useRequestAsAnchor(item.id);
      if (stale()) return;
      setPrompt(applyAnchorPrompt($("prompt-text").value), true);
      updateAnchorPromptState();
      showBox("copy-banner", `Đang tiếp nối “${item.title}”: đã chép prompt, thông số, 3 ảnh nhân vật, lấy khung cuối của video làm khung đầu và thêm câu neo vào prompt. ` +
        `Hãy sửa phần mô tả cảnh (summary, detailed_description) cho cảnh mới rồi bấm Gửi request.${durationNote}`);
    } else {
      // A retry of an anchored shot keeps the same anchor and the same parent scene.
      const anchor = item.images.find((entry) => entry.slot === 4);
      if (anchor) {
        const response = await fetch(anchor.url);
        if (!response.ok) throw new Error("Không đọc được khung neo đã lưu.");
        const blob = await response.blob();
        if (stale()) return;
        setAnchorEnabled(true);
        setAnchorFile(new File([blob], anchor.name, { type: anchor.mime }), item.continues_from);
        updateAnchorPromptState();
      }
      showBox("copy-banner", `Đã sao chép tiêu đề, prompt, thông số và ${anchor ? "3 ảnh cùng khung neo" : "3 ảnh"} từ “${item.title}”. ` +
        `Chỉnh sửa nếu cần rồi bấm Gửi request.${durationNote}`);
    }
    updateTimingHint();
  } catch (error) {
    if (stale()) return;
    showBox("copy-banner", "");
    showBox("form-error", error.message || "Không sao chép được request.");
  }
}

function toDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error(`Không đọc được tệp ${file.name}.`));
    reader.readAsDataURL(file);
  });
}

async function buildRequest() {
  const graph = structuredClone(state.workflow);
  const prompt = $("prompt-text").value.trim();
  if (!prompt) throw new Error("Hãy nhập prompt video.");
  graph["190"].inputs.value = prompt;
  const duration = Number($("duration-seconds").value);
  const crf = Number($("video-size").value);
  const resolution = Number($("resolution").value);
  if (!state.durations.includes(duration) || ![15, 28].includes(crf) || ![0.2, 1].includes(resolution)) {
    throw new Error("Thiết lập video không hợp lệ.");
  }
  graph["111"].inputs.value = duration;
  graph["154"].inputs.crf = crf;
  if (resolution === 0.2) {
    graph["112"].inputs.megapixels = 0.2;
    graph["102"].inputs.value = 0.2;
  }
  const images = [];
  const imageName = (slot, file) => `h3_ref_${slot}_${Date.now()}_${file.name.replace(/[^A-Za-z0-9._-]/g, "_")}`;
  for (let index = 1; index <= 3; index++) {
    const file = $(`image-${index}`).files[0];
    if (!file) throw new Error(`Hãy chọn ảnh tham chiếu ${index}.`);
    const name = imageName(index, file);
    graph[NODE_IDS[index - 1]].inputs.image = name;
    images.push({ name, image: await toDataUrl(file) });
  }
  const anchored = $("anchor-enabled").checked;
  if (anchored) {
    if (!state.anchorFile) throw new Error("Đã bật khung neo nhưng chưa có ảnh neo. Chọn cảnh trước hoặc tải ảnh lên.");
    const missing = anchorPromptMissing(prompt);
    if (missing.length) throw new Error(`Prompt còn thiếu ${missing.join(" và ")}. Bấm “Thêm câu neo vào prompt”.`);
    const name = imageName(4, state.anchorFile);
    addFrameAnchor(graph, name);
    images.push({ name, image: await toDataUrl(state.anchorFile) });
  } else if (prompt.includes(ANCHOR.label)) {
    throw new Error(`Prompt nhắc tới ${ANCHOR.label} nhưng chưa bật khung neo. Bật khung neo hoặc xoá các câu nhắc tới ${ANCHOR.label}.`);
  }
  const body = JSON.stringify({
    title: $("title-text").value.trim(),
    ...(anchored && state.continuesFrom ? { continues_from: state.continuesFrom } : {}),
    input: { workflow: graph, images },
  });
  if (new TextEncoder().encode(body).length > MAX_BODY_BYTES) {
    throw new Error("Các ảnh và workflow vượt giới hạn 9,5 MB. Hãy giảm kích thước ảnh rồi thử lại.");
  }
  return body;
}

async function loadTimingHistory() {
  try {
    state.history = await localJson("/api/requests");
  } catch {
    state.history = [];
  }
  updateTimingHint();
  populateAnchorOptions();
}

function updateTimingHint() {
  const duration = Number($("duration-seconds").value);
  const resolution = Number($("resolution").value);
  const same = state.history.filter((item) => Number(item.duration) === duration && Number(item.resolution) === resolution);
  const measured = same.find((item) => item.status === "COMPLETED" && item.execution_ms != null);
  const timedOut = same.find((item) => item.status === "TIMED_OUT");
  const label = `${duration} giây · ${resolution === 0.2 ? "0,2 MP" : "1 MP"}`;
  let text;
  if (measured) {
    text = `Lần gần nhất với ${label}: chạy ${formatMs(measured.execution_ms)}` +
      (measured.delay_ms != null ? `, chờ worker ${formatMs(measured.delay_ms)}.` : ".");
  } else {
    text = `Chưa có số đo cho ${label}. Hãy thử cấu hình ngắn hơn trước để ước lượng.`;
  }
  if (timedOut && (!measured || timedOut.created_at > measured.created_at)) {
    text += ` Lưu ý: một request ${label} trước đó đã vượt giới hạn.`;
  }
  $("timing-hint").textContent = `${text} Giới hạn mỗi job: ${state.timeoutMin} phút.`;
  $("timing-hint").hidden = false;
}

async function submitRequest(event) {
  event.preventDefault();
  showBox("form-error", "");
  if (!state.savedKey) return showBox("form-error", "Chưa có Runpod API key. Vào Cài đặt để lưu key trước.");
  if (!state.workflow) return showBox("form-error", "Workflow chưa tải được. Hãy tải lại trang.");
  const button = $("submit-button");
  button.disabled = true;
  button.firstChild.textContent = "Đang gửi… ";
  try {
    const body = await buildRequest();
    const data = await localJson("/api/run", { method: "POST", headers: { "Content-Type": "application/json" }, body });
    if (!data.request_id) {
      throw new Error(data.id ? `Runpod đã nhận job ${data.id} nhưng không lưu được vào lịch sử.` : `Runpod chưa trả Job ID: ${JSON.stringify(data).slice(0, 250)}`);
    }
    resetForm();
    location.hash = `#/requests/${data.request_id}`;
  } catch (error) {
    showBox("form-error", error.message || "Không gửi được request.");
  } finally {
    button.disabled = false;
    button.firstChild.textContent = "Gửi request ";
  }
}

/* ---------- request detail ---------- */

async function loadDetail(id) {
  clearTimeout(state.detailTimer);
  try {
    let item = await localJson(`/api/requests/${encodeURIComponent(id)}`);
    if (item.job_id && !FINAL_STATES.has(item.status)) {
      // Asking Runpod directly also stores the latest state on the server.
      await localJson(`/api/status/${encodeURIComponent(item.job_id)}`).catch(() => null);
      item = await localJson(`/api/requests/${encodeURIComponent(id)}`);
    }
    if (state.route?.name !== "detail" || state.route.id !== id) return;
    renderDetail(item);
    if (state.chainFor !== id) loadChain(item);
    if (!FINAL_STATES.has(item.status)) state.detailTimer = setTimeout(() => loadDetail(id), 5000);
  } catch (error) {
    if (state.route?.name !== "detail") return;
    $("detail-title").textContent = "Không tải được request";
    showBox("detail-error", error.message);
  }
}

function renderDetail(item) {
  const previous = state.detail;
  state.detail = item;
  $("detail-title").textContent = item.title;
  const meta = $("detail-meta");
  meta.replaceChildren(statusBadge(item.status), element("span", "", formatTime(item.created_at)), element("span", "", settingsLabel(item)));

  const progress = PROGRESS[item.status];
  $("detail-progress").hidden = !progress;
  if (progress) {
    $("progress-fill").style.width = `${progress[0]}%`;
    $("progress-text").textContent = progress[1];
  }
  showBox("detail-error", item.error || "");

  const videoBox = $("detail-video");
  const sameVideo = previous?.id === item.id && previous?.video === item.video && videoBox.childElementCount;
  if (!sameVideo) {
    videoBox.replaceChildren();
    if (item.video) {
      const video = element("video");
      video.controls = true;
      video.playsInline = true;
      video.preload = "metadata";
      video.src = item.video;
      const link = element("a", "download-button", item.video_name ? `Tải ${item.video_name} ↓` : "Mở video ↗");
      link.href = item.video;
      if (item.video_name) link.download = item.video_name;
      else { link.target = "_blank"; link.rel = "noopener"; }
      videoBox.append(video, link);
    } else {
      videoBox.append(element("div", "no-video", FINAL_STATES.has(item.status) ? "Request này không có video." : "Video sẽ xuất hiện ở đây khi job hoàn tất."));
    }
  }

  const canContinue = item.status === "COMPLETED" && Boolean(item.video_name);
  $("detail-continue").disabled = !canContinue;
  $("detail-continue").title = canContinue ? "Tạo cảnh mới bắt đầu từ khung cuối của video này" : "Cần request đã hoàn tất và có video lưu trên máy";

  $("detail-prompt").textContent = item.prompt;
  const labels = IMAGE_LABELS;
  $("detail-images").replaceChildren(...item.images.map((image) => {
    const figure = element("figure");
    const link = element("a");
    link.href = image.url;
    link.target = "_blank";
    link.rel = "noopener";
    const img = element("img");
    img.src = image.url;
    img.alt = `${labels[image.slot - 1]}: ${image.name}`;
    link.append(img);
    figure.append(link, element("figcaption", "", `${labels[image.slot - 1]} · ${image.name}`));
    return figure;
  }));

  const info = [
    ["Request ID", item.id],
    ["Runpod Job ID", item.job_id || "—"],
    ["API key", item.key_hint || "—"],
    ["Chờ worker", formatMs(item.delay_ms)],
    ["Thời gian chạy", formatMs(item.execution_ms)],
    ["Tạo lúc", formatTime(item.created_at)],
    ["Cập nhật", formatTime(item.updated_at)],
  ];
  $("detail-info").replaceChildren(...info.flatMap(([label, value]) => [element("dt", "", label), element("dd", "", value)]));
}

/* ---------- scene chain ---------- */

async function loadChain(item) {
  state.chainFor = item.id;
  state.chain = [];
  $("chain-card").hidden = true;
  if (!item.continues_from && !item.next?.length) return;
  try {
    const chain = await localJson(`/api/requests/${item.id}/chain`);
    if (state.route?.name !== "detail" || state.route.id !== item.id) return;
    state.chain = chain;
    renderChain(item.id);
  } catch {
    state.chainFor = null;
  }
}

function renderChain(currentId, playingId = null) {
  const list = $("chain-list");
  list.replaceChildren(...state.chain.map((scene, index) => {
    const entry = element("li");
    entry.classList.toggle("current", scene.id === currentId);
    entry.classList.toggle("playing", scene.id === playingId);
    const link = element("a", "", `Cảnh ${index + 1}: ${scene.title}`);
    link.href = `#/requests/${scene.id}`;
    entry.append(link, element("span", "muted", scene.video ? ` · ${STATUS_LABELS[scene.status] || scene.status}` : " · chưa có video"));
    return entry;
  }));
  const playable = state.chain.filter((scene) => scene.video_name);
  $("chain-play").disabled = playable.length < 2;
  $("chain-play").textContent = `▶ Phát liên tục ${playable.length} cảnh`;
  $("chain-card").hidden = false;
}

/** Play every stored scene of the chain back to back in the detail player. */
function playChain() {
  const scenes = state.chain.filter((scene) => scene.video_name);
  const video = $("detail-video").querySelector("video");
  if (!video || scenes.length < 2) return;
  let index = 0;
  const playAt = () => {
    const scene = scenes[index];
    renderChain(state.detail?.id, scene.id);
    video.src = `/api/requests/${scene.id}/video`;
    video.play().catch(() => {});
  };
  video.onended = () => {
    index += 1;
    if (index < scenes.length) playAt();
    else {
      video.onended = null;
      renderChain(state.detail?.id);
    }
  };
  playAt();
  video.scrollIntoView({ behavior: "smooth", block: "center" });
}

/* ---------- films ---------- */

const FILM_STATUS = { DRAFT: "Nháp", RUNNING: "Đang tạo", PAUSED: "Tạm dừng", FAILED: "Lỗi", COMPLETED: "Hoàn tất" };

function filmBadge(status) {
  const badge = element("span", "status-badge", FILM_STATUS[status] || status);
  badge.classList.add({ COMPLETED: "ok", FAILED: "bad", RUNNING: "live" }[status] || "draft");
  return badge;
}

function filmDone(film) {
  return film.status === "COMPLETED" ? film.scene_count : Math.min(film.current_index, film.scene_count);
}

function durationLabel(seconds) {
  const minutes = Math.floor(seconds / 60);
  return minutes ? `${seconds} giây (${minutes} phút ${seconds % 60} giây)` : `${seconds} giây`;
}

async function loadFilms() {
  clearTimeout(state.filmTimer);
  try {
    const films = await localJson("/api/projects");
    if (state.route?.name !== "films") return;
    $("film-rows").replaceChildren(...films.map((film) => {
      const row = element("tr");
      row.tabIndex = 0;
      const open = () => { location.hash = `#/films/${film.id}`; };
      row.addEventListener("click", (event) => { if (!event.target.closest("a")) open(); });
      row.addEventListener("keydown", (event) => { if (event.key === "Enter") open(); });
      const titleCell = element("td", "title-cell");
      const link = element("a", "row-title", film.title);
      link.href = `#/films/${film.id}`;
      titleCell.append(link);
      if (film.error) titleCell.append(element("span", "row-prompt", film.error));
      const statusCell = element("td");
      statusCell.append(filmBadge(film.status));
      row.append(titleCell, statusCell, element("td", "muted nowrap", `${filmDone(film)}/${film.scene_count} cảnh`),
        element("td", "muted nowrap", durationLabel(film.total_duration)), element("td", "muted nowrap", formatTime(film.updated_at)));
      return row;
    }));
    const empty = $("films-empty");
    empty.hidden = films.length > 0;
    if (!films.length) {
      const link = element("a", "primary-button", "＋ Tạo phim đầu tiên");
      link.href = "#/films/new";
      empty.replaceChildren(element("p", "", "Chưa có phim nào."), link);
    }
    if (films.some((film) => film.status === "RUNNING")) state.filmTimer = setTimeout(loadFilms, 5000);
  } catch (error) {
    $("film-rows").replaceChildren();
    $("films-empty").hidden = false;
    $("films-empty").textContent = `Không tải được danh sách phim: ${error.message}`;
  }
}

function newFilmScene() {
  return { duration: 5, description: "", soundscape: "", music: "" };
}

function setFilmPreview(slot, url, label) {
  const img = $(`film-preview-${slot}`);
  img.hidden = !url;
  if (url) img.src = url;
  else img.removeAttribute("src");
  $(`film-label-${slot}`).hidden = Boolean(url);
  $(`film-meta-${slot}`).textContent = label || "PNG, JPG hoặc WebP";
}

function fillFilmForm(film, template) {
  const source = film || template;
  $("film-title").value = film?.title || "";
  $("film-resolution").value = String(film ? Number(film.resolution) : 0.2);
  $("film-size").value = String(film ? film.crf : 28);
  for (const field of ["subjects", "style", "soundscape", "music"]) $(`film-${field}`).value = source?.[field] || "";
  state.filmImages = [null, null, null];
  for (let slot = 1; slot <= 3; slot++) {
    $(`film-image-${slot}`).value = "";
    const image = film?.images.find((entry) => entry.slot === slot);
    setFilmPreview(slot, image?.url, image?.name);
  }
  state.filmScenes = film ? film.scenes.map((scene) => ({ ...scene })) : [newFilmScene()];
}

async function openNewFilm() {
  state.film = null;
  showBox("film-form-error", "");
  try {
    state.filmTemplate ||= await localJson("/api/project-template");
  } catch {
    state.filmTemplate = null;
  }
  if (state.route?.name !== "film" || state.route.id) return;
  fillFilmForm(null, state.filmTemplate);
  renderFilm();
}

async function loadFilm(id) {
  clearTimeout(state.filmTimer);
  try {
    const film = await localJson(`/api/projects/${id}`);
    if (state.route?.name !== "film" || state.route.id !== id) return;
    // Keep unsaved edits when the page just re-polls a film that is not running.
    const refill = state.film?.id !== id || film.status === "RUNNING" || state.film?.status === "RUNNING";
    state.film = film;
    if (refill) fillFilmForm(film);
    else state.filmScenes.forEach((scene, index) => Object.assign(scene, {
      request: film.scenes[index]?.request, prompt: film.scenes[index]?.prompt, attempts: film.scenes[index]?.attempts,
    }));
    renderFilm();
    if (film.status === "RUNNING") state.filmTimer = setTimeout(() => loadFilm(id), 5000);
  } catch (error) {
    if (state.route?.name !== "film") return;
    $("film-heading").textContent = "Không tải được phim";
    showBox("film-form-error", error.message);
  }
}

function filmLocks() {
  const film = state.film;
  const running = film?.status === "RUNNING";
  const started = Boolean(film) && (film.status !== "DRAFT" || film.locked_scenes > 0);
  return { running, started, lockedScenes: running ? Infinity : film?.locked_scenes || 0 };
}

function renderFilm() {
  const film = state.film;
  const { running, started } = filmLocks();
  $("film-heading").textContent = film?.title || "Phim mới";
  document.title = `${film?.title || "Phim mới"} · MiniMax H3 Studio`;
  $("film-meta").replaceChildren(...(film ? [filmBadge(film.status), element("span", "", `${film.scene_count} cảnh · ${durationLabel(film.total_duration)}`)] : []));

  const canStart = !film || ["DRAFT", "PAUSED", "FAILED"].includes(film.status);
  $("film-start").hidden = !canStart;
  $("film-start").textContent = !film || film.status === "DRAFT" ? "▶ Bắt đầu tạo phim"
    : film.status === "FAILED" ? `↻ Thử lại từ cảnh ${film.current_index + 1}` : "▶ Tiếp tục";
  $("film-stop").hidden = !running;
  $("film-save").hidden = running;
  $("film-save").textContent = !film || film.status === "DRAFT" ? "Lưu nháp" : "Lưu thay đổi";
  $("film-delete").hidden = !film || running;

  const progress = $("film-progress");
  progress.hidden = !film || film.status === "DRAFT";
  if (film && film.status !== "DRAFT") {
    const done = filmDone(film);
    $("film-progress-label").textContent = `${done}/${film.scene_count} cảnh`;
    $("film-progress-fill").style.width = `${film.scene_count ? Math.round((done / film.scene_count) * 100) : 0}%`;
    const current = film.scenes[film.current_index];
    const currentStatus = current?.request ? STATUS_LABELS[current.request.status] || current.request.status : "Đang chuẩn bị";
    $("film-progress-text").textContent = {
      RUNNING: film.current_index >= film.scene_count ? "Đang ghép các cảnh thành một video…"
        : `Đang tạo cảnh ${film.current_index + 1}/${film.scene_count}: ${currentStatus}. Có thể đóng trang, máy chủ vẫn tiếp tục.`,
      PAUSED: "Đã tạm dừng. Job đang chạy dở (nếu có) vẫn chạy xong trên Runpod; bấm Tiếp tục để làm tiếp.",
      FAILED: "Phim dừng vì lỗi. Sửa phân cảnh nếu cần rồi bấm Thử lại.",
      COMPLETED: "Phim đã ghép xong.",
    }[film.status] || "";
    showBox("film-error", film.error || "");
    $("film-result").hidden = !film.film;
    if (film.film && $("film-video").getAttribute("src") !== `${film.film}?v=${film.updated_at}`) {
      $("film-video").src = `${film.film}?v=${film.updated_at}`;
      $("film-download").href = film.film;
      $("film-download").download = `${film.title.replace(/[\\/:*?"<>|]+/g, "_")}.mp4`;
    }
  }

  for (const id of ["film-subjects", "film-style", "film-soundscape", "film-music", "film-resolution", "film-size"]) $(id).disabled = started || running;
  $("film-title").disabled = running;
  for (let slot = 1; slot <= 3; slot++) $(`film-image-${slot}`).disabled = started || running;
  $("film-add-scene").disabled = running;
  renderFilmScenes();
}

function syncFilmScenes() {
  document.querySelectorAll("#film-scenes .film-scene").forEach((card) => {
    const scene = state.filmScenes[Number(card.dataset.index)];
    if (!scene || card.classList.contains("locked")) return;
    scene.duration = Number(card.querySelector("select").value);
    for (const field of ["description", "soundscape", "music"]) scene[field] = card.querySelector(`[data-field="${field}"]`).value;
  });
}

function renderFilmScenes() {
  const { lockedScenes } = filmLocks();
  const total = state.filmScenes.reduce((sum, scene) => sum + Number(scene.duration), 0);
  $("film-total").textContent = `${state.filmScenes.length} cảnh · tổng ${durationLabel(total)}`;
  $("film-scenes").replaceChildren(...state.filmScenes.map((scene, index) => {
    const locked = index < lockedScenes;
    const card = element("div", `film-scene${locked ? " locked" : ""}`);
    card.dataset.index = index;

    const head = element("div", "film-scene-head");
    head.append(element("b", "", `Cảnh ${index + 1}`));
    const duration = element("select");
    duration.setAttribute("aria-label", `Thời lượng cảnh ${index + 1}`);
    for (const value of state.durations) {
      const option = element("option", "", `${value} giây`);
      option.value = String(value);
      duration.append(option);
    }
    duration.value = String(scene.duration);
    duration.disabled = locked;
    duration.addEventListener("change", () => { syncFilmScenes(); renderFilmScenes(); });
    head.append(duration);
    if (scene.request) {
      head.append(statusBadge(scene.request.status));
      if (scene.request.execution_ms != null) head.append(element("span", "muted", `chạy ${formatMs(scene.request.execution_ms)}`));
      if (scene.attempts > 1) head.append(element("span", "muted", `lần thử ${scene.attempts}`));
    }
    const tools = element("span", "film-scene-tools");
    const tool = (label, title, disabled, action) => {
      const button = element("button", "icon-button", label);
      button.type = "button";
      button.title = title;
      button.setAttribute("aria-label", `${title} cảnh ${index + 1}`);
      button.disabled = disabled;
      button.addEventListener("click", () => { syncFilmScenes(); action(); renderFilmScenes(); });
      return button;
    };
    const scenes = state.filmScenes;
    tools.append(
      tool("↑", "Đưa lên", locked || index === 0 || index - 1 < lockedScenes, () => scenes.splice(index - 1, 0, scenes.splice(index, 1)[0])),
      tool("↓", "Đưa xuống", locked || index === scenes.length - 1, () => scenes.splice(index + 1, 0, scenes.splice(index, 1)[0])),
      tool("✕", "Xoá", locked || scenes.length === 1, () => scenes.splice(index, 1)),
    );
    head.append(tools);

    const description = element("textarea");
    description.dataset.field = "description";
    description.rows = 5;
    description.spellcheck = false;
    description.value = scene.description;
    description.disabled = locked;
    description.placeholder = index === 0
      ? "[Shot 1] Mô tả cảnh mở đầu bằng tiếng Anh: khung hình, nhân vật, hành động, góc máy, thoại <d>[English] …</d>"
      : "[Shot 1] Mở đầu đúng trạng thái cuối của cảnh trước (vị trí, tư thế, ánh sáng, góc máy), rồi mô tả hành động tiếp theo bằng tiếng Anh.";
    description.setAttribute("aria-label", `Mô tả cảnh ${index + 1}`);

    const sound = element("details", "film-scene-extra");
    sound.append(element("summary", "", scene.soundscape || scene.music ? "Âm thanh riêng cho cảnh này (đang dùng)" : "Âm thanh riêng cho cảnh này"));
    for (const [field, label] of [["soundscape", "Âm thanh nền"], ["music", "Nhạc nền"]]) {
      const input = element("textarea");
      input.dataset.field = field;
      input.rows = 2;
      input.value = scene[field] || "";
      input.disabled = locked;
      input.placeholder = `${label}: để trống để dùng mặc định của phim`;
      input.setAttribute("aria-label", `${label} cảnh ${index + 1}`);
      sound.append(input);
    }
    card.append(head, description, sound);

    if (scene.prompt) {
      const preview = element("details", "film-scene-extra");
      preview.append(element("summary", "", "Xem prompt gửi đi"), element("pre", "detail-prompt", scene.prompt));
      card.append(preview);
    }
    if (scene.request) {
      const media = element("div", "film-scene-media");
      if (scene.request.video) {
        const video = element("video");
        video.controls = true;
        video.preload = "none";
        video.src = scene.request.video;
        media.append(video);
      }
      if (scene.request.error && scene.request.status !== "COMPLETED") {
        media.append(element("p", "history-error", scene.request.error));
      }
      const link = element("a", "text-link", "Mở request →");
      link.href = `#/requests/${scene.request.id}`;
      media.append(link);
      card.append(media);
    }
    return card;
  }));
}

async function readFilmBody() {
  syncFilmScenes();
  const film = state.film;
  const images = [];
  for (let slot = 1; slot <= 3; slot++) {
    const file = state.filmImages[slot - 1];
    if (file) images.push({ slot, name: file.name, image: await toDataUrl(file) });
  }
  if (!film && images.length < 3) throw new Error("Hãy chọn đủ 3 ảnh tham chiếu.");
  const body = {
    title: $("film-title").value.trim(),
    subjects: $("film-subjects").value, style: $("film-style").value,
    soundscape: $("film-soundscape").value, music: $("film-music").value,
    crf: Number($("film-size").value), resolution: Number($("film-resolution").value),
    images,
    scenes: state.filmScenes.map(({ duration, description, soundscape, music }) => ({ duration: Number(duration), description, soundscape, music })),
  };
  const blank = body.scenes.findIndex((scene) => !scene.description.trim());
  if (blank >= 0) throw new Error(`Cảnh ${blank + 1} chưa có mô tả.`);
  if (new TextEncoder().encode(JSON.stringify(body)).length > MAX_BODY_BYTES) {
    throw new Error("Ảnh tham chiếu quá lớn (giới hạn 9,5 MB). Hãy giảm kích thước ảnh.");
  }
  return body;
}

async function saveFilm() {
  const body = await readFilmBody();
  const film = state.film;
  const saved = await localJson(film ? `/api/projects/${film.id}` : "/api/projects", {
    method: film ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  state.film = saved;
  fillFilmForm(saved);
  if (!film) history.replaceState(null, "", `#/films/${saved.id}`);
  state.route = { name: "film", id: saved.id };
  renderFilm();
  return saved;
}

async function filmAction(button, work) {
  showBox("film-form-error", "");
  button.disabled = true;
  try {
    await work();
  } catch (error) {
    showBox("film-form-error", error.message || "Thao tác không thành công.");
    $("film-form-error").scrollIntoView({ behavior: "smooth", block: "center" });
  } finally {
    button.disabled = false;
  }
}

async function startFilm() {
  if (!state.savedKey) throw new Error("Chưa có Runpod API key. Vào Cài đặt để lưu key trước.");
  const film = await saveFilm();
  state.film = await localJson(`/api/projects/${film.id}/start`, { method: "POST" });
  renderFilm();
  clearTimeout(state.filmTimer);
  state.filmTimer = setTimeout(() => loadFilm(film.id), 3000);
}

async function stopFilm() {
  state.film = await localJson(`/api/projects/${state.film.id}/stop`, { method: "POST" });
  clearTimeout(state.filmTimer);
  fillFilmForm(state.film);
  renderFilm();
}

async function deleteFilm() {
  if (!confirm(`Xoá phim “${state.film.title}” và video đã ghép?\nCác request của từng cảnh vẫn giữ trong mục Requests.`)) return;
  await localJson(`/api/projects/${state.film.id}`, { method: "DELETE" });
  state.film = null;
  location.hash = "#/films";
}

/* ---------- routing ---------- */

function parseRoute() {
  const [path, query] = location.hash.replace(/^#/, "").split("?");
  const params = new URLSearchParams(query || "");
  const detail = path.match(/^\/requests\/([a-f0-9]{32})$/);
  if (detail) return { name: "detail", id: detail[1] };
  if (path === "/new") return { name: "new", from: params.get("from"), continueFrom: params.get("continue") };
  if (path === "/settings") return { name: "settings" };
  if (path === "/films") return { name: "films" };
  if (path === "/films/new") return { name: "film", id: null };
  const film = path.match(/^\/films\/([a-f0-9]{32})$/);
  if (film) return { name: "film", id: film[1] };
  return { name: "requests" };
}

function route() {
  closeMenu();
  clearTimeout(state.listTimer);
  clearTimeout(state.detailTimer);
  clearTimeout(state.filmTimer);
  const current = parseRoute();
  state.route = current;
  document.querySelectorAll(".view").forEach((section) => { section.hidden = section.id !== `view-${current.name}`; });
  const nav = { detail: "requests", film: "films" }[current.name] || current.name;
  document.querySelectorAll("[data-nav]").forEach((link) => {
    const active = link.dataset.nav === nav;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  window.scrollTo(0, 0);

  if (current.name === "requests") {
    document.title = "Requests · MiniMax H3 Studio";
    loadList();
  } else if (current.name === "new") {
    document.title = "Tạo request · MiniMax H3 Studio";
    // A copied form is single-use; a plain draft is kept while you browse.
    // History loads first so the anchor picker lists the previous scenes.
    const history = loadTimingHistory();
    const source = current.continueFrom || current.from;
    if (source) {
      // Clear the previous form at once and block submitting until the copy is loaded.
      resetForm();
      showBox("copy-banner", "Đang nạp request cũ…");
      $("submit-button").disabled = true;
      history
        .then(() => fillFromRequest(source, { continueScene: Boolean(current.continueFrom) }))
        .finally(() => { $("submit-button").disabled = false; });
    } else if (state.formSource) resetForm();
    updateKeyBadge();
  } else if (current.name === "detail") {
    document.title = "Chi tiết request · MiniMax H3 Studio";
    state.detail = null;
    $("detail-title").textContent = "Đang tải…";
    $("detail-meta").replaceChildren();
    $("detail-video").replaceChildren();
    $("detail-images").replaceChildren();
    $("detail-info").replaceChildren();
    $("detail-prompt").textContent = "";
    $("detail-progress").hidden = true;
    $("chain-card").hidden = true;
    state.chainFor = null;
    showBox("detail-error", "");
    loadDetail(current.id);
  } else if (current.name === "films") {
    document.title = "Phim dài · MiniMax H3 Studio";
    loadFilms();
  } else if (current.name === "film") {
    showBox("film-form-error", "");
    $("film-progress").hidden = true;
    if (current.id) {
      $("film-heading").textContent = "Đang tải…";
      loadFilm(current.id);
    } else openNewFilm();
  } else {
    document.title = "Cài đặt · MiniMax H3 Studio";
    $("api-key").value = state.savedKey;
    settingsStatus("");
  }
}

/* ---------- events ---------- */

$("filter-title").addEventListener("input", () => {
  clearTimeout(state.listDebounce);
  state.listDebounce = setTimeout(loadList, 250);
});
$("refresh-list").addEventListener("click", loadList);
$("row-menu").addEventListener("click", async (event) => {
  const action = event.target.closest("button")?.dataset.action;
  const item = state.menuItem;
  closeMenu();
  if (!item) return;
  if (action === "copy") location.hash = `#/new?from=${item.id}`;
  if (action === "continue") {
    if (item.status === "COMPLETED" && item.video_name) location.hash = `#/new?continue=${item.id}`;
    else alert("Chỉ tiếp nối được request đã hoàn tất và có video lưu trên máy.");
  }
  if (action === "delete" && await deleteRequest(item)) loadList();
});
document.addEventListener("click", (event) => {
  if (!event.target.closest("#row-menu, .more-button")) closeMenu();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("row-menu").hidden) closeMenu();
});
window.addEventListener("resize", closeMenu);
window.addEventListener("scroll", closeMenu, { passive: true });

$("request-form").addEventListener("submit", submitRequest);
for (let index = 1; index <= 3; index++) {
  $(`image-${index}`).addEventListener("change", (event) => previewImage(index, event.target.files[0]));
}
$("prompt-text").addEventListener("input", () => { state.promptTouched = true; updatePromptCount(); updateAnchorPromptState(); });
$("anchor-enabled").addEventListener("change", () => setAnchorEnabled($("anchor-enabled").checked));
$("anchor-request").addEventListener("change", () => {
  showBox("form-error", "");
  useRequestAsAnchor($("anchor-request").value).catch((error) => {
    setAnchorFile(null, null);
    showBox("form-error", error.message || "Không lấy được khung cuối.");
  });
});
$("anchor-file").addEventListener("change", (event) => {
  const file = event.target.files[0];
  state.anchorSeq++;
  $("anchor-request").value = "";
  if (file && !["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
    event.target.value = "";
    setAnchorFile(null, null);
    return showBox("form-error", "Ảnh neo phải là PNG, JPG hoặc WebP.");
  }
  setAnchorFile(file || null, null);
});
$("anchor-apply").addEventListener("click", () => {
  setPrompt(applyAnchorPrompt($("prompt-text").value), true);
  updateAnchorPromptState();
});
$("chain-play").addEventListener("click", playChain);
$("detail-continue").addEventListener("click", () => {
  if (state.detail) location.hash = `#/new?continue=${state.detail.id}`;
});
$("reset-prompt").addEventListener("click", () => setPrompt(state.defaultPrompt, false));
$("duration-seconds").addEventListener("change", updateTimingHint);
$("resolution").addEventListener("change", updateTimingHint);

$("detail-copy").addEventListener("click", () => { if (state.detail) location.hash = `#/new?from=${state.detail.id}`; });
$("detail-delete").addEventListener("click", async () => {
  if (state.detail && await deleteRequest(state.detail)) location.hash = "#/requests";
});

$("film-add-scene").addEventListener("click", () => {
  syncFilmScenes();
  state.filmScenes.push(newFilmScene());
  renderFilmScenes();
  document.querySelector("#film-scenes .film-scene:last-child textarea")?.focus();
});
$("film-save").addEventListener("click", () => filmAction($("film-save"), saveFilm));
$("film-start").addEventListener("click", () => filmAction($("film-start"), startFilm));
$("film-stop").addEventListener("click", () => filmAction($("film-stop"), stopFilm));
$("film-delete").addEventListener("click", () => filmAction($("film-delete"), deleteFilm));
$("film-form").addEventListener("submit", (event) => event.preventDefault());
for (let slot = 1; slot <= 3; slot++) {
  $(`film-image-${slot}`).addEventListener("change", (event) => {
    const file = event.target.files[0];
    if (file && !["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
      event.target.value = "";
      return showBox("film-form-error", `Ảnh ${slot} phải là PNG, JPG hoặc WebP.`);
    }
    state.filmImages[slot - 1] = file || null;
    setFilmPreview(slot, file ? URL.createObjectURL(file) : null, file ? `${file.name} · ${sizeLabel(file.size)}` : null);
  });
}

$("settings-form").addEventListener("submit", saveKey);
$("delete-key").addEventListener("click", deleteKey);
$("test-key").addEventListener("click", testKey);
$("toggle-key").addEventListener("click", () => {
  const input = $("api-key");
  input.type = input.type === "password" ? "text" : "password";
  $("toggle-key").textContent = input.type === "password" ? "Hiện" : "Ẩn";
  $("toggle-key").setAttribute("aria-label", input.type === "password" ? "Hiện API key" : "Ẩn API key");
});

window.addEventListener("hashchange", route);
Promise.all([loadSavedKey(), loadWorkflow()]).finally(route);
