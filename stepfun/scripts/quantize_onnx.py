#!/usr/bin/env python3
"""把 Xenova/all-MiniLM-L6-v2 的 ONNX 权重做 INT8 动态量化，并输出体积对比。

用法：
    pip install "huggingface_hub>=0.23" "onnxruntime>=1.18"
    python scripts/quantize_onnx.py --out results

产物：
    models/all-MiniLM-L6-v2/onnx/model.onnx              FP32 原始权重
    models/all-MiniLM-L6-v2/onnx/model_quantized.onnx    INT8 权重
    results/quantization.json                            体积、压缩比、耗时

量化参数与 transformers.js 的 dtype="q8" 对齐：权重按 INT8 存储（QuantType.QInt8），
激活值运行时动态量化；MatMul/Gemm 走动态量化算子，因此不需要额外校准集。
"""
import argparse
import json
import shutil
import time
from pathlib import Path

from huggingface_hub import snapshot_download
from onnxruntime.quantization import QuantType, quantize_dynamic

REPO = "Xenova/all-MiniLM-L6-v2"
ALLOW = ["onnx/model.onnx", "tokenizer.json", "tokenizer_config.json", "config.json", "vocab.txt", "special_tokens_map.json"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--models", default="models")
    ap.add_argument("--out", default="results")
    args = ap.parse_args()

    models = Path(args.models)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    target = models / args.repo.split("/")[-1]

    if not (target / "onnx" / "model.onnx").exists():
        print(f"[1/3] 下载 {args.repo} …")
        snapshot_download(
            repo_id=args.repo,
            allow_patterns=ALLOW,
            local_dir=str(target),
            local_dir_use_symlinks=False,
        )
    src = target / "onnx" / "model.onnx"
    dst = target / "onnx" / "model_quantized.onnx"

    print(f"[2/3] INT8 动态量化 {src.name} …")
    t0 = time.perf_counter()
    quantize_dynamic(str(src), str(dst), weight_type=QuantType.QInt8)
    quant_ms = (time.perf_counter() - t0) * 1000

    fp32 = src.stat().st_size
    int8 = dst.stat().st_size
    report = {
        "repo": args.repo,
        "method": "onnxruntime.quantization.quantize_dynamic(weight_type=QInt8)",
        "fp32_bytes": fp32,
        "int8_bytes": int8,
        "fp32_mb": round(fp32 / 1e6, 2),
        "int8_mb": round(int8 / 1e6, 2),
        "compression_ratio": round(fp32 / int8, 2),
        "quantize_ms": round(quant_ms, 1),
        "files": [str(src), str(dst)],
    }
    (out / "quantization.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[3/3] FP32 {report['fp32_mb']} MB → INT8 {report['int8_mb']} MB，压缩比 {report['compression_ratio']}×")
    print(f"      写入 {out / 'quantization.json'}")

    # 可选：把量化后的模型放进 web/models，让页面离线加载本地权重
    local = Path("web/models") / args.repo.split("/")[-1] / "onnx"
    if local.parent.parent.parent.exists():
        local.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dst, local / "model_quantized.onnx")
        print(f"      已复制到 {local / 'model_quantized.onnx'}")


if __name__ == "__main__":
    main()
