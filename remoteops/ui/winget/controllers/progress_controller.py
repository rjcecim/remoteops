"""Barra de progresso por item e por operação (download / instalação)."""

from __future__ import annotations

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QLabel, QProgressBar, QWidget


class ProgressController:
    """Controla as barras de progresso e rótulos do painel de execução."""

    def __init__(
        self,
        *,
        parent: QWidget,
        lbl_step: QLabel,
        lbl_current: QLabel,
        pb_current: QProgressBar,
        pb_total: QProgressBar,
    ) -> None:
        self._parent = parent
        self.lbl_step = lbl_step
        self.lbl_current = lbl_current
        self.pb_current = pb_current
        self.pb_total = pb_total

        self._timer: QTimer | None = None
        self._step_target = 0
        self._seen_real = False
        self.phase: str | None = None
        self.item_in_progress = False
        self.current_pkg_id = ""
        self.current_pkg_idx = 0
        self.current_pkg_total = 0

    def reset(self) -> None:
        self._stop_animation()
        self._seen_real = False
        self.phase = None
        self.item_in_progress = False
        self.current_pkg_id = ""
        self.current_pkg_idx = 0
        self.current_pkg_total = 0
        self.lbl_step.setText("0 de 0 (00/00)")
        self.lbl_current.setText("-")
        self.pb_current.setRange(0, 100)
        self.pb_current.setValue(0)
        self.pb_current.setFormat("%p%")
        self.pb_total.setValue(0)

    def begin_exec(self, *, exec_ids: list[str]) -> None:
        self.reset()
        self.current_pkg_total = max(len(exec_ids), 1)

    def on_item_started(self, idx: int, total: int, package_id: str) -> None:
        self.current_pkg_id = package_id
        self.current_pkg_idx = idx
        self.current_pkg_total = total
        self.lbl_step.setText(f"{idx} de {total} ({idx:02d}/{total:02d})")
        self.lbl_current.setText(package_id)
        self._seen_real = False
        self.phase = None
        self.item_in_progress = True
        self.pb_current.setRange(0, 100)
        self.pb_current.setValue(0)
        self.pb_current.setFormat("%p%")
        self.pb_total.setValue(int(((idx - 1) / max(total, 1)) * 100))

    def on_item_finished(self, idx: int, total: int, package_id: str, status_suffix: str, *, stream_done: bool) -> None:
        self.item_in_progress = False
        self.phase = None
        self.lbl_step.setText(f"{idx} de {total} ({idx:02d}/{total:02d})")
        self.lbl_current.setText(f"{package_id} — {status_suffix}")
        self.pb_total.setValue(int((idx / max(total, 1)) * 100))
        if not stream_done:
            self.pb_current.setRange(0, 100)
            self.pb_current.setValue(100)
            self.pb_current.setFormat("%p%")
            self._stop_animation()

    def on_download_start(self) -> None:
        if not self.item_in_progress or self.phase == "install":
            return
        self.phase = "download"
        self.pb_current.setRange(0, 100)
        self.pb_current.setValue(0)
        self.pb_current.setFormat("%p%")
        label = self.current_pkg_id or self.lbl_current.text().split(" — ")[0]
        self.lbl_current.setText(label)
        if not self._seen_real:
            self._start_animation()

    def on_percent(self, pct: int) -> None:
        if self.phase == "install":
            return
        self._seen_real = True
        self._stop_animation()
        pct = max(0, min(100, int(pct)))
        self.phase = "download"
        if self.pb_current.maximum() == 0:
            self.pb_current.setRange(0, 100)
        self.pb_current.setValue(pct)
        self.pb_current.setFormat("%p%")

    def on_install_start(self) -> None:
        if self.phase == "install" or not self.item_in_progress:
            return
        self.phase = "install"
        self._seen_real = True
        self._stop_animation()
        self.pb_current.setRange(0, 0)
        self.pb_current.setFormat("Instalando…")

    def finalize_stream_item(self, package_id: str, hint: str) -> None:
        self._stop_animation()
        self.phase = None
        self.item_in_progress = False
        self.pb_current.setRange(0, 100)
        self.pb_current.setValue(100)
        self.pb_current.setFormat("%p%")
        idx = self.current_pkg_idx
        total = self.current_pkg_total
        self.pb_total.setValue(int((idx / max(total, 1)) * 100))
        self.lbl_step.setText(f"{idx} de {total} ({idx:02d}/{total:02d})")
        self.lbl_current.setText(f"{package_id} — {hint}" if hint else package_id)

    def stop_animation(self) -> None:
        self._stop_animation()

    def complete_exec(self, *, item_count: int) -> None:
        self._stop_animation()
        self.item_in_progress = False
        self.phase = None
        if item_count:
            self.pb_current.setRange(0, 100)
            self.pb_current.setValue(100)
            self.pb_current.setFormat("%p%")
            self.pb_total.setValue(100)
            self.lbl_step.setText(
                f"{item_count} de {item_count} ({item_count:02d}/{item_count:02d})"
            )
        else:
            self.pb_current.setValue(0)
            self.pb_total.setValue(0)
            self.lbl_step.setText("0 de 0 (00/00)")
        self.lbl_current.setText("Concluído")

    def _start_animation(self) -> None:
        self._stop_animation()
        if self._seen_real or self.phase != "download":
            return
        self._step_target = 92
        timer = QTimer(self._parent)
        timer.setInterval(200)

        def tick() -> None:
            v = self.pb_current.value()
            if v < self._step_target:
                self.pb_current.setValue(min(self._step_target, v + 5))

        timer.timeout.connect(tick)
        timer.start()
        self._timer = timer

    def _stop_animation(self) -> None:
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:
                pass
        self._timer = None
