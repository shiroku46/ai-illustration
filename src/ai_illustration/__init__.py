"""Local-first software MVP for the AI illustration project."""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Callable

__version__ = "1.0.0"


def _install_windows_path_compatibility() -> None:
    """Normalize native Windows aliases without weakening path security.

    Windows may expose one directory through both an 8.3 short path (for
    example ``RUNNER~1``) and its long path (for example ``runneradmin``).
    Lexical ``Path.relative_to`` rejects those equivalent spellings. The
    compatibility layer rejects traversal and symlink components first, then
    compares resolved physical paths. POSIX behavior is left unchanged.
    """

    if os.name != "nt":
        return

    from . import audio_preview as _audio_preview
    from . import composition as _composition
    from . import frame_preview as _frame_preview
    from . import frame_renderer as _frame_renderer
    from . import render_plan as _render_plan
    from . import video_export_common as _video_export_common

    ErrorFactory = Callable[[str, str, str], Exception]
    SafeFile = Callable[[Path, str, str], tuple[str, Path]]

    def native_path_guard(
        value: Path,
        field: str,
        error_type: ErrorFactory,
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

    def reject_symlink_components(
        path: Path,
        field: str,
        error_type: ErrorFactory,
    ) -> None:
        lexical = path if path.is_absolute() else Path.cwd() / path
        for candidate in (lexical, *lexical.parents):
            try:
                if candidate.exists() and candidate.is_symlink():
                    raise error_type(
                        "PATH_SYMLINK",
                        f"{field} contains a symlink component",
                        field,
                    )
            except OSError as exc:
                raise error_type("PATH_ERROR", str(exc), field) from exc

    def canonical_relative_file(
        value: Path,
        root: Path,
        field: str,
        *,
        guard: Callable[[Path, str], Path],
        safe_file: SafeFile,
        error_type: ErrorFactory,
    ) -> tuple[str, Path]:
        expanded = guard(value, field)
        reject_symlink_components(expanded, field, error_type)
        lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
        try:
            canonical_root = root.resolve(strict=True)
            canonical_file = lexical.resolve(strict=True)
            relative = canonical_file.relative_to(canonical_root).as_posix()
        except ValueError as exc:
            raise error_type(
                "PATH_ESCAPE",
                f"{field} must be beneath its configured root",
                field,
            ) from exc
        except OSError as exc:
            raise error_type("FILE_MISSING", str(exc), field) from exc
        return safe_file(canonical_root, relative, field)

    def composition_guard(value: Path, field: str) -> Path:
        return native_path_guard(value, field, _composition.CompositionError)

    def frame_preview_guard(value: Path, field: str) -> Path:
        return native_path_guard(value, field, _frame_preview.FramePreviewError)

    def frame_renderer_guard(value: Path, field: str) -> Path:
        return native_path_guard(value, field, _frame_renderer.FrameRenderError)

    def audio_guard(value: Path, field: str) -> Path:
        return native_path_guard(value, field, _audio_preview.AudioPreviewError)

    def render_plan_guard(value: Path, field: str) -> Path:
        return native_path_guard(value, field, _render_plan.RenderPlanError)

    def video_guard(value: Path, field: str) -> Path:
        return _video_export_common._reject_native_lexical(value, field)

    def composition_relative(
        value: Path, root: Path, field: str
    ) -> tuple[str, Path]:
        return canonical_relative_file(
            value,
            root,
            field,
            guard=composition_guard,
            safe_file=_composition._safe_existing_file,
            error_type=_composition.CompositionError,
        )

    def frame_preview_relative(
        value: Path, root: Path, field: str
    ) -> tuple[str, Path]:
        return canonical_relative_file(
            value,
            root,
            field,
            guard=frame_preview_guard,
            safe_file=_frame_preview._safe_file,
            error_type=_frame_preview.FramePreviewError,
        )

    def frame_renderer_relative(
        value: Path, root: Path, field: str
    ) -> tuple[str, Path]:
        return canonical_relative_file(
            value,
            root,
            field,
            guard=frame_renderer_guard,
            safe_file=_frame_renderer._safe_file,
            error_type=_frame_renderer.FrameRenderError,
        )

    def audio_relative(
        value: Path, root: Path, field: str
    ) -> tuple[str, Path]:
        return canonical_relative_file(
            value,
            root,
            field,
            guard=audio_guard,
            safe_file=_audio_preview._safe_existing_file,
            error_type=_audio_preview.AudioPreviewError,
        )

    def render_plan_relative(
        value: Path, root: Path, field: str
    ) -> tuple[str, Path]:
        return canonical_relative_file(
            value,
            root,
            field,
            guard=render_plan_guard,
            safe_file=_render_plan._safe_existing_file,
            error_type=_render_plan.RenderPlanError,
        )

    def video_relative(
        value: Path, root: Path, field: str
    ) -> tuple[str, Path]:
        return canonical_relative_file(
            value,
            root,
            field,
            guard=video_guard,
            safe_file=_video_export_common._safe_file,
            error_type=_video_export_common.VideoExportError,
        )

    _composition._reject_lexical_path = composition_guard
    _composition._lexical_file_under_root = composition_relative
    _frame_preview._reject_lexical = frame_preview_guard
    _frame_preview._relative_file = frame_preview_relative
    _frame_renderer._reject_lexical = frame_renderer_guard
    _frame_renderer._relative_file = frame_renderer_relative
    _audio_preview._lexical_file_under_root = audio_relative
    _render_plan._lexical_file_under_root = render_plan_relative
    _video_export_common._relative_file = video_relative

    # Import downstream modules only after common helpers are patched, because
    # some modules bind private helper references at import time.
    from . import video_export_source as _video_export_source

    _video_export_source._relative_file = video_relative

    from . import workspace_checks as _workspace_checks

    windows_absolute = re.compile(
        r"(?i)(?P<path>[a-z]:[\\/][^\s\"']+)"
    )

    def sanitize_path_text(text: str, workspace_root: Path) -> str:
        canonical_root = workspace_root.resolve(strict=False)
        result = text
        matches = list(windows_absolute.finditer(text))
        for match in reversed(matches):
            raw = match.group("path")
            trimmed = raw.rstrip(".,;:)]}")
            suffix = raw[len(trimmed) :]
            try:
                candidate = Path(trimmed).resolve(strict=False)
                relative = candidate.relative_to(canonical_root)
            except (OSError, ValueError):
                continue
            replacement = "."
            if relative.parts:
                replacement += "/" + relative.as_posix()
            start, end = match.span("path")
            result = result[:start] + replacement + suffix + result[end:]
        for variant in {
            str(workspace_root),
            str(canonical_root),
            str(workspace_root).replace("/", "\\"),
            str(canonical_root).replace("/", "\\"),
        }:
            result = result.replace(variant, ".")
        return result

    def workspace_diagnostic(
        exc: Exception, workspace_root: Path
    ) -> dict[str, str]:
        code = getattr(exc, "code", exc.__class__.__name__.upper())
        field = str(getattr(exc, "field", ""))
        message = str(getattr(exc, "message", str(exc)))
        return {
            "code": str(code),
            "message": sanitize_path_text(message, workspace_root),
            "field": sanitize_path_text(field, workspace_root),
        }

    _workspace_checks._diagnostic = workspace_diagnostic


_install_windows_path_compatibility()
del _install_windows_path_compatibility
