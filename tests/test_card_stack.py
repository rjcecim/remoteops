"""Layout vertical de CardWidget — bind_card_stack e políticas de stretch."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from remoteops.ui import style as ui_style  # noqa: E402
from remoteops.ui.widgets.card import (  # noqa: E402
    CardWidget,
    bind_card_stack,
    make_card_stack,
)
from remoteops.ui.widgets.content_tab_widget import ContentSizedTabWidget  # noqa: E402

ui_style.ANIMATIONS_ENABLED = False

_APP = QApplication.instance() or QApplication([])


def _spacer_count(layout: QVBoxLayout) -> int:
    n = 0
    for i in range(layout.count()):
        item = layout.itemAt(i)
        if item is not None and item.spacerItem() is not None:
            n += 1
    return n


def _active_spacer_stretch(layout: QVBoxLayout) -> int:
    last = -1
    for i in range(layout.count()):
        item = layout.itemAt(i)
        if item is not None and item.spacerItem() is not None:
            last = i
    if last < 0:
        return 0
    return layout.stretch(last)


def _flush() -> None:
    _APP.processEvents()


class _StackHost(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.layout_ = make_card_stack(self)
        self.cards: list[CardWidget] = []

    def add_card(
        self,
        title: str,
        *,
        stretch: int = 0,
        expanding: bool = False,
        collapsed: bool = False,
    ) -> CardWidget:
        card = CardWidget("\uE8A5", title)
        card.set_collapsible(True, collapsed=collapsed)
        if stretch:
            card.set_layout_stretch(stretch)
        if expanding:
            inner = QWidget()
            inner.setObjectName("expandInner")
            inner.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
            )
            inner.setMinimumHeight(24)
            card.content_layout.addWidget(inner, 1)
            card.set_expanding(True)
        else:
            edit = QLineEdit()
            edit.setObjectName("formEdit")
            card.content_layout.addWidget(edit)
        self.layout_.addWidget(card)
        self.cards.append(card)
        return card

    def bind(self, **kwargs) -> None:
        bind_card_stack(self.layout_, self.cards, **kwargs)


class TestMakeCardStack(unittest.TestCase):
    def test_alignment_is_cleared(self) -> None:
        host = QWidget()
        lay = make_card_stack(host)
        self.assertEqual(int(lay.alignment()), 0)

    def test_no_permanent_tail_spacer(self) -> None:
        host = QWidget()
        lay = make_card_stack(host)
        self.assertEqual(_spacer_count(lay), 0)


class TestBindCardStack(unittest.TestCase):
    def _shown_stack(self, *stretches: int, height: int = 800) -> _StackHost:
        host = _StackHost()
        for i, stretch in enumerate(stretches, start=1):
            host.add_card(f"Card {i}", stretch=stretch)
        host.bind()
        host.setFixedSize(420, height)
        host.show()
        _flush()
        host.layout_.activate()
        _flush()
        return host

    def test_two_open_cards_share_leftover(self) -> None:
        host = self._shown_stack(1, 1)
        c1, c2 = host.cards
        self.assertGreater(c1.height(), c1.minimumSizeHint().height())
        self.assertGreater(c2.height(), c2.minimumSizeHint().height())
        self.assertAlmostEqual(c1.height(), c2.height(), delta=8)
        self.assertEqual(host.layout_.stretch(host.layout_.indexOf(c1)), 1)
        self.assertEqual(host.layout_.stretch(host.layout_.indexOf(c2)), 1)
        self.assertEqual(_active_spacer_stretch(host.layout_), 0)

    def test_weights_two_to_one(self) -> None:
        host = self._shown_stack(2, 1)
        c1, c2 = host.cards
        extra1 = c1.height() - c1.minimumSizeHint().height()
        extra2 = c2.height() - c2.minimumSizeHint().height()
        self.assertGreater(extra1, 0)
        self.assertGreater(extra2, 0)
        self.assertGreater(extra1, extra2)
        ratio = extra1 / extra2
        self.assertGreater(ratio, 1.4)
        self.assertLess(ratio, 2.8)
        self.assertEqual(host.layout_.stretch(host.layout_.indexOf(c1)), 2)
        self.assertEqual(host.layout_.stretch(host.layout_.indexOf(c2)), 1)

    def test_one_collapsed_gives_leftover_to_open(self) -> None:
        host = self._shown_stack(1, 1)
        c1, c2 = host.cards
        c1.set_collapsed(True)
        _flush()
        self.assertTrue(c1.is_collapsed)
        self.assertEqual(host.layout_.stretch(host.layout_.indexOf(c1)), 0)
        self.assertEqual(host.layout_.stretch(host.layout_.indexOf(c2)), 1)
        self.assertLessEqual(c1.height(), c1.sizeHint().height() + 4)
        self.assertGreater(c2.height(), c2.minimumSizeHint().height() + 40)
        self.assertEqual(_active_spacer_stretch(host.layout_), 0)

    def test_all_collapsed_headers_at_top(self) -> None:
        host = self._shown_stack(1, 1, height=700)
        c1, c2 = host.cards
        c1.set_collapsed(True)
        c2.set_collapsed(True)
        _flush()
        self.assertEqual(host.layout_.stretch(host.layout_.indexOf(c1)), 0)
        self.assertEqual(host.layout_.stretch(host.layout_.indexOf(c2)), 0)
        self.assertEqual(_active_spacer_stretch(host.layout_), 1)
        self.assertLess(c1.y(), c2.y())
        gap = c2.y() - (c1.y() + c1.height())
        self.assertLessEqual(gap, host.layout_.spacing() + 4)
        self.assertLess(c2.y() + c2.height(), host.height() - 80)

    def test_reopen_disables_tail_spacer(self) -> None:
        host = self._shown_stack(1, 1)
        c1, c2 = host.cards
        c1.set_collapsed(True)
        c2.set_collapsed(True)
        _flush()
        self.assertEqual(_active_spacer_stretch(host.layout_), 1)
        c2.set_collapsed(False)
        _flush()
        self.assertEqual(_active_spacer_stretch(host.layout_), 0)
        self.assertEqual(host.layout_.stretch(host.layout_.indexOf(c2)), 1)
        self.assertGreater(c2.height(), c2.minimumSizeHint().height() + 40)

    def test_layout_stretch_preserved_on_collapse(self) -> None:
        host = self._shown_stack(2, 1)
        c1 = host.cards[0]
        self.assertEqual(c1.layout_stretch, 2)
        c1.set_collapsed(True)
        _flush()
        self.assertEqual(c1.layout_stretch, 2)
        c1.set_collapsed(False)
        _flush()
        self.assertEqual(c1.layout_stretch, 2)
        self.assertEqual(host.layout_.stretch(host.layout_.indexOf(c1)), 2)

    def test_bind_is_idempotent(self) -> None:
        host = self._shown_stack(1, 1)
        for _ in range(8):
            host.bind()
            host.cards[0].set_collapsed(True)
            host.cards[1].set_collapsed(True)
            host.cards[0].set_collapsed(False)
            host.cards[1].set_collapsed(False)
        _flush()
        self.assertEqual(_spacer_count(host.layout_), 1)
        host.cards[0].set_collapsed(True)
        host.cards[1].set_collapsed(True)
        _flush()
        self.assertEqual(_spacer_count(host.layout_), 1)
        self.assertEqual(_active_spacer_stretch(host.layout_), 1)

    def test_dynamic_rebind_does_not_stack_spacers(self) -> None:
        host = _StackHost()
        host.add_card("A", stretch=1)
        host.bind()
        host.add_card("B", stretch=2)
        host.bind()
        host.add_card("C", stretch=1)
        host.bind()
        host.setFixedSize(420, 640)
        host.show()
        _flush()
        for card in host.cards:
            card.set_collapsed(True)
        host.bind()
        _flush()
        self.assertEqual(_spacer_count(host.layout_), 1)
        self.assertEqual(_active_spacer_stretch(host.layout_), 1)
        host.cards[-1].set_collapsed(False)
        host.bind()
        _flush()
        self.assertEqual(_active_spacer_stretch(host.layout_), 0)
        self.assertEqual(_spacer_count(host.layout_), 1)

    def test_fill_false_does_not_absorb(self) -> None:
        host = _StackHost()
        host.add_card("A", stretch=2)
        host.add_card("B", stretch=1)
        host.bind(fill=False)
        host.setFixedSize(420, 800)
        host.show()
        _flush()
        c1, c2 = host.cards
        self.assertEqual(host.layout_.stretch(host.layout_.indexOf(c1)), 0)
        self.assertEqual(host.layout_.stretch(host.layout_.indexOf(c2)), 0)
        self.assertEqual(_active_spacer_stretch(host.layout_), 0)
        self.assertLess(c1.height() + c2.height(), 400)

    def test_form_grows_but_line_edit_does_not(self) -> None:
        host = self._shown_stack(1)
        card = host.cards[0]
        edit = card.findChild(QLineEdit, "formEdit")
        self.assertIsNotNone(edit)
        self.assertGreater(card.height(), card.minimumSizeHint().height() + 40)
        self.assertLessEqual(edit.height(), edit.sizeHint().height() + 6)
        top = edit.mapTo(card, edit.rect().topLeft())
        self.assertLess(top.y(), 80)

    def test_expanding_inner_absorbs_extra(self) -> None:
        host = _StackHost()
        host.add_card("Tabela", stretch=1, expanding=True)
        host.bind()
        host.setFixedSize(420, 700)
        host.show()
        _flush()
        card = host.cards[0]
        inner = card.findChild(QWidget, "expandInner")
        self.assertIsNotNone(inner)
        self.assertGreater(inner.height(), 80)

    def test_hide_field_reflows_siblings(self) -> None:
        host = _StackHost()
        c1 = host.add_card("Form", stretch=1)
        extra = QLabel("linha extra")
        extra.setObjectName("extraLine")
        c1.content_layout.addWidget(extra)
        host.add_card("Dois", stretch=1)
        host.bind()
        host.setFixedSize(420, 720)
        host.show()
        _flush()
        y_before = host.cards[1].y()
        extra.hide()
        c1.updateGeometry()
        host.layout_.invalidate()
        host.layout_.activate()
        _flush()
        self.assertLessEqual(host.cards[1].y(), y_before)
        extra.show()
        c1.updateGeometry()
        host.layout_.invalidate()
        host.layout_.activate()
        _flush()
        self.assertGreaterEqual(host.cards[1].y(), y_before - 2)

    def test_repeated_collapse_does_not_grow(self) -> None:
        host = self._shown_stack(1, 1, height=640)
        c1, c2 = host.cards
        heights = []
        for _ in range(6):
            c1.set_collapsed(True)
            c2.set_collapsed(True)
            _flush()
            c1.set_collapsed(False)
            c2.set_collapsed(False)
            _flush()
            heights.append((c1.height(), c2.height()))
        self.assertEqual(len(set(heights)), 1)


class TestContentSizedTab(unittest.TestCase):
    def test_compact_tab_keeps_maximum_policy(self) -> None:
        page = QWidget()
        lay = make_card_stack(page)
        card = CardWidget("\uE8A5", "Form")
        card.set_collapsible(True, collapsed=False)
        card.content_layout.addWidget(QLineEdit())
        lay.addWidget(card)
        bind_card_stack(lay, (card,))
        tabs = ContentSizedTabWidget()
        tabs.addTab(page, "PsExec")
        tabs.set_fill_available(False)
        self.assertEqual(
            tabs.sizePolicy().verticalPolicy(), QSizePolicy.Policy.Maximum
        )
        natural = tabs.sizeHint().height()
        tabs.resize(400, natural + 240)
        _flush()
        self.assertLessEqual(page.sizeHint().height(), natural + 8)

    def test_fill_available_expands_policy(self) -> None:
        tabs = ContentSizedTabWidget()
        tabs.addTab(QWidget(), "X")
        tabs.set_fill_available(True)
        self.assertEqual(
            tabs.sizePolicy().verticalPolicy(), QSizePolicy.Policy.Expanding
        )


if __name__ == "__main__":
    unittest.main()
