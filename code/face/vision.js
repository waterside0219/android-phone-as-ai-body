(() => {
  const API = 'http://127.0.0.1:8192';
  const button = document.querySelector('#vision-toggle');
  const statusLabel = document.querySelector('#vision-status');
  const gestureText = {
    wave: '看到你在挥手',
    thumb_up: '收到你的点赞',
    victory: '看到你在比耶',
    iloveyou: '看到“我爱你”',
    heart: '收到你的比心',
  };
  const frameCanvas = document.createElement('canvas');
  const frameContext = frameCanvas.getContext('2d');
  const hashCanvas = document.createElement('canvas');
  hashCanvas.width = 9; hashCanvas.height = 8;
  const hashContext = hashCanvas.getContext('2d', {willReadFrequently: true});

  let stream = null;
  let video = null;
  let worker = null;
  let starting = false;
  let active = false;
  let cameraFacing = null;
  let enabled = true;
  let cameraMode = 'auto';
  let allowed = false;
  let watchActive = false;
  let offMode = false;
  let closed = document.body.classList.contains('closed');
  let generation = 0;
  let captureTimer = 0;
  let temperatureTimer = 0;
  let temperatureController = null;
  let captureInFlight = false;
  let lastTimestamp = 0;
  let lastPresenceAt = 0;
  let lastPresent = false;
  let lastFacePresent = false;
  let lastLook = {x: 0, y: 0};
  let frameCaptureEnabled = true;
  let latestFrame = null;
  let lastFrameAt = 0;
  const frameControllers = new Set();
  const candidateSession = Date.now().toString(36) + Math.random().toString(36).slice(2,8);
  let pendingCandidate = null;
  let temperature = null;
  let thermalHold = false;
  let gestureStatusTimer = 0;
  let resizeTimer = 0;
  let cameraStartedAt = 0;
  let lastVideoFrameAt = 0;
  let lastVideoTime = -1;
  let probeMode = false;
  let probeSleeping = false;
  let probeTimer = 0;
  let probeCloseTimer = 0;
  let streamLostAt = 0;
  let detectionCount = 0;
  let lastDetectionAt = 0;
  let snapshotCount = 0;
  let lookUntil = 0, lookSentUntil = 0, lookVideoTime = -1, lookTimer = 0, lookUploading = false;
  const looking = () => lookUntil * 1000 > Date.now() && enabled && allowed && !offMode && !thermalHold;
  const cameraClosed = () => closed && !looking();

  // Two minutes is the candidate within A11's 2–3 minute range; phone CPU,
  // temperature and frame-rate comparison must still validate this setting.
  function snapshotInterval() {
    return window.faceCall?.diagnostic().state === 'connected' ? 120000 : 60000;
  }
  function detectionInterval() {
    if (!probeMode && !lastPresent) return 3000;
    return temperature !== null && temperature > 40 ? 400 : 200;
  }

  function scheduleProbeClose() {
    if (!probeMode || !active || probeCloseTimer) return;
    probeCloseTimer = setTimeout(() => {
      probeCloseTimer = 0;
      if (!probeMode || lastPresent) return;
      stopCamera(false, true);
      probeSleeping = true;
      probeTimer = setTimeout(() => {
        probeTimer = 0;
        probeSleeping = false;
        if (allowed && !cameraClosed()) startCamera();
      }, 28000);
    }, 2000);
  }

  function observeVideoFrame() {
    if (!video || !stream) return;
    if (video.readyState >= 2 && video.currentTime !== lastVideoTime) {
      lastVideoTime = video.currentTime;
      lastVideoFrameAt = Date.now();
    }
  }

  function watchVideoFrames(currentVideo, currentGeneration) {
    if (!currentVideo.requestVideoFrameCallback) return;
    currentVideo.requestVideoFrameCallback(() => {
      if (currentGeneration !== generation || video !== currentVideo) return;
      observeVideoFrame();
      watchVideoFrames(currentVideo, currentGeneration);
    });
  }

  function facingForScreen() {
    // MIX Flip 2: the inner display is tall; the cover display is nearly square.
    return Math.max(screen.width, screen.height) / Math.min(screen.width, screen.height) > 2
      ? 'user' : 'environment';
  }

  function updateButton() {
    button.disabled = starting || closed || offMode;
    button.value = cameraMode;
    button.setAttribute('aria-pressed', String(enabled && !offMode && !closed));
  }

  function updateStatus() {
    statusLabel.classList.remove('gesture-visible');
    if (thermalHold) statusLabel.textContent = '温度偏高 · 镜头已暂停';
    else if (closed) statusLabel.textContent = '摄像头已关闭';
    else if (starting) statusLabel.textContent = '正在准备本地视觉…';
    else if (!active) statusLabel.textContent = !enabled ? '摄像头已关' : !watchActive ? '看着时段外 · 摄像头休息' : '摄像头已关';
    else {
      const hot = temperature !== null && temperature > 40;
      statusLabel.textContent = '本地看见中' + (temperature === null ? '' : ` · ${temperature.toFixed(1)}℃`) + (!lastPresent ? ' · 3 秒检测' : hot ? ' · 降频检测' : '');
    }
  }

  function clearLatestFrame() {
    for (const controller of frameControllers) controller.abort();
    frameControllers.clear();
    pendingCandidate = null;
    latestFrame = null;
    lastFrameAt = 0;
    frameCanvas.width = 1;
    frameCanvas.height = 1;
    hashContext.clearRect?.(0, 0, 9, 8);
  }

  function publishPresence(face, force = false) {
    const now = Date.now();
    if (!force && face.present === lastPresent && now - lastPresenceAt < 1000) return;
    lastPresent = face.present;
    lastFacePresent = face.face_present === true;
    lastLook = face.look;
    lastPresenceAt = now;
    window.dispatchEvent(new CustomEvent('face-vision-presence', {detail: face}));
    if (!face.present) clearLatestFrame();
    return postPresence({present: face.present, face_present: face.face_present === true, look: face.look, looking: face.looking === true});
  }

  async function postPresence(body) {
    try {
      const response = await fetch(API + '/api/presence', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
        keepalive: true,
      });
      if (!response.ok) throw new Error('HTTP ' + response.status);
    } catch (error) {
      console.warn('视觉状态暂未上报', error);
    }
  }

  function showGesture(kind) {
    if (!active || !gestureText[kind]) return;
    setReaction('hold', 1800);
    clearTimeout(gestureStatusTimer);
    statusLabel.textContent = gestureText[kind];
    statusLabel.classList.add('gesture-visible');
    gestureStatusTimer = setTimeout(updateStatus, 3000);
    // The phone service already emitted this cloud-confirmed gesture event.
  }

  async function handleWorkerMessage(event) {
    const message = event.data;
    if (message.type === 'result') {
      if (!active) return;
      detectionCount++;
      lastDetectionAt = Date.now();
      if (message.face?.present) {
        probeMode = false;
        clearTimeout(probeCloseTimer);probeCloseTimer = 0;
      }
      if (message.face) await publishPresence(message.face);
      if (message.gesture_candidate) {
        const candidate = message.gesture_candidate;
        const identifier = candidateSession + ':' + generation + ':' + candidate.id;
        pendingCandidate = {id:identifier, workerId:candidate.id, generation};
        let sent = false;
        try {sent = await maybeCaptureFrame('gesture_candidate', identifier);}
        finally {
          if (!sent && pendingCandidate?.id === identifier) {
            worker?.postMessage({type:'gesture_result', candidate_id:candidate.id, gesture:null});
            pendingCandidate = null;
          }
        }
      } else if (message.face?.present) maybeCaptureFrame();
    } else if (message.type === 'error') {
      console.error('本地视觉检测失败', message.message);
      stopCamera(true);
      statusLabel.textContent = '本地视觉加载或检测失败';
    }
  }

  function initializeWorker() {
    return new Promise((resolve, reject) => {
      const instance = new Worker('./vision-worker.js?v=6');
      worker = instance;
      const timeout = setTimeout(() => reject(new Error('Worker 初始化超时')), 20000);
      const fail = error => {clearTimeout(timeout);reject(new Error(error.message || 'Worker 初始化失败'));};
      instance.onerror = fail;
      instance.onmessage = event => {
        if (event.data.type === 'ready') {
          clearTimeout(timeout);
          instance.onmessage = handleWorkerMessage;
          instance.onerror = error => {
            console.error('本地视觉 Worker 失败', error);
            stopCamera(true);
            statusLabel.textContent = '本地视觉已停止';
          };
          resolve(instance);
        } else if (event.data.type === 'error') {
          fail(event.data);
        }
      };
      instance.postMessage({type: 'init'});
    });
  }

  function scheduleCapture() {
    clearTimeout(captureTimer);
    if (!active) return;
    // Face recognition is already limited to 1 Hz in the worker; keep its
    // 600ms hand stability sampling while a person is present.
    captureTimer = setTimeout(captureFrame, detectionInterval());
  }

  async function captureFrame() {
    observeVideoFrame();
    if (looking()) void maybeCaptureFrame('look');
    if (!active || captureInFlight || !video || video.readyState < 2 || !worker) {
      scheduleCapture();
      return;
    }
    captureInFlight = true;
    const currentGeneration = generation;
    try {
      // Models resize to ~256px internally; handing them a 640px-wide bitmap saves copy and resize work and keeps the phone cooler.
      const bitmap = await createImageBitmap(video, video.videoWidth > 640 ? {resizeWidth: 640, resizeHeight: Math.round(video.videoHeight * 640 / video.videoWidth), resizeQuality: 'low'} : {});
      if (currentGeneration !== generation || !active) {
        bitmap.close();
        return;
      }
      lastTimestamp = Math.max(performance.now(), lastTimestamp + 0.01);
      worker.postMessage({type: 'frame', bitmap, timestamp: lastTimestamp,
        candidate_allowed: frameCaptureEnabled && allowed && !closed && !offMode && !thermalHold && (temperature === null || temperature < 42)}, [bitmap]);
    } catch (error) {
      if (active) console.warn('跳过一帧本地视觉检测', error);
    } finally {
      captureInFlight = false;
      scheduleCapture();
    }
  }

  async function pollTemperature() {
    clearTimeout(temperatureTimer);
    if (!active) return;
    temperatureController = new AbortController();
    try {
      const response = await fetch(API + '/api/temperature', {
        cache: 'no-store',
        signal: temperatureController.signal,
      });
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const data = await response.json();
      const value = Number(data.temperature_c ?? data.temperature ?? data.celsius);
      temperature = Number.isFinite(value) ? value : null;
      thermalHold = data.thermal_hold === true;
      if (thermalHold && data.cooling_recovering !== true) {allowed = false;stopCamera(true);}
      updateStatus();
    } catch (error) {
      if (error.name !== 'AbortError') {
        temperature = null;
        updateStatus();
      }
    } finally {
      temperatureController = null;
      if (active) temperatureTimer = setTimeout(pollTemperature, 5000);
    }
  }

  async function maybeCaptureFrame(reason = 'auto', candidateId = null) {
    const look = reason === 'look' && looking();
    if ((!look && (!frameCaptureEnabled || !active || closed || !lastPresent)) || !allowed || offMode || thermalHold || !video || video.readyState < 2) return false;
    if (reason === 'look' && (!look || !stream || lookUploading || lookSentUntil === lookUntil || video.currentTime === lookVideoTime)) return false;
    if (reason === 'gesture_candidate' && temperature !== null && temperature >= 42) return false;
    if (reason === 'auto' && Date.now() - lastFrameAt < snapshotInterval()) return false;
    const currentGeneration = generation;
    const capturedAt = Date.now();
    if (reason !== 'gesture_candidate' && !look) lastFrameAt = capturedAt;
    const width = 768;
    const height = Math.max(1, Math.min(2048, Math.round(width * video.videoHeight / video.videoWidth)));
    frameCanvas.width = width;
    frameCanvas.height = height;
    frameContext.drawImage(video, 0, 0, width, height);
    hashContext.drawImage(frameCanvas, 0, 0, 9, 8);
    const pixels = hashContext.getImageData(0, 0, 9, 8).data;
    const gray = i => pixels[i] * 299 + pixels[i + 1] * 587 + pixels[i + 2] * 114;
    let dhash = '', nibble = 0;
    for (let y = 0; y < 8; y++) for (let x = 0; x < 8; x++) {
      const i = (y * 9 + x) * 4;
      nibble = (nibble << 1) | Number(gray(i) > gray(i + 4));
      if (x % 4 === 3) {dhash += nibble.toString(16); nibble = 0;}
    }
    let jpeg_base64;
    for (const quality of [0.82, 0.65, 0.45, 0.25, 0.1]) {
      jpeg_base64 = frameCanvas.toDataURL('image/jpeg', quality).split(',')[1];
      if (jpeg_base64?.length <= 109228) break;
    }
    if (!jpeg_base64 || jpeg_base64.length > 109228) return false;
    const frame = {jpeg_base64, dhash, captured_at: capturedAt / 1000, reason};
    if (candidateId) frame.candidate_id = candidateId;
    latestFrame = frame;
    snapshotCount++;
    const controller = new AbortController();
    frameControllers.add(controller);
    const requestedUntil = lookUntil;
    if (look) lookUploading = true;
    try {
      const result = await postJSON('/api/camera-frame', frame, {keepalive: false, controller});
      const sent = currentGeneration === generation && (look ? !!stream : active) && result?.selected != null;
      if (look && sent) lookSentUntil = requestedUntil;
      return sent;
    } finally {
      if (look) lookUploading = false;
      frameControllers.delete(controller);
      // The phone service owns the one-minute frame; never retain a second copy
      // in the page after handoff, including the canvas backing pixels.
      if (currentGeneration === generation) {
        if (latestFrame === frame) latestFrame = null;
        frameCanvas.width = frameCanvas.height = 1;
        hashContext.clearRect(0, 0, 9, 8);
      }
    }
  }

  window.addEventListener('face-look-now', async () => {
    const captured = await maybeCaptureFrame('manual');
    statusLabel.textContent = captured ? '已拍好，等识图' : '镜头休息中或没看见人，未拍照';
    statusLabel.classList.add('gesture-visible');
    clearTimeout(gestureStatusTimer);
    gestureStatusTimer = setTimeout(updateStatus, 4000);
  });

  function stopCamera(terminateWorker = false, probePause = false) {
    const wasRunning = active || starting || !!stream;
    generation++;
    active = false;
    starting = false;
    clearTimeout(captureTimer);
    clearTimeout(temperatureTimer);
    clearTimeout(gestureStatusTimer);
    clearTimeout(probeCloseTimer);probeCloseTimer = 0;
    if (!probePause) {
      clearTimeout(probeTimer);probeTimer = 0;probeSleeping = false;
    }
    if (temperatureController) temperatureController.abort();
    temperatureController = null;
    temperature = null;
    if (worker) worker.postMessage({type: 'reset'});
    if (terminateWorker && worker) {
      worker.terminate();
      worker = null;
    }
    if (stream) for (const track of stream.getTracks()) track.stop();
    stream = null;
    cameraStartedAt = lastVideoFrameAt = 0;
    lastVideoTime = -1;
    if (video) video.srcObject = null;
    video = null;
    clearLatestFrame();
    lastPresent = false;
    lastFacePresent = false;
    lastLook = {x: 0, y: 0};
    lastPresenceAt = 0;
    document.body.classList.remove('vision-awake');
    window.dispatchEvent(new CustomEvent('face-vision-presence', {
      detail: {present: false, look: {x: 0, y: 0}, looking: false},
    }));
    updateButton();
    updateStatus();
    if (wasRunning && !probePause) postPresence({present: false, look: {x: 0, y: 0}});
  }

  async function startCamera() {
    if (active || starting || cameraClosed() || !allowed || probeSleeping || document.visibilityState !== 'visible') return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      statusLabel.textContent = '当前浏览器不支持摄像头';
      return;
    }
    starting = true;
    const currentGeneration = ++generation;
    updateButton();
    statusLabel.textContent = '请允许摄像头权限';
    try {
      const facing = facingForScreen();
      const nextStream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: {facingMode: facing, width: {ideal: 960}, height: {ideal: 540}, frameRate: {ideal: 15, max: 15}},
      });
      if (currentGeneration !== generation || cameraClosed() || !allowed) {
        for (const track of nextStream.getTracks()) track.stop();
        return;
      }
      stream = nextStream;
      cameraStartedAt = Date.now() / 1000;
      cameraFacing = facing;
      video = document.createElement('video');
      video.muted = true;
      video.playsInline = true;
      video.srcObject = stream;
      await video.play();
      if (currentGeneration !== generation || cameraClosed() || !allowed) return;
      watchVideoFrames(video, currentGeneration);
      // Explicit look needs pixels, not a person/model result. Cold model loading
      // must not delay the requested still image.
      observeVideoFrame();
      if (looking()) void maybeCaptureFrame('look');
      statusLabel.textContent = '正在加载本地视觉模型…';
      if (!worker) await initializeWorker();
      if (currentGeneration !== generation || cameraClosed() || !allowed) return;
      if (facing !== facingForScreen()) {
        stopCamera(true);
        if (allowed && !cameraClosed()) startCamera();
        return;
      }
      active = true;
      starting = false;
      worker.postMessage({type: 'reset'});
      updateButton();
      updateStatus();
      captureFrame();
      scheduleProbeClose();
      pollTemperature();
    } catch (error) {
      if (currentGeneration === generation) {
        console.error('无法开启本地视觉', error);
        stopCamera(true);
        statusLabel.textContent = error.name === 'NotAllowedError' ? '摄像头权限未开启' : '本地视觉开启失败';
      }
    } finally {
      if (currentGeneration === generation && starting) {
        starting = false;
        updateButton();
      }
    }
  }

  button.addEventListener('pointerdown', event => event.stopPropagation());
  button.addEventListener('change', event => {
    event.stopPropagation();
    saveControls({camera_mode: button.value});
  });
  function applySettings(settings) {
    if (!settings) return;
    cameraMode = settings.camera_mode || (settings.camera_enabled ? 'auto' : 'off');
    enabled = cameraMode !== 'off';
    offMode = settings.off_mode === true;
    watchActive = settings.watch_active === true;
    thermalHold = settings.thermal_hold === true;
    if (Number.isFinite(settings.temperature_c)) temperature = settings.temperature_c;
    allowed = (settings.camera_allowed ?? settings.camera_active) === true;
    const requestedUntil = Number(settings.camera_look_until) || 0;
    if (requestedUntil !== lookUntil) {
      clearTimeout(lookTimer);
      lookUntil = requestedUntil;
      lookVideoTime = video?.currentTime ?? -1;
      if (looking()) lookTimer = setTimeout(() => {lookUntil = 0;stopCamera(true);}, Math.max(0, lookUntil * 1000 - Date.now()));
    }
    const result = settings.gesture_result;
    if (result && pendingCandidate?.id === result.candidate_id && pendingCandidate.generation === generation) {
      if (active && allowed && !closed && !offMode && !thermalHold && (temperature === null || temperature < 42)) {
        worker?.postMessage({type:'gesture_result',candidate_id:pendingCandidate.workerId,
          gesture:result.doing !== 'other' ? 'none' : result.gesture});
        if (result.confidence >= .7 && result.event !== 'none') showGesture(result.event);
      }
      pendingCandidate = null;
    }
    probeMode = settings.camera_probe_mode === true && !lastPresent && !looking();
    if ((!allowed || cameraClosed()) && (active || starting || probeSleeping || worker)) stopCamera(true);
    else if (allowed && !active && !starting && !cameraClosed()) startCamera();
    if (probeMode) scheduleProbeClose();
    else if (probeSleeping) {clearTimeout(probeTimer);probeTimer = 0;probeSleeping = false;if (allowed && !cameraClosed()) startCamera();}
    if (looking()) void maybeCaptureFrame('look');
    updateButton();
    button.disabled ||= settings.controls_pending === true;
    if (!statusLabel.classList.contains('gesture-visible')) updateStatus();
    if(settings.work_silent_period&&!silentForm.contains(document.activeElement)){document.querySelector('#silent-start').value=settings.work_silent_period.start;document.querySelector('#silent-end').value=settings.work_silent_period.end==='24:00'?'00:00':settings.work_silent_period.end;}
    if (settings.watch_periods && !scheduleForm.contains(document.activeElement)) {
      settings.watch_periods.slice(0,3).forEach((period,i) => {
        scheduleForm.elements['days'+i].value = period.days;
        scheduleForm.elements['start'+i].value = period.start;
        scheduleForm.elements['end'+i].value = period.end === '24:00' ? '00:00' : period.end;
      });
    }
    document.querySelector('#notify-sadness').checked = settings.notify_sadness !== false;
    const remaining = Math.max(0, (settings.companion_until || 0) * 1000 - Date.now());
    const companionSelect = document.querySelector('#companion-hours');
    const pendingCompanion = Number.isInteger(settings.companion_hours);
    if (pendingCompanion) companionSelect.value = String(settings.companion_hours);
    else if (!remaining) companionSelect.value = '0';
    else if (companionSelect.value === '0') companionSelect.value = remaining > 3600000 ? '2' : '1';
    document.querySelector('#companion-status').textContent = pendingCompanion ? '陪我：待确认' : remaining ? `陪着 · 还剩 ${Math.ceil(remaining / 60000)} 分钟` : '双击脸：看这个';
  }
  document.querySelector('#notify-sadness').addEventListener('change', event => saveControls({notify_sadness: event.target.checked}));
  document.querySelector('#companion-hours').addEventListener('change', event => saveControls({companion_hours: Number(event.target.value)}));
  const silentForm=document.querySelector('#silent-form');
  silentForm.addEventListener('submit',async event=>{
    event.preventDefault();
    const result=await postJSON('/api/settings',{work_silent_period:{days:'weekdays',start:document.querySelector('#silent-start').value,end:document.querySelector('#silent-end').value==='00:00'?'24:00':document.querySelector('#silent-end').value}});
    document.querySelector('#silent-result').textContent=result?'已保存':'保存失败：结束时间需晚于开始';
  });
  const scheduleForm = document.querySelector('#watch-form');
  scheduleForm.addEventListener('pointerdown', event => event.stopPropagation());
  scheduleForm.addEventListener('submit', async event => {
    event.preventDefault();
    const watch_periods = [0,1,2].map(i => ({days:scheduleForm.elements['days'+i].value, start:scheduleForm.elements['start'+i].value, end:scheduleForm.elements['end'+i].value === '00:00' ? '24:00' : scheduleForm.elements['end'+i].value}));
    const result = await postJSON('/api/settings',{watch_periods});
    document.querySelector('#watch-result').textContent = result ? '已保存' : '保存失败：结束时间需晚于开始';
    // The regular poll applies the result without racing a privacy change.

  });
  window.addEventListener('face-settings', event => applySettings(event.detail));
  window.addEventListener('face-closed-change', event => {
    closed = event.detail.closed;
    if (cameraClosed()) stopCamera(true);
    else {
      updateButton();
      updateStatus();
      if (allowed) startCamera();
    }
  });
  window.addEventListener('pagehide', () => stopCamera(true));
  function resumePipeline() {
    if (document.visibilityState !== 'visible') {stopCamera(true);return;}
    if (allowed && !cameraClosed() && !probeSleeping && !window.faceVision.streamAlive) {
      stopCamera(true);startCamera();
    }
  }
  document.addEventListener('visibilitychange', resumePipeline);
  document.addEventListener('resume', resumePipeline);
  window.addEventListener('pageshow', resumePipeline);
  setInterval(() => {
    if (!allowed || cameraClosed() || thermalHold || probeSleeping || document.visibilityState !== 'visible' || window.faceVision.streamAlive) {
      streamLostAt = 0;
      return;
    }
    if (!streamLostAt) streamLostAt = Date.now();
    if (Date.now() - streamLostAt >= 50000) {
      streamLostAt = 0;stopCamera(true);startCamera();
    }
  }, 5000);
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (active && cameraFacing !== facingForScreen()) {
        stopCamera(true);
        if (allowed && !cameraClosed()) startCamera();
      }
    }, 250);
  });

  window.faceVision = Object.freeze({
    diagnostic() {
      return {temperature_c:temperature,detection_interval_ms:detectionInterval(),snapshot_interval_ms:snapshotInterval(),
        detection_count:detectionCount,last_detection_at:lastDetectionAt,snapshot_count:snapshotCount};
    },
    setFrameCapture(enabled) {
      frameCaptureEnabled = !!enabled;
      if (!frameCaptureEnabled) clearLatestFrame();
      return frameCaptureEnabled;
    },
    getLatestFrame() {
      return latestFrame;
    },
    get active() {
      return active;
    },
    get streamAlive() {
      return document.visibilityState === 'visible' && !!stream && lastVideoFrameAt > 0 &&
        Date.now() - lastVideoFrameAt < 5000 && stream.getTracks().some(track => track.readyState === 'live');
    },
    get startedAt() { return cameraStartedAt; },
    get probeSleeping() { return probeSleeping; },
    get detectionInterval() { return detectionInterval(); },
  });
  updateButton();
  updateStatus();
  if (allowed && !closed) startCamera();
})();
