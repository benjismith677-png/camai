"""YOLOv8 object detector with CoreML / MPS acceleration."""

from __future__ import annotations

import os
import time

from sentinel.models import BBox, Detection


class ObjectDetector:
    """YOLOv8n inference — prefers CoreML on Apple Silicon, falls back to MPS/CPU."""

    CLASSES_OF_INTEREST: dict[int, str] = {
        0: "person",
        1: "bicycle",
        2: "car",
        3: "motorcycle",
        5: "bus",
        7: "truck",
        14: "bird",
        15: "cat",
        16: "dog",
    }

    def __init__(self, model_path: str = "models/yolov8n.pt"):
        from ultralytics import YOLO

        # Try CoreML first (Apple Neural Engine), then fall back to PT
        coreml_path = model_path.replace(".pt", ".mlpackage")
        if os.path.exists(coreml_path):
            print(f"[DETECT] Loading CoreML model: {coreml_path}")
            self.model = YOLO(coreml_path)
            self._backend = "coreml"
        elif os.path.exists(model_path):
            print(f"[DETECT] Loading PyTorch model: {model_path}")
            self.model = YOLO(model_path)
            self._backend = "pytorch"
        else:
            print(f"[DETECT] Model not found, downloading yolov8n.pt")
            self.model = YOLO("yolov8n.pt")
            self._backend = "pytorch"

        self._total_infer_ms: float = 0.0
        self._total_calls: int = 0

    @property
    def avg_infer_ms(self) -> float:
        if self._total_calls == 0:
            return 0.0
        return self._total_infer_ms / self._total_calls

    def detect(self, frame, confidence: float = 0.35) -> list[Detection]:
        """Run detection on a BGR numpy frame.

        Args:
            frame: BGR numpy array (H, W, 3).
            confidence: Minimum confidence threshold.

        Returns:
            List of Detection objects for classes of interest.
        """
        t0 = time.perf_counter()
        device = "mps" if self._backend == "pytorch" else None
        results = self.model(frame, verbose=False, conf=confidence, device=device)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        self._total_infer_ms += elapsed_ms
        self._total_calls += 1

        detections: list[Detection] = []
        if not results or len(results) == 0:
            return detections

        boxes = results[0].boxes
        if boxes is None:
            return detections

        for box in boxes:
            cls_id = int(box.cls)
            if cls_id not in self.CLASSES_OF_INTEREST:
                continue

            coords = box.xyxy[0].tolist()
            detections.append(Detection(
                class_name=self.CLASSES_OF_INTEREST[cls_id],
                confidence=float(box.conf),
                bbox=BBox(
                    x1=coords[0],
                    y1=coords[1],
                    x2=coords[2],
                    y2=coords[3],
                ),
            ))

        return detections
