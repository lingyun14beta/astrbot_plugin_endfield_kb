/* 终末地知识库视图：通过 AstrBotPluginView bridge 调用插件后端。 */

const bridge = window.AstrBotPluginView ?? window.AstrBotPluginPage;
const prefersReduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const mobileLayout = window.matchMedia("(max-width: 860px), (orientation: portrait)");

const els = {
  form: document.getElementById("search-form"),
  input: document.getElementById("q"),
  submit: document.getElementById("submit"),
  rewrite: document.getElementById("rewrite"),
  semantic: document.getElementById("semantic"),
  semanticProvider: document.getElementById("semantic-provider"),
  semanticNote: document.getElementById("semantic-note"),
  scope: document.getElementById("scope"),
  limit: document.getElementById("limit"),
  list: document.getElementById("list"),
  placeholder: document.getElementById("placeholder"),
  readout: document.getElementById("readout"),
  detailBody: document.getElementById("detail-body"),
  detailMeta: document.getElementById("detail-meta"),
  detail: document.getElementById("detail"),
  docs: document.getElementById("stat-docs"),
  chars: document.getElementById("stat-chars"),
  size: document.getElementById("stat-size"),
  built: document.getElementById("stat-built"),
  release: document.getElementById("release"),
  examples: document.getElementById("examples"),
  stage: document.getElementById("stage"),
  results: document.querySelector(".ark-results"),
  checkUpdate: document.getElementById("check-update"),
  importIndex: document.getElementById("import-index"),
  updateStatus: document.getElementById("update-status"),
  dialog: document.getElementById("import-dialog"),
  importForm: document.getElementById("import-form"),
  tabFile: document.getElementById("tab-file"),
  tabUrl: document.getElementById("tab-url"),
  importFile: document.getElementById("import-file"),
  importUrl: document.getElementById("import-url"),
  importStatus: document.getElementById("import-status"),
  importTrack: document.getElementById("import-track"),
  importSubmit: document.getElementById("import-submit"),
  importClose: document.getElementById("import-close"),
  importCancel: document.getElementById("import-cancel"),
  pager: document.getElementById("pager"),
  pagerPrev: document.getElementById("pager-prev"),
  pagerNext: document.getElementById("pager-next"),
  pagerStatus: document.getElementById("pager-status"),
};

const PAGE_SIZE = 10;

const state = {
  busy: false,
  hits: [],
  active: -1,
  page: 0,
  pendingUpdate: null,
  hasIndex: false,
};

function t(key, fallback) {
  if (bridge?.t) {
    const value = bridge.t(key, fallback);
    return typeof value === "string" && value ? value : fallback;
  }
  return fallback;
}

function formatCount(value) {
  if (!Number.isFinite(value)) return "—";
  return value.toLocaleString("zh-CN");
}

function formatChars(value) {
  if (!Number.isFinite(value)) return "—";
  if (value >= 10000) return `${(value / 10000).toFixed(1)} 万`;
  return formatCount(value);
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return "—";
  return `${(bytes / 1048576).toFixed(1)} MiB`;
}

function compactTime(value) {
  if (!value) return "—";
  return String(value).slice(5, 16);
}

function formatRelease(value) {
  if (!value) return "PRTS 终末地本地语料";
  const match = /endfield-(\d+\.\d+\.\d+)/.exec(value);
  if (match) return `终末地 ${match[1]}`;
  return value.length > 42 ? `${value.slice(0, 42)}…` : value;
}

function countUp(el, target, format, duration = 900) {
  if (!Number.isFinite(target)) {
    el.textContent = format(0);
    return;
  }
  if (prefersReduced) {
    el.textContent = format(target);
    return;
  }
  const started = performance.now();
  const step = (now) => {
    const progress = Math.min(1, (now - started) / duration);
    const eased = 1 - Math.pow(1 - progress, 3);
    el.textContent = format(Math.round(target * eased));
    if (progress < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function pulseResults() {
  if (prefersReduced || !els.results) return;
  els.results.classList.remove("is-scanning");
  void els.results.offsetWidth;
  els.results.classList.add("is-scanning");
  window.setTimeout(() => els.results?.classList.remove("is-scanning"), 700);
}

function replayDetail() {
  if (prefersReduced) return;
  els.detailBody.classList.remove("is-entering");
  void els.detailBody.offsetWidth;
  els.detailBody.classList.add("is-entering");
}

function setBusy(busy) {
  state.busy = busy;
  els.submit.disabled = busy;
  els.submit.setAttribute("aria-busy", String(busy));
}

function renderSkeleton() {
  els.placeholder.hidden = true;
  els.list.replaceChildren();
  for (let index = 0; index < 3; index += 1) {
    const row = document.createElement("li");
    const block = document.createElement("div");
    block.className = "ark-skeleton";
    row.appendChild(block);
    els.list.appendChild(row);
  }
}

function clearList() {
  els.list.replaceChildren();
}

function renderEmpty(message, note) {
  clearList();
  els.placeholder.hidden = true;
  const box = document.createElement("div");
  box.className = "ark-empty";
  const line = document.createElement("p");
  line.textContent = message;
  box.appendChild(line);
  if (note) {
    const hint = document.createElement("p");
    hint.className = "ark-placeholder-note";
    hint.textContent = note;
    box.appendChild(hint);
  }
  els.list.appendChild(box);
}

function renderError(message) {
  clearList();
  els.placeholder.hidden = true;
  const box = document.createElement("div");
  box.className = "ark-error";
  box.setAttribute("role", "alert");
  box.textContent = message;
  els.list.appendChild(box);
}

function renderHits(payload) {
  clearList();
  els.placeholder.hidden = true;
  pulseResults();
  state.hits = payload.hits ?? [];
  state.active = -1;
  state.page = 0;
  els.detailMeta.textContent = t("views.kb-console.noSelection", "未选择");
  els.detailBody.replaceChildren();
  const empty = document.createElement("p");
  empty.className = "ark-detail-empty";
  empty.textContent = t(
    "views.kb-console.detailEmpty",
    "从左侧选择一条结果，这里显示完整正文与来源信息。",
  );
  els.detailBody.appendChild(empty);

  if (!state.hits.length) {
    updatePager();
    renderEmpty(
      t("views.kb-console.noHit", "没有命中。"),
      t(
        "views.kb-console.noHitNote",
        "换更具体的专名或原文片段再试；口语描述可以打开“关键词改写”。",
      ),
    );
    return;
  }

  renderPage();
}

function renderPage() {
  clearList();
  const start = state.page * PAGE_SIZE;
  state.hits.slice(start, start + PAGE_SIZE).forEach((hit, offset) => {
    const index = start + offset;
    const item = document.createElement("li");
    item.className = "ark-item";
    item.dataset.index = String(index);
    item.style.setProperty("--i", String(offset));

    const button = document.createElement("button");
    button.type = "button";
    button.className = "ark-item-button";
    button.dataset.index = String(index);

    const number = document.createElement("span");
    number.className = "ark-item-index";
    number.textContent = String(index + 1).padStart(2, "0");

    const body = document.createElement("span");
    body.className = "ark-item-content";

    const head = document.createElement("span");
    head.className = "ark-item-head";

    const badge = document.createElement("span");
    badge.className = "ark-badge";
    badge.dataset.kind = hit.kind;
    badge.textContent = hit.kind_label;

    const title = document.createElement("span");
    title.className = "ark-item-title";
    title.textContent = hit.title;

    const coll = document.createElement("span");
    coll.className = "ark-item-coll";
    coll.textContent = hit.collection ? `· ${hit.collection}` : "";

    head.append(badge, title, coll);

    const snippet = document.createElement("span");
    snippet.className = "ark-item-snippet";
    snippet.textContent = hit.body.split("\n").slice(0, 2).join(" ");

    body.append(head, snippet);
    button.append(number, body);
    button.addEventListener("click", () => selectHit(index, true));
    item.appendChild(button);
    els.list.appendChild(item);
  });
  updatePager();
}

function updatePager() {
  const total = Math.max(1, Math.ceil(state.hits.length / PAGE_SIZE));
  const paged = state.hits.length > PAGE_SIZE;
  els.pager.hidden = !paged;
  if (!paged) return;
  els.pagerStatus.textContent = `${state.page + 1} / ${total}`;
  els.pagerPrev.disabled = state.page === 0;
  els.pagerNext.disabled = state.page >= total - 1;
}

function goToPage(delta) {
  const total = Math.ceil(state.hits.length / PAGE_SIZE);
  if (total < 2) return;
  const target = Math.min(Math.max(state.page + delta, 0), total - 1);
  if (target === state.page) return;
  state.page = target;
  renderPage();
  selectHit(target * PAGE_SIZE);
  els.list.firstElementChild?.scrollIntoView({
    behavior: prefersReduced ? "auto" : "smooth",
    block: "nearest",
  });
}

function selectHit(index, scrollDetail = false) {
  const hit = state.hits[index];
  if (!hit) return;
  state.active = index;

  [...els.list.children].forEach((item) => {
    const isActive = Number(item.dataset.index) === index;
    item.dataset.active = isActive ? "true" : "false";
    const button = item.querySelector("button");
    if (isActive) {
      button?.setAttribute("aria-current", "true");
    } else {
      button?.removeAttribute("aria-current");
    }
  });

  els.detailMeta.textContent = `${hit.kind_label} · ${hit.chars} 字`;

  els.detailBody.replaceChildren();
  const title = document.createElement("h2");
  title.className = "ark-detail-title";
  title.textContent = hit.title;

  const facts = document.createElement("p");
  facts.className = "ark-detail-facts";
  const bits = [
    hit.kind_label,
    hit.collection || "未归类",
    hit.semantic ? "语义召回" : `BM25 ${hit.score}`,
    `${hit.chars} 字`,
  ];
  bits.forEach((text) => {
    const span = document.createElement("span");
    span.textContent = text;
    facts.appendChild(span);
  });

  const pre = document.createElement("pre");
  pre.className = "ark-body";
  pre.textContent = hit.body;

  const refs = Array.isArray(hit.refs) ? hit.refs : [];
  if (refs.length) {
    const details = document.createElement("details");
    details.className = "ark-refs";
    const summary = document.createElement("summary");
    summary.textContent = `出处 · ${refs.length}`;
    const ul = document.createElement("ul");
    refs.forEach((ref) => {
      const li = document.createElement("li");
      const code = document.createElement("code");
      code.textContent = ref;
      li.appendChild(code);
      ul.appendChild(li);
    });
    details.append(summary, ul);
    els.detailBody.append(title, facts, details, pre);
  } else {
    els.detailBody.append(title, facts, pre);
  }
  els.detailBody.scrollTop = 0;
  replayDetail();
  if (scrollDetail && mobileLayout.matches) {
    els.detail?.scrollIntoView({
      behavior: prefersReduced ? "auto" : "smooth",
      block: "start",
    });
  }
}

async function runSearch(rawQuery) {
  const query = (rawQuery ?? els.input.value ?? "").trim();
  if (!query || state.busy) {
    els.input.focus();
    return;
  }
  setBusy(true);
  els.readout.textContent = t("views.kb-console.searching", "检索中…");
  renderSkeleton();

  try {
    const scope = els.scope?.value || "";
    const limit = Number(els.limit?.value) || 30;
    const payload = await bridge.apiGet("search", {
      q: query,
      k: limit,
      scope,
      rewrite: els.rewrite.checked ? 1 : 0,
    });
    const count = payload.count ?? 0;
    const elapsed = payload.elapsed_ms ?? 0;
    const rewritten = payload.rewritten
      ? ` · 改写为 ${payload.used_query}`
      : "";
    const scopeNote = scope
      ? ` · 仅${els.scope.options[els.scope.selectedIndex].textContent}`
      : "";
    els.readout.textContent = `命中 ${count} 条 · ${elapsed} ms${scopeNote}${rewritten}`;
    renderHits(payload);
    if (count > 0) selectHit(0);
  } catch (error) {
    els.readout.textContent = t("views.kb-console.failed", "检索失败");
    renderError(error?.message || "请求失败，请检查插件是否已加载。");
  } finally {
    setBusy(false);
  }
}

async function loadStats() {
  try {
    const stats = await bridge.apiGet("stats");
    state.hasIndex = true;
    countUp(els.docs, Number(stats.documents), formatCount, 900);
    countUp(els.chars, Number(stats.characters), formatChars, 1100);
    els.size.textContent = `${stats.size_mb} MB`;
    els.built.textContent = compactTime(stats.built_at);
    const refs = Number(stats.refs);
    const refNote = Number.isFinite(refs) && refs > 0 ? ` · 出处 ${formatCount(refs)} 处` : "";
    const indexNote = stats.index_version ? ` · 索引 ${stats.index_version}` : "";
    els.release.textContent = `语料：${formatRelease(stats.release)}${indexNote}${refNote} · 磁盘建议预留 150 MB`;
  } catch (error) {
    state.hasIndex = false;
    els.release.textContent = "尚未安装索引 · 点击上方「下载索引」";
    markNoIndex();
  }
}

function semanticNote(payload) {
  if (payload.building) return "构建中…";
  if (!payload.index) return "未构建向量（/zmd向量）";
  return `${formatCount(Number(payload.index.count) || 0)} 篇已建`;
}

function renderSemantic(payload) {
  const providers = Array.isArray(payload.providers) ? payload.providers : [];
  els.semantic.checked = Boolean(payload.enable);
  els.semanticProvider.innerHTML = "";
  const auto = document.createElement("option");
  auto.value = "";
  auto.textContent = providers.length ? "默认（第一个）" : "无可用模型";
  els.semanticProvider.appendChild(auto);
  providers.forEach((item) => {
    const option = document.createElement("option");
    option.value = item.id;
    const dim = item.dim ? ` · ${item.dim}维` : "";
    option.textContent = `${item.id}${item.model ? ` · ${item.model}` : ""}${dim}`;
    els.semanticProvider.appendChild(option);
  });
  els.semanticProvider.value = payload.provider || "";
  els.semanticProvider.disabled = providers.length === 0;
  els.semanticNote.textContent = semanticNote(payload);
}

async function loadSemantic() {
  try {
    const payload = await bridge.apiGet("semantic");
    if (payload) renderSemantic(payload);
  } catch (error) {
    els.semanticNote.textContent = "状态读取失败";
  }
}

async function saveSemantic(patch) {
  try {
    const payload = await bridge.apiPost("semantic", patch);
    if (payload) renderSemantic(payload);
  } catch (error) {
    els.semanticNote.textContent = "保存失败";
  }
}

function markNoIndex() {
  if (!els.checkUpdate) return;
  els.checkUpdate.textContent = "下载索引";
  els.checkUpdate.dataset.tone = "primary";
  updateStatus("尚未安装索引，点击下载（约 12.5 MiB）");
}

function markHasIndex() {
  if (!els.checkUpdate) return;
  els.checkUpdate.textContent = "检查更新";
  els.checkUpdate.removeAttribute("data-tone");
}

function updateStatus(text) {
  if (els.updateStatus) els.updateStatus.textContent = text;
}

async function handleFirstDownload() {
  els.checkUpdate.disabled = true;
  updateStatus("下载索引…");
  try {
    const result = await bridge.apiPost("update", {});
    if (!result.ok) {
      updateStatus(result.message || "下载失败");
      return;
    }
    state.hasIndex = true;
    markHasIndex();
    updateStatus(`索引已就绪 · ${result.release} · ${result.documents} 篇`);
    loadStats();
  } catch (error) {
    updateStatus(error?.message || "下载失败");
  } finally {
    els.checkUpdate.disabled = false;
  }
}

async function handleCheckUpdate() {
  if (!els.checkUpdate || els.checkUpdate.disabled) return;
  els.checkUpdate.disabled = true;
  updateStatus("检查中…");
  try {
    const info = await bridge.apiGet("updates");
    if (!info.ok) {
      updateStatus(info.message || "检查失败");
      return;
    }
    if (!info.remote) {
      updateStatus(info.message || "无法获取更新清单");
      return;
    }
    if (info.up_to_date) {
      state.pendingUpdate = null;
      els.checkUpdate.textContent = "检查更新";
      els.checkUpdate.removeAttribute("data-tone");
      updateStatus(`已是最新 · ${info.remote.release}`);
    } else {
      state.pendingUpdate = info.remote;
      els.checkUpdate.textContent = `更新到 ${info.remote.release}`;
      els.checkUpdate.dataset.tone = "primary";
      updateStatus(
        `发现 ${info.remote.release} · ${info.remote.documents} 篇 · 需下载 ${formatBytes(info.remote.size)}`,
      );
    }
  } catch (error) {
    updateStatus(error?.message || "检查失败");
  } finally {
    els.checkUpdate.disabled = false;
  }
}

async function handleApplyUpdate() {
  els.checkUpdate.disabled = true;
  updateStatus("下载并安装…");
  try {
    const result = await bridge.apiPost("update", {});
    if (!result.ok) {
      updateStatus(result.message || "更新失败");
      return;
    }
    state.pendingUpdate = null;
    state.hasIndex = true;
    markHasIndex();
    updateStatus(`已更新到 ${result.release} · ${result.documents} 篇`);
    loadStats();
  } catch (error) {
    updateStatus(error?.message || "更新失败");
  } finally {
    els.checkUpdate.disabled = false;
  }
}

let importMode = "file";

function setImportTrack(state) {
  if (!els.importTrack) return;
  els.importTrack.dataset.state = state;
}

function setImportMode(mode) {
  importMode = mode;
  const isFile = mode === "file";
  els.tabFile?.setAttribute("aria-selected", String(isFile));
  els.tabUrl?.setAttribute("aria-selected", String(!isFile));
  const filePanel = document.querySelector('[data-panel="file"]');
  const urlPanel = document.querySelector('[data-panel="url"]');
  if (filePanel) filePanel.hidden = !isFile;
  if (urlPanel) urlPanel.hidden = isFile;
  els.importStatus.textContent = "";
  (isFile ? els.importFile : els.importUrl)?.focus();
}

function openImportDialog() {
  if (!els.dialog) return;
  els.importFile.value = "";
  els.importUrl.value = "";
  els.importStatus.textContent = "";
  setImportTrack("idle");
  els.dialog.showModal();
  setImportMode("file");
}

function closeImportDialog() {
  els.dialog?.close();
}

function finishImport(result) {
  state.hasIndex = true;
  markHasIndex();
  setImportTrack("done");
  els.importStatus.textContent = `已导入 ${result.release || ""} · ${result.documents} 篇`;
  loadStats();
  window.setTimeout(closeImportDialog, 900);
}

async function submitImport(event) {
  event.preventDefault();
  if (els.importSubmit.disabled) return;

  if (importMode === "file") {
    const file = els.importFile.files?.[0];
    if (!file) {
      els.importStatus.textContent = "请选择一个文件";
      return;
    }
    if (!/\.(zip|gz|sqlite3)$/i.test(file.name)) {
      els.importStatus.textContent = "只支持 .zip / .gz / .sqlite3";
      return;
    }
    els.importSubmit.disabled = true;
    setImportTrack("busy");
    els.importStatus.textContent = `上传 ${file.name}…`;
    try {
      const result = await bridge.upload("upload", file);
      if (!result.ok) {
        setImportTrack("idle");
        els.importStatus.textContent = result.message || "导入失败";
        return;
      }
      finishImport(result);
    } catch (error) {
      setImportTrack("idle");
      els.importStatus.textContent = error?.message || "导入失败";
    } finally {
      els.importSubmit.disabled = false;
    }
    return;
  }

  const url = els.importUrl.value.trim();
  if (!url) {
    els.importStatus.textContent = "请填写下载链接";
    return;
  }
  if (!/^https?:\/\//i.test(url)) {
    els.importStatus.textContent = "只支持 http/https 链接";
    return;
  }
  els.importSubmit.disabled = true;
  setImportTrack("busy");
  els.importStatus.textContent = "下载中…";
  try {
    const result = await bridge.apiPost("import_url", { url });
    if (!result.ok) {
      setImportTrack("idle");
      els.importStatus.textContent = result.message || "导入失败";
      return;
    }
    finishImport(result);
  } catch (error) {
    setImportTrack("idle");
    els.importStatus.textContent = error?.message || "导入失败";
  } finally {
    els.importSubmit.disabled = false;
  }
}

function boot() {
  if (!bridge) {
    els.readout.textContent = "bridge 不可用";
    renderError("当前页面不在 AstrBot 插件视图中运行，无法调用后端接口。");
    return;
  }

  document.title = t("views.kb-console.title", "终末地知识库");

  els.form.addEventListener("submit", (event) => {
    event.preventDefault();
    runSearch();
  });

  els.examples.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-q]");
    if (!button) return;
    els.input.value = button.dataset.q;
    runSearch(button.dataset.q);
  });

  const rerunIfIdle = () => {
    if (state.busy || !state.hasIndex) return;
    if (els.input.value.trim()) runSearch(els.input.value);
  };
  els.scope?.addEventListener("change", rerunIfIdle);
  els.limit?.addEventListener("change", rerunIfIdle);
  els.semantic?.addEventListener("change", () => {
    saveSemantic({ enable: els.semantic.checked });
  });
  els.semanticProvider?.addEventListener("change", () => {
    saveSemantic({ provider: els.semanticProvider.value });
  });
  els.pagerPrev?.addEventListener("click", () => goToPage(-1));
  els.pagerNext?.addEventListener("click", () => goToPage(1));

  els.checkUpdate?.addEventListener("click", () => {
    if (!state.hasIndex) handleFirstDownload();
    else if (state.pendingUpdate) handleApplyUpdate();
    else handleCheckUpdate();
  });

  els.importIndex?.addEventListener("click", openImportDialog);
  els.tabFile?.addEventListener("click", () => setImportMode("file"));
  els.tabUrl?.addEventListener("click", () => setImportMode("url"));
  els.importForm?.addEventListener("submit", submitImport);
  els.importClose?.addEventListener("click", closeImportDialog);
  els.importCancel?.addEventListener("click", closeImportDialog);

  document.addEventListener("keydown", (event) => {
    if (event.key === "/" && document.activeElement !== els.input) {
      event.preventDefault();
      els.input.focus();
    }
    if (event.key === "Escape" && document.activeElement === els.input) {
      els.input.blur();
    }
  });

  bridge.ready?.().then?.(() => {
    document.title = t("views.kb-console.title", "终末地知识库");
  });

  loadStats();
  loadSemantic();
  els.input.focus();
}

boot();
