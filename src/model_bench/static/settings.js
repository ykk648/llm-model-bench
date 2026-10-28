const $ = (id) => document.getElementById(id);
let configData = null;
let discovered = [];
let configuredRequests = new Set();
let hasRefreshed = false;

function notice(text, type) {
  const node = $("notice");
  if (!node) return;
  node.hidden = false;
  node.className = `notice ${type}`;
  node.textContent = text;
}

async function request(path, options = {}) {
  const token = sessionStorage.getItem("model-bench-admin-token");
  const headers = new Headers(options.headers || {});
  if (token) headers.set("X-Model-Bench-Token", token);
  const response = await fetch(path, {...options, headers});
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
  return body;
}

function renderEndpoints() {
  const select = $("discover-endpoint");
  const list = $("endpoint-list");
  if (!select || !list || !configData) return;
  const previous = select.value;
  select.replaceChildren();
  for (const endpoint of configData.endpoints) {
    const option = document.createElement("option");
    option.value = endpoint.id;
    option.textContent = `${endpoint.id} · ${endpoint.protocol}`;
    select.append(option);
  }
  if (configData.endpoints.some(endpoint => endpoint.id === previous)) select.value = previous;
  list.replaceChildren();
  for (const endpoint of configData.endpoints) {
    const item = document.createElement("div");
    item.className = "endpoint-item";
    const id = document.createElement("strong");
    id.textContent = endpoint.id;
    const meta = document.createElement("span");
    const keyState = endpoint.api_key_env
      ? (endpoint.api_key_configured ? "密钥已配置" : "密钥未配置")
      : "无需密钥";
    meta.textContent = `${endpoint.base_url} · ${endpoint.protocol} · ${endpoint.kind} · ${keyState}`;
    item.append(id, meta);
    list.append(item);
  }
}

function renderDiscovered() {
  const list = $("discover-list");
  const importButton = $("import-models");
  if (!list) return;
  list.replaceChildren();
  if (!discovered.length) {
    const empty = document.createElement("span");
    empty.className = "muted";
    empty.textContent = hasRefreshed ? "这个端点没有返回可导入模型。" : "选择端点后刷新模型列表。";
    list.append(empty);
    if (importButton) importButton.disabled = true;
    if ($("select-all-models")) $("select-all-models").disabled = true;
    return;
  }
  for (const model of discovered) {
    const label = document.createElement("label");
    label.className = "discover-item";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.dataset.discoveredModel = model;
    checkbox.checked = false;
    checkbox.disabled = configuredRequests.has(model);
    const text = document.createElement("span");
    text.textContent = configuredRequests.has(model) ? `${model}（已配置）` : model;
    label.append(checkbox, text);
    list.append(label);
  }
  updateImportButton();
}

function updateImportButton() {
  const button = $("import-models");
  const selectAll = $("select-all-models");
  if (!button) return;
  button.disabled = !document.querySelector("input[data-discovered-model]:checked");
  if (selectAll) selectAll.disabled = !document.querySelector("input[data-discovered-model]:not(:disabled)");
}

async function load(preferredEndpoint = "") {
  try {
    configData = await request("/api/config");
    configuredRequests = new Set(configData.configured_models.map(model => model.request_model));
    $("token-field").hidden = !configData.admin_token_required;
    $("toml").value = configData.toml;
    $("benchmark-use-proxy").checked = configData.network.benchmark_use_proxy;
    $("fallback-proxy").value = configData.network.fallback_proxy || "";
    const artificialAnalysis = configData.ranking_sources.find(source => source.id === "artificial_analysis");
    const keyConfigured = Boolean(artificialAnalysis?.api_key_configured);
    $("artificial-analysis-key-status").textContent = keyConfigured ? "已配置" : "未配置";
    $("artificial-analysis-key-status").classList.toggle("ready", keyConfigured);
    $("artificial-analysis-key").placeholder = keyConfigured ? "已配置，输入新 Key 可替换" : "粘贴 API Key";
    $("state").textContent = `${configData.endpoints.length} 个端点 · ${configData.configured_models.length} 个模型`;
    renderEndpoints();
    if (preferredEndpoint && configData.endpoints.some(endpoint => endpoint.id === preferredEndpoint)) {
      $("discover-endpoint").value = preferredEndpoint;
    }
    discovered = [];
    hasRefreshed = false;
    renderDiscovered();
  } catch (error) {
    notice(error.message, "error");
  }
}

async function discoverEndpoint(endpoint) {
  const data = await request(`/api/discover/${encodeURIComponent(endpoint)}`, { method: "POST" });
  discovered = data.models;
  configuredRequests = new Set(data.configured_models);
  hasRefreshed = true;
  renderDiscovered();
  return data;
}

async function importModels(endpoint, models) {
  return request(`/api/discover/${encodeURIComponent(endpoint)}/import`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ models }),
  });
}

$("token").value = sessionStorage.getItem("model-bench-admin-token") || "";
$("token").addEventListener("input", () => {
  sessionStorage.setItem("model-bench-admin-token", $("token").value);
});

$("endpoint-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = $("add-endpoint");
  let endpointAdded = false;
  button.disabled = true;
  button.textContent = "正在添加并读取模型...";
  try {
    const data = await request("/api/endpoints", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: $("endpoint-id").value,
        base_url: $("endpoint-url").value,
        protocol: $("endpoint-protocol").value,
        api_key: $("endpoint-key").value,
        kind: $("endpoint-kind").value,
      }),
    });
    endpointAdded = true;
    $("endpoint-form").reset();
    await load(data.endpoint);
    const discovery = await discoverEndpoint(data.endpoint);
    const pending = discovery.models.filter(model => !configuredRequests.has(model));
    if (!pending.length) {
      notice(`端点 ${data.endpoint} 已添加，但没有新的模型可导入。`, "success");
      return;
    }
    const imported = await importModels(data.endpoint, pending);
    await load(data.endpoint);
    notice(`端点 ${data.endpoint} 已添加，密钥已保存在本机；已导入 ${imported.imported.length} 个模型。`, "success");
  } catch (error) {
    notice(endpointAdded ? `端点已保存，但读取模型失败：${error.message}` : error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "添加服务并导入模型";
  }
});

$("discover").addEventListener("click", async () => {
  const endpoint = $("discover-endpoint").value;
  const button = $("discover");
  if (!endpoint) return;
  button.disabled = true;
  try {
    await discoverEndpoint(endpoint);
    notice(`已从 ${endpoint} 刷新 ${discovered.length} 个模型；请选择需要加入测速列表的文本模型。`, "success");
  } catch (error) {
    notice(error.message, "error");
  } finally {
    button.disabled = false;
  }
});

$("discover-endpoint").addEventListener("change", () => {
  discovered = [];
  hasRefreshed = false;
  renderDiscovered();
});

$("discover-list").addEventListener("change", updateImportButton);

$("select-all-models").addEventListener("click", () => {
  for (const checkbox of document.querySelectorAll("input[data-discovered-model]:not(:disabled)")) {
    checkbox.checked = true;
  }
  updateImportButton();
});

$("import-models").addEventListener("click", async () => {
  const endpoint = $("discover-endpoint").value;
  const models = [...document.querySelectorAll("input[data-discovered-model]:checked")].map(node => node.dataset.discoveredModel);
  const button = $("import-models");
  if (!endpoint || !models.length) return;
  button.disabled = true;
  try {
    const data = await importModels(endpoint, models);
    notice(`已添加 ${data.imported.length} 个模型${data.skipped.length ? `；跳过 ${data.skipped.length} 个已配置模型` : ""}。`, "success");
    await load();
  } catch (error) {
    notice(error.message, "error");
  } finally {
    button.disabled = false;
  }
});

$("save").addEventListener("click", async () => {
  const button = $("save");
  button.disabled = true;
  try {
    const data = await request("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ toml: $("toml").value }),
    });
    notice(`配置已保存并重新加载：${data.endpoint_count} 个端点，${data.model_count} 个模型。`, "success");
    await load();
  } catch (error) {
    notice(error.message, "error");
  } finally {
    button.disabled = false;
  }
});

$("save-network").addEventListener("click", async () => {
  const button = $("save-network");
  button.disabled = true;
  try {
    const data = await request("/api/network", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        benchmark_use_proxy: $("benchmark-use-proxy").checked,
        fallback_proxy: $("fallback-proxy").value.trim(),
      }),
    });
    await load();
    const benchmarkMode = data.benchmark_use_proxy ? "测速将使用代理" : "测速将直接连接";
    const rankingMode = data.fallback_proxy_configured ? "榜单已配置直连失败后的备用代理" : "榜单仅使用直连";
    notice(`网络设置已保存：${benchmarkMode}；${rankingMode}。`, "success");
  } catch (error) {
    notice(error.message, "error");
  } finally {
    button.disabled = false;
  }
});

$("save-artificial-analysis-key").addEventListener("click", async () => {
  const button = $("save-artificial-analysis-key");
  const input = $("artificial-analysis-key");
  const apiKey = input.value.trim();
  if (!apiKey) {
    notice("请先填写 Artificial Analysis API Key。", "error");
    input.focus();
    return;
  }
  button.disabled = true;
  try {
    await request("/api/ranking-sources/artificial-analysis", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: apiKey }),
    });
    input.value = "";
    await load();
    notice("Artificial Analysis API Key 已保存到本机，可以刷新排名。", "success");
  } catch (error) {
    notice(error.message, "error");
  } finally {
    button.disabled = false;
  }
});

load();
