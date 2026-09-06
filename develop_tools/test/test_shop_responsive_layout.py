"""Regression tests for the PR2 shop / cafe configuration editors.

Run from the repository root with::

    python -m unittest develop_tools.test.test_shop_responsive_layout

The legacy suite targeted the old flow-layout shop editor (variable column
count, ShopRefreshBox, dialog-level ScrollArea). PR2 replaces that editor
with a fixed four-column GoodsCard grid inside a ShopPanel with internal
scrolling, so these assertions are rewritten against the new contract while
keeping the file name and run command unchanged.
"""

import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFontMetrics
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QBoxLayout, QFrame, QLabel, QScrollArea, QWidget
from qfluentwidgets import ScrollArea

from gui.components.expand import arenaShopPriority, shopPriority
from gui.components.expand.cafeInvite import Layout as CafeLayout
from gui.components.expand.shop_panel import estimate_common_shop_daily
from gui.components.shop_goods import (
    GRID_COLUMNS,
    GRID_H_SPACING,
    MIN_COL_W,
    SHOP_MAX_WIDTH,
    GoodsCard,
    ShopGoodCard,
    ShopGoodsGrid,
)
from gui.util.customized_ui import DialogSettingBox

_HORIZONTAL_CHROME = 48
_VERTICAL_CHROME = 129
_SCREEN_MARGIN = 64


class _Config:
    server_mode = "CN"

    def __init__(self, *, common_goods=None, arena_goods=None):
        common_goods = common_goods or []
        arena_goods = arena_goods or []
        self.static_config = SimpleNamespace(
            common_shop_price_list={self.server_mode: common_goods},
            tactical_challenge_shop_price_list={self.server_mode: arena_goods},
            student_names=[],
        )
        self.values = {
            "CommonShopList": [0] * len(common_goods),
            "CommonShopRefreshTime": 0,
            "TacticalChallengeShopList": [0] * len(arena_goods),
            "TacticalChallengeShopRefreshTime": 0,
        }

    def get(self, key=None, default=None, **kwargs):
        if key is None:
            key = kwargs.get("key")
        return self.values.get(key, default)

    def set(self, key=None, value=None, **kwargs):
        if key is None:
            key = kwargs.get("key")
        self.values[key] = value


class ShopAndCafeLayoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widgets = []

    def tearDown(self):
        for widget in reversed(self.widgets):
            widget.close()
            widget.deleteLater()
        self.app.processEvents()

    def _show(self, widget, width, height=900):
        self.widgets.append(widget)
        widget.resize(width, height)
        widget.show()
        self.app.processEvents()
        return widget

    # ------------------------------------------------------------------
    # shop_goods exports
    # ------------------------------------------------------------------

    def test_legacy_import_name_still_resolves(self):
        # ShopGoodCard is kept purely as an import-compatibility alias for
        # the new GoodsCard API; old positional signatures are gone.
        self.assertIs(ShopGoodCard, GoodsCard)

    def test_category_color_hook_returns_defaults(self):
        from gui.components.shop_goods import get_category_colors

        colors = get_category_colors()
        self.assertIn("default", colors)
        self.assertIsInstance(colors, dict)

    def test_category_colors_are_qcolor_parseable(self):
        # 深色模式下商品名用分类底色做描边字填充，QColor 解析失败
        # 会得到无效黑色（曾因此出现“所有文字黑底”的回归），
        # 所以分类底色必须是 QColor 可解析的十六进制值。
        from PyQt5.QtGui import QColor
        from gui.components.shop_goods import get_category_colors

        for name, value in get_category_colors().items():
            self.assertTrue(QColor(value).isValid(), name)

    def test_check_color_follows_theme_color_setting(self):
        # 勾色必须跟随设置中可修改的「主题颜色」（选中框除外）。
        from PyQt5.QtGui import QColor
        from gui.components.shop_goods import _check_color
        from gui.util.config_gui import configGui

        self.assertEqual(
            QColor(configGui.themeColor.value).name(), _check_color().name()
        )

    # ------------------------------------------------------------------
    # GoodsCard
    # ------------------------------------------------------------------

    def test_goods_card_click_toggles_selection_overlay(self):
        card = self._show(GoodsCard(2, "Item name", "125000"), 220, 120)
        events = []
        card.toggled.connect(lambda index, checked: events.append((index, checked)))

        self.assertFalse(card.is_checked())
        self.assertFalse(card._selection_frame.isVisible())

        QTest.mouseClick(card, Qt.LeftButton, pos=card.rect().center())
        self.app.processEvents()
        self.assertTrue(card.is_checked())
        self.assertTrue(card._selection_frame.isVisible())
        self.assertEqual([(2, True)], events)

        QTest.mouseClick(card, Qt.LeftButton, pos=card.rect().center())
        self.app.processEvents()
        self.assertFalse(card.is_checked())
        self.assertFalse(card._selection_frame.isVisible())
        self.assertEqual([(2, True), (2, False)], events)

    def test_long_names_wrap_without_fixed_height(self):
        long_name = "Random Intermediate Material 美游神明文字x5 상급 활동 보고서 x3"
        card = GoodsCard(0, long_name, "50")
        self.assertTrue(card.name_lbl.wordWrap())
        single_line = QFontMetrics(card.name_lbl.font()).height()
        self.assertGreater(card.name_lbl.heightForWidth(80), single_line)

    # ------------------------------------------------------------------
    # ShopGoodsGrid: always four columns
    # ------------------------------------------------------------------

    def test_grid_keeps_four_columns_at_any_width(self):
        grid = ShopGoodsGrid()
        grid.set_cards([GoodsCard(i, f"Item {i}", "50") for i in range(8)])
        self._show(grid, 800, 600)

        def column_count():
            return len({card.geometry().x() for card in grid.cards()})

        self.assertEqual(GRID_COLUMNS, column_count())

        grid.resize(500, 600)
        self.app.processEvents()
        self.assertEqual(GRID_COLUMNS, column_count())

        minimum = GRID_COLUMNS * MIN_COL_W + GRID_H_SPACING * (GRID_COLUMNS - 1)
        self.assertGreaterEqual(grid.minimumSizeHint().width(), minimum)

    def test_grid_minimum_height_follows_actual_width(self):
        # 网格最小高度必须跟随真实宽度：构造期宽度为 0 时曾按默认宽度
        # 量高并把该高度锁成最小高度，宽窗口下真实内容更矮，滚动区域
        # 误判内容超高、强行出滚动条并裁掉底部（竞技场商店的回归）。
        grid = ShopGoodsGrid()
        grid.set_cards([GoodsCard(i, f"Item {i} name", "50") for i in range(8)])
        self.widgets.append(grid)
        self.assertLessEqual(grid.minimumSizeHint().height(), 40)

        grid.resize(700, 400)
        self.app.processEvents()
        self.assertEqual(grid.heightForWidth(700), grid.minimumSizeHint().height())

    # ------------------------------------------------------------------
    # ShopPanel refresh input must match the executor's limit
    # ------------------------------------------------------------------

    def test_refresh_commit_is_clamped_to_three(self):
        config = _Config(common_goods=[["Advanced Report", 25, "creditpoints"]])
        editor = self._show(shopPriority.Layout(config=config), 800)

        editor.refresh_input.setText("9")
        editor.refresh_input.editingFinished.emit()
        self.app.processEvents()
        self.assertEqual("3", editor.refresh_input.text())
        self.assertEqual(3, config.get("CommonShopRefreshTime"))

        arena_config = _Config(arena_goods=[["Mashiro's Eleph", 50]])
        arena_editor = self._show(arenaShopPriority.Layout(config=arena_config), 800)
        arena_editor.refresh_input.setText("7")
        arena_editor.refresh_input.editingFinished.emit()
        self.app.processEvents()
        self.assertEqual("3", arena_editor.refresh_input.text())
        self.assertEqual(3, arena_config.get("TacticalChallengeShopRefreshTime"))

    def test_estimate_matches_runtime_refresh_prices(self):
        dummy = SimpleNamespace(tr=lambda text: text)

        # No refresh, credit-only: plain total with the unit, no detail line.
        title, detail = estimate_common_shop_daily(
            dummy, [1], 0, [["Credit Item", 100, "creditpoints"]]
        )
        self.assertIn("100", title)
        self.assertIn("信用点", title)
        self.assertEqual("", detail)

        # Refreshing costs pyroxene even when only credit items are picked,
        # so the detail line must surface 40 + 60 + 80.
        title, detail = estimate_common_shop_daily(
            dummy, [1], 3, [["Credit Item", 100, "creditpoints"]]
        )
        self.assertIn("400", title)
        self.assertIn("180", detail)

        # Pyroxene goods: item total x rounds plus the same refresh costs,
        # never the removed 100 / 120 tiers.
        _, detail = estimate_common_shop_daily(
            dummy, [1], 3, [["Report", 50, "pyroxene"]]
        )
        self.assertIn("380", detail)

        # Requests beyond the runtime cap are clamped, not extrapolated.
        clamped = estimate_common_shop_daily(
            dummy, [1], 9, [["Report", 50, "pyroxene"]]
        )
        self.assertEqual(clamped, estimate_common_shop_daily(
            dummy, [1], 3, [["Report", 50, "pyroxene"]]
        ))

    def test_checking_goods_updates_estimate_label(self):
        config = _Config(common_goods=[["Credit Item", 100, "creditpoints"]])
        editor = self._show(shopPriority.Layout(config=config), 800)

        editor.cards[0].set_checked(True)
        editor.refresh_input.setText("3")
        editor.refresh_input.editingFinished.emit()
        self.app.processEvents()

        self.assertIn("400", editor.estimate_label.text())
        self.assertEqual([1], config.get("CommonShopList"))

    # ------------------------------------------------------------------
    # Dark theme: upstream dark background + white text, no pure black
    # ------------------------------------------------------------------

    def test_dark_mode_shop_and_cafe_use_theme_tokens(self):
        from qfluentwidgets import Theme
        from gui.util.config_gui import configGui, COLOR_THEME

        dark = COLOR_THEME["Dark"]
        configGui.set(configGui.themeMode, Theme.DARK, save=False)
        try:
            # 商品卡：底色用深色主题底色（不是纯黑），商品名保留
            # 与分类底色一致的颜色，不用纯白字。
            card = GoodsCard(0, "Advanced Report", "50", category="exp_book")
            self.widgets.append(card)
            self.assertIn(dark["background"], card.styleSheet())
            self.assertNotIn("#000000", card.styleSheet())
            self.assertIn("#F2F0F0", card.name_lbl.styleSheet())

            # 商店顶栏：文字为主题白字；刷新控件为上游半透明构成，
            # 深色下不能整块发白。
            config = _Config(common_goods=[["Advanced Report", 25, "creditpoints"]])
            editor = shopPriority.Layout(config=config)
            self.widgets.append(editor)
            self.assertIn(dark["text"], editor.guide_label.styleSheet())
            self.assertIn("font-weight:bold", editor.guide_label.styleSheet())
            self.assertIn("rgba(0, 0, 0, 4)", editor.refresh_box.styleSheet())
            self.assertNotIn("palette(base)", editor.refresh_box.styleSheet())
            # 深色下面板底框必须透明，不能整块发白。
            self.assertIn("background:transparent", editor.styleSheet())

            # 咖啡厅：面板与单元格用深色主题底色，正文为近白字。
            cafe = CafeLayout(config=_Config())
            self.widgets.append(cafe)
            cells = cafe.findChildren(QFrame, "cafeCell")
            self.assertTrue(cells)
            for cell in cells:
                self.assertIn(dark["background"], cell.styleSheet())
            texts = cafe.findChildren(QLabel, "cafeText")
            self.assertTrue(texts)
            for label in texts:
                self.assertIn(dark["text"], label.styleSheet())
        finally:
            configGui.set(configGui.themeMode, Theme.LIGHT, save=False)

    # ------------------------------------------------------------------
    # DialogSettingBox sizing
    # ------------------------------------------------------------------

    def _expected_content_width(self, dialog, parent, lower, upper):
        available = dialog._available_geometry(parent)
        limit = min(parent.width(), available.width()) - _HORIZONTAL_CHROME - _SCREEN_MARGIN
        return max(lower, min(upper, limit))

    def test_shop_dialog_width_follows_viewport_formula(self):
        config = _Config(arena_goods=[[f"Item {index}", 50] for index in range(8)])
        parent = self._show(QWidget(), 640, 600)
        dialog = self._show(
            DialogSettingBox(
                parent,
                config,
                arenaShopPriority.Layout(config=config),
                setting_name="arenaShopPriority",
            ),
            parent.width(),
            parent.height(),
        )

        minimum = GRID_COLUMNS * MIN_COL_W + GRID_H_SPACING * (GRID_COLUMNS - 1)
        expected = self._expected_content_width(dialog, parent, minimum, SHOP_MAX_WIDTH)
        self.assertEqual(expected + _HORIZONTAL_CHROME, dialog.widget.width())

    def test_shop_dialog_height_is_bound_by_a_short_parent(self):
        config = _Config(arena_goods=[[f"Item {index}", 50] for index in range(8)])
        parent = self._show(QWidget(), 640, 300)
        dialog = self._show(
            DialogSettingBox(
                parent,
                config,
                arenaShopPriority.Layout(config=config),
                setting_name="arenaShopPriority",
            ),
            parent.width(),
            parent.height(),
        )

        available = dialog._available_geometry(parent)
        limit = min(parent.height(), available.height()) - _VERTICAL_CHROME - _SCREEN_MARGIN
        # 矮宿主窗口把内容高度上限压到单行商品最小高度以下，
        # 弹窗钉在最小高度上，并且留在宿主窗口以内。
        expected = max(160, limit)
        self.assertEqual(expected + _VERTICAL_CHROME, dialog.widget.height())
        # The dialog must stay inside a short host window instead of
        # spilling past it onto a taller monitor.
        self.assertLessEqual(dialog.widget.height(), parent.height())

    def test_shop_dialog_grows_with_content_height(self):
        # 弹窗高度必须随内容高度增长，不再被固定视口上限压矮：
        # 内容低于宿主窗口/屏幕上限时，弹窗高度就是内容高度本身
        # （竞技场商店一页放不下的回归）。构造期字体可能尚未应用
        # 到商品名标签、长名换行按更小字体量矮，show 时
        # _refit_shop_height 按就绪字体再量一次重设尺寸，断言的是
        # show 后的最终高度。
        config = _Config(arena_goods=[[f"Item {index}", 50] for index in range(8)])
        parent = self._show(QWidget(), 700, 720)
        panel = arenaShopPriority.Layout(config=config)
        dialog = self._show(
            DialogSettingBox(
                parent,
                config,
                panel,
                setting_name="arenaShopPriority",
            ),
            parent.width(),
            parent.height(),
        )

        available = dialog._available_geometry(parent)
        cap = max(
            160,
            min(parent.height(), available.height()) - _VERTICAL_CHROME - _SCREEN_MARGIN,
        )
        content_width = dialog.widget.width() - _HORIZONTAL_CHROME
        desired = panel.heightForWidth(content_width)
        self.assertEqual(min(cap, desired) + _VERTICAL_CHROME, dialog.widget.height())

    def test_shop_dialog_without_overflow_has_no_goods_scrollbar(self):
        # 商品一页放得下时，商品区不得出现竖向滚动条：弹窗高度按
        # 内容收窄后，任何量高误差（滚动条宽度、网格最小高度钉死）
        # 都会在这里显形。
        config = _Config(arena_goods=[[f"Item {index}", 50] for index in range(8)])
        parent = self._show(QWidget(), 700, 720)
        dialog = self._show(
            DialogSettingBox(
                parent,
                config,
                arenaShopPriority.Layout(config=config),
                setting_name="arenaShopPriority",
            ),
            parent.width(),
            parent.height(),
        )
        self.app.processEvents()

        scroll = dialog.findChild(QScrollArea, "shopGoodsScroll")
        self.assertIsNotNone(scroll)
        self.assertEqual(0, scroll.verticalScrollBar().maximum())

    def test_cafe_dialog_reaches_narrow_vertical_mode(self):
        narrow_parent = self._show(QWidget(), 600, 700)
        narrow_cafe = CafeLayout(config=_Config())
        narrow_dialog = self._show(
            DialogSettingBox(
                narrow_parent,
                narrow_cafe.config,
                narrow_cafe,
                setting_name="cafeinvite",
            ),
            narrow_parent.width(),
            narrow_parent.height(),
        )

        expected = self._expected_content_width(narrow_dialog, narrow_parent, 360, 820)
        self.assertEqual(expected + _HORIZONTAL_CHROME, narrow_dialog.widget.width())
        # The content must be able to drop below the 640 px breakpoint so
        # the cafe layout can actually switch to its vertical arrangement.
        self.assertLess(expected, 640)
        self.assertEqual(QBoxLayout.TopToBottom, narrow_cafe.root_layout.direction())

        # Vertically stacked content is taller than the dialog; a scroll
        # area must carry the overflow instead of clipping controls.
        narrow_scroll = narrow_dialog.findChild(ScrollArea)
        self.assertIsNotNone(narrow_scroll)
        self.assertGreater(narrow_scroll.verticalScrollBar().maximum(), 0)

        wide_parent = self._show(QWidget(), 1200, 800)
        wide_cafe = CafeLayout(config=_Config())
        wide_dialog = self._show(
            DialogSettingBox(
                wide_parent,
                wide_cafe.config,
                wide_cafe,
                setting_name="cafeinvite",
            ),
            wide_parent.width(),
            wide_parent.height(),
        )
        self.assertGreater(wide_dialog.widget.width(), narrow_dialog.widget.width())
        self.assertEqual(QBoxLayout.LeftToRight, wide_cafe.root_layout.direction())

    def test_cafe_titles_follow_palette(self):
        cafe = CafeLayout(config=_Config())
        self.widgets.append(cafe)

        left_title = next(
            label for label in cafe.findChildren(QLabel) if label.text() == "通用设置"
        )
        self.assertIn("palette(text)", left_title.styleSheet())
        self.assertNotIn("#1a1a1a", left_title.styleSheet())

        # card1 is active by default -> palette(text); card2 starts inactive
        # -> palette(placeholder-text). Both must stay palette-driven, never
        # a hard-coded dark hex that becomes unreadable on dark themes.
        self.assertIn("palette(text)", cafe.card1.title_lbl.styleSheet())
        self.assertNotIn("#1a1a1a", cafe.card1.title_lbl.styleSheet())
        self.assertIn("palette(placeholder-text)", cafe.card2.title_lbl.styleSheet())
        self.assertNotIn("#1a1a1a", cafe.card2.title_lbl.styleSheet())


if __name__ == "__main__":
    unittest.main()
