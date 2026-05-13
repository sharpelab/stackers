"""Shared widget helpers for the air-stacker GUI."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton


def action_button(text: str) -> QPushButton:
    """QPushButton with keyboard focus disabled — the default for
    consequential action buttons in instrument panels.

    Prevents Qt's tab chain from landing on Move / Go / Home / Reset /
    Enable / Disable / STOP / Run when a focused widget elsewhere gets
    disabled (which is exactly what happens when a jog button disables
    itself mid-motion). Only +/− jog buttons — constructed via plain
    QPushButton — stay spacebar-activatable; everything else routes
    spacebar to a spinbox (where Space is a no-op)."""
    btn = QPushButton(text)
    btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    return btn
