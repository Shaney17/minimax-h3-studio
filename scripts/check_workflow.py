"""Check that workflow/cinematic_api.json still matches what the UI edits.

    python3 scripts/check_workflow.py [--worker-dir PATH]

With --worker-dir (or H3_WORKER_DIR) pointing at a checkout of
minimax-h3-cinematic-runpod, it also checks that every model file the workflow
loads is downloaded by that repo's Dockerfile, so the Runpod image can run it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Node id -> (class_type, input the UI reads or writes)
EXPECTED = {
    "28": ("LoadImage", "image"),
    "29": ("LoadImage", "image"),
    "30": ("LoadImage", "image"),
    "190": ("PrimitiveStringMultiline", "value"),
    "111": ("PrimitiveFloat", "value"),
    "154": ("VHS_VideoCombine", "crf"),
    "112": ("ResolutionSelector", "megapixels"),
    "102": ("easy float", "value"),
    "120": ("ModelAttentionBackend", "attention"),
    # Wired by the frame anchor (addFrameAnchor in web/app.js).
    "4": ("VAELoader", "vae_name"),
    "121": ("MiniMaxH3ReferenceToVideo", "prompt"),
    "145": ("BasicGuider", "conditioning"),
    "146": ("BasicGuider", "conditioning"),
    "157": ("LTXVConcatAVLatent", "video_latent"),
}
# Guider -> latent its sampler must use: stage 1 samples the base latent from 121,
# stage 2 the upscaled latent from 157. The anchor guides must match these sizes.
GUIDER_LATENTS = {"145": ["121", 1], "146": ["157", 0]}
# Nodes added by the frame anchor; the bundled workflow must leave these ids free.
ANCHOR_IDS = ("300", "301", "302")
MODEL_FILE = re.compile(r"\.(safetensors|pth|ckpt|gguf)$")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-dir", default=os.environ.get("H3_WORKER_DIR"))
    args = parser.parse_args()
    errors = []

    graph = json.loads((ROOT / "workflow" / "cinematic_api.json").read_text(encoding="utf-8"))
    for node_id, (class_type, field) in EXPECTED.items():
        node = graph.get(node_id)
        if not node or node.get("class_type") != class_type:
            errors.append(f"nút {node_id} phải là {class_type}, đang là {node and node.get('class_type')}")
        elif field not in node.get("inputs", {}):
            errors.append(f"nút {node_id} ({class_type}) thiếu input {field}")
    if not any(node.get("class_type") == "MiniMaxH3ReferenceToVideo" for node in graph.values()):
        errors.append("thiếu nút MiniMaxH3ReferenceToVideo")
    for guider, latent in GUIDER_LATENTS.items():
        samplers = [
            node for node in graph.values()
            if node.get("class_type") == "SamplerCustomAdvanced" and node.get("inputs", {}).get("guider") == [guider, 0]
        ]
        if len(samplers) != 1 or samplers[0]["inputs"].get("latent_image") != latent:
            errors.append(f"sampler dùng guider {guider} phải lấy latent {latent} (khung neo dựa vào điều này)")
    taken = [node_id for node_id in ANCHOR_IDS if node_id in graph]
    if taken:
        errors.append(f"id {', '.join(taken)} đã được dùng; khung neo cần các id này")
    if "ref_images.ref_image_3" in graph.get("121", {}).get("inputs", {}):
        errors.append("nút 121 đã dùng ref_image_3; khung neo cần vị trí <Picture 4>")

    ui = json.loads((ROOT / "workflow" / "cinematic_ui.json").read_text(encoding="utf-8"))
    if not any(node.get("id") == 190 for node in ui.get("nodes", [])):
        errors.append("cinematic_ui.json thiếu nút prompt 190")

    if args.worker_dir:
        dockerfile = (Path(args.worker_dir).expanduser() / "Dockerfile").read_text(encoding="utf-8")
        for node_id, node in graph.items():
            for name, value in node.get("inputs", {}).items():
                if isinstance(value, str) and MODEL_FILE.search(value) and value.rsplit("/", 1)[-1] not in dockerfile:
                    errors.append(f"model {value} (nút {node_id}.{name}) không có trong Dockerfile của worker")

    if errors:
        print("Workflow không hợp lệ:", *errors, sep="\n  - ", file=sys.stderr)
        return 1
    checked = " và model trong Dockerfile worker" if args.worker_dir else ""
    print(f"Workflow hợp lệ: {len(graph)} nút{checked}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
