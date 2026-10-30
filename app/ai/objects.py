"""YOLOv8 prohibited-object detection (optional, loaded lazily).

Install ``ultralytics`` (see requirements-ai.txt) to enable server-side
detection. The browser also runs a lightweight detector, so monitoring keeps
working when the server has no GPU / no ultralytics.
"""

import logging
import threading

log = logging.getLogger(__name__)

# COCO class name -> event type
PROHIBITED = {
    "cell phone": "phone_detected",
    "book": "book_detected",
    "laptop": "extra_device",
    "tv": "extra_device",
    "remote": "phone_detected",
}


class ObjectDetector:
    def __init__(self, model_name="yolov8n.pt", enabled=True):
        self.model_name = model_name
        self.enabled = enabled
        self._model = None
        self._failed = False
        self._lock = threading.Lock()

    @property
    def available(self):
        return self.enabled and not self._failed and self._load() is not None

    def _load(self):
        if self._model is not None or self._failed or not self.enabled:
            return self._model
        with self._lock:
            if self._model is None and not self._failed:
                try:
                    from ultralytics import YOLO  # type: ignore

                    self._model = YOLO(self.model_name)
                    log.info("YOLOv8 model loaded: %s", self.model_name)
                except Exception as exc:  # ImportError or download failure
                    log.info("YOLOv8 disabled (%s)", exc)
                    self._failed = True
        return self._model

    def detect(self, img, conf=0.45):
        """Returns {'persons': int, 'items': [{'label','event','confidence','box'}]} or None."""
        model = self._load()
        if model is None:
            return None
        result = model.predict(img, conf=conf, verbose=False, imgsz=640)[0]
        names = result.names
        persons = 0
        items = []
        for box in result.boxes:
            label = names[int(box.cls[0])]
            score = float(box.conf[0])
            if label == "person":
                persons += 1
            elif label in PROHIBITED:
                x1, y1, x2, y2 = [int(v) for v in box.xyxy[0].tolist()]
                items.append(
                    {"label": label, "event": PROHIBITED[label], "confidence": round(score, 2), "box": [x1, y1, x2 - x1, y2 - y1]}
                )
        return {"persons": persons, "items": items}
