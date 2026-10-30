"""Face detection and recognition with graceful fallbacks.

Backends, best first:

1. ``sface``  – OpenCV YuNet detector + SFace recogniser (ONNX, fast, accurate).
               Download with ``python scripts/download_models.py``.
2. ``dlib``   – the ``face_recognition`` package if it is installed.
3. ``basic``  – Haar cascade detector + spatial LBP histogram descriptor.
               Always available, lower accuracy; thresholds are stricter and
               the UI labels the backend so examiners know.
"""

import logging
import os
from dataclasses import dataclass, field

import cv2
import numpy as np

log = logging.getLogger(__name__)

YUNET_FILE = "face_detection_yunet_2023mar.onnx"
SFACE_FILE = "face_recognition_sface_2021dec.onnx"


@dataclass
class Face:
    box: tuple  # x, y, w, h
    score: float = 1.0
    landmarks: list = field(default_factory=list)  # 5 points (eyes, nose, mouth corners) when available
    raw: object = None

    @property
    def area(self):
        return self.box[2] * self.box[3]


class FaceAnalyzer:
    # cosine (sface, basic) or mapped-distance (dlib) similarity needed to count as the same person.
    # "basic" was calibrated on sample portraits: same face under scale/light/rotation changes >= 0.96,
    # different people ~0.91.
    THRESHOLDS = {"sface": 0.363, "dlib": 0.93, "basic": 0.935}
    # how much to trust a mismatch from each backend when scoring
    RELIABILITY = {"sface": 1.0, "dlib": 0.95, "basic": 0.6}

    def __init__(self, models_dir=None):
        self.backend = "basic"
        self._yunet = None
        self._sface = None
        self._fr = None
        self._haar = None
        self._profile = None
        models_dir = models_dir or ""
        yunet_path = os.path.join(models_dir, YUNET_FILE)
        sface_path = os.path.join(models_dir, SFACE_FILE)
        if os.path.exists(yunet_path) and os.path.exists(sface_path) and hasattr(cv2, "FaceDetectorYN"):
            try:
                self._yunet = cv2.FaceDetectorYN.create(yunet_path, "", (320, 320), 0.8, 0.3, 5000)
                self._sface = cv2.FaceRecognizerSF.create(sface_path, "")
                self.backend = "sface"
            except cv2.error as exc:  # pragma: no cover - depends on local models
                log.warning("Could not load YuNet/SFace models: %s", exc)
        if self.backend == "basic":
            try:
                import face_recognition  # type: ignore

                self._fr = face_recognition
                self.backend = "dlib"
            except ImportError:
                pass
        cascades = getattr(getattr(cv2, "data", None), "haarcascades", "")
        frontal = os.path.join(cascades, "haarcascade_frontalface_default.xml")
        if os.path.exists(frontal):
            self._haar = cv2.CascadeClassifier(frontal)
        profile = os.path.join(cascades, "haarcascade_profileface.xml")
        if os.path.exists(profile):
            self._profile = cv2.CascadeClassifier(profile)
        log.info("Face backend: %s", self.backend)

    @property
    def threshold(self):
        return self.THRESHOLDS[self.backend]

    @property
    def reliability(self):
        return self.RELIABILITY[self.backend]

    @property
    def available(self):
        return self._yunet is not None or self._fr is not None or self._haar is not None

    # ------------------------------------------------------------------ detect
    def detect(self, img):
        if self._yunet is not None:
            h, w = img.shape[:2]
            self._yunet.setInputSize((w, h))
            _, faces = self._yunet.detect(img)
            out = []
            if faces is not None:
                for f in faces:
                    x, y, fw, fh = [int(v) for v in f[:4]]
                    lm = [(float(f[4 + 2 * i]), float(f[5 + 2 * i])) for i in range(5)]
                    out.append(Face((x, y, fw, fh), float(f[14]), lm, f))
            return sorted(out, key=lambda f: -f.area)
        if self._fr is not None:
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            locs = self._fr.face_locations(rgb, model="hog")
            out = []
            for top, right, bottom, left in locs:
                out.append(Face((left, top, right - left, bottom - top), 1.0, [], (top, right, bottom, left)))
            return sorted(out, key=lambda f: -f.area)
        if self._haar is not None:
            gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
            min_side = max(40, int(min(gray.shape[:2]) * 0.12))
            rects = self._haar.detectMultiScale(gray, 1.1, 6, minSize=(min_side, min_side))
            out = [Face(tuple(int(v) for v in r)) for r in rects]
            return sorted(out, key=lambda f: -f.area)
        return []

    def detect_profile(self, img):
        """Detect side-facing faces (used to tell 'turned away' from 'absent')."""
        if self._profile is None:
            return 0
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        found = len(self._profile.detectMultiScale(gray, 1.1, 5, minSize=(50, 50)))
        flipped = len(self._profile.detectMultiScale(cv2.flip(gray, 1), 1.1, 5, minSize=(50, 50)))
        return found + flipped

    # ------------------------------------------------------------------- embed
    def embed(self, img, face):
        if self._sface is not None and face.raw is not None:
            aligned = self._sface.alignCrop(img, face.raw)
            vec = self._sface.feature(aligned).flatten()
        elif self._fr is not None:
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            enc = self._fr.face_encodings(rgb, [face.raw])
            if not enc:
                return None
            vec = np.asarray(enc[0])
        else:
            vec = lbp_descriptor(img, face.box)
        vec = np.asarray(vec, dtype=np.float32)
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec

    def similarity(self, a, b):
        if a is None or b is None or len(a) != len(b):
            return None
        a = np.asarray(a, dtype=np.float32)
        b = np.asarray(b, dtype=np.float32)
        if self.backend == "dlib":
            # map euclidean distance (0.6 = match boundary) into a 0..1 similarity
            return float(max(0.0, 1.0 - np.linalg.norm(a - b) / 8.6))
        return float(np.dot(a, b) / ((np.linalg.norm(a) * np.linalg.norm(b)) or 1.0))

    def is_match(self, similarity):
        return similarity is not None and similarity >= self.threshold

    # -------------------------------------------------------------------- pose
    @staticmethod
    def estimate_pose(face):
        """Rough yaw/pitch (degrees) from the 5 YuNet landmarks.

        Uses the nose position relative to the eye line and mouth line; accurate
        enough to tell 'facing the screen' from 'turned away'.
        """
        if len(face.landmarks) < 5:
            return None
        (rex, rey), (lex, ley), (nx, ny), (rmx, rmy), (lmx, lmy) = face.landmarks
        eye_cx, eye_cy = (rex + lex) / 2, (rey + ley) / 2
        mouth_cy = (rmy + lmy) / 2
        eye_dist = max(1.0, abs(lex - rex))
        yaw = float(np.degrees(np.arctan2((nx - eye_cx), eye_dist * 0.9)) * 1.6)
        span = max(1.0, mouth_cy - eye_cy)
        ratio = (ny - eye_cy) / span  # ~0.55 when looking straight
        pitch = float((ratio - 0.55) * 120)
        return {"yaw": round(yaw, 1), "pitch": round(pitch, 1)}


def lbp_descriptor(img, box, grid=4):
    """Spatial uniform-LBP histogram of the face crop (fallback descriptor)."""
    x, y, w, h = box
    pad = int(0.08 * w)
    H, W = img.shape[:2]
    crop = img[max(0, y - pad) : min(H, y + h + pad), max(0, x - pad) : min(W, x + w + pad)]
    if crop.size == 0:
        return np.zeros(grid * grid * 59, dtype=np.float32)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    gray = cv2.resize(gray, (96, 96))
    gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4)).apply(gray)
    g = gray.astype(np.int16)
    c = g[1:-1, 1:-1]
    code = np.zeros_like(c, dtype=np.uint8)
    neighbours = [(-1, -1), (-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1)]
    for bit, (dy, dx) in enumerate(neighbours):
        n = g[1 + dy : g.shape[0] - 1 + dy, 1 + dx : g.shape[1] - 1 + dx]
        code |= ((n >= c).astype(np.uint8) << bit)
    mapped = _UNIFORM_LUT[code]
    feats = []
    step = mapped.shape[0] // grid
    for i in range(grid):
        for j in range(grid):
            cell = mapped[i * step : (i + 1) * step, j * step : (j + 1) * step]
            hist = np.bincount(cell.ravel(), minlength=59).astype(np.float32)
            hist /= hist.sum() or 1.0
            feats.append(np.sqrt(hist))  # Hellinger kernel
    return np.concatenate(feats)


def _build_uniform_lut():
    lut = np.full(256, 58, dtype=np.uint8)
    idx = 0
    for v in range(256):
        bits = [(v >> i) & 1 for i in range(8)]
        transitions = sum(bits[i] != bits[(i + 1) % 8] for i in range(8))
        if transitions <= 2:
            lut[v] = idx
            idx += 1
    return lut


_UNIFORM_LUT = _build_uniform_lut()
