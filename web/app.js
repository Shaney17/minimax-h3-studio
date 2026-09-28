const $ = (id) => document.getElementById(id);
const NODE_IDS = ["28", "29", "30"];
const MAX_BODY_BYTES = 9_500_000;
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
  const link = element("a", "row-title", item.title);
  link.href = `#/requests/${item.id}`;
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

  row.append(titleCell, statusCell, element("td", "muted nowrap", settingsLabel(item)), videoCell, element("td", "muted nowrap", formatTime(item.created_at)), actionCell);
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

function resetForm() {
  $("title-text").value = "";
  setPrompt(state.defaultPrompt, false);
  $("duration-seconds").value = "5";
  $("video-size").value = "28";
  $("resolution").value = "0.2";
  for (let index = 1; index <= 3; index++) {
    $(`image-${index}`).value = "";
    previewImage(index, null);
  }
  showBox("form-error", "");
  $("copy-banner").hidden = true;
  $("new-heading").textContent = "Tạo request mới";
  state.formSource = null;
}

async function fillFromRequest(id) {
  resetForm();
  $("new-heading").textContent = "Sao chép request";
  showBox("copy-banner", "Đang nạp request cũ…");
  try {
    const item = await localJson(`/api/requests/${encodeURIComponent(id)}`);
    for (const image of item.images) {
      const response = await fetch(image.url);
      if (!response.ok) throw new Error(`Không đọc được ảnh ${image.slot} đã lưu.`);
      const blob = await response.blob();
      const file = new File([blob], image.name, { type: image.mime });
      const transfer = new DataTransfer();
      transfer.items.add(file);
      $(`image-${image.slot}`).files = transfer.files;
      previewImage(image.slot, file);
    }
    $("title-text").value = `${item.title} (bản sao)`.slice(0, 200);
    setPrompt(item.prompt, true);
    if (item.duration != null) $("duration-seconds").value = String(Number(item.duration));
    if (item.crf != null) $("video-size").value = String(Number(item.crf));
    if (item.resolution != null) $("resolution").value = String(Number(item.resolution));
    state.formSource = id;
    showBox("copy-banner", `Đã sao chép tiêu đề, prompt, thông số và 3 ảnh từ “${item.title}”. Chỉnh sửa nếu cần rồi bấm Gửi request.`);
  } catch (error) {
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
  if (![2, 5, 10, 30].includes(duration) || ![15, 28].includes(crf) || ![0.2, 1].includes(resolution)) {
    throw new Error("Thiết lập video không hợp lệ.");
  }
  graph["111"].inputs.value = duration;
  graph["154"].inputs.crf = crf;
  if (resolution === 0.2) {
    graph["112"].inputs.megapixels = 0.2;
    graph["102"].inputs.value = 0.2;
  }
  const images = [];
  for (let index = 1; index <= 3; index++) {
    const file = $(`image-${index}`).files[0];
    if (!file) throw new Error(`Hãy chọn ảnh tham chiếu ${index}.`);
    const safeName = file.name.replace(/[^A-Za-z0-9._-]/g, "_");
    const name = `h3_ref_${index}_${Date.now()}_${safeName}`;
    graph[NODE_IDS[index - 1]].inputs.image = name;
    images.push({ name, image: await toDataUrl(file) });
  }
  const body = JSON.stringify({
    title: $("title-text").value.trim(),
    input: { workflow: graph, images },
    policy: { executionTimeout: 1_200_000, ttl: 3_600_000 },
  });
  if (new TextEncoder().encode(body).length > MAX_BODY_BYTES) {
    throw new Error("Ba ảnh và workflow vượt giới hạn 9,5 MB. Hãy giảm kích thước ảnh rồi thử lại.");
  }
  return body;
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

  $("detail-prompt").textContent = item.prompt;
  const labels = ["Nhân vật 1", "Nhân vật 2", "Bối cảnh"];
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
    ["Tạo lúc", formatTime(item.created_at)],
    ["Cập nhật", formatTime(item.updated_at)],
  ];
  $("detail-info").replaceChildren(...info.flatMap(([label, value]) => [element("dt", "", label), element("dd", "", value)]));
}

/* ---------- routing ---------- */

function parseRoute() {
  const [path, query] = location.hash.replace(/^#/, "").split("?");
  const params = new URLSearchParams(query || "");
  const detail = path.match(/^\/requests\/([a-f0-9]{32})$/);
  if (detail) return { name: "detail", id: detail[1] };
  if (path === "/new") return { name: "new", from: params.get("from") };
  if (path === "/settings") return { name: "settings" };
  return { name: "requests" };
}

function route() {
  closeMenu();
  clearTimeout(state.listTimer);
  clearTimeout(state.detailTimer);
  const current = parseRoute();
  state.route = current;
  document.querySelectorAll(".view").forEach((section) => { section.hidden = section.id !== `view-${current.name}`; });
  const nav = current.name === "detail" ? "requests" : current.name;
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
    if (current.from) fillFromRequest(current.from);
    else if (state.formSource) resetForm();
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
    showBox("detail-error", "");
    loadDetail(current.id);
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
$("prompt-text").addEventListener("input", () => { state.promptTouched = true; updatePromptCount(); });
$("reset-prompt").addEventListener("click", () => setPrompt(state.defaultPrompt, false));

$("detail-copy").addEventListener("click", () => { if (state.detail) location.hash = `#/new?from=${state.detail.id}`; });
$("detail-delete").addEventListener("click", async () => {
  if (state.detail && await deleteRequest(state.detail)) location.hash = "#/requests";
});

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
