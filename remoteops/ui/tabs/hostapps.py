"""Aba de aplicativos de um único host via Remote Registry."""

from __future__ import annotations

import csv
import datetime
import os
from typing import List, Optional

from PyQt6 import sip
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QWidget,
)

from remoteops.ui.style import (
    COLOR_HOVER,
    COLOR_TEXT,
    COLOR_TEXT_MUTED,
    ICON_FONT_PT,
    RADIUS_SMALL,
    make_icon_button,
    table_frame_qss,
)
from remoteops.ui.widgets.card import CardWidget, make_card_stack, make_field_label
from remoteops.ui.widgets.log import LogOutputWidget
from remoteops.ui.widgets.spinner import DotsSpinner
from remoteops.utils.app_catalog import resolve_uninstall_extras
from remoteops.utils.psinfo import (
    InstalledApp,
    build_uninstall_remote_cmd,
    describe_uninstall,
)
from remoteops.utils.remote_registry_query import (
    get_remote_registry_timeout,
    query_remote_installed_apps,
)


class _HostAppsWorker(QThread):
    finished_ok = pyqtSignal(object)  # list[InstalledApp]
    finished_err = pyqtSignal(str, str)  # error_kind, message

    def __init__(self, host: str, parent=None):
        super().__init__(parent)
        self.host = host
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        status = query_remote_installed_apps(
            self.host,
            timeout=get_remote_registry_timeout(),
            should_cancel=lambda: self._abort,
        )
        if self._abort or status.error_kind == "cancelled":
            return
        if status.ok:
            self.finished_ok.emit(list(status.apps or []))
            return
        self.finished_err.emit(status.error_kind or "internal_error", status.message or "")


class HostAppsTab(QWidget):
    """Lista aplicativos instalados no host atual (Remote Registry)."""

    # host, remote_cmd, rótulo
    uninstallRequested = pyqtSignal(str, str, str)

    def __init__(self, parent=None, host_source: Optional[QLineEdit] = None):
        super().__init__(parent)
        self._host_source = host_source
        self._worker: Optional[_HostAppsWorker] = None
        self._apps: List[InstalledApp] = []
        self._trash_buttons: list = []
        self._loading = False

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root = make_card_stack(self)
        self._bottom_stretch_idx = None

        # Toolbar
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(8)
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color: palette(windowText); opacity: 0.75;")
        self.refresh_btn = make_icon_button(
            "\uE72C", self.tr("Renovar lista de aplicativos"), size=28
        )
        self.refresh_btn.clicked.connect(self.run_inventory)
        toolbar.addWidget(self._status_lbl, 1)
        toolbar.addWidget(self.refresh_btn, 0, Qt.AlignmentFlag.AlignRight)
        toolbar_wrap = QWidget()
        toolbar_wrap.setLayout(toolbar)
        root.addWidget(toolbar_wrap, 0)

        # Card Aplicativos
        self.apps_card = CardWidget("\uE71D", self.tr("Aplicativos"))
        self.apps_card.set_collapsible(True, collapsed=False)
        self.apps_card.set_expanding(True)
        self.apps_card.set_layout_stretch(2)
        self.apps_card.set_downloadable(True)
        self._apps_download_btn = self.apps_card.findChild(QToolButton, "cardDownload")
        if self._apps_download_btn is not None:
            self._apps_download_btn.setToolTip(
                self.tr("Baixar CSV dos aplicativos (respeita o filtro)")
            )
            self._apps_download_btn.setEnabled(False)
        self.apps_card.downloadRequested.connect(self._export_apps_csv)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText(self.tr("Buscar aplicativo..."))
        self.count_lbl = QLabel("")
        self.count_lbl.setStyleSheet("color: palette(windowText); opacity: 0.75;")
        top.addWidget(self.filter_edit, 1)
        top.addWidget(self.count_lbl)
        top_wrap = QWidget()
        top_wrap.setLayout(top)
        self.apps_card.content_layout.addWidget(top_wrap, 0)

        self._spinner = DotsSpinner()
        self._spinner.setVisible(False)
        spin_row = QHBoxLayout()
        spin_row.setContentsMargins(0, 2, 0, 2)
        spin_row.addStretch()
        spin_row.addWidget(self._spinner)
        spin_row.addStretch()
        spin_wrap = QWidget()
        spin_wrap.setLayout(spin_row)
        self._spin_wrap = spin_wrap
        self._spin_wrap.setVisible(False)
        self.apps_card.content_layout.addWidget(self._spin_wrap, 0)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(
            [self.tr("Nome"), self.tr("Editor"), self.tr("Versão"), self.tr("Tipo"), ""]
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(4, 36)
        self.table.setStyleSheet(
            table_frame_qss()
            + "QTableWidget::item { padding: 4px 6px; }"
        )
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.table.setMinimumHeight(80)
        self.apps_card.content_layout.addWidget(self.table, 1)

        extras_row = QHBoxLayout()
        extras_row.setContentsMargins(0, 4, 0, 0)
        extras_row.setSpacing(10)
        extras_lbl = make_field_label(self.tr("Parametros Extras"))
        self.extras_edit = QLineEdit()
        self.extras_edit.setPlaceholderText(
            self.tr(
                "Opcional — vazio usa ApplicationCatalog.json. "
                "EXE: /S. MSI: REBOOT=ReallySuppress"
            )
        )
        self.extras_edit.setToolTip(
            self.tr(
                "Se preenchido, sobrescreve o ApplicationCatalog.json.\n"
                "Se vazio, usa uninstallArgs do catálogo quando o app for reconhecido.\n"
                "EXE: switches do fabricante (WinRAR: /S).\n"
                "MSI: adicionais além de /qn /norestart."
            )
        )
        extras_row.addWidget(extras_lbl)
        extras_row.addWidget(self.extras_edit, 1)
        extras_wrap = QWidget()
        extras_wrap.setLayout(extras_row)
        self.apps_card.content_layout.addWidget(extras_wrap, 0)
        self.extras_edit.textChanged.connect(lambda _t: self._refresh_trash_tooltips())
        self.filter_edit.textChanged.connect(self._apply_filter)

        root.addWidget(self.apps_card, 2)

        self.log_output = LogOutputWidget()
        self.log_output.set_layout_stretch(1)
        root.addWidget(self.log_output, 1)

        self.apps_card.collapsedChanged.connect(self._redistribute_expandable_space)
        self.log_output.collapsedChanged.connect(self._redistribute_expandable_space)
        self._redistribute_expandable_space()

        self.destroyed.connect(self._abort_worker)

    def _ui_alive(self) -> bool:
        return not sip.isdeleted(self)

    def _get_host(self) -> str:
        if self._host_source is None or sip.isdeleted(self._host_source):
            return ""
        return (self._host_source.text() or "").strip().strip("\\")

    def _redistribute_expandable_space(self, _collapsed: bool = False) -> None:
        lay = self.layout()
        if lay is None:
            return
        open_cards = []
        for w, stretch in ((self.apps_card, 2), (self.log_output, 1)):
            idx = lay.indexOf(w)
            if idx < 0:
                continue
            if w.is_collapsed:
                lay.setStretch(idx, 0)
            else:
                open_cards.append(w)
                w.set_layout_stretch(stretch)
                lay.setStretch(idx, stretch)

        # Sem AlignTop: stretch final mantém cabeçalhos no topo ao recolher tudo.
        need_tail = len(open_cards) == 0
        if need_tail:
            if getattr(self, "_bottom_stretch_idx", None) is None:
                lay.addStretch(1)
                self._bottom_stretch_idx = lay.count() - 1
            else:
                lay.setStretch(self._bottom_stretch_idx, 1)
        elif getattr(self, "_bottom_stretch_idx", None) is not None:
            lay.setStretch(self._bottom_stretch_idx, 0)

        lay.activate()
        self.updateGeometry()

    def _abort_worker(self, _destroyed: object = None) -> None:
        w = self._worker
        if w is None:
            return
        try:
            w.abort()
        except Exception:
            pass

    def shutdown(self, wait_ms: int = 8000) -> None:
        w = self._worker
        if w is None:
            return
        try:
            w.finished_ok.disconnect(self._on_ok)
        except TypeError:
            pass
        try:
            w.finished_err.disconnect(self._on_err)
        except TypeError:
            pass
        try:
            w.abort()
        except Exception:
            pass
        if w.isRunning():
            w.wait(max(0, int(wait_ms)))
        self._worker = None

    def run_inventory(self) -> None:
        host = self._get_host()
        if not host:
            self.log_output.append_log(
                self.tr("[APPS] Preencha o Host remoto na aba PsExec.")
            )
            self._status_lbl.setText(self.tr("Host remoto não informado"))
            return
        if self._worker and self._worker.isRunning():
            return

        self._set_loading(True, host)
        self._apps = []
        self._trash_buttons = []
        self.table.setRowCount(0)
        self._apply_filter()

        self._worker = _HostAppsWorker(host)
        self._worker.finished_ok.connect(self._on_ok)
        self._worker.finished_err.connect(self._on_err)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()
        self.log_output.append_log(
            self.tr(
                f"[APPS] Consultando aplicativos em {host} via Remote Registry "
                f"(timeout {int(get_remote_registry_timeout())}s)..."
            )
        )

    def _set_loading(self, loading: bool, host: str = "") -> None:
        self._loading = loading
        self.refresh_btn.setEnabled(not loading)
        self._spinner.setVisible(loading)
        self._spin_wrap.setVisible(loading)
        if loading:
            self._status_lbl.setText(
                self.tr(f"Coletando aplicativos de {host}...")
                if host
                else self.tr("Coletando aplicativos...")
            )

    def _on_worker_finished(self) -> None:
        if not self._ui_alive():
            return
        self._set_loading(False)

    def _on_err(self, error_kind: str, message: str) -> None:
        if not self._ui_alive():
            return
        kind_labels = {
            "auth": "falha de autenticação",
            "remote_registry": "Remote Registry/RPC indisponível",
            "unreachable": "host inacessível",
            "invalid_host": "host inválido",
            "timed_out": "consulta expirada (timeout)",
            "internal_error": "erro interno",
        }
        label = kind_labels.get(error_kind, error_kind or "erro")
        detail = (message or "").strip()
        msg = f"{label}" + (f" — {detail}" if detail else "")
        self._status_lbl.setText(self.tr(f"Falha: {label}"))
        self.log_output.append_log(self.tr(f"[APPS] {msg}"))

    def _on_ok(self, apps: object) -> None:
        if not self._ui_alive():
            return
        host = self._get_host()
        items: List[InstalledApp] = []
        if isinstance(apps, list):
            for a in apps:
                if isinstance(a, InstalledApp) and (
                    a.display_name.strip() or a.display_line.strip()
                ):
                    items.append(a)
        items.sort(key=lambda a: (a.display_name or a.display_line or "").lower())
        self._apps = items
        self._populate_table(items)
        self._status_lbl.setText(
            self.tr(f"{len(items)} aplicativo(s) em {host}")
            if host
            else self.tr(f"{len(items)} aplicativo(s)")
        )
        self.log_output.append_log(
            self.tr(f"[APPS] {len(items)} aplicativo(s) encontrados em {host}.")
        )

    def _populate_table(self, apps: List[InstalledApp]) -> None:
        self._trash_buttons = []
        self.table.setRowCount(len(apps))
        for row, app in enumerate(apps):
            name = app.display_name or app.display_line
            publisher = app.publisher or ""
            version = app.version or ""
            kind = "MSI" if (app.is_msi and app.product_code) else "EXE"
            try:
                build_uninstall_remote_cmd(app, "")
                can_uninstall = True
            except ValueError:
                can_uninstall = False

            name_item = QTableWidgetItem(name)
            name_item.setData(Qt.ItemDataRole.UserRole, app)
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, QTableWidgetItem(publisher))
            self.table.setItem(row, 2, QTableWidgetItem(version))
            kind_item = QTableWidgetItem(kind)
            kind_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 3, kind_item)

            trash = QToolButton()
            trash.setText("\uE74D")
            trash.setFont(QFont("Segoe MDL2 Assets", ICON_FONT_PT - 2))
            trash.setCursor(Qt.CursorShape.PointingHandCursor)
            trash.setAutoRaise(True)
            trash.setFixedSize(26, 26)
            trash.setStyleSheet(
                f"""
                QToolButton {{
                    border: none;
                    background: transparent;
                    color: {COLOR_TEXT};
                }}
                QToolButton:hover {{
                    background: {COLOR_HOVER};
                    border-radius: {RADIUS_SMALL}px;
                    color: #c42b1c;
                }}
                QToolButton:disabled {{ color: {COLOR_TEXT_MUTED}; }}
                """
            )
            if can_uninstall:
                trash._installed_app = app  # type: ignore[attr-defined]
                self._trash_buttons.append(trash)
                trash.clicked.connect(
                    lambda _checked=False, a=app: self._on_uninstall_clicked(a)
                )
            else:
                trash.setEnabled(False)
                trash.setToolTip(
                    self.tr("Desinstalação indisponível (sem UninstallString)")
                )

            cell = QWidget()
            cell_lay = QHBoxLayout(cell)
            cell_lay.setContentsMargins(0, 0, 0, 0)
            cell_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell_lay.addWidget(trash)
            self.table.setCellWidget(row, 4, cell)

        self._refresh_trash_tooltips()
        self._apply_filter()

    def _apply_filter(self) -> None:
        q = (self.filter_edit.text() or "").strip().lower()
        visible = 0
        total = self.table.rowCount()
        for r in range(total):
            texts = []
            for c in range(4):
                it = self.table.item(r, c)
                texts.append(it.text() if it else "")
            ok = (q in " ".join(texts).lower()) if q else True
            self.table.setRowHidden(r, not ok)
            if ok:
                visible += 1
        self.count_lbl.setText(self.tr(f"{visible}/{total}"))
        self._sync_apps_download_button()

    def _sync_apps_download_button(self) -> None:
        btn = getattr(self, "_apps_download_btn", None)
        if btn is None or sip.isdeleted(btn):
            return
        btn.setEnabled(bool(self._collect_visible_apps()))

    def _collect_visible_apps(self) -> List[tuple[str, str, str, str]]:
        """Linhas visíveis da tabela: Nome, Editor, Versão, Tipo."""
        rows: List[tuple[str, str, str, str]] = []
        for r in range(self.table.rowCount()):
            if self.table.isRowHidden(r):
                continue
            cells = []
            for c in range(4):
                it = self.table.item(r, c)
                cells.append((it.text() if it else "").strip())
            if any(cells):
                rows.append((cells[0], cells[1], cells[2], cells[3]))
        return rows

    def _export_apps_csv(self) -> None:
        rows = self._collect_visible_apps()
        if not rows:
            QMessageBox.information(
                self,
                self.tr("Aplicativos"),
                self.tr(
                    "Não há aplicativos visíveis para exportar.\n"
                    "Atualize a lista e ajuste o filtro, se houver."
                ),
            )
            return
        host = (self._get_host() or "host").strip().strip("\\") or "host"
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_host = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in host)
        default_name = f"apps_{safe_host}_{stamp}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("Salvar aplicativos"),
            default_name,
            self.tr("CSV (*.csv)"),
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["Host", "Nome", "Editor", "Versao", "Tipo"])
                for name, publisher, version, kind in rows:
                    w.writerow([host, name, publisher, version, kind])
        except OSError as exc:
            QMessageBox.warning(
                self,
                self.tr("Aplicativos"),
                self.tr(f"Não foi possível salvar o arquivo:\n{exc}"),
            )
            return
        self.log_output.append_log(
            self.tr(f"[APPS] CSV exportado: {path} ({len(rows)} aplicativo(s))")
        )
        self._status_lbl.setText(
            self.tr(f"CSV salvo: {os.path.basename(path)} ({len(rows)} app(s))")
        )

    def _current_extras(self) -> str:
        return (self.extras_edit.text() or "").strip()

    def _refresh_trash_tooltips(self) -> None:
        extras_manual = self._current_extras()
        for btn in self._trash_buttons:
            app_obj = getattr(btn, "_installed_app", None)
            if isinstance(app_obj, InstalledApp):
                btn.setToolTip(
                    describe_uninstall(
                        app_obj, resolve_uninstall_extras(app_obj, extras_manual)
                    )
                )

    def _on_uninstall_clicked(self, app: InstalledApp) -> None:
        if not self._ui_alive():
            return
        host = self._get_host()
        if not host:
            self.log_output.append_log(
                self.tr("[APPS] Host remoto não informado.")
            )
            return
        manual = self._current_extras()
        extras = resolve_uninstall_extras(app, manual)
        try:
            remote_cmd = build_uninstall_remote_cmd(app, extras)
        except ValueError as exc:
            self.log_output.append_log(self.tr(f"[APPS] {exc}"))
            return
        if extras and not manual:
            self.log_output.append_log(
                self.tr(
                    f"[APPS] Parametros do catálogo para {app.display_name}: {extras}"
                )
            )
        self.uninstallRequested.emit(host, remote_cmd, app.display_line)
