/**
 * FaithView Pro — Display Manager
 *
 * Manages the output/projector window communication.
 * Handles monitor detection, fullscreen, and output control.
 */

const DisplayManager = (() => {
  let _outputWindow = null;
  let _isFullscreen = false;

  // ---- Tauri integration (desktop mode) ----

  async function _isTauri() {
    return window.__TAURI__ !== undefined;
  }

  async function showOutput() {
    if (await _isTauri()) {
      await window.__TAURI__.core.invoke('show_output_window');
    } else {
      // Browser fallback — open output.html in new window
      if (!_outputWindow || _outputWindow.closed) {
        _outputWindow = window.open('output.html', 'faithview_output',
          'width=1920,height=1080,fullscreen=yes');
      } else {
        _outputWindow.focus();
      }
    }
  }

  async function hideOutput() {
    if (await _isTauri()) {
      await window.__TAURI__.core.invoke('hide_output_window');
    } else if (_outputWindow && !_outputWindow.closed) {
      _outputWindow.close();
    }
  }

  async function toggleFullscreen() {
    if (await _isTauri()) {
      _isFullscreen = await window.__TAURI__.core.invoke('toggle_output_fullscreen');
    } else if (_outputWindow && !_outputWindow.closed) {
      if (_outputWindow.document.fullscreenElement) {
        _outputWindow.document.exitFullscreen();
        _isFullscreen = false;
      } else {
        _outputWindow.document.documentElement.requestFullscreen();
        _isFullscreen = true;
      }
    }
    return _isFullscreen;
  }

  async function isFullscreen() {
    if (await _isTauri()) {
      return await window.__TAURI__.core.invoke('get_output_fullscreen');
    }
    return _isFullscreen;
  }

  // ---- Presentation ----

  function presentToOutput(displayEvent) {
    // Send via WebSocket to the output window
    if (window.FV && window.FV.isWSConnected()) {
      // The output window connects to the same WS and receives the same events
      // No need to send separately — the WS broadcast handles it
    }
    // Also postMessage to output window if open
    if (_outputWindow && !_outputWindow.closed) {
      _outputWindow.postMessage({ type: 'display_update', ...displayEvent }, '*');
    }
  }

  function clearOutput() {
    if (_outputWindow && !_outputWindow.closed) {
      _outputWindow.postMessage({ type: 'display_update', clear: true }, '*');
    }
  }

  function blackScreen() {
    if (_outputWindow && !_outputWindow.closed) {
      _outputWindow.postMessage({ type: 'black_screen' }, '*');
    }
  }

  return {
    showOutput,
    hideOutput,
    toggleFullscreen,
    isFullscreen,
    presentToOutput,
    clearOutput,
    blackScreen,
  };
})();

window.FVDisplay = DisplayManager;
