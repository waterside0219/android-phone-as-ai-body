(() => {
  let lock = null;
  async function keepAwake() {
    if (document.visibilityState !== 'visible' || (lock && !lock.released)) return;
    try {
      lock = await navigator.wakeLock?.request('screen');
    } catch {
      // Android may refuse during power saving; retry when the page returns.
    }
  }
  document.addEventListener('visibilitychange', keepAwake);
  keepAwake();
})();
