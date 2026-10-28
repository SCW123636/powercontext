// PitWall KB 本地服务：静态页面 + seekdb（seekdb-js）数据层
// 只做“存向量 / 执行混合检索”，不产生任何 embedding：向量由浏览器端算好后提交。
import http from "node:http";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { SeekdbClient, Schema, VectorIndexConfig, FulltextIndexConfig } from "seekdb";

const here = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.resolve(here, "..", "web");
const PORT = Number(process.env.PORT || 8787);
const DB_PATH = process.env.SEEKDB_PATH || path.resolve(here, "..", ".seekdb");
const DB_NAME = process.env.SEEKDB_DB || "kb";
const DIM = 384;

// 嵌入式模式：无需单独启动 seekdb server，数据落在 DB_PATH 目录。
// 若改用本地 server：new SeekdbClient({ host, port: 2881, user, password, database: DB_NAME })
const client = new SeekdbClient({ path: DB_PATH, database: DB_NAME });
const cache = new Map();

async function getCollection(name) {
  if (cache.has(name)) return cache.get(name);
  const schema = new Schema({
    fulltextIndex: new FulltextIndexConfig("ik", { ik_mode: "smart" }),
    vectorIndex: new VectorIndexConfig({ hnsw: { dimension: DIM, distance: "cosine" } }),
  });
  const col = await client.getOrCreateCollection({ name, schema });
  cache.set(name, col);
  return col;
}

function json(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, { "content-type": "application/json; charset=utf-8", "access-control-allow-origin": "*" });
  res.end(body);
}

async function readBody(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const raw = Buffer.concat(chunks).toString("utf8");
  return raw ? JSON.parse(raw) : {};
}

const MIME = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8" };

async function serveStatic(req, res) {
  const url = new URL(req.url, "http://local");
  let p = url.pathname === "/" ? "/index.html" : url.pathname;
  const file = path.join(webRoot, path.normalize(p).replace(/^(\.\.[/\\])+/, ""));
  if (!file.startsWith(webRoot)) return json(res, 403, { error: "forbidden" });
  try {
    const data = await readFile(file);
    res.writeHead(200, { "content-type": MIME[path.extname(file)] || "application/octet-stream" });
    res.end(data);
  } catch {
    json(res, 404, { error: "not found" });
  }
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://local");
  try {
    if (req.method === "OPTIONS") {
      res.writeHead(204, { "access-control-allow-origin": "*", "access-control-allow-headers": "content-type", "access-control-allow-methods": "GET,POST" });
      return res.end();
    }
    if (url.pathname === "/api/health") {
      return json(res, 200, { ok: true, mode: "embedded", path: DB_PATH, dimension: DIM });
    }
    if (url.pathname === "/api/collections" && req.method === "POST") {
      const { name } = await readBody(req);
      if (!name) return json(res, 400, { error: "name required" });
      await getCollection(name);
      return json(res, 200, { ok: true, name });
    }
    if (url.pathname === "/api/ingest" && req.method === "POST") {
      const { collection, rows } = await readBody(req);
      const col = await getCollection(collection);
      await col.add({
        ids: rows.map((r) => r.id),
        documents: rows.map((r) => r.document),
        embeddings: rows.map((r) => r.embedding),
        metadatas: rows.map((r) => r.metadata || {}),
      });
      // 增量写入后显式刷新索引，保证新文档立即可检索
      await col.refresh_index();
      return json(res, 200, { ok: true, count: await col.count() });
    }
    if (url.pathname === "/api/query" && req.method === "POST") {
      const { collection, text, embedding, k = 8 } = await readBody(req);
      const col = await getCollection(collection);
      const base = { nResults: k, include: ["documents", "metadatas", "distances"] };
      // 混合检索：向量 + 全文，排序在 seekdb 内完成（rank 默认 RRF），不在客户端拼合
      const r = text && text.trim()
        ? await col.hybridSearch({ ...base, query: { whereDocument: { $contains: text.trim() } }, knn: { queryEmbeddings: [embedding] } })
        : await col.query({ ...base, queryEmbeddings: [embedding] });
      return json(res, 200, r);
    }
    if (url.pathname === "/api/stats") {
      const col = await getCollection(url.searchParams.get("collection") || "default");
      return json(res, 200, { ok: true, count: await col.count() });
    }
    if (url.pathname.startsWith("/api/")) return json(res, 404, { error: "unknown api" });
    return serveStatic(req, res);
  } catch (e) {
    console.error(e);
    return json(res, 500, { error: String(e && e.message ? e.message : e) });
  }
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`PitWall KB: http://127.0.0.1:${PORT}  (seekdb embedded at ${DB_PATH})`);
});
