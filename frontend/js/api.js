/**
 * FaithView Pro — Backend API Client
 *
 * Provides a unified interface for REST and WebSocket communication
 * with the Python backend. Works both in development (direct connection)
 * and in the Tauri desktop app (dynamic port from sidecar).
 */

const FaithViewAPI = (() => {
  let _baseUrl = '';
  let _wsUrl = '';
  let _ws = null;
  let _wsCallbacks = {};
  let _reconnectTimer = null;
  let _reconnectDelay = 1000;
  let _connected = false;
  let _autoReconnect = true;

  // ---- Initialization ----

  function init(port) {
    if (port) {
      _baseUrl = `http://127.0.0.1:${port}`;
      _wsUrl = `ws://127.0.0.1:${port}/ws`;
    } else {
      // Development mode — detect from current page URL
      const loc = window.location;
      const host = loc.hostname || '127.0.0.1';
      const portStr = loc.port || '8000';
      _baseUrl = `${loc.protocol}//${host}:${portStr}`;
      _wsUrl = `${loc.protocol === 'https:' ? 'wss' : 'ws'}://${host}:${portStr}/ws`;
    }
    console.log(`[api] init: base=${_baseUrl} ws=${_wsUrl}`);
  }

  function getBaseUrl() { return _baseUrl; }

  // ---- REST helpers ----

  async function _get(path) {
    const resp = await fetch(`${_baseUrl}${path}`);
    if (!resp.ok) throw new Error(`GET ${path} failed: ${resp.status}`);
    return resp.json();
  }

  async function _post(path, body) {
    const resp = await fetch(`${_baseUrl}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error(`POST ${path} failed: ${resp.status}`);
    return resp.json();
  }

  async function _del(path) {
    const resp = await fetch(`${_baseUrl}${path}`, { method: 'DELETE' });
    if (!resp.ok) throw new Error(`DELETE ${path} failed: ${resp.status}`);
    return resp.json();
  }

  // ---- REST API ----

  async function health() {
    return _get('/health');
  }

  async function ready() {
    try {
      const resp = await fetch(`${_baseUrl}/ready`);
      return resp.ok;
    } catch {
      return false;
    }
  }

  async function getQueue() {
    return _get('/queue');
  }

  async function addToQueue(item) {
    return _post('/queue/add', item);
  }

  async function removeFromQueue(id) {
    return _del(`/queue/${id}`);
  }

  async function clearQueue() {
    return _post('/queue/clear', {});
  }

  async function present(ref, translation, text) {
    return _post('/present', { reference: ref, translation, text });
  }

  async function presentTheme(theme) {
    return _post('/present-theme', { theme });
  }

  async function clearDisplay() {
    return _post('/display/clear', {});
  }

  async function clearDetections() {
    return _post('/detections/clear', {});
  }

  async function setMode(mode) {
    return _post('/mode', { mode });
  }

  async function setTranscriptionSource(source) {
    return _post('/transcription-source', { source });
  }

  async function search(query, topK = 10) {
    return _get(`/search?q=${encodeURIComponent(query)}&top_k=${topK}`);
  }

  async function bibleChapter(translation, book, chapter) {
    return _get(`/bible/${encodeURIComponent(translation)}/${encodeURIComponent(book)}/${chapter}`);
  }

  // ---- WebSocket ----

  function connectWS(onMessage, onOpen, onClose) {
    if (_ws && (_ws.readyState === WebSocket.OPEN || _ws.readyState === WebSocket.CONNECTING)) {
      return;
    }

    _autoReconnect = true;
    _ws = new WebSocket(_wsUrl);

    _ws.onopen = () => {
      console.log('[api] WebSocket connected');
      _connected = true;
      _reconnectDelay = 1000;
      if (onOpen) onOpen();
    };

    _ws.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data);
        if (onMessage) onMessage(msg);
        // Fire registered callbacks
        const cbs = _wsCallbacks[msg.type];
        if (cbs) cbs.forEach(cb => cb(msg));
      } catch (e) {
        console.warn('[api] WS message parse error:', e);
      }
    };

    _ws.onclose = () => {
      console.log('[api] WebSocket disconnected');
      _connected = false;
      if (onClose) onClose();
      if (_autoReconnect) {
        _scheduleReconnect();
      }
    };

    _ws.onerror = (err) => {
      console.error('[api] WebSocket error:', err);
    };
  }

  function disconnectWS() {
    _autoReconnect = false;
    clearTimeout(_reconnectTimer);
    if (_ws) {
      _ws.close();
      _ws = null;
    }
  }

  function _scheduleReconnect() {
    clearTimeout(_reconnectTimer);
    _reconnectTimer = setTimeout(() => {
      if (_autoReconnect && !_connected) {
        console.log(`[api] reconnecting in ${_reconnectDelay}ms...`);
        connectWS();
        _reconnectDelay = Math.min(_reconnectDelay * 1.5, 5000);
      }
    }, _reconnectDelay);
  }

  function onWSEvent(type, callback) {
    if (!_wsCallbacks[type]) _wsCallbacks[type] = [];
    _wsCallbacks[type].push(callback);
    return () => {
      _wsCallbacks[type] = _wsCallbacks[type].filter(cb => cb !== callback);
    };
  }

  function isWSConnected() { return _connected; }

  // ---- Wait for backend ----

  async function waitForBackend(maxWait = 30000, interval = 500) {
    const start = Date.now();
    while (Date.now() - start < maxWait) {
      if (await ready()) return true;
      await new Promise(r => setTimeout(r, interval));
    }
    return false;
  }

  return {
    init,
    getBaseUrl,
    health,
    ready,
    waitForBackend,
    getQueue,
    addToQueue,
    removeFromQueue,
    clearQueue,
    present,
    presentTheme,
    clearDisplay,
    clearDetections,
    setMode,
    setTranscriptionSource,
    search,
    bibleChapter,
    connectWS,
    disconnectWS,
    onWSEvent,
    isWSConnected,
  };
})();

// Make available globally
window.FV = FaithViewAPI;
