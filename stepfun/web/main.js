// PitWall KB — 端侧 embedding + seekdb 混合检索
// 推理全部发生在浏览器：文档与查询向量都由本文件在本地计算，页面不调用任何云端推理 API。
import { pipeline } from "https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.7.5";

const MODEL_ID = "Xenova/all-MiniLM-L6-v2";
const params = new URLSearchParams(location.search);
const DTYPE = params.get("dtype") || "q8";      // q8 = INT8, fp32 = 基线
const FORCE_DEVICE = params.get("device");       // webgpu | wasm | 空 = 自动

const el = (id) => document.getElementById(id);
const state = {
  extractor: null,
  device: null,
  dim: 384,
  kb: "",
  docs: new Map(),
  bench: null,
};

function setBadge(node, text, cls) {
  node.textContent = text;
  node.className = "badge" + (cls ? " " + cls : "");
}

// ---- 端侧推理：优先 WebGPU，失败自动降级 WASM ----
async function loadModel() {
  const order = FORCE_DEVICE
    ? [FORCE_DEVICE]
    : navigator.gpu
      ? ["webgpu", "wasm"]
      : ["wasm"];
  const errs = [];
  for (const device of order) {
    const t0 = performance.now();
    try {
      const extractor = await pipeline("feature-extraction", MODEL_ID, {
        device,
        dtype: DTYPE,
      });
      state.extractor = extractor;
      state.device = device;
      state.loadMs = Math.round(performance.now() - t0);
      const cls = device === "webgpu" ? "ok" : "warn";
      setBadge(el("backend"), `推理后端：${device.toUpperCase()}`, cls);
      return;
    } catch (e) {
      errs.push(`${device}: ${e.message}`);
    }
  }
  throw new Error("所有推理后端均不可用 —— " + errs.join(" | "));
}

async function embed(texts) {
  const out = await state.extractor(texts, { pooling: "mean", normalize: true });
  return out.tolist();
}

function chunk(text, size = 400, overlap = 40) {
  const clean = text.replace(/\r\n/g, "\n").trim();
  if (!clean) return [];
  const paras = clean.split(/\n{2,}/);
  const chunks = [];
  let buf = "";
  for (const p of paras) {
    if ((buf + "\n\n" + p).length <= size) {
      buf = buf ? buf + "\n\n" + p : p;
    } else {
      if (buf) chunks.push(buf);
      if (p.length <= size) {
        buf = p;
      } else {
        for (let i = 0; i < p.length; i += size - overlap) chunks.push(p.slice(i, i + size));
        buf = "";
      }
    }
  }
  if (buf) chunks.push(buf);
  return chunks;
}

async function api(path, body) {
  const res = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: body ? { "content-type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const json = await res.json();
  if (!res.ok) throw new Error(json.error || res.statusText);
  return json;
}

// ---- 导入 ----
async function readFiles(fileList) {
  const out = [];
  for (const f of fileList) {
    out.push({ name: f.name, text: await f.text() });
  }
  const paste = el("paste").value.trim();
  if (paste) out.push({ name: `paste-${Date.now()}`, text: paste });
  return out;
}

async function ingest(files) {
  const kb = el("kbname").value.trim() || "default";
  state.kb = kb;
  await api("/api/collections", { name: kb });
  let done = 0;
  const total = files.length;
  for (const f of files) {
    const chunks = chunk(f.text);
    if (chunks.length === 0) continue;
    const t0 = performance.now();
    const vectors = [];
    for (let i = 0; i < chunks.length; i += 8) {
      vectors.push(...(await embed(chunks.slice(i, i + 8))));
    }
    const embedMs = performance.now() - t0;
    const rows = chunks.map((doc, i) => ({
      id: `${f.name}#${i}`,
      document: doc,
      embedding: vectors[i],
      metadata: { source: f.name, chunk: i },
    }));
    const t1 = performance.now();
    const r = await api("/api/ingest", { collection: kb, rows });
    const ingestMs = performance.now() - t1;
    state.docs.set(f.name, { chunks: chunks.length, embedMs: Math.round(embedMs), ingestMs: Math.round(ingestMs), count: r.count });
    const li = document.createElement("li");
    li.textContent = `${f.name} → ${chunks.length} 个片段 · 向量化 ${Math.round(embedMs)} ms · 写入+刷新 ${Math.round(ingestMs)} ms`;
    el("doclist").appendChild(li);
    done += 1;
    el("prog").value = Math.round((done / total) * 100);
    el("ingestInfo").textContent = `已写入 ${r.count} 个片段到 seekdb`;
  }
  setBadge(el("dbstate"), `seekdb：${kb} · ${await count(kb)} 片段`, "ok");
}

async function count(kb) {
  const r = await api(`/api/stats?collection=${encodeURIComponent(kb)}`);
  return r.count;
}

// ---- 查询 ----
async function search() {
  const q = el("q").value.trim();
  if (!q) return;
  const kb = el("kbname").value.trim() || state.kb || "default";
  const t0 = performance.now();
  const [vec] = await embed([q]);
  const tEmb = performance.now() - t0;
  const t1 = performance.now();
  const r = await api("/api/query", { collection: kb, text: q, embedding: vec, k: 8 });
  const tDb = performance.now() - t1;
  el("qinfo").textContent = `查询向量化 ${tEmb.toFixed(1)} ms · seekdb 检索 ${tDb.toFixed(1)} ms · 端到端 ${(performance.now() - t0).toFixed(1)} ms`;
  const ol = el("hits");
  ol.innerHTML = "";
  const ids = r.ids[0] || [];
  const docs = (r.documents && r.documents[0]) || [];
  const metas = (r.metadatas && r.metadatas[0]) || [];
  const dists = (r.distances && r.distances[0]) || [];
  ids.forEach((id, i) => {
    const li = document.createElement("li");
    const meta = document.createElement("div");
    meta.className = "meta";
    const d = dists[i];
    meta.textContent = `${metas[i]?.source || id} · ${d == null ? "" : "distance " + Number(d).toFixed(4)}`;
    const body = document.createElement("div");
    body.className = "body";
    body.textContent = docs[i] || "";
    li.append(meta, body);
    ol.appendChild(li);
  });
  state.lastQuery = { q, tEmb, tDb, e2e: performance.now() - t0, ids };
}

// ---- 基准 ----
const BENCH_QUERIES = [
  "高速弯转向不足怎么调",
  "刹车点推迟的代价",
  "胎压对圈速的影响",
  "如何读取 telemetry 的 throttle trace",
  "雨战刹车平衡",
  "减震器高速阻尼调整",
  "下压力与直道速度的取舍",
  "暖胎策略",
  "差速器锁止率",
  "如何找参考圈",
  "understeer on corner entry",
  "brake bias migration",
];

async function bench() {
  if (state.docs.size === 0) {
    el("benchOut").textContent = "请先导入语料再跑基准。";
    return;
  }
  const kb = el("kbname").value.trim() || state.kb;
  const lat = [];
  const rows = [];
  const tAll = performance.now();
  for (const q of BENCH_QUERIES) {
    const t0 = performance.now();
    const [vec] = await embed([q]);
    const tEmb = performance.now() - t0;
    const t1 = performance.now();
    const r = await api("/api/query", { collection: kb, text: q, embedding: vec, k: 5 });
    const tDb = performance.now() - t1;
    const e2e = performance.now() - t0;
    lat.push(e2e);
    rows.push({ q, tEmb: +tEmb.toFixed(1), tDb: +tDb.toFixed(1), e2e: +e2e.toFixed(1), top: (r.ids[0] || [])[0] || null });
  }
  const med = (xs) => {
    const s = [...xs].sort((a, b) => a - b);
    return s[Math.floor(s.length / 2)];
  };
  const docs = [...state.docs.entries()].map(([name, d]) => ({ name, ...d }));
  const totalChunks = docs.reduce((a, d) => a + d.chunks, 0);
  const embedMs = docs.reduce((a, d) => a + d.embedMs, 0);
  const ingestMs = docs.reduce((a, d) => a + d.ingestMs, 0);
  state.bench = {
    at: new Date().toISOString(),
    ua: navigator.userAgent,
    model: MODEL_ID,
    dtype: DTYPE,
    device: state.device,
    modelLoadMs: state.loadMs,
    chunks: totalChunks,
    embedTotalMs: Math.round(embedMs),
    chunksPerSec: +(totalChunks / (embedMs / 1000)).toFixed(2),
    ingestTotalMs: Math.round(ingestMs),
    queryMedianMs: +med(lat).toFixed(1),
    queryP90Ms: +([...lat].sort((a, b) => a - b)[Math.floor(lat.length * 0.9)]).toFixed(1),
    benchWallMs: Math.round(performance.now() - tAll),
    queries: rows,
  };
  el("benchOut").textContent = JSON.stringify(state.bench, null, 2);
  el("dlBtn").disabled = false;
}

// ---- 启动 ----
(async () => {
  try {
    await loadModel();
    el("ingestBtn").disabled = false;
    el("qBtn").disabled = false;
    el("benchBtn").disabled = false;
    el("ingestInfo").textContent = `模型就绪：${DTYPE.toUpperCase()} · 加载 ${state.loadMs} ms`;
    setBadge(el("model"), `模型：${MODEL_ID} · ${DTYPE.toUpperCase()}`, "ok");
    const h = await api("/api/health").catch(() => null);
    if (h) setBadge(el("dbstate"), `seekdb：${h.mode} 模式已连接`, "ok");
  } catch (e) {
    el("ingestInfo").textContent = "初始化失败：" + e.message;
    setBadge(el("dbstate"), "seekdb：连接失败", "warn");
  }
})();

el("ingestBtn").addEventListener("click", async () => {
  const btn = el("ingestBtn");
  btn.disabled = true;
  try {
    const files = await readFiles(el("files").files);
    if (files.length === 0) {
      el("ingestInfo").textContent = "没有可导入的文本";
      return;
    }
    await ingest(files);
  } catch (e) {
    el("ingestInfo").textContent = "导入失败：" + e.message;
  } finally {
    btn.disabled = false;
  }
});

el("qBtn").addEventListener("click", () => search().catch((e) => (el("qinfo").textContent = "检索失败：" + e.message)));
el("q").addEventListener("keydown", (e) => { if (e.key === "Enter") el("qBtn").click(); });
el("benchBtn").addEventListener("click", () => bench().catch((e) => (el("benchOut").textContent = "基准失败：" + e.message)));
el("dlBtn").addEventListener("click", () => {
  const blob = new Blob([JSON.stringify(state.bench, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `bench-${DTYPE}-${state.device}.json`;
  a.click();
});
