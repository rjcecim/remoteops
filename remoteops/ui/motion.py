"""Animações leves, desligáveis via ``style.ANIMATIONS_ENABLED``."""

from __future__ import annotations

from PyQt6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QPropertyAnimation,
    QParallelAnimationGroup,
)
from PyQt6.QtWidgets import QGraphicsOpacityEffect, QWidget

from remoteops.ui.style import ANIM_PAGE, anim_ms, animations_enabled


def fade_in(
    widget: QWidget | None,
    *,
    duration_ms: int = ANIM_PAGE,
    start: float = 0.45,
    end: float = 1.0,
) -> QPropertyAnimation | None:
    """Fade curto no widget. Não usar em streaming de log."""
    if widget is None or not animations_enabled() or not widget.isVisible():
        return None
    ms = anim_ms(duration_ms)
    if ms <= 0:
        return None
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setDuration(ms)
    anim.setStartValue(start)
    anim.setEndValue(end)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _clear() -> None:
        if widget.graphicsEffect() is effect:
            widget.setGraphicsEffect(None)

    anim.finished.connect(_clear)
    anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
    return anim


def animate_int(
    target,
    property_name: bytes,
    start: int,
    end: int,
    *,
    duration_ms: int,
    easing: QEasingCurve.Type = QEasingCurve.Type.OutCubic,
    on_finished=None,
) -> QPropertyAnimation | None:
    ms = anim_ms(duration_ms)
    if ms <= 0 or start == end:
        if on_finished is not None:
            on_finished()
        return None
    anim = QPropertyAnimation(target, property_name, target)
    anim.setDuration(ms)
    anim.setStartValue(int(start))
    anim.setEndValue(int(end))
    anim.setEasingCurve(easing)
    if on_finished is not None:
        anim.finished.connect(on_finished)
    anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
    return anim


def parallel_group(*anims: QPropertyAnimation | None, parent=None) -> QParallelAnimationGroup | None:
    live = [a for a in anims if a is not None]
    if not live:
        return None
    group = QParallelAnimationGroup(parent)
    for a in live:
        group.addAnimation(a)
    return group
