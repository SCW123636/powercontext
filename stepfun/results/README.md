# results/

运行后才出现的实测产物，仓库默认不提交数据文件，避免把一次机器的数字当成通用结论。

- `quantization.json`：`python scripts/quantize_onnx.py` 生成，含 FP32/INT8 体积、压缩比、量化耗时。
- `bench-<dtype>-<device>.json`：页面「运行基准测试 → 下载结果 JSON」生成，含模型加载、向量化吞吐、写入+刷新、查询中位数与 P90、12 条查询逐条延迟。

复现口径：同一台设备、同一浏览器版本、同一批语料与查询，分别跑 `?dtype=fp32&device=wasm`（基线）与 `?dtype=q8`（自动优先 WebGPU，失败降级 WASM）。
