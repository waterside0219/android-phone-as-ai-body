const MODEL_PATHS = {
  face: './models/face_landmarker.task',
  gesture: './models/gesture_recognizer.task',
  person: './vendor/efficientdet_lite0_uint8.task',
};
const GESTURE_NAMES = {
  Open_Palm: 'wave',
  Thumb_Up: 'thumb_up',
  Victory: 'victory',
  ILoveYou: 'iloveyou',
};

let faceLandmarker;
let gestureRecognizer;
let personDetector;
let initializePromise;
let pendingFrame = null;
let processing = false;
let lastFaceAt = 0;
let present = false;
let absentAt = null;
let lastLook = {x: 0, y: 0};
let faceHistory = [];
let stableCandidate = null;
let candidateCount = 0;
let latchedGesture = null;
let lastChin = null;
let heartHold = null;
let handPose = null;
let poseSerial = 0;
let candidateSerial = 0;
let candidatePending = null;
let candidateTimes = [];
let noneCount = 0;

const clamp = value => Math.max(-1, Math.min(1, value));
const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y, (a.z || 0) - (b.z || 0));

async function initialize() {
  const {FilesetResolver, FaceLandmarker, GestureRecognizer, ObjectDetector} =
    await import('./vendor/vision/vision_bundle.mjs');
  const fileset = await FilesetResolver.forVisionTasks('./vendor/vision/wasm');
  [faceLandmarker, gestureRecognizer, personDetector] = await Promise.all([
    FaceLandmarker.createFromOptions(fileset, {
      baseOptions: {modelAssetPath: MODEL_PATHS.face, delegate: 'CPU'},
      runningMode: 'VIDEO',
      numFaces: 1,
      outputFacialTransformationMatrixes: true,
    }),
    GestureRecognizer.createFromOptions(fileset, {
      baseOptions: {modelAssetPath: MODEL_PATHS.gesture, delegate: 'CPU'},
      runningMode: 'VIDEO',
      numHands: 2,
      minHandDetectionConfidence: 0.75,
      minHandPresenceConfidence: 0.75,
      cannedGesturesClassifierOptions: {scoreThreshold: 0.75},
    }),
    ObjectDetector.createFromOptions(fileset, {
      baseOptions: {modelAssetPath: MODEL_PATHS.person, delegate: 'CPU'},
      runningMode: 'VIDEO',
      categoryAllowlist: ['person'],
      scoreThreshold: 0.5,
      maxResults: 1,
    }),
  ]);
}

function reset() {
  lastFaceAt = 0;
  present = false;
  absentAt = null;
  lastLook = {x: 0, y: 0};
  faceHistory = [];
  stableCandidate = null;
  candidateCount = 0;
  latchedGesture = null;
  lastChin = null;
  heartHold = null;
  handPose = null;
  candidatePending = null;
  noneCount = 0;
  if (pendingFrame) {
    pendingFrame.bitmap.close();
    pendingFrame = null;
  }
}

function detectFace(bitmap, timestamp) {
  const result = faceLandmarker.detectForVideo(bitmap, timestamp);
  const landmarks = result.faceLandmarks[0];
  if (!landmarks) {
    lastChin = null;
    faceHistory.push(false);
    faceHistory = faceHistory.slice(-6);
    return {present: false, look: {x: 0, y: 0}, yaw: null, looking: false};
  }

  lastChin = landmarks[152]?.y ?? null;

  let minX = 1, maxX = 0, minY = 1, maxY = 0;
  for (const point of landmarks) {
    minX = Math.min(minX, point.x);
    maxX = Math.max(maxX, point.x);
    minY = Math.min(minY, point.y);
    maxY = Math.max(maxY, point.y);
  }
  const matrix = result.facialTransformationMatrixes[0];
  const yaw = matrix && matrix.data.length >= 16
    ? Math.asin(clamp(-matrix.data[8]))
    : 0;
  const facing = Math.abs(yaw) < 0.45;
  faceHistory.push(facing);
  faceHistory = faceHistory.slice(-6);
  const looking = faceHistory.length === 6 && faceHistory.filter(Boolean).length >= 4;
  return {
    present: true,
    look: {
      x: clamp(((minX + maxX) / 2 - 0.5) * 2),
      y: clamp(((minY + maxY) / 2 - 0.5) * 2),
    },
    yaw,
    looking,
  };
}

function handScale(landmarks) {
  return Math.max(0.001, distance(landmarks[0], landmarks[9]));
}

function isRaised(landmarks) {
  const wrist = landmarks[0];
  const margin = handScale(landmarks) * 0.08;
  return [4, 8, 12, 16, 20].some(index => landmarks[index].y < wrist.y - margin);
}

function isHeart(landmarks) {
  if (!isRaised(landmarks)) return false;
  const scale = handScale(landmarks);
  if (distance(landmarks[4], landmarks[8]) > scale * 0.45) return false;
  // Thumb/index must cross above their knuckles. A fist also has nearby tips.
  if (distance(landmarks[8], landmarks[0]) < distance(landmarks[5], landmarks[0]) + scale * 0.25) return false;
  if (distance(landmarks[4], landmarks[0]) < distance(landmarks[2], landmarks[0]) + scale * 0.25) return false;
  if ((landmarks[4].y + landmarks[8].y) / 2 > landmarks[5].y - scale * 0.15) return false;
  // Tips being close is also a pinch. Require their distal segments to cross
  // in the camera plane, with the palm facing the camera rather than edge-on.
  const side = (a, b, c) => (b.x-a.x)*(c.y-a.y)-(b.y-a.y)*(c.x-a.x);
  const [a,b,c,d] = [3,4,7,8].map(i => landmarks[i]);
  if (side(a,b,c)*side(a,b,d) >= 0 || side(c,d,a)*side(c,d,b) >= 0) return false;
  const wrist = landmarks[0];
  const u = landmarks[5], v = landmarks[17];
  const ux=u.x-wrist.x, uy=u.y-wrist.y, uz=(u.z||0)-(wrist.z||0);
  const vx=v.x-wrist.x, vy=v.y-wrist.y, vz=(v.z||0)-(wrist.z||0);
  const nx=uy*vz-uz*vy, ny=uz*vx-ux*vz, nz=ux*vy-uy*vx;
  if (Math.abs(nz) < scale*scale*0.08 || Math.abs(nz)/Math.hypot(nx,ny,nz) < 0.65) return false;
  return [[12, 10], [16, 14], [20, 18]].every(([tip, pip]) =>
    distance(landmarks[tip], landmarks[0]) < distance(landmarks[pip], landmarks[0]) * 1.08
  );
}

function detectGesture(hands, recognized, timestamp) {
  let candidate = null;
  let heartPoints = null, heartHand = null;

  for (let index = 0; index < hands.landmarks.length; index++) {
    const points = hands.landmarks[index];
    if (!isRaised(points)) continue;
    const categories = recognized.gestures[index] || [];
    const best = categories[0];
    if (isHeart(points) && (!best || best.categoryName === 'None') &&
        lastChin !== null && timestamp - lastFaceAt <= 1500 &&
        [4,8,5,9,17].every(i => points[i].y < lastChin)) {
      candidate = 'heart';
      heartPoints = points;
      heartHand = recognized.handedness?.[index]?.[0]?.categoryName ?? index;
      break;
    }
    if (!best || best.score <= 0.75) continue;
    if (GESTURE_NAMES[best.categoryName]) {
      candidate = GESTURE_NAMES[best.categoryName];
      break;
    }
  }

  if (candidate === 'heart') {
    const scale = handScale(heartPoints);
    if (!heartHold || heartHold.hand !== heartHand || timestamp <= heartHold.last ||
        timestamp-heartHold.last > 500 ||
        [0,4,8,9].some(i => distance(heartPoints[i],heartHold.points[i]) > scale*0.25)) {
      heartHold = {since: timestamp, last: timestamp, points: heartPoints, hand: heartHand};
    }
    heartHold.last = timestamp;
  } else heartHold = null;

  if (candidate !== stableCandidate) {
    stableCandidate = candidate;
    candidateCount = 0;
  }
  if (!candidate) {
    latchedGesture = null;
    return null;
  }
  candidateCount++;
  if ((candidate === 'heart' ? timestamp-heartHold.since >= 800 : candidateCount >= 5) && latchedGesture !== candidate) {
    latchedGesture = candidate;
    return candidate;
  }
  return null;
}

// Geometry above is only an optional local_hint. Cloud classification owns events.
function gestureCandidate(hands, timestamp, allowed) {
  const points = hands.landmarks.filter(hand => hand.length === 21).slice().sort((a,b) => a[0].x-b[0].x);
  const changed = !handPose || points.length !== handPose.points.length || points.some((hand,index) => {
    const old = handPose.points[index], scale = handScale(hand), oldScale = handScale(old);
    return distance(hand[0],old[0]) > .05 || hand.some((p,i) =>
      Math.hypot((p.x-hand[0].x)/scale-(old[i].x-old[0].x)/oldScale,
                 (p.y-hand[0].y)/scale-(old[i].y-old[0].y)/oldScale) > .45);
  });
  if (!points.length) {
    handPose = null; noneCount = 0; candidatePending = null;
    return null;
  }
  if (changed) {
    handPose = {points, since:timestamp, last:timestamp, id:++poseSerial};
    noneCount = 0;
  } else if (timestamp-handPose.last > 500 || timestamp <= handPose.last) {
    handPose.since = timestamp;
  }
  handPose.last = timestamp;
  candidateTimes = candidateTimes.filter(t => timestamp-t < 3600000);
  if (candidatePending && timestamp-candidatePending.at >= 10000) candidatePending = null;
  if (!allowed || timestamp-handPose.since < 600 || noneCount >= 2 || candidatePending ||
      candidateTimes.length >= 60 || candidateTimes.filter(t => timestamp-t < 60000).length >= 6 ||
      candidateTimes.length && timestamp-candidateTimes.at(-1) < 4000) return null;
  candidateTimes.push(timestamp);
  candidatePending = {id:String(++candidateSerial), pose:handPose.id, at:timestamp};
  return {...candidatePending};
}

async function processFrames() {
  if (processing) return;
  processing = true;
  try {
    while (pendingFrame) {
      const frame = pendingFrame;
      pendingFrame = null;
      try {
        const recognized = gestureRecognizer.recognizeForVideo(frame.bitmap, frame.timestamp);
        let face = null;
        if (!lastFaceAt || frame.timestamp - lastFaceAt >= 1000) {
          face = detectFace(frame.bitmap, frame.timestamp);
          face.face_present = face.present;
          if (face.face_present) lastLook = face.look;
          else face.look = {...lastLook};
          face.interval_ms = lastFaceAt ? frame.timestamp - lastFaceAt : 1000;
          // Reuse confident face/hand detections first; only run the extra
          // person model when they cannot establish presence (at most 1 Hz).
          face.present = face.face_present || recognized.landmarks.some(hand => hand.length === 21) ||
            personDetector.detectForVideo(frame.bitmap, frame.timestamp).detections.some(detection =>
              detection.categories.some(category => category.categoryName === 'person' && category.score >= 0.5));
          if (face.present) {
            present = true;
            absentAt = null;
          } else {
            // A pipeline outage is not a continuous sequence of empty frames.
            if (absentAt === null || lastFaceAt && frame.timestamp - lastFaceAt > 10000) absentAt = frame.timestamp;
            if (frame.timestamp - absentAt >= 10000) present = false;
          }
          face.present = present;
          lastFaceAt = frame.timestamp;
        }
        const local_hint = detectGesture(recognized, recognized, frame.timestamp);
        const gesture_candidate = gestureCandidate(recognized, frame.timestamp, frame.candidate_allowed !== false);
        postMessage({type: 'result', face, gesture: null, local_hint, gesture_candidate});
      } catch (error) {
        postMessage({type: 'error', message: error.message || String(error)});
      } finally {
        frame.bitmap.close();
      }
    }
  } finally {
    processing = false;
  }
}

onmessage = event => {
  const message = event.data;
  if (message.type === 'init') {
    if (!initializePromise) initializePromise = initialize();
    initializePromise.then(
      () => postMessage({type: 'ready', models: MODEL_PATHS}),
      error => {
        initializePromise = null;
        postMessage({type: 'error', message: error.message || String(error)});
      },
    );
  } else if (message.type === 'gesture_result') {
    if (candidatePending && message.candidate_id === candidatePending.id) {
      if (handPose?.id === candidatePending.pose) {
        if (message.gesture === 'none') noneCount++;
        else if (message.gesture) noneCount = 0;
      }
      candidatePending = null;
    }
  } else if (message.type === 'reset') {
    reset();
  } else if (message.type === 'frame' && faceLandmarker) {
    if (pendingFrame) pendingFrame.bitmap.close();
    pendingFrame = {bitmap: message.bitmap, timestamp: message.timestamp, candidate_allowed: message.candidate_allowed};
    processFrames();
  }
};
