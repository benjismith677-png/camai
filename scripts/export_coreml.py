#!/usr/bin/env python3
"""Export YOLOv8n to CoreML for Apple Neural Engine inference.

Run once: python3 scripts/export_coreml.py
Output: models/yolov8n.mlpackage
"""

import os
import shutil
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR = os.path.join(PROJECT_ROOT, "models")


def main() -> None:
    os.makedirs(MODELS_DIR, exist_ok=True)

    from ultralytics import YOLO

    pt_path = os.path.join(MODELS_DIR, "yolov8n.pt")
    coreml_path = os.path.join(MODELS_DIR, "yolov8n.mlpackage")

    if os.path.exists(coreml_path):
        print(f"CoreML model already exists: {coreml_path}")
        print("Delete it to re-export.")
        return

    # Download PT model if needed
    if not os.path.exists(pt_path):
        print("Downloading YOLOv8n...")
        model = YOLO("yolov8n.pt")
        # YOLO downloads to cwd, move to models/
        if os.path.exists("yolov8n.pt"):
            shutil.move("yolov8n.pt", pt_path)
    else:
        model = YOLO(pt_path)

    print("Exporting to CoreML (imgsz=576x704)...")
    export_path = model.export(format="coreml", nms=True, imgsz=[576, 704])

    # Move exported model to models/ if needed
    if export_path and os.path.exists(export_path):
        if os.path.abspath(export_path) != os.path.abspath(coreml_path):
            if os.path.exists(coreml_path):
                shutil.rmtree(coreml_path)
            shutil.move(export_path, coreml_path)
        print(f"CoreML model saved: {coreml_path}")
    else:
        print("Export completed. Check models/ directory.")

    # Verify
    if os.path.exists(coreml_path):
        size_mb = sum(
            os.path.getsize(os.path.join(dp, f))
            for dp, _, fns in os.walk(coreml_path)
            for f in fns
        ) / (1024 * 1024)
        print(f"Model size: {size_mb:.1f} MB")


if __name__ == "__main__":
    main()
