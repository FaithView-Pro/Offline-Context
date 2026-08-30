"""Cross-platform path resolver for FaithView Pro.

Resolves paths for:
  - bundled resources (PyInstaller / Nuitka)
  - development mode (running from source)
  - user data (settings, themes, logs, models)
  - platform-specific directories

Usage:
    from paths import Paths
    p = Paths()
    print(p.onnx_model)       # -> /path/to/onnx_model/model.onnx
    print(p.user_data_dir)    # -> ~/.local/share/FaithViewPro  (Linux)
"""

from __future__ import annotations

import os
import sys
import platform
from pathlib import Path
from typing import Optional


def _is_frozen() -> bool:
    """True when running as a PyInstaller/Nuitka bundle."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def _bundle_dir() -> Path:
    """Base directory of the bundled application."""
    if _is_frozen():
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def _exe_dir() -> Path:
    """Directory containing the executable (may differ from _MEIPASS)."""
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


class Paths:
    """Centralized path resolver for FaithView Pro.

    In development mode, resources are relative to this file's directory.
    In bundled mode, resources are in the PyInstaller temp dir (_MEIPASS)
    and user data is in the platform-appropriate location.
    """

    def __init__(self, app_name: str = "FaithViewPro"):
        self.app_name = app_name
        self._bundle = _bundle_dir()
        self._exe = _exe_dir()
        self._dev_root = Path(__file__).resolve().parent

    # ---- Resource paths (bundled or development) ----

    @property
    def resource_root(self) -> Path:
        """Root directory for bundled resources (bible, models, index)."""
        if _is_frozen():
            return self._bundle
        # In dev mode, resources are in the project root (parent of backend/)
        return self._dev_root.parent

    @property
    def bible_dir(self) -> Path:
        p = self.resource_root / "resources" / "bible"
        if p.exists():
            return p
        return self.resource_root

    @property
    def bible_all_versions(self) -> Path:
        # Check project root first, then versions/ subdirectory, then resources/
        for candidate in [
            self.resource_root / "bible_all_versions.json",
            self.resource_root / "versions" / "bible_all_versions.json",
            self.resource_root / "resources" / "bible" / "bible_all_versions.json",
        ]:
            if candidate.exists():
                return candidate
        return self.resource_root / "bible_all_versions.json"

    @property
    def nkjv_json(self) -> Path:
        p = self.resource_root / "nkjv.json"
        if p.exists():
            return p
        return self.resource_root / "resources" / "bible" / "nkjv.json"

    @property
    def amplified_json(self) -> Path:
        p = self.resource_root / "amplified.json"
        if p.exists():
            return p
        return self.resource_root / "resources" / "bible" / "amplified.json"

    @property
    def onnx_dir(self) -> Path:
        # Check project root first, then resources subdirectory
        p = self.resource_root / "onnx_model"
        if (p / "model.onnx").exists():
            return p
        return self.resource_root / "resources" / "onnx"

    @property
    def onnx_model(self) -> Path:
        p = self.onnx_dir / "model.onnx"
        if p.exists():
            return p
        return self.resource_root / "onnx_model" / "model.onnx"

    @property
    def onnx_tokenizer(self) -> Path:
        return self.onnx_dir

    @property
    def index_dir(self) -> Path:
        # Check project root first, then resources subdirectory
        p = self.resource_root / "index"
        if (p / "verses.faiss").exists():
            return p
        return self.resource_root / "resources" / "index"

    @property
    def faiss_index(self) -> Path:
        return self.index_dir / "verses.faiss"

    @property
    def faiss_meta(self) -> Path:
        return self.index_dir / "verses_meta.json"

    @property
    def static_dir(self) -> Path:
        """Frontend static files (for development server)."""
        return self._dev_root / "static"

    # ---- User data paths (platform-specific) ----

    @property
    def user_data_dir(self) -> Path:
        """Platform-appropriate user data directory."""
        system = platform.system()
        if system == "Windows":
            base = os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        elif system == "Darwin":
            base = Path.home() / "Library" / "Application Support"
        else:  # Linux
            base = os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
        return Path(base) / self.app_name

    @property
    def user_config_dir(self) -> Path:
        """Platform-appropriate user config directory."""
        system = platform.system()
        if system == "Windows":
            base = os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        elif system == "Darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
        return Path(base) / self.app_name

    @property
    def log_dir(self) -> Path:
        system = platform.system()
        if system == "Windows":
            base = os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        elif system == "Darwin":
            base = Path.home() / "Library" / "Logs"
        else:
            base = os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
        return Path(base) / self.app_name

    @property
    def models_dir(self) -> Path:
        """Whisper model cache directory."""
        return self.user_data_dir / "models"

    @property
    def themes_file(self) -> Path:
        return self.user_data_dir / "themes.json"

    @property
    def settings_file(self) -> Path:
        return self.user_config_dir / "settings.json"

    @property
    def user_resources_dir(self) -> Path:
        """User-uploaded resources (backgrounds, logos)."""
        return self.user_data_dir / "resources"

    # ---- Ensure directories exist ----

    def ensure_dirs(self) -> None:
        """Create all user data directories if they don't exist."""
        for d in [
            self.user_data_dir,
            self.user_config_dir,
            self.log_dir,
            self.models_dir,
            self.user_resources_dir,
        ]:
            d.mkdir(parents=True, exist_ok=True)

    # ---- Development helper ----

    def resolve_for_config(self) -> dict:
        """Return a dict of all resolved paths (for debugging/config)."""
        return {
            "resource_root": str(self.resource_root),
            "bible_all_versions": str(self.bible_all_versions),
            "onnx_model": str(self.onnx_model),
            "faiss_index": str(self.faiss_index),
            "faiss_meta": str(self.faiss_meta),
            "index_dir": str(self.index_dir),
            "user_data_dir": str(self.user_data_dir),
            "user_config_dir": str(self.user_config_dir),
            "log_dir": str(self.log_dir),
            "models_dir": str(self.models_dir),
            "themes_file": str(self.themes_file),
            "settings_file": str(self.settings_file),
            "static_dir": str(self.static_dir),
            "frozen": _is_frozen(),
        }


# Module-level singleton for convenience
_paths: Optional[Paths] = None


def get_paths() -> Paths:
    global _paths
    if _paths is None:
        _paths = Paths()
    return _paths
