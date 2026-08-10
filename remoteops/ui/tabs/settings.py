from __future__ import annotations

import os
import subprocess
from typing import List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from remoteops.ui.branding import APP_DISPLAY_NAME, APP_NAME, APP_VERSION
from remoteops.ui.style import SIZE_UI_SMALL, make_icon_button
from remoteops.ui.widgets.card import (
    CardWidget,
    add_row,
    add_row_full_width,
    finish_card_stack,
    grid_in_card,
    make_card_stack,
)
from remoteops.utils.app_logging import (
    get_log_dir,
    is_file_logging_enabled,
    set_file_logging_enabled,
)
from remoteops.utils.app_settings import SETTINGS_SAVE_ERROR_MSG, SettingsWriteError
from remoteops.utils.hosts import default_hosts_path, load_hosts_file
from remoteops.utils.pstools import (
    DEFAULT_PSTOOLS_DIR,
    get_pstools_dir,
    probe_pstools,
    probe_rustdesk_local,
    set_pstools_dir,
)
from remoteops.utils.search_settings import (
    DEFAULT_SEARCH_MAX_WORKERS,
    MAX_SEARCH_MAX_WORKERS,
    MIN_SEARCH_MAX_WORKERS,
    get_search_max_workers,
    resolve_configured_hosts_path,
    set_search_hosts_path,
    set_search_max_workers,
)

from remoteops.ui.widgets.status_dot import STATUS_COLORS as _STATUS_COLORS
from remoteops.ui.widgets.status_dot import StatusDot as _StatusDot


def _caption(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("settingsCaption")
    lbl.setWordWrap(True)
    lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    lbl.setMinimumWidth(0)
    lbl.setStyleSheet(
        f"QLabel#settingsCaption {{ color: palette(mid); font-size: {SIZE_UI_SMALL}pt; }}"
    )
    return lbl


def _add_caption(grid, row: int, text: str) -> None:
    """Legenda em largura total (sem AlignLeft, senão o texto é cortado)."""
    grid.addWidget(_caption(text), row, 0, 1, 2)


def _open_in_explorer(path: str) -> None:
    target = (path or "").strip()
    if not target:
        return
    try:
        if os.path.isdir(target):
            os.startfile(target)  # type: ignore[attr-defined]
        elif os.path.isfile(target):
            subprocess.run(["explorer", "/select,", target], check=False)
        else:
            parent = os.path.dirname(target) or target
            if os.path.isdir(parent):
                os.startfile(parent)  # type: ignore[attr-defined]
    except Exception:
        pass


class SettingsTab(QWidget):
    """Aba de configurações do aplicativo."""

    pstoolsPathChanged = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._tool_rows: List[tuple[QWidget, QLabel, QLabel]] = []

        root = make_card_stack(self)

        # ── Card 1 — PSTools ──────────────────────────────────────────────────
        card_ps = CardWidget("\uE8B7", self.tr("PSTools"))
        card_ps.set_collapsible(True, collapsed=False)
        g1 = grid_in_card(card_ps)
        row = 0

        path_row = QHBoxLayout()
        path_row.setSpacing(4)
        path_row.setContentsMargins(0, 0, 0, 0)
        self.pstools_edit = QLineEdit()
        self.pstools_edit.setReadOnly(True)
        self.pstools_edit.setText(get_pstools_dir())
        self.pstools_edit.setToolTip(self.tr("Pasta onde estão PsExec, PsInfo e utilitários"))
        self.pstools_browse_btn = make_icon_button("\uED25", self.tr("Alterar pasta PSTools"))
        self.pstools_browse_btn.clicked.connect(self._browse_pstools)
        self.pstools_open_btn = make_icon_button("\uED43", self.tr("Abrir pasta no Explorer"))
        self.pstools_open_btn.clicked.connect(self._open_pstools_folder)
        self.pstools_refresh_btn = make_icon_button("\uE72C", self.tr("Atualizar status"))
        self.pstools_refresh_btn.clicked.connect(self.refresh_pstools_status)
        self.pstools_reset_btn = make_icon_button("\uE777", self.tr("Restaurar padrão C:\\PSTools"))
        self.pstools_reset_btn.clicked.connect(self._reset_pstools)
        path_row.addWidget(self.pstools_edit, 1)
        path_row.addWidget(self.pstools_browse_btn)
        path_row.addWidget(self.pstools_open_btn)
        path_row.addWidget(self.pstools_refresh_btn)
        path_row.addWidget(self.pstools_reset_btn)
        path_wrap = QWidget()
        path_wrap.setLayout(path_row)
        path_wrap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        add_row(g1, row, self.tr("Caminho"), path_wrap)
        row += 1

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        status_row.setContentsMargins(2, 0, 0, 0)
        self.pstools_status_dot = _StatusDot()
        self.pstools_status_label = QLabel()
        self.pstools_status_label.setObjectName("pstoolsStatus")
        self.pstools_status_label.setStyleSheet(
            f"QLabel#pstoolsStatus {{ color: palette(mid); font-size: {SIZE_UI_SMALL}pt; }}"
        )
        status_row.addWidget(self.pstools_status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        status_row.addWidget(self.pstools_status_label, 0, Qt.AlignmentFlag.AlignVCenter)
        status_row.addStretch()
        status_wrap = QWidget()
        status_wrap.setLayout(status_row)
        add_row(g1, row, self.tr("Status"), status_wrap)
        row += 1

        tools_box = QVBoxLayout()
        tools_box.setSpacing(4)
        tools_box.setContentsMargins(0, 2, 0, 0)
        for _ in range(2):
            line = QHBoxLayout()
            line.setSpacing(8)
            line.setContentsMargins(0, 0, 0, 0)
            dot = _StatusDot(diameter=8)
            name = QLabel()
            name.setMinimumWidth(72)
            detail = QLabel()
            detail.setObjectName("toolDetail")
            detail.setStyleSheet(
                f"QLabel#toolDetail {{ color: palette(mid); font-size: {SIZE_UI_SMALL}pt; }}"
            )
            detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            line.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)
            line.addWidget(name, 0, Qt.AlignmentFlag.AlignVCenter)
            line.addWidget(detail, 1, Qt.AlignmentFlag.AlignVCenter)
            wrap = QWidget()
            wrap.setLayout(line)
            tools_box.addWidget(wrap)
            self._tool_rows.append((dot, name, detail))
        tools_wrap = QWidget()
        tools_wrap.setLayout(tools_box)
        add_row(g1, row, self.tr("Ferramentas"), tools_wrap)

        root.addWidget(card_ps)

        # ── Card 2 — RustDesk (Program Files, não PSTools) ────────────────────
        card_rd = CardWidget("\uE774", self.tr("RustDesk"))
        card_rd.set_collapsible(True, collapsed=False)
        g_rd = grid_in_card(card_rd)

        rd_status_row = QHBoxLayout()
        rd_status_row.setSpacing(8)
        rd_status_row.setContentsMargins(2, 0, 0, 0)
        self.rustdesk_status_dot = _StatusDot()
        self.rustdesk_status_label = QLabel()
        self.rustdesk_status_label.setObjectName("rustdeskStatus")
        self.rustdesk_status_label.setStyleSheet(
            f"QLabel#rustdeskStatus {{ color: palette(mid); font-size: {SIZE_UI_SMALL}pt; }}"
        )
        rd_status_row.addWidget(self.rustdesk_status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        rd_status_row.addWidget(self.rustdesk_status_label, 0, Qt.AlignmentFlag.AlignVCenter)
        rd_status_row.addStretch()
        rd_status_wrap = QWidget()
        rd_status_wrap.setLayout(rd_status_row)
        add_row(g_rd, 0, self.tr("Status"), rd_status_wrap)

        rd_path_row = QHBoxLayout()
        rd_path_row.setSpacing(4)
        rd_path_row.setContentsMargins(0, 0, 0, 0)
        self.rustdesk_edit = QLineEdit()
        self.rustdesk_edit.setReadOnly(True)
        self.rustdesk_open_btn = make_icon_button("\uED43", self.tr("Abrir pasta do RustDesk"))
        self.rustdesk_open_btn.clicked.connect(self._open_rustdesk_folder)
        self.rustdesk_refresh_btn = make_icon_button("\uE72C", self.tr("Atualizar status do RustDesk"))
        self.rustdesk_refresh_btn.clicked.connect(self.refresh_rustdesk_status)
        rd_path_row.addWidget(self.rustdesk_edit, 1)
        rd_path_row.addWidget(self.rustdesk_open_btn)
        rd_path_row.addWidget(self.rustdesk_refresh_btn)
        rd_path_wrap = QWidget()
        rd_path_wrap.setLayout(rd_path_row)
        add_row(g_rd, 1, self.tr("Caminho"), rd_path_wrap)
        _add_caption(
            g_rd,
            2,
            self.tr(
                "Instalação local em C:\\Program Files\\RustDesk\\ "
                "(não fica na pasta PSTools)."
            ),
        )
        root.addWidget(card_rd)

        # ── Card 3 — Logs ─────────────────────────────────────────────────────
        card_logs = CardWidget("\uE7C3", self.tr("Logs"))
        card_logs.set_collapsible(True, collapsed=False)
        g2 = grid_in_card(card_logs)

        self.log_session_check = QCheckBox(self.tr("Salvar log em arquivo"))
        self.log_session_check.setChecked(is_file_logging_enabled())
        self.log_session_check.setToolTip(
            self.tr("Marque para gravar as operações em arquivo (preferência salva no settings.ini).")
        )
        self.log_session_check.toggled.connect(self._on_log_session_toggled)
        add_row_full_width(g2, 0, self.log_session_check)

        logs_row = QHBoxLayout()
        logs_row.setSpacing(4)
        logs_row.setContentsMargins(0, 0, 0, 0)
        self.logs_edit = QLineEdit()
        self.logs_edit.setReadOnly(True)
        try:
            self.logs_edit.setText(get_log_dir(create=False))
        except Exception:
            self.logs_edit.setText("")
        self.logs_open_btn = make_icon_button("\uED43", self.tr("Abrir pasta de logs"))
        self.logs_open_btn.clicked.connect(self._open_logs_folder)
        logs_row.addWidget(self.logs_edit, 1)
        logs_row.addWidget(self.logs_open_btn)
        logs_wrap = QWidget()
        logs_wrap.setLayout(logs_row)
        add_row(g2, 1, self.tr("Pasta"), logs_wrap)
        _add_caption(
            g2,
            2,
            self.tr(
                "Desmarcado: não salva em disco. "
                "O log na parte de baixo da janela continua aparecendo."
            ),
        )
        root.addWidget(card_logs)

        # ── Card 4 — Pesquisa de aplicativos ──────────────────────────────────
        # \uE721 = Find / Search (mesmo ícone da pesquisa)
        card_search = CardWidget("\uE721", self.tr("Pesquisa de aplicativos"))
        card_search.set_collapsible(True, collapsed=False)
        g3 = grid_in_card(card_search)

        hosts_row = QHBoxLayout()
        hosts_row.setSpacing(4)
        hosts_row.setContentsMargins(0, 0, 0, 0)
        self.hosts_edit = QLineEdit()
        self.hosts_edit.setReadOnly(True)
        self.hosts_edit.setToolTip(self.tr("Arquivo JSON com a lista de computadores"))
        self.hosts_browse_btn = make_icon_button("\uED25", self.tr("Selecionar outro hosts.json"))
        self.hosts_browse_btn.clicked.connect(self._browse_hosts_file)
        self.hosts_open_btn = make_icon_button("\uED43", self.tr("Abrir pasta do hosts.json"))
        self.hosts_open_btn.clicked.connect(lambda: _open_in_explorer(self.hosts_edit.text()))
        self.hosts_reset_btn = make_icon_button(
            "\uE777", self.tr("Restaurar hosts.json padrão do aplicativo")
        )
        self.hosts_reset_btn.clicked.connect(self._reset_hosts_file)
        hosts_row.addWidget(self.hosts_edit, 1)
        hosts_row.addWidget(self.hosts_browse_btn)
        hosts_row.addWidget(self.hosts_open_btn)
        hosts_row.addWidget(self.hosts_reset_btn)
        hosts_wrap = QWidget()
        hosts_wrap.setLayout(hosts_row)
        add_row(g3, 0, self.tr("hosts.json"), hosts_wrap)

        hosts_status_row = QHBoxLayout()
        hosts_status_row.setSpacing(8)
        hosts_status_row.setContentsMargins(2, 0, 0, 0)
        self.hosts_status_dot = _StatusDot()
        self.hosts_status_label = QLabel()
        self.hosts_status_label.setObjectName("hostsStatus")
        self.hosts_status_label.setStyleSheet(
            f"QLabel#hostsStatus {{ color: palette(mid); font-size: {SIZE_UI_SMALL}pt; }}"
        )
        hosts_status_row.addWidget(
            self.hosts_status_dot, 0, Qt.AlignmentFlag.AlignVCenter
        )
        hosts_status_row.addWidget(
            self.hosts_status_label, 0, Qt.AlignmentFlag.AlignVCenter
        )
        hosts_status_row.addStretch()
        hosts_status_wrap = QWidget()
        hosts_status_wrap.setLayout(hosts_status_row)
        add_row(g3, 1, self.tr("Status"), hosts_status_wrap)
        self._refresh_hosts_ui()

        workers_row = QHBoxLayout()
        workers_row.setSpacing(4)
        workers_row.setContentsMargins(0, 0, 0, 0)
        self.search_workers_spin = QSpinBox()
        self.search_workers_spin.setRange(MIN_SEARCH_MAX_WORKERS, MAX_SEARCH_MAX_WORKERS)
        self.search_workers_spin.setSingleStep(1)
        self.search_workers_spin.setToolTip(
            self.tr(
                "Quantidade máxima de computadores consultados ao mesmo tempo "
                f"(padrão {DEFAULT_SEARCH_MAX_WORKERS})."
            )
        )
        self.search_workers_spin.blockSignals(True)
        self.search_workers_spin.setValue(get_search_max_workers())
        self.search_workers_spin.blockSignals(False)
        self.search_workers_spin.valueChanged.connect(self._on_search_workers_changed)
        self.search_workers_reset_btn = make_icon_button(
            "\uE777",
            self.tr(f"Restaurar padrão ({DEFAULT_SEARCH_MAX_WORKERS})"),
        )
        self.search_workers_reset_btn.clicked.connect(self._reset_search_workers)
        workers_row.addWidget(self.search_workers_spin)
        workers_row.addWidget(self.search_workers_reset_btn)
        workers_row.addStretch()
        workers_wrap = QWidget()
        workers_wrap.setLayout(workers_row)
        add_row(g3, 2, self.tr("Consultas simultâneas"), workers_wrap)
        _add_caption(
            g3,
            3,
            self.tr(
                "Define quantos computadores podem ser consultados ao mesmo tempo. "
                "Valores maiores podem acelerar a pesquisa, mas aumentam o número de "
                "conexões simultâneas. A alteração será aplicada na próxima pesquisa."
            ),
        )
        root.addWidget(card_search)

        # ── Card 5 — Sobre ────────────────────────────────────────────────────
        card_about = CardWidget("\uE946", self.tr("Sobre"))
        card_about.set_collapsible(True, collapsed=False)
        g4 = grid_in_card(card_about)
        app_lbl = QLabel(f"{APP_NAME}  ·  v{APP_VERSION}")
        app_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        add_row(g4, 0, self.tr("Aplicativo"), app_lbl)
        desc = QLabel(APP_DISPLAY_NAME)
        desc.setWordWrap(True)
        add_row(g4, 1, self.tr("Descrição"), desc)
        root.addWidget(card_about)
        finish_card_stack(root)

        self.refresh_pstools_status()
        self.refresh_rustdesk_status()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh_pstools_status()
        self.refresh_rustdesk_status()
        self.log_session_check.setChecked(is_file_logging_enabled())
        try:
            self.logs_edit.setText(get_log_dir(create=False))
        except Exception:
            pass
        self._refresh_hosts_ui()
        self.search_workers_spin.blockSignals(True)
        self.search_workers_spin.setValue(get_search_max_workers())
        self.search_workers_spin.blockSignals(False)

    def _refresh_hosts_ui(self) -> None:
        path, origin = resolve_configured_hosts_path()
        self.hosts_edit.setText(path or default_hosts_path())
        self._set_hosts_status(origin, path)

    def _show_settings_save_error(self, exc: BaseException | None = None) -> None:
        msg = SETTINGS_SAVE_ERROR_MSG
        if isinstance(exc, SettingsWriteError) and getattr(exc, "message", None):
            msg = exc.message
        QMessageBox.warning(self, self.tr("Configurações"), self.tr(msg))

    def _browse_hosts_file(self) -> None:
        start = self.hosts_edit.text().strip() or default_hosts_path()
        if start and not os.path.isdir(os.path.dirname(start)):
            start = default_hosts_path()
        path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Selecionar arquivo de hosts"),
            start,
            self.tr("JSON (*.json)"),
        )
        if not path:
            return
        try:
            set_search_hosts_path(path)
        except SettingsWriteError as exc:
            self._show_settings_save_error(exc)
            return
        self._refresh_hosts_ui()

    def _reset_hosts_file(self) -> None:
        try:
            set_search_hosts_path("")
        except SettingsWriteError as exc:
            self._show_settings_save_error(exc)
            return
        self._refresh_hosts_ui()

    def _on_search_workers_changed(self, value: int) -> None:
        try:
            set_search_max_workers(value)
        except SettingsWriteError as exc:
            self._show_settings_save_error(exc)
            self.search_workers_spin.blockSignals(True)
            self.search_workers_spin.setValue(get_search_max_workers())
            self.search_workers_spin.blockSignals(False)

    def _reset_search_workers(self) -> None:
        try:
            normalized = set_search_max_workers(DEFAULT_SEARCH_MAX_WORKERS)
        except SettingsWriteError as exc:
            self._show_settings_save_error(exc)
            return
        self.search_workers_spin.blockSignals(True)
        self.search_workers_spin.setValue(normalized)
        self.search_workers_spin.blockSignals(False)

    def _apply_hosts_status(self, state: str, text: str) -> None:
        color = _STATUS_COLORS.get(state, _STATUS_COLORS["idle"])
        self.hosts_status_dot.set_color(color)
        self.hosts_status_label.setText(text)
        self.hosts_status_dot.setToolTip(text)
        self.hosts_status_label.setToolTip(text)

    def _set_hosts_status(self, origin: str, path: Optional[str]) -> None:
        if origin == "missing" or not path or not os.path.isfile(path):
            self._apply_hosts_status("err", self.tr("Não encontrado"))
            return
        p = os.path.normpath(path)
        if len(p) >= 2 and p[1] == ":":
            p = p[0].upper() + p[1:]
        try:
            hosts = load_hosts_file(p)
        except Exception:
            self._apply_hosts_status("invalid", self.tr("Arquivo inválido"))
            return
        if not hosts:
            self._apply_hosts_status("warn", self.tr("Encontrado — lista vazia"))
            return
        self._apply_hosts_status(
            "ok", self.tr(f"Encontrado — {len(hosts)} host(s)")
        )

    def _browse_pstools(self) -> None:
        start = get_pstools_dir()
        folder = QFileDialog.getExistingDirectory(
            self,
            self.tr("Selecionar pasta PSTools"),
            start if os.path.isdir(start) else DEFAULT_PSTOOLS_DIR,
        )
        if not folder:
            return
        try:
            new_path = set_pstools_dir(folder)
        except SettingsWriteError as exc:
            self._show_settings_save_error(exc)
            return
        self.pstools_edit.setText(new_path)
        self.refresh_pstools_status()
        self.pstoolsPathChanged.emit(new_path)

    def _reset_pstools(self) -> None:
        try:
            new_path = set_pstools_dir(DEFAULT_PSTOOLS_DIR)
        except SettingsWriteError as exc:
            self._show_settings_save_error(exc)
            return
        self.pstools_edit.setText(new_path)
        self.refresh_pstools_status()
        self.pstoolsPathChanged.emit(new_path)

    def _on_log_session_toggled(self, checked: bool) -> None:
        try:
            set_file_logging_enabled(checked)
        except SettingsWriteError as exc:
            self._show_settings_save_error(exc)
            self.log_session_check.blockSignals(True)
            self.log_session_check.setChecked(is_file_logging_enabled())
            self.log_session_check.blockSignals(False)
            return
        # Atualiza caminho (cria pasta só se acabou de habilitar)
        try:
            self.logs_edit.setText(get_log_dir(create=checked))
        except Exception:
            pass

    def _open_logs_folder(self) -> None:
        try:
            path = get_log_dir(create=is_file_logging_enabled())
        except Exception:
            path = self.logs_edit.text()
        self.logs_edit.setText(path)
        _open_in_explorer(path)

    def _open_pstools_folder(self) -> None:
        _open_in_explorer(get_pstools_dir())

    def _open_rustdesk_folder(self) -> None:
        info = probe_rustdesk_local()
        path = str(info.get("path") or "")
        folder = os.path.dirname(path) if path else r"C:\Program Files\RustDesk"
        _open_in_explorer(folder if os.path.isdir(folder) else path)

    def refresh_rustdesk_status(self) -> None:
        info = probe_rustdesk_local()
        path = str(info.get("path") or "")
        self.rustdesk_edit.setText(path)
        if info.get("found"):
            self.rustdesk_status_dot.set_color(_STATUS_COLORS["ok"])
            self.rustdesk_status_label.setText(self.tr("Instalado"))
        else:
            self.rustdesk_status_dot.set_color(_STATUS_COLORS["err"])
            self.rustdesk_status_label.setText(
                self.tr("Não encontrado em C:\\Program Files\\RustDesk\\")
            )

    def refresh_pstools_status(self) -> None:
        info = probe_pstools(get_pstools_dir())
        self.pstools_edit.setText(str(info["dir"]))
        dir_ok = bool(info["dir_ok"])
        healthy = bool(info["healthy"])
        ok_count = int(info["ok_count"])
        total = int(info["total"])

        if healthy:
            self.pstools_status_dot.set_color(_STATUS_COLORS["ok"])
            self.pstools_status_label.setText(
                self.tr(f"Pronto — {ok_count}/{total} ferramentas encontradas")
            )
        elif dir_ok and ok_count > 0:
            self.pstools_status_dot.set_color(_STATUS_COLORS["warn"])
            self.pstools_status_label.setText(
                self.tr(f"Parcial — {ok_count}/{total} ferramentas encontradas")
            )
        elif dir_ok:
            self.pstools_status_dot.set_color(_STATUS_COLORS["err"])
            self.pstools_status_label.setText(self.tr("Pasta existe, mas nenhum binário foi encontrado"))
        else:
            self.pstools_status_dot.set_color(_STATUS_COLORS["err"])
            self.pstools_status_label.setText(self.tr("Pasta PSTools não encontrada"))

        tools = list(info["tools"])
        for idx, (dot, name_lbl, detail_lbl) in enumerate(self._tool_rows):
            if idx >= len(tools):
                name_lbl.setText("")
                detail_lbl.setText("")
                dot.set_color(_STATUS_COLORS["idle"])
                continue
            tool = tools[idx]
            name_lbl.setText(str(tool["label"]))
            if tool["found"]:
                dot.set_color(_STATUS_COLORS["ok"])
                detail_lbl.setText(os.path.basename(str(tool["path"])))
                detail_lbl.setToolTip(str(tool["path"]))
            else:
                dot.set_color(_STATUS_COLORS["err"])
                expected = " / ".join(tool["names"])
                detail_lbl.setText(self.tr(f"Ausente ({expected})"))
                detail_lbl.setToolTip(str(tool["path"]))
