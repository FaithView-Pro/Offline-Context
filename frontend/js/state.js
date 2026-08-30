/**
 * FaithView Pro — Application State Manager
 *
 * Centralized state for the desktop app. Persists to localStorage (dev)
 * and to Tauri fs store (desktop). Replaces scattered localStorage usage.
 */

const FaithViewState = (() => {
  const STORAGE_KEY = 'faithview-state';

  // Default state
  const defaults = {
    // Transcription
    transcriptionEngine: 'whisper',
    whisperModel: 'small.en',
    deepgramApiKey: '',

    // Operator mode
    mode: 'semi_autopilot',  // 'autopilot' | 'semi_autopilot' | 'manual'

    // Confidence thresholds
    quoteThreshold: 0.40,
    autopilotThreshold: 0.60,
    reviewThreshold: 0.40,
    liveConfidenceFloor: 0.45,

    // Audio
    selectedMicrophone: null,
    audioDevice: null,

    // Display / Output
    selectedOutputMonitor: null,
    outputFullscreen: false,

    // Theme
    activeThemeId: null,

    // Window state
    operatorWindowWidth: 1400,
    operatorWindowHeight: 900,

    // UI
    darkMode: true,
    sidebarCollapsed: false,

    // First-run
    firstRun: true,
    setupCompleted: false,
  };

  let _state = { ...defaults };
  let _listeners = {};

  // ---- Persistence ----

  function load() {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      if (stored) {
        const parsed = JSON.parse(stored);
        _state = { ...defaults, ...parsed };
      }
    } catch (e) {
      console.warn('[state] failed to load:', e);
      _state = { ...defaults };
    }
  }

  function save() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(_state));
    } catch (e) {
      console.warn('[state] failed to save:', e);
    }
  }

  // ---- Getters / Setters ----

  function get(key) {
    return _state[key];
  }

  function set(key, value) {
    const old = _state[key];
    _state[key] = value;
    save();
    // Notify listeners
    if (_listeners[key]) {
      _listeners[key].forEach(cb => cb(value, old));
    }
  }

  function getAll() {
    return { ..._state };
  }

  function reset() {
    _state = { ...defaults };
    save();
    _notifyAll();
  }

  // ---- Listeners ----

  function on(key, callback) {
    if (!_listeners[key]) _listeners[key] = [];
    _listeners[key].push(callback);
    return () => {
      _listeners[key] = _listeners[key].filter(cb => cb !== callback);
    };
  }

  function _notifyAll() {
    for (const [key, cbs] of Object.entries(_listeners)) {
      cbs.forEach(cb => cb(_state[key], _state[key]));
    }
  }

  // ---- Theme helpers ----

  function getThemes() {
    try {
      return JSON.parse(localStorage.getItem('faithview-themes') || '[]');
    } catch {
      return [];
    }
  }

  function saveThemes(themes) {
    localStorage.setItem('faithview-themes', JSON.stringify(themes));
  }

  function getActiveTheme() {
    const id = _state.activeThemeId;
    if (!id) return null;
    return getThemes().find(t => t.id === id) || null;
  }

  // ---- Migration from old localStorage keys ----

  function migrateFromLegacy() {
    // Migrate theme data
    const oldThemes = localStorage.getItem('faithview-themes');
    if (oldThemes) {
      try {
        const themes = JSON.parse(oldThemes);
        if (Array.isArray(themes) && themes.length > 0) {
          console.log(`[state] migrated ${themes.length} themes from legacy storage`);
        }
      } catch {}
    }

    // Migrate dark mode
    const darkMode = localStorage.getItem('fv-dark-mode');
    if (darkMode !== null) {
      set('darkMode', darkMode === 'true');
      localStorage.removeItem('fv-dark-mode');
    }

    // Mark first run as complete
    if (_state.firstRun) {
      set('firstRun', false);
    }
  }

  // Initialize
  load();
  migrateFromLegacy();

  return {
    get,
    set,
    getAll,
    reset,
    on,
    getThemes,
    saveThemes,
    getActiveTheme,
    defaults,
  };
})();

window.FVState = FaithViewState;
