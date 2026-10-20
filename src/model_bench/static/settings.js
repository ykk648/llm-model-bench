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
    meta.textContent = `${endpoint.base_url} · ${endpoint.protocol} · ${endpoint.kind}${endpoint.api_key_env ? ` · ${endpoint.api_key_env}` : ""}`;
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
  if (!button) return;
  button.disabled = !document.querySelector("input[data-discovered-model]:checked");
}

async function load() {
  try {
    configData = await request("/api/config");
    configuredRequests = new Set(configData.configured_models.map(model => model.request_model));
    $("toml").value = configData.toml;
    $("state").textContent = `${configData.endpoints.length} 个端点 · ${configData.configured_models.length} 个模型`;
    renderEndpoints();
    discovered = [];
    hasRefreshed = false;
    renderDiscovered();
  } catch (error) {
    notice(error.message, "error");
  }
}

$("token").value = sessionStorage.getItem("model-bench-admin-token") || "";
$("token").addEventListener("input", () => {
  sessionStorage.setItem("model-bench-admin-token", $("token").value);
});

$("endpoint-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = $("add-endpoint");
  button.disabled = true;
  try {
    const data = await request("/api/endpoints", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: $("endpoint-id").value,
        base_url: $("endpoint-url").value,
        protocol: $("endpoint-protocol").value,
        api_key_env: $("endpoint-key-env").value,
        kind: $("endpoint-kind").value,
      }),
    });
    $("endpoint-form").reset();
    notice(`端点 ${data.endpoint} 已添加。请确认对应密钥环境变量已在服务端设置。`, "success");
    await load();
  } catch (error) {
    notice(error.message, "error");
  } finally {
    button.disabled = false;
  }
});

$("discover").addEventListener("click", async () => {
  const endpoint = $("discover-endpoint").value;
  const button = $("discover");
  if (!endpoint) return;
  button.disabled = true;
  try {
    const data = await request(`/api/discover/${encodeURIComponent(endpoint)}`, { method: "POST" });
    discovered = data.models;
    configuredRequests = new Set(data.configured_models);
    hasRefreshed = true;
    renderDiscovered();
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

$("import-models").addEventListener("click", async () => {
  const endpoint = $("discover-endpoint").value;
  const models = [...document.querySelectorAll("input[data-discovered-model]:checked")].map(node => node.dataset.discoveredModel);
  const button = $("import-models");
  if (!endpoint || !models.length) return;
  button.disabled = true;
  try {
    const data = await request(`/api/discover/${encodeURIComponent(endpoint)}/import`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ models }),
    });
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

load();
