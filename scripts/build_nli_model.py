"""Build the in-app Local AI model from the public fp32 ONNX export.

  python scripts/build_nli_model.py <fp32.onnx> <out.onnx>   (needs onnx + onnxruntime)

Input: Xenova/DeBERTa-v3-large-mnli-fever-anli-ling-wanli @ a70e12f8244efae07cf6fdfa935df30e3ac4a060,
onnx/model.onnx (sha256 9fbf57959bd9143720289cd1eab1aaaa00fc99459fd25ffeff8eba1ccf955d2b, 1,741,989,392 bytes).

Recipe (chosen by a parity check against the PyTorch model on 631 NLI pairs from the Apply Assist gold set):
  1. MatMul weights -> 8-bit symmetric blocks of 32 (MatMulNBits), accuracy_level 4 (int8 compute: 2.4x
     faster on CPU than fp32-compute blocks, parity unchanged);
  2. the 128k-token embedding (Gather) -> int8 (dynamic quantization, weight only).
The public dynamic-int8 exports (Xenova model_int8, MoritzLaurer model_quantized) quantize activations
of every op and lose ~20 correct fills; 4-bit MatMuls or a 4-bit embedding miss the 99% identical-decision bar.
"""
import os
import sys

import onnx
from onnxruntime.quantization import QuantType, quantize_dynamic
from onnxruntime.quantization.matmul_nbits_quantizer import DefaultWeightOnlyQuantConfig, MatMulNBitsQuantizer

src, dst = sys.argv[1], sys.argv[2]
tmp = dst + ".matmul.onnx"
kw = dict(block_size=32, is_symmetric=True, accuracy_level=4, op_types_to_quantize=("MatMul",),
          quant_axes=(("MatMul", 0),))
q = MatMulNBitsQuantizer(onnx.load(src), bits=8, algo_config=DefaultWeightOnlyQuantConfig(bits=8, **kw), **kw)
q.process()
onnx.save(q.model.model, tmp)
quantize_dynamic(tmp, dst, op_types_to_quantize=["Gather"], weight_type=QuantType.QInt8)
os.remove(tmp)
print("wrote", dst)
