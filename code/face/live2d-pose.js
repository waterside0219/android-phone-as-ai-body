// Overrides for the existing model; the idle motion remains the base pose.
function live2dPose({now, look, reaction, closed, blink, pokeAt, speaking}) {
  const elapsed = now - pokeAt;
  const poke = elapsed >= 0 && elapsed < 900 ? Math.sin(elapsed / 90) * Math.exp(-elapsed / 280) : 0;
  const hold = reaction === 'hold', chin = reaction === 'chin';
  const eye = closed || hold ? 0 : chin ? .16 : blink;
  const ear = hold ? -.8 : chin ? -.28 : poke * .85;
  return {
    ParamEyeLOpen: eye, ParamEyeROpen: eye,
    ParamEyeBallX: look.x, ParamEyeBallY: -look.y,
    ParamAngleX: look.x * (chin ? 12 : 8),
    ParamAngleY: -look.y * 4 + (chin ? 3 : 0),
    ParamAngleZ: poke * 5 + (hold || chin ? look.x * 4 : 0),
    ParamEarL: ear, ParamEarR: hold || chin ? ear : -poke * .7,
    ParamHairSwing: Math.max(-1, Math.min(1, poke * .65 + look.x * .18)),
    ParamMouthOpenY: speaking ? .16 + .54 * Math.abs(Math.sin(now / 93)) : 0,
  };
}
