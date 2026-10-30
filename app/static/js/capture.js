/* Webcam photo capture widget used for face enrolment.
   Markup: <div data-capture data-target="input-name"> ... see partials/capture.html */
(function () {
  async function setup(root) {
    const video = root.querySelector('video');
    const img = root.querySelector('img.preview');
    const msg = root.querySelector('.cam-msg');
    const input = document.querySelector(`input[name="${root.dataset.target}"]`);
    const startBtn = root.parentElement.querySelector('[data-cam-start]');
    const shootBtn = root.parentElement.querySelector('[data-cam-shoot]');
    const retakeBtn = root.parentElement.querySelector('[data-cam-retake]');
    const placeholder = root.querySelector('.cam-placeholder');
    let stream = null;

    async function start() {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: 'user' }, audio: false });
        video.srcObject = stream;
        await video.play();
        placeholder.hidden = true;
        video.hidden = false; img.hidden = true;
        startBtn.hidden = true; shootBtn.hidden = false; retakeBtn.hidden = true;
        msg.textContent = 'Centre your face inside the oval, look straight at the camera.';
        msg.hidden = false;
      } catch (e) {
        msg.hidden = false;
        msg.textContent = 'Camera access was blocked. Allow camera permission in your browser and try again.';
      }
    }

    function shoot() {
      if (!video.videoWidth) return;
      const c = document.createElement('canvas');
      c.width = video.videoWidth; c.height = video.videoHeight;
      const ctx = c.getContext('2d');
      ctx.drawImage(video, 0, 0); // raw (un-mirrored) frame, same orientation as exam-room frames
      const data = c.toDataURL('image/jpeg', 0.9);
      input.value = data;
      img.src = data; img.hidden = false; video.hidden = true;
      shootBtn.hidden = true; retakeBtn.hidden = false;
      msg.textContent = 'Photo captured. The server will check that exactly one clear face is visible.';
      stop();
      root.dispatchEvent(new CustomEvent('captured', { bubbles: true }));
    }

    function stop() { if (stream) stream.getTracks().forEach((t) => t.stop()); stream = null; }

    startBtn?.addEventListener('click', start);
    shootBtn?.addEventListener('click', shoot);
    retakeBtn?.addEventListener('click', () => { input.value = ''; start(); });
    window.addEventListener('beforeunload', stop);
  }
  document.addEventListener('DOMContentLoaded', () => document.querySelectorAll('[data-capture]').forEach(setup));
})();
