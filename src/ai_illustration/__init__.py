"""Local-first software MVP for the AI illustration project."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

__version__ = "1.0.0"


def _install_windows_path_compatibility() -> None:
    """Allow native Windows paths without weakening POSIX path checks.

    Several late pipeline modules deliberately reject backslashes on POSIX,
    where a backslash is a literal filename character rather than a path
    separator. On Windows the same character is the native separator, so the
    original guards rejected every normal absolute path before validation
    could reach the actual containment and symlink checks.
    """

    if os.name != "nt":
        return

    from . import composition as _composition
    from . import frame_preview as _frame_preview
    from . import frame_renderer as _frame_renderer

    def native_path_guard(
        value: Path,
        field: str,
        error_type: Callable[[str, str, str], Exception],
    ) -> Path:
        raw = str(value)
        if "\x00" in raw:
            raise error_type(
                "UNSAFE_PATH",
                f"{field} contains a null byte",
                field,
            )
        expanded = value.expanduser()
        if ".." in expanded.parts:
            raise error_type(
                "UNSAFE_PATH",
                f"{field} must not contain parent traversal",
                field,
            )
        return expanded

    def composition_guard(value: Path, field: str) -> Path:
        return native_path_guard(value, field, _composition.CompositionError)

    def frame_preview_guard(value: Path, field: str) -> Path:
        return native_path_guard(value, field, _frame_preview.FramePreviewError)

    def frame_renderer_guard(value: Path, field: str) -> Path:
        return native_path_guard(value, field, _frame_renderer.FrameRenderError)

    _composition._reject_lexical_path = composition_guard
    _frame_preview._reject_lexical = frame_preview_guard
    _frame_renderer._reject_lexical = frame_renderer_guard


_install_windows_path_compatibility()
del _install_windows_path_compatibility
