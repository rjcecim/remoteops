from __future__ import annotations

import csv
import datetime
import os
import subprocess
from typing import Any, Callable, List, Optional, Tuple

from PyQt6 import sip
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLineEdit,
    QFileDialog,
    QSizePolicy,
    QPushButton,
    QLabel,
    QPlainTextEdit,
    QScrollArea,
    QFrame,
    QGridLayout,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QAbstractItemView,
)

from remoteops.ui.style import ICON_FONT_PT
from remoteops.ui.widgets.card import CardWidget
from remoteops.ui.widgets.spinner import DotsSpinner
from remoteops.utils.pstools import get_pstools_dir
from remoteops.utils.psinfo import (
    PsInfoDiskRow,
    PsInfoHotfix,
    build_psinfo_argv,
    build_psinfo_target,
    extract_psinfo_host,
    format_system_display,
    is_psinfo_usage_text,
    list_remote_hotfixes,
    parse_psinfo_output,
    parse_disks_table,
    prepare_disks_for_display,
)

# Timeout padrão para PsInfo remoto (host offline/problemático não deve travar a UI).
PSINFO_TIMEOUT_SECONDS = 90.0


def _icon_button(icon_char: str, tooltip: str = "", size: int = 32) -> QPushButton:
    btn = QPushButton(icon_char)
    font = QFont("Segoe MDL2 Assets", ICON_FONT_PT)
    btn.setFont(font)
    btn.setFixedSize(size, size)
    btn.setToolTip(tooltip)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setStyleSheet(
        """
        QPushButton {
            border: 1px solid palette(mid);
            border-radius: 4px;
            background: palette(button);
            color: palette(highlight);
            padding: 0;
        }
        QPushButton:hover { background: palette(light); border-color: palette(highlight); }
        QPushButton:pressed { background: palette(dark); }
        QPushButton:disabled { color: palette(mid); }
        """
    )
    return btn


class _PsInfoWorker(QThread):
    """Coleta Sistema/Discos via PsInfo; hotfixes via PsInfo -h ou Get-HotFix."""

    # stdout, lista[PsInfoHotfix]|None (fonte externa), nota
    finished_ok = pyqtSignal(str, object, str)
    finished_err = pyqtSignal(str)

    def __init__(
        self,
        exe_path: str,
        host: str,
        include_disks: bool,
        include_hotfixes: bool,
        nobanner: bool,
        pstools_dir: str = "",
        user: str = "",
        password: str = "",
    ):
        super().__init__()
        self.exe_path = exe_path
        self.host = host
        self.include_disks = include_disks
        self.include_hotfixes = include_hotfixes
        self.nobanner = nobanner
        self.pstools_dir = pstools_dir
        self.user = user or ""
        self.password = password or ""
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        try:
            from remoteops.utils.pstools import resolve_pstools_tool

            if not build_psinfo_target(self.host):
                self.finished_err.emit("Host remoto não informado.")
                return

            exe = (self.exe_path or "").strip()
            if exe:
                exe = os.path.normpath(exe.replace('"', "").replace("'", ""))
            else:
                exe = resolve_pstools_tool(
                    self.pstools_dir or get_pstools_dir(),
                    ("PsInfo64.exe", "PsInfo.exe"),
                )
                if not exe:
                    exe = "PsInfo64.exe"

            # PsInfo v1.79: switches antes do \\computer; -u/-p depois do alvo
            args = build_psinfo_argv(
                exe,
                self.host,
                include_disks=self.include_disks,
                include_hotfixes=self.include_hotfixes,
                nobanner=self.nobanner,
                user=self.user,
                password=self.password,
            )
            if not args:
                self.finished_err.emit("Não foi possível montar o comando PsInfo.")
                return

            creationflags = 0
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                creationflags = subprocess.CREATE_NO_WINDOW

            try:
                proc = subprocess.run(
                    args,
                    capture_output=True,
                    text=False,
                    creationflags=creationflags,
                    timeout=PSINFO_TIMEOUT_SECONDS,
                    shell=False,
                )
            except subprocess.TimeoutExpired:
                self.finished_err.emit(
                    f"PsInfo excedeu o tempo limite ({int(PSINFO_TIMEOUT_SECONDS)}s). "
                    "O host pode estar inacessível ou sobrecarregado."
                )
                return
            except FileNotFoundError:
                self.finished_err.emit(
                    f"PsInfo não encontrado: {args[0] if args else '?'}."
                )
                return
            except OSError as exc:
                self.finished_err.emit(f"Falha ao iniciar PsInfo: {exc}")
                return

            if self._abort:
                return

            stdout_b = proc.stdout or b""
            stderr_b = proc.stderr or b""

            def decode_best_effort(b: bytes) -> str:
                if not b:
                    return ""
                try:
                    return b.decode("utf-8-sig")
                except Exception:
                    pass
                try:
                    return b.decode("mbcs", errors="replace")
                except Exception:
                    pass
                return b.decode("cp1252", errors="replace")

            out = decode_best_effort(stdout_b).strip()
            err = decode_best_effort(stderr_b).strip()
            combined = out if out else err

            if is_psinfo_usage_text(combined):
                # Não vazar senha no log de erro
                safe_args = [
                    a if a != self.password else "********" for a in args
                ]
                self.finished_err.emit(
                    "PsInfo devolveu a tela de Usage (comando rejeitado). "
                    f"Argv: {' '.join(safe_args)}"
                )
                return

            if proc.returncode != 0:
                msg = (
                    err
                    or (out if ("error" in out.lower()) else "")
                    or f"Falha ao executar PsInfo (exit code {proc.returncode})."
                )
                self.finished_err.emit(msg)
                if not out:
                    return

            if self._abort:
                return

            # PsInfo -h costuma vir vazio no Windows moderno → Get-HotFix / PsExec
            hotfix_override = None
            hotfix_note = ""
            if self.include_hotfixes and not self._abort:
                parsed_probe = parse_psinfo_output(combined, host=self.host)
                if parsed_probe.hotfixes:
                    hotfix_note = "Hotfixes obtidos do PsInfo (-h)."
                else:
                    items, err_hf = list_remote_hotfixes(
                        self.host,
                        user=self.user,
                        password=self.password,
                        timeout=90.0,
                        pstools_dir=self.pstools_dir,
                    )
                    if self._abort:
                        return
                    if items:
                        hotfix_override = items
                        via = "PsExec" if err_hf == "via PsExec" else "Get-HotFix"
                        hotfix_note = (
                            f"Hotfixes obtidos via {via} ({len(items)} item(ns)). "
                            "PsInfo -h não retornou dados."
                        )
                    else:
                        hotfix_note = (
                            "Nenhum hotfix listado (PsInfo -h vazio"
                            + (f"; {err_hf}" if err_hf else "")
                            + ")."
                        )

            if self._abort:
                return

            self.finished_ok.emit(combined, hotfix_override, hotfix_note)
        except FileNotFoundError:
            self.finished_err.emit(
                "Não foi possível encontrar o PsInfo na pasta PSTools configurada."
            )
        except Exception as exc:
            self.finished_err.emit(f"Erro ao executar PsInfo: {exc}")
        finally:
            self.password = ""


class PsInfoTab(QWidget):
    def __init__(
        self,
        parent=None,
        log_output=None,
        host_source: Optional[QLineEdit] = None,
        creds_provider: Optional[Callable[[], Tuple[str, str]]] = None,
    ):
        super().__init__(parent)
        self.log_output = log_output
        self._worker: Optional[_PsInfoWorker] = None
        self._host_source = host_source
        self._creds_provider = creds_provider
        self._loading_card: Optional[CardWidget] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(3)

        # Barra da pesquisa completa: renovar inventário
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(8)
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color: palette(windowText); opacity: 0.75;")
        self.refresh_btn = _icon_button("\uE72C", self.tr("Renovar (buscar informações novamente)"), size=28)
        self.refresh_btn.clicked.connect(self.run_psinfo)
        toolbar.addWidget(self._status_lbl, 1)
        toolbar.addWidget(self.refresh_btn, 0, Qt.AlignmentFlag.AlignRight)
        root.addLayout(toolbar)

        # Área dos cards (sem scrollbar externo)
        self.results_root = QWidget()
        self.results_layout = QVBoxLayout(self.results_root)
        self.results_layout.setContentsMargins(0, 0, 0, 0)
        self.results_layout.setSpacing(3)
        # Stretch final (não AlignTop): evita sobreposição ao recolher cards.
        self.results_layout.addStretch(1)
        root.addWidget(self.results_root, 1)

        if host_source is not None:
            host_source.textChanged.connect(self._on_host_changed)

        self.destroyed.connect(self._abort_psinfo_worker)

        # Execução é disparada pelo MainWindow ao abrir/clicar no botão.

    def _ui_alive(self) -> bool:
        return not sip.isdeleted(self)

    def _add_result_card(self, card: CardWidget, stretch: int = 1) -> None:
        """Insere o card antes do stretch final e registra o stretch para restaurar ao expandir."""
        card.set_layout_stretch(stretch)
        layout_stretch = 0 if card.is_collapsed else stretch
        idx = max(0, self.results_layout.count() - 1)
        self.results_layout.insertWidget(idx, card, layout_stretch)
        try:
            card.collapsedChanged.disconnect(self._redistribute_card_space)
        except TypeError:
            pass
        card.collapsedChanged.connect(lambda _collapsed=False: self._redistribute_card_space())
        self._redistribute_card_space()

    def _iter_result_cards(self):
        for i in range(self.results_layout.count()):
            item = self.results_layout.itemAt(i)
            w = item.widget() if item is not None else None
            if isinstance(w, CardWidget):
                yield w

    def _redistribute_card_space(self) -> None:
        """
        Com algum card expandido: eles preenchem a janela (stretch final = 0).
        Com todos minimizados: só cabeçalhos no topo (stretch final = 1).
        """
        if self.results_layout.count() == 0:
            return

        cards = list(self._iter_result_cards())
        expanded = [c for c in cards if not c.is_collapsed]
        last = self.results_layout.count() - 1

        if not expanded:
            # Todos minimizados → cabeçalhos no topo + espaço vazio embaixo
            for c in cards:
                idx = self.results_layout.indexOf(c)
                if idx >= 0:
                    self.results_layout.setStretch(idx, 0)
            self.results_layout.setStretch(last, 1)
        else:
            # Há card(s) aberto(s) → preenchem a janela inteira
            self.results_layout.setStretch(last, 0)
            for c in cards:
                idx = self.results_layout.indexOf(c)
                if idx < 0:
                    continue
                if c.is_collapsed:
                    self.results_layout.setStretch(idx, 0)
                else:
                    self.results_layout.setStretch(idx, max(1, c.layout_stretch))

        self.results_layout.activate()
        self.updateGeometry()

    def _abort_psinfo_worker(self, _destroyed: object = None) -> None:
        w = self._worker
        if w is None:
            return
        self._worker = None
        try:
            w.finished_ok.disconnect(self._on_psinfo_ok)
        except TypeError:
            pass
        try:
            w.finished_err.disconnect(self._on_psinfo_err)
        except TypeError:
            pass
        try:
            w.finished.disconnect(self._on_worker_thread_finished)
        except TypeError:
            pass
        w.abort()
        if w.isRunning():
            # PsInfo subprocess; espera limitada.
            w.wait(max(3000, int(PSINFO_TIMEOUT_SECONDS * 1000)))
        if w.isRunning():
            w.finished.connect(w.deleteLater)
        else:
            w.deleteLater()

    def shutdown(self, wait_ms: int = 8000) -> None:
        """Aborta worker/PsInfo e espera a QThread — antes de fechar/remover a aba."""
        w = self._worker
        if w is None:
            return
        self._worker = None
        try:
            w.finished_ok.disconnect(self._on_psinfo_ok)
        except TypeError:
            pass
        try:
            w.finished_err.disconnect(self._on_psinfo_err)
        except TypeError:
            pass
        try:
            w.finished.disconnect(self._on_worker_thread_finished)
        except TypeError:
            pass
        w.abort()
        if w.isRunning():
            w.wait(max(0, int(wait_ms)))
        if w.isRunning():
            w.finished.connect(w.deleteLater)
        else:
            w.deleteLater()

    def _get_host(self) -> str:
        if self._host_source is None:
            return ""
        return (self._host_source.text() or "").strip()

    def _on_host_changed(self, _text: str) -> None:
        # Não auto-executar a cada tecla; só mantém o host atualizado para a próxima execução.
        return

    def clear_results(self) -> None:
        while self.results_layout.count():
            item = self.results_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self.results_layout.addStretch(1)
        self._loading_card = None

    def _set_loading(self, loading: bool, host: str = "") -> None:
        if not self._ui_alive():
            return
        self.refresh_btn.setEnabled(not loading)
        if loading:
            self.clear_results()
            host_disp = host or self._get_host()
            self._status_lbl.setText(
                self.tr(f"Coletando inventário de {host_disp}...") if host_disp else self.tr("Coletando inventário...")
            )
            card = CardWidget("\uE895", self.tr("Coletando informações"))
            card.set_collapsible(False)
            card.set_expanding(True)

            wrap = QWidget()
            lay = QVBoxLayout(wrap)
            lay.setContentsMargins(0, 6, 0, 2)
            lay.setSpacing(8)

            msg = self.tr("Aguarde...") if not host else self.tr(f"Aguarde... ({host})")
            lbl = QLabel(msg)
            lbl.setStyleSheet("color: palette(windowText); opacity: 0.85;")

            spinner_row = QHBoxLayout()
            spinner_row.setContentsMargins(0, 0, 0, 0)
            spinner_row.addStretch()
            spinner = DotsSpinner()
            spinner_row.addWidget(spinner)
            spinner_row.addStretch()
            spinner_wrap = QWidget()
            spinner_wrap.setLayout(spinner_row)

            lay.addStretch(1)
            lay.addWidget(lbl, 0, Qt.AlignmentFlag.AlignHCenter)
            lay.addWidget(spinner_wrap)
            lay.addStretch(1)
            card.content_layout.addWidget(wrap, 1)

            self._loading_card = card
            self._add_result_card(card, 1)
        else:
            if self._loading_card is not None:
                card = self._loading_card
                self._loading_card = None
                if not sip.isdeleted(card):
                    card.deleteLater()

    def _add_text_card(self, icon: str, title: str, text: str) -> None:
        card = CardWidget(icon, title)
        card.set_collapsible(True, collapsed=False)
        card.set_expanding(True)
        editor = QPlainTextEdit()
        editor.setReadOnly(True)
        editor.setPlainText(text or "")
        editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        editor.setStyleSheet("QPlainTextEdit { border: 1px solid palette(mid); border-radius: 4px; }")
        editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        card.content_layout.addWidget(editor, 1)
        self._add_result_card(card, 1)

    def _add_system_card(
        self,
        icon: str,
        title: str,
        rows: list[tuple[str, str, str]],
    ) -> None:
        """rows: (grupo, rótulo, valor) — agrupados SO / Hardware / Registro."""
        card = CardWidget(icon, title)
        card.set_collapsible(True, collapsed=False)
        card.set_expanding(True)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)

        export_kv: list[tuple[str, str]] = []
        current_group = None
        row_i = 0
        for group, label, value in rows:
            if group != current_group:
                current_group = group
                g_lbl = QLabel(self.tr(group))
                g_lbl.setStyleSheet(
                    "color: palette(highlight); font-weight: 600; padding-top: 4px;"
                )
                g_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                grid.addWidget(g_lbl, row_i, 0, 1, 2, Qt.AlignmentFlag.AlignLeft)
                row_i += 1

            k_lbl = QLabel(label)
            k_lbl.setStyleSheet("color: palette(windowText); opacity: 0.75;")
            k_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            k_lbl.setMinimumWidth(180)

            v_lbl = QLabel(value)
            v_lbl.setWordWrap(True)
            v_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

            grid.addWidget(k_lbl, row_i, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(v_lbl, row_i, 1, Qt.AlignmentFlag.AlignTop)
            export_kv.append((label, value))
            row_i += 1

        wrap = QWidget()
        wrap.setLayout(grid)
        inner = QScrollArea()
        inner.setWidgetResizable(True)
        inner.setFrameShape(QFrame.Shape.NoFrame)
        inner.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner.setWidget(wrap)
        inner.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        card.content_layout.addWidget(inner, 1)
        self._wire_card_download(card, "sistema", export_kv)
        self._add_result_card(card, 2)

    def _add_hotfixes_card(
        self,
        icon: str,
        title: str,
        hotfixes: List[PsInfoHotfix],
    ) -> None:
        card = CardWidget(icon, title)
        card.set_collapsible(True, collapsed=False)
        card.set_expanding(True)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        search = QLineEdit()
        search.setPlaceholderText(self.tr("Buscar hotfix (KB/Q)..."))
        count_lbl = QLabel("")
        count_lbl.setStyleSheet("color: palette(windowText); opacity: 0.75;")
        top.addWidget(search, 1)
        top.addWidget(count_lbl)
        top_wrap = QWidget()
        top_wrap.setLayout(top)
        card.content_layout.addWidget(top_wrap, 0)

        table = QTableWidget()
        table.setColumnCount(3)
        table.setHorizontalHeaderLabels(
            [self.tr("Hotfix"), self.tr("Descrição"), self.tr("Instalado em")]
        )
        table.setRowCount(len(hotfixes))
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.setShowGrid(False)
        table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        table.setStyleSheet(
            "QTableWidget { border: 1px solid palette(mid); border-radius: 4px; }"
        )
        table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        table.setMinimumHeight(80)

        for r, hf in enumerate(hotfixes):
            table.setItem(r, 0, QTableWidgetItem(hf.id))
            table.setItem(r, 1, QTableWidgetItem(hf.description or ""))
            table.setItem(r, 2, QTableWidgetItem(hf.installed))

        def update_filter():
            q = (search.text() or "").strip().lower()
            visible = 0
            for row in range(table.rowCount()):
                parts = []
                for c in range(3):
                    it = table.item(row, c)
                    parts.append(it.text() if it else "")
                text = " ".join(parts).lower()
                ok = (q in text) if q else True
                table.setRowHidden(row, not ok)
                if ok:
                    visible += 1
            count_lbl.setText(self.tr(f"{visible}/{len(hotfixes)}"))

        search.textChanged.connect(update_filter)
        update_filter()

        card.content_layout.addWidget(table, 1)
        self._wire_card_download(card, "hotfixes", list(hotfixes))
        self._add_result_card(card, 1)

    def _wire_card_download(self, card: CardWidget, kind: str, payload: Any) -> None:
        card.set_downloadable(True)
        card.downloadRequested.connect(lambda k=kind, p=payload: self._download_card_data(k, p))

    def _download_card_data(self, kind: str, payload: Any) -> None:
        host = (self._get_host() or "host").strip().strip("\\") or "host"
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_host = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in host)

        if kind == "sistema":
            default_name = f"psinfo_{safe_host}_sistema_{stamp}.txt"
            filt = self.tr("Texto (*.txt)")
            path, _ = QFileDialog.getSaveFileName(
                self, self.tr("Salvar Sistema"), default_name, filt
            )
            if not path:
                return
            lines = [f"Host: {host}", f"Gerado: {stamp}", ""]
            for k, v in payload or []:
                lines.append(f"{k}: {v}")
            text = "\n".join(lines) + "\n"
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        elif kind == "hotfixes":
            default_name = f"psinfo_{safe_host}_hotfixes_{stamp}.csv"
            filt = self.tr("CSV (*.csv)")
            path, _ = QFileDialog.getSaveFileName(
                self, self.tr("Salvar Hotfixes"), default_name, filt
            )
            if not path:
                return
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["Hotfix", "Descricao", "InstaladoEm"])
                for hf in payload or []:
                    w.writerow([hf.id, getattr(hf, "description", ""), hf.installed])
        elif kind == "discos":
            default_name = f"psinfo_{safe_host}_discos_{stamp}.csv"
            filt = self.tr("CSV (*.csv)")
            path, _ = QFileDialog.getSaveFileName(self, self.tr("Salvar Discos"), default_name, filt)
            if not path:
                return
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(
                    [
                        "Volume",
                        "Tipo",
                        "Formato",
                        "Rotulo",
                        "Tamanho",
                        "Usado",
                        "Livre",
                        "PctLivre",
                    ]
                )
                for row in payload or []:
                    w.writerow(
                        [
                            row.volume,
                            row.type,
                            row.format,
                            row.label,
                            row.size,
                            getattr(row, "used", ""),
                            row.free,
                            row.free_pct,
                        ]
                    )
        else:
            return

        if self.log_output:
            self.log_output.append_log(self.tr(f"[PSINFO] Arquivo salvo: {path}"))
        self._status_lbl.setText(self.tr(f"Arquivo salvo: {os.path.basename(path)}"))

    @staticmethod
    def _disk_free_color(pct: Optional[float]) -> QColor:
        if pct is None:
            return QColor()
        if pct < 10:
            return QColor("#c42b1c")  # crítico
        if pct < 20:
            return QColor("#ca5010")  # baixo
        return QColor("#0f7b0f")  # ok

    def _add_disks_card(
        self,
        icon: str,
        title: str,
        disks_raw: list[str],
        *,
        system_root: str = "",
    ) -> None:
        parsed_rows = parse_disks_table(disks_raw)
        rows, totals, root_vol = prepare_disks_for_display(
            parsed_rows, system_root=system_root, hide_empty_media=True
        )
        if not rows:
            self._add_text_card(icon, title, "\n".join(disks_raw) if disks_raw else "")
            return

        display_rows: List[PsInfoDiskRow] = list(rows)
        if totals is not None:
            display_rows.append(totals)

        card = CardWidget(icon, title)
        card.set_collapsible(True, collapsed=False)
        table = QTableWidget()
        table.setColumnCount(8)
        table.setHorizontalHeaderLabels(
            [
                self.tr("Volume"),
                self.tr("Tipo"),
                self.tr("Formato"),
                self.tr("Rótulo"),
                self.tr("Tamanho"),
                self.tr("Usado"),
                self.tr("Livre"),
                self.tr("% Livre"),
            ]
        )
        table.setRowCount(len(display_rows))
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)
        table.setStyleSheet("QTableWidget { border: 1px solid palette(mid); border-radius: 4px; }")

        bold = QFont(table.font())
        bold.setBold(True)

        for r, row in enumerate(display_rows):
            is_total = (row.volume or "").strip().lower() == "total"
            is_system = bool(root_vol) and row.volume.upper().rstrip("\\") == root_vol
            vol_label = row.volume
            if is_system and not is_total:
                vol_label = f"{row.volume} *"

            values = [
                vol_label,
                row.type,
                row.format,
                row.label,
                row.size,
                row.used,
                row.free,
                row.free_pct,
            ]
            free_color = self._disk_free_color(row.free_pct_value)
            for c, val in enumerate(values):
                it = QTableWidgetItem(val)
                if is_total or is_system:
                    it.setFont(bold)
                if c == 7 and free_color.isValid():
                    it.setForeground(free_color)
                if is_system and not is_total:
                    it.setToolTip(self.tr("Volume do System root"))
                table.setItem(r, c, it)

        table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        table.setMinimumHeight(56)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        card.set_expanding(True)
        card.content_layout.addWidget(table, 1)
        # Exporta linhas reais (sem totais) + totais no fim se houver
        export_rows = list(rows)
        if totals is not None:
            export_rows.append(totals)
        self._wire_card_download(card, "discos", export_rows)
        self._add_result_card(card, 1)

    def run_psinfo(self) -> None:
        host = self._get_host()
        if not host:
            if self.log_output:
                self.log_output.append_log(self.tr("[PSINFO] Preencha o Host remoto na aba PsExec."))
            return

        if self._worker and self._worker.isRunning():
            return

        self._set_loading(True, host=host)

        user, password = "", ""
        if self._creds_provider is not None:
            try:
                user, password = self._creds_provider()
            except Exception:
                user, password = "", ""

        self._worker = _PsInfoWorker(
            exe_path="",
            host=host,
            include_disks=True,
            include_hotfixes=True,
            nobanner=True,
            pstools_dir=get_pstools_dir(),
            user=user or "",
            password=password or "",
        )
        self._worker.finished_ok.connect(self._on_psinfo_ok)
        self._worker.finished_err.connect(self._on_psinfo_err)
        self._worker.finished.connect(self._on_worker_thread_finished)
        self._worker.start()

        if self.log_output:
            self.log_output.append_log(self.tr(f"[PSINFO] Coletando informações de {host}..."))

    def _on_worker_thread_finished(self) -> None:
        self._set_loading(False)

    def _on_psinfo_err(self, msg: str) -> None:
        if not self._ui_alive():
            return
        self._set_loading(False)
        if self.log_output:
            self.log_output.append_log(self.tr(f"[PSINFO] {msg}"))
        self._status_lbl.setText(self.tr("Falha na coleta"))
        self._add_text_card("\uE783", self.tr("Erro"), msg)

    def _on_psinfo_ok(
        self,
        stdout: str,
        hotfix_override=None,
        hotfix_note: str = "",
    ) -> None:
        if not self._ui_alive():
            return
        self._set_loading(False)
        host = self._get_host()
        parsed = parse_psinfo_output(stdout, host=host)
        info_host = extract_psinfo_host(parsed) or host

        hotfixes: List[PsInfoHotfix] = []
        if isinstance(hotfix_override, list) and hotfix_override:
            hotfixes = [h for h in hotfix_override if isinstance(h, PsInfoHotfix)]
        elif parsed.hotfixes:
            hotfixes = list(parsed.hotfixes)

        parsed.hotfixes = hotfixes

        # Card Sistema — host + grupos SO / Hardware / Registro (rótulos em PT)
        system_rows = format_system_display(
            parsed.system,
            host=info_host,
            tool_version=parsed.tool_version,
            hotfix_count=len(hotfixes) if hotfixes else 0,
        )
        if system_rows:
            self._add_system_card("\uE8FE", self.tr("Sistema"), system_rows)
        else:
            self._add_text_card(
                "\uE8FE",
                self.tr("Sistema"),
                self.tr("Nenhuma informação de sistema foi detectada no output."),
            )

        # Card Discos — Fixed primeiro, Usado, totais e destaque do System root
        if parsed.disks_raw:
            self._add_disks_card(
                "\uE7B8",
                self.tr("Discos"),
                parsed.disks_raw,
                system_root=parsed.system.get("System root", ""),
            )

        # Card Hotfixes — sempre visível (PsInfo -h ou Get-HotFix)
        if hotfixes:
            self._add_hotfixes_card("\uE895", self.tr("Hotfixes"), hotfixes)
        else:
            self._add_text_card(
                "\uE895",
                self.tr("Hotfixes"),
                self.tr(
                    "Nenhum hotfix encontrado.\n\n"
                    "O PsInfo -h costuma vir vazio no Windows moderno. "
                    "Foi tentado Get-HotFix (WMI) e, se necessário, PsExec. "
                    "Confira Usuário/Senha em Autenticação (aba PsExec)."
                ),
            )

        note = (hotfix_note or "").strip()
        if note and self.log_output:
            self.log_output.append_log(self.tr(f"[PSINFO] {note}"))
        if self.log_output:
            self.log_output.append_log(self.tr(f"[PSINFO] Coleta finalizada para {host}."))
        self._status_lbl.setText(
            self.tr(f"Inventário de {host}") if host else self.tr("Inventário atualizado")
        )
