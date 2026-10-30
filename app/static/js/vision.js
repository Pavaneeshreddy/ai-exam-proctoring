/* In-browser computer vision (MediaPipe Tasks): face landmarks -> head pose,
   eye gaze, blinks, smile; EfficientDet object detection (phones, books,
   screens, extra people). Runs at several frames per second on the student's
   device; the server re-checks sampled frames independently. */
(function () {
  // Asset locations come from the server (self-hosted copy if downloaded, CDN otherwise).
  const assets = window.VISION_ASSETS || {};
  const BASE = assets.lib || 'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14';
  const FACE_MODEL = assets.faceModel || 'https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task';
  const OBJECT_MODEL = assets.objectModel || 'https://storage.googleapis.com/mediapipe-models/object_detector/efficientdet_lite0/float16/1/efficientdet_lite0.tflite';

  let fileset = null;
  let lib = null;

  async function ensureLib() {
    if (!lib) {
      lib = await import(`${BASE}/vision_bundle.mjs`);
      fileset = await lib.FilesetResolver.forVisionTasks(`${BASE}/wasm`);
    }
    return lib;
  }

  async function create(Task, options) {
    try {
      return await Task.createFromOptions(fileset, { ...options, baseOptions: { ...options.baseOptions, delegate: 'GPU' } });
    } catch (e) {
      return Task.createFromOptions(fileset, { ...options, baseOptions: { ...options.baseOptions, delegate: 'CPU' } });
    }
  }

  async function loadFace(numFaces = 4) {
    await ensureLib();
    return create(lib.FaceLandmarker, {
      baseOptions: { modelAssetPath: FACE_MODEL },
      runningMode: 'VIDEO',
      numFaces,
      minFaceDetectionConfidence: 0.5,
      minFacePresenceConfidence: 0.5,
      outputFaceBlendshapes: true,
    });
  }

  async function loadObjects() {
    await ensureLib();
    return create(lib.ObjectDetector, {
      baseOptions: { modelAssetPath: OBJECT_MODEL },
      runningMode: 'VIDEO',
      scoreThreshold: 0.35,
      maxResults: 12,
    });
  }

  function blend(categories) {
    const out = {};
    (categories || []).forEach((c) => { out[c.categoryName] = c.score; });
    return out;
  }

  /* Pose from landmarks (normalised image coords, un-mirrored camera frame).
     yaw   > 0 : nose toward image right; ratio of nose position between cheeks
     pitch > 0 : looking down; ratio of nose position between eye line and chin */
  function describeFace(lm, shapes) {
    const L = (i) => lm[i];
    const cheekA = L(234), cheekB = L(454), nose = L(1), chin = L(152), eyeA = L(33), eyeB = L(263);
    const width = Math.max(1e-4, cheekB.x - cheekA.x);
    const yawRatio = (nose.x - cheekA.x) / width;
    const eyeY = (eyeA.y + eyeB.y) / 2;
    const pitchRatio = (nose.y - eyeY) / Math.max(1e-4, chin.y - eyeY);
    const roll = Math.atan2(eyeB.y - eyeA.y, eyeB.x - eyeA.x) * 180 / Math.PI;
    const b = blend(shapes);
    const gazeH = ((b.eyeLookInLeft || 0) - (b.eyeLookOutLeft || 0) + (b.eyeLookOutRight || 0) - (b.eyeLookInRight || 0)) / 2;
    const gazeV = ((b.eyeLookDownLeft || 0) + (b.eyeLookDownRight || 0) - (b.eyeLookUpLeft || 0) - (b.eyeLookUpRight || 0)) / 2;
    let minX = 1, minY = 1, maxX = 0, maxY = 0;
    for (const p of lm) { if (p.x < minX) minX = p.x; if (p.y < minY) minY = p.y; if (p.x > maxX) maxX = p.x; if (p.y > maxY) maxY = p.y; }
    return {
      yaw: (yawRatio - 0.5) * 180,
      pitch: (pitchRatio - 0.42) * 160,
      roll,
      gazeH,
      gazeV,
      blink: ((b.eyeBlinkLeft || 0) + (b.eyeBlinkRight || 0)) / 2,
      smile: ((b.mouthSmileLeft || 0) + (b.mouthSmileRight || 0)) / 2,
      jawOpen: b.jawOpen || 0,
      box: { x: minX, y: minY, w: maxX - minX, h: maxY - minY },
    };
  }

  function analyzeFaces(landmarker, video, ts) {
    const res = landmarker.detectForVideo(video, ts);
    const faces = (res.faceLandmarks || []).map((lm, i) => describeFace(lm, res.faceBlendshapes?.[i]?.categories));
    faces.sort((a, b) => (b.box.w * b.box.h) - (a.box.w * a.box.h));
    // ignore tiny faces (posters, background screens) relative to the main face
    const main = faces[0];
    const significant = main ? faces.filter((f) => f.box.w * f.box.h >= 0.2 * main.box.w * main.box.h) : [];
    return { count: significant.length, faces: significant };
  }

  function analyzeObjects(detector, video, ts) {
    const res = detector.detectForVideo(video, ts);
    const items = [];
    for (const d of res.detections || []) {
      const c = d.categories?.[0];
      if (!c) continue;
      const bb = d.boundingBox;
      items.push({ label: c.categoryName, score: c.score, box: bb ? { x: bb.originX / video.videoWidth, y: bb.originY / video.videoHeight, w: bb.width / video.videoWidth, h: bb.height / video.videoHeight } : null });
    }
    return items;
  }

  function drawOverlay(canvas, video, faces, objects, opts = {}) {
    const ctx = canvas.getContext('2d');
    const w = canvas.width = canvas.clientWidth * devicePixelRatio;
    const h = canvas.height = canvas.clientHeight * devicePixelRatio;
    ctx.clearRect(0, 0, w, h);
    // map normalised video coords to the object-fit: cover canvas
    const vw = video.videoWidth || 640, vh = video.videoHeight || 480;
    const scale = Math.max(w / vw, h / vh);
    const ox = (w - vw * scale) / 2, oy = (h - vh * scale) / 2;
    const rect = (b) => [ox + b.x * vw * scale, oy + b.y * vh * scale, b.w * vw * scale, b.h * vh * scale];
    ctx.lineWidth = 3 * devicePixelRatio;
    const ok = faces.length === 1 && !opts.away;
    faces.forEach((f, i) => {
      ctx.strokeStyle = i === 0 && ok ? '#34d399' : '#f87171';
      const [x, y, bw, bh] = rect(f.box);
      roundRect(ctx, x, y, bw, bh, 12 * devicePixelRatio);
    });
    (objects || []).forEach((o) => {
      if (!o.box) return;
      ctx.strokeStyle = '#f59e0b';
      const [x, y, bw, bh] = rect(o.box);
      roundRect(ctx, x, y, bw, bh, 8 * devicePixelRatio);
    });
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath(); ctx.stroke();
  }

  function captureFrame(video, { maxWidth = 640, quality = 0.75 } = {}) {
    if (!video || !video.videoWidth) return null;
    const scale = Math.min(1, maxWidth / video.videoWidth);
    const c = document.createElement('canvas');
    c.width = Math.round(video.videoWidth * scale);
    c.height = Math.round(video.videoHeight * scale);
    c.getContext('2d').drawImage(video, 0, 0, c.width, c.height);
    return c.toDataURL('image/jpeg', quality);
  }

  window.Vision = { loadFace, loadObjects, analyzeFaces, analyzeObjects, drawOverlay, captureFrame };
})();
