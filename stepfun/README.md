# PitWall KB — 端侧向量化 + seekdb 混合检索（阶跃星辰赛题）

一个在浏览器里完成 embedding、把向量与原文写入 seekdb、由 seekdb 执行向量+全文混合检索的迷你知识库。示例语料是赛车工程笔记（转向特性、刹车与轮胎、telemetry 读数、空气动力学设定）。

## 1. 运行

需要 Node.js 20+、Chrome/Edge 最新版；seekdb 以 **嵌入式** 方式随服务端启动（原生绑定支持 Linux / macOS，Windows 请在 WSL 中运行）。

```bash
cd stepfun/server
npm install          # 安装 seekdb（含嵌入式原生绑定）
npm start            # http://127.0.0.1:8787
```

打开页面后：

1. 在「知识库名称」填一个名字（相同名字会复用已有集合）；
2. 选择 `data/corpus/*.md` 导入，或直接粘贴文本；
3. 输入查询，例如「高速弯转向不足怎么调」；
4. 「运行基准测试」会跑 12 条固定查询，输出模型加载、向量化吞吐、写入+刷新、检索延迟，并可下载 JSON。

可选配置：`PORT`、`SEEKDB_PATH`（默认 `stepfun/.seekdb`）、`SEEKDB_DB`。

## 2. 目录

```
stepfun/
├── web/                 # 前端（纯静态）：index.html / main.js / style.css
├── server/              # Node 服务：静态托管 + seekdb-js 数据层
├── scripts/quantize_onnx.py   # INT8 量化与体积对比
├── data/corpus/         # 示例语料
├── data/queries.jsonl   # 12 条查询与相关文档（检索质量评测）
└── results/             # 量化与基准输出
```

## 3. 一页技术说明

### 3.1 模型与量化

- 模型：`Xenova/all-MiniLM-L6-v2`（Apache-2.0，384 维，ONNX 权重由 Hugging Face 仓库 `Xenova/all-MiniLM-L6-v2` 提供）。
- 量化：`scripts/quantize_onnx.py` 用 `onnxruntime.quantization.quantize_dynamic(weight_type=QInt8)` 对权重做 INT8 动态量化；页面通过 transformers.js 的 `dtype="q8"` 加载同一精度，`?dtype=fp32` 可切回基线做对照。
- 体积对比与压缩比：运行脚本后写入 `results/quantization.json`（字段 `fp32_mb` / `int8_mb` / `compression_ratio`）。压缩比按同一口径 `量化前总体积 ÷ 量化后总体积` 计算。
- 质量对照：用 `data/queries.jsonl` 的 12 条查询，分别以 INT8 与 FP32 建库，计算 Recall@3（命中 `relevant` 中任一篇即算 1）。检索链路除精度外完全相同，便于比较。

### 3.2 端侧运行

- 运行时：transformers.js 3.x（ONNX Runtime Web）。
- 后端选择：检测到 `navigator.gpu` 时显式以 `device:"webgpu"` 创建 pipeline；创建失败则自动以 `device:"wasm"` 重建，页面顶部徽标显示实际后端（WebGPU 绿色 / WASM 橙色）。`?device=wasm` 可强制降级路径用于验收。
- 推理位置：文档与查询向量都在页面内计算；服务端只接收向量与文本，不做任何 embedding，也不调用云端推理 API。

### 3.3 seekdb 使用

- 接入：`seekdb`（seekdb-js 1.4，`SeekdbClient` 嵌入式模式）建集合；schema 同时声明 `FulltextIndexConfig("ik", {ik_mode:"smart"})` 与 `VectorIndexConfig({hnsw:{dimension:384,distance:"cosine"}})`。
- 混合检索：`collection.hybridSearch({ query:{whereDocument:{$contains:q}}, knn:{queryEmbeddings:[vec]}, nResults })`，排序在库内完成（默认 RRF），客户端不分别取两路结果再拼合。
- 写入与刷新：每批 `add()` 之后调用 `refresh_index()`，保证刚导入的片段立刻可检索；`/api/ingest` 返回集合当前条数供页面核对。
- 增量与会话状态：集合按知识库名称隔离，`getOrCreateCollection` 复用已有集合；页面切换知识库时以名称作为会话键，查询始终落在当前集合。

### 3.4 状态与优化

- 批处理：向量化以 8 个片段为一批，减少推理调用次数。
- 分块：按段落合并到约 400 字符，超长段落按 400/40 字符重叠切分，减少语义截断。
- 首次与预热分开记录：`modelLoadMs` 是首次加载耗时，查询延迟取 12 条的中位数与 P90。
- 结果表（`results/bench-*.json`，由页面「下载结果 JSON」产出）：字段含 `device`、`dtype`、`chunksPerSec`、`ingestTotalMs`、`queryMedianMs`、`queryP90Ms`，可直接填下表。

| 配置 | 模型体积 | 向量化吞吐 | seekdb 检索延迟 | 端到端查询延迟 | 检索质量 |
|---|---|---|---|---|---|
| FP32 + WASM（基线） | 见 `results/quantization.json` | 页面输出 | 页面输出 | 页面输出 | Recall@3 |
| INT8 + WebGPU | 同上 | 同上 | 同上 | 同上 | Recall@3 |
| INT8 + WASM（降级） | 同上 | 同上 | 同上 | 同上 | Recall@3 |

> 表中数值取自 `results/` 的实跑 JSON；在本机复现即生成同一格式的文件。

## 4. 复现说明

- 语料与查询集都在 `data/`，无需额外下载。
- 模型权重由 transformers.js 首次运行时从 Hugging Face 拉取并缓存；离线场景可先运行 `scripts/quantize_onnx.py`，把 `web/models/` 指向本地权重（脚本会自动复制量化后的 `model_quantized.onnx`）。
- 检索质量评测需要固定语料，导入后不要改名，`Recall@3` 按文件名匹配 `relevant`。
