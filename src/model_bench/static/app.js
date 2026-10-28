const $ = (id) => document.getElementById(id);
const setText = (id, value) => {
  const node = $(id);
  if (node) node.textContent = value;
};

let models = [];
let lastRunning = false;
let serviceStatus = { running: false, current_model: "", web_benchmark_enabled: false };
let rankingSource = localStorage.getItem("model-bench-ranking-source") || "epoch";
const selectedModels = new Set();
const fmt = (n, suffix = "") => n == null ? "—" : `${Number(n).toLocaleString("zh-CN", { maximumFractionDigits: 1 })}${suffix}`;
const priceFmt = (n) => `$${Number(n).toFixed(3)}`;
const escapeHtml = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

async function api(path, options = {}) {
  const token = sessionStorage.getItem("model-bench-admin-token");
  const headers = new Headers(options.headers || {});
  if (token) headers.set("X-Model-Bench-Token", token);
  const response = await fetch(path, {...options, headers});
  const body = await response.json();
  if (!response.ok) throw Error(body.detail || `HTTP ${response.status}`);
  return body;
}

function filteredModels() {
  const q = $("search")?.value.trim().toLowerCase() || "";
  const filter = $("filter")?.value || "all";
  return models.filter(model => (model.id.toLowerCase().includes(q) || model.name.toLowerCase().includes(q))
    && (filter === "all" || (filter === "ranked" ? model.rank != null : model.rank == null)));
}

function updateSelectionControls(visible) {
  const enabled = serviceStatus.web_benchmark_enabled;
  const selectColumn = $("select-header");
  const selectAll = $("select-all");
  const batch = $("batch-benchmark");
  if (selectColumn) selectColumn.hidden = !enabled;
  if (batch) {
    batch.hidden = !enabled;
    batch.disabled = !enabled || serviceStatus.running || selectedModels.size === 0;
    batch.textContent = selectedModels.size ? `测速所选 (${selectedModels.size})` : "测速所选";
  }
  if (!selectAll) return;
  selectAll.disabled = !enabled || serviceStatus.running || visible.length === 0;
  selectAll.checked = visible.length > 0 && visible.every(model => selectedModels.has(model.id));
  selectAll.indeterminate = !selectAll.checked && visible.some(model => selectedModels.has(model.id));
}

function render() {
  const filtered = filteredModels();
  const benchmarkEnabled = serviceStatus.web_benchmark_enabled;
  for (const modelId of selectedModels) {
    if (!models.some(model => model.id === modelId)) selectedModels.delete(modelId);
  }
  setText("total", models.length || "0");
  setText("ranked", models.filter(model => model.rank != null).length);
  setText("measured", models.filter(model => model.speed?.status === "ok").length);
  setText("count", `显示 ${filtered.length} / ${models.length} 个模型`);
  const empty = $("empty");
  if (empty) empty.hidden = filtered.length > 0;
  setText("empty", models.length ? "没有符合条件的模型" : "配置文件中尚未添加模型。");
  const rows = $("rows");
  if (!rows) return;
  rows.innerHTML = filtered.map(model => {
    const speed = model.speed;
    const shortNames = { "LiveBench 综合能力": "LiveBench", "Artificial Analysis Intelligence Index": "AA", "LMArena Text": "Arena" };
    const epochMetrics = model.metrics.filter(metric => metric.label.startsWith("Epoch "));
    const sourceLinks = [];
    if (epochMetrics.length) {
      const details = epochMetrics.map(metric => `${metric.label.replace("Epoch ", "")} ${metric.value}`).join(" · ");
      sourceLinks.push(`<a href="${escapeHtml(epochMetrics[0].url || "#")}" target="_blank" rel="noopener" title="${escapeHtml(`Epoch AI · ${details}`)}">Epoch</a>`);
    }
    for (const metric of model.metrics.filter(metric => !metric.label.startsWith("Epoch "))) {
      sourceLinks.push(`<a href="${escapeHtml(metric.url || "#")}" target="_blank" rel="noopener" title="${escapeHtml(metric.label + " " + metric.value)}">${escapeHtml(shortNames[metric.label] || metric.label)}</a>`);
    }
    const sources = sourceLinks.join('<span class="dot">·</span>');
    const price = model.price ? `<div class="price"><span>输入 <b>${priceFmt(model.price.input)}</b></span><span>输出 <b>${priceFmt(model.price.output)}</b></span></div>` : '<span class="muted">—</span>';
    const outputSpeed = speed?.status === "ok" ? `<span class="numeric">${fmt(speed.tokens_per_second)}</span>${speed.confidence !== "moderate" ? `<sup title="${escapeHtml(speed.token_source === "estimated" ? "接口未返回精确用量，按文本长度粗估" : "可见输出少于150 token或仅一次有效样本")}">参考</sup>` : ""}` : speed ? `<span class="error" title="${escapeHtml(speed.error)}">${speed.status === "no_text" ? "无文本" : "失败"}</span>` : "—";
    const customBadge = model.custom ? '<span class="custom-badge">自建</span>' : "";
    const disabled = serviceStatus.running ? "disabled" : "";
    const label = serviceStatus.current_model === model.id ? "测速中" : (speed?.status === "ok" ? "重测" : "测速");
    const checked = selectedModels.has(model.id) ? "checked" : "";
    return `<tr>
      ${benchmarkEnabled ? `<td class="select-cell"><input type="checkbox" data-select-model="${escapeHtml(model.id)}" ${checked} ${disabled} aria-label="选择 ${escapeHtml(model.id)}"></td>` : ""}
      <td><span class="rank ${model.rank && model.rank <= 3 ? "top" : ""}">${model.rank ? String(model.rank).padStart(2, "0") : "—"}</span></td>
      <td><strong class="model-name"><span class="model-label">${escapeHtml(model.id)}${customBadge}</span></strong><small>${escapeHtml(model.name !== model.id ? model.name : "")}</small></td>
      <td>${model.score == null ? '<span class="muted">数据不足</span>' : `<div class="score"><strong>${fmt(model.score)}</strong><div class="bar"><i style="width:${model.score}%"></i></div></div>`}</td>
      <td>${price}</td>
      <td class="sources">${sources || '<span class="muted">未匹配</span>'}</td>
      <td class="numeric">${speed?.status === "ok" ? fmt(speed.ttft_ms, " ms") : "—"}</td>
      <td>${outputSpeed}</td>
      <td class="date">${speed ? new Date(speed.tested_at).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—"}${speed ? `<small>${escapeHtml(speed.protocol)} · ${escapeHtml(speed.effort || "默认")} · ${speed.samples}次</small>` : ""}</td>
      ${benchmarkEnabled ? `<td><button class="benchmark-button" data-model="${escapeHtml(model.id)}" ${disabled} title="串行发送真实请求并取中位数">${label}</button></td>` : ""}
    </tr>`;
  }).join("");
  updateSelectionControls(filtered);
}

async function load() {
  try {
    const [status, board] = await Promise.all([
      api("/api/status"),
      api(`/api/leaderboard?source=${encodeURIComponent(rankingSource)}`),
    ]);
    models = board;
    serviceStatus = status;
    setText("endpoint", status.endpoint_label || "本地配置");
    setText("connection", status.configured ? "● 配置已加载" : "○ 等待配置");
    $("connection")?.classList.toggle("ready", status.configured);
    setText("updated", status.snapshot_at ? `榜单数据 ${new Date(status.snapshot_at).toLocaleDateString("zh-CN")}` : "暂无榜单数据");
    setText("price-date", status.price_snapshot_at ? new Date(`${status.price_snapshot_at}T12:00:00`).toLocaleDateString("zh-CN") : "未更新");
    setText("scope", status.ranked_only ? `前 ${status.top_n} 名` : "配置模型");
    setText("interval", status.interval_hours);
    setText("tokens", status.max_tokens);
    const actionHeader = $("action-header");
    if (actionHeader) actionHeader.hidden = !status.web_benchmark_enabled;
    $("config-link")?.toggleAttribute("hidden", !status.web_config_enabled);
    $("refresh-ranking")?.toggleAttribute("hidden", !status.web_config_enabled);
    if (status.running) showNotice(`串行测速中：${status.progress}`, "info");
    else if (lastRunning && status.last_error) showNotice(`测速完成，部分请求失败：${status.last_error}`, "error");
    else if (lastRunning) showNotice(status.progress || "测速完成", "success");
    else if (!status.web_benchmark_enabled) showNotice("当前为只读展示。管理员可在配置中启用网页测速。", "info");
    lastRunning = status.running;
    render();
  } catch (error) {
    showNotice(error.message, "error");
  }
}

async function refreshRankings() {
  const button = $("refresh-ranking");
  if (!button) return;
  button.disabled = true;
  const original = button.innerHTML;
  button.textContent = "拉取中…";
  showNotice("正在刷新 Epoch AI、LiveBench、LMArena 和 Artificial Analysis…", "info");
  try {
    const result = await api(`/api/rankings/refresh?source=${encodeURIComponent(rankingSource)}`, { method: "POST" });
    await load();
    const sources = (result.sources || []).map(source => {
      if (source.status === "cached") return `${source.name} 已复用`;
      if (source.status === "skipped") return `${source.name} 已跳过（${source.reason || "未配置"}）`;
      if (source.status === "error") return `${source.name} 失败（${source.reason || "未知错误"}${source.using_cache ? "，保留旧数据" : ""}）`;
      return `${source.name} 已更新`;
    }).join("，");
    const network = result.fetched_via === "fallback_proxy"
      ? "部分来源在直连失败后使用了备用代理"
      : result.fetched_via === "cache"
        ? "本次未重复请求上游"
        : result.fetched_via === "mixed"
          ? "已复用成功来源并重试其余来源"
          : "更新来源已通过官网直连完成";
    showNotice(`${sources || "上游数据已更新"}；${network}；${result.ranked_count} / ${result.model_count} 个模型进入排名。`, "success");
  } catch (error) {
    showNotice(`刷新失败，已保留原排名数据：${error.message}`, "error");
  } finally {
    button.innerHTML = original;
    button.disabled = false;
  }
}

async function benchmark(model) {
  try {
    await api(`/api/benchmark/${encodeURIComponent(model)}`, { method: "POST" });
    showNotice(`已开始串行测速：${model}`, "info");
    await load();
  } catch (error) {
    showNotice(error.message, "error");
  }
}

async function batchBenchmark() {
  const selected = [...selectedModels];
  if (!selected.length) return;
  try {
    await api("/api/benchmark", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ models: selected }),
    });
    showNotice(`已排队 ${selected.length} 个模型，将严格逐个测速。`, "info");
    await load();
  } catch (error) {
    showNotice(error.message, "error");
  }
}

function showNotice(text, type) {
  const node = $("notice");
  if (!node) return;
  node.hidden = false;
  node.className = `notice ${type}`;
  node.textContent = text;
}

$("search")?.addEventListener("input", render);
$("filter")?.addEventListener("change", render);
$("ranking-source")?.addEventListener("change", event => {
  rankingSource = event.target.value;
  localStorage.setItem("model-bench-ranking-source", rankingSource);
  load();
});
$("select-all")?.addEventListener("change", event => {
  for (const model of filteredModels()) {
    if (event.target.checked) selectedModels.add(model.id);
    else selectedModels.delete(model.id);
  }
  render();
});
$("batch-benchmark")?.addEventListener("click", batchBenchmark);
$("refresh-ranking")?.addEventListener("click", refreshRankings);
$("rows")?.addEventListener("change", event => {
  const checkbox = event.target.closest("input[data-select-model]");
  if (!checkbox) return;
  if (checkbox.checked) selectedModels.add(checkbox.dataset.selectModel);
  else selectedModels.delete(checkbox.dataset.selectModel);
  updateSelectionControls(filteredModels());
});
$("rows")?.addEventListener("click", event => {
  const button = event.target.closest("button[data-model]");
  if (button && !button.disabled) benchmark(button.dataset.model);
});

if ($("ranking-source")) $("ranking-source").value = rankingSource;
load();
setInterval(load, 5000);
