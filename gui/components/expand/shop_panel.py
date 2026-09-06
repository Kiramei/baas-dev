# -*- coding: utf-8 -*-
"""商店页壳：顶栏 + 商品 4 列网格（积木在 shop_goods）。

Card 模式（设置弹窗）：
  顶栏贴顶不滚（置顶标题的 ScrollPane），商品区内部滚动。
  顶栏第 1 行：「请勾选购买物品」 | 货币图标+单位；
  第 2 行：完整日耗公式 | 刷新次数（宽度不足时自动换为上下排列）。

List 模式（设置页内嵌展开卡）：
  不套内部滚动区，整块跟随外层页面顺畅滚动：
  「请勾选购买物品」+ 货币图标+单位、日耗算式依次放在刷新次数之上，
  刷新次数由一个 Panel 包裹，商品网格直接平铺。
"""
from __future__ import annotations

from typing import List, Sequence, Tuple

from PyQt5.QtCore import Qt, QSize
from PyQt5.QtWidgets import (
    QBoxLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import LineEdit, ImageLabel, ExpandSettingCard

from gui.util.config_gui import configGui, COLOR_THEME
from gui.util.translator import baasTranslator as bt
from gui.components.shop_goods import (
    DEFAULT_VIEW_WIDTH,
    GRID_COLUMNS,
    GRID_H_SPACING,
    MIN_COL_W,
    SIDE_MARGIN,
    GoodsCard,
    ShopGoodsGrid,
    classify_goods,
    is_dark_theme,
)


PAD_Y = 8
# Card 模式商品区外包层的上下内边距，量总高时必须一并算入。
CARD_BODY_PAD_TOP = 4
CARD_BODY_PAD_BOTTOM = 8
# Card 模式商品区竖向滚动条占用的宽度（含余量）。量总高时网格按
# 扣除该宽度后的可用宽度量取：内容恰好放得下时滚动条不会出现，
# 弹窗只是极轻微贴边收口；内容真正超出时，出滚动条后的网格按更窄
# 宽度重排的高度不会超过已量高度，不会出现「越滚越矮不下」的循环。
CARD_SCROLLBAR_RESERVE = 12


def _theme_colors() -> dict:
    """商店顶栏主题配色：文字颜色跟随亮/暗主题。"""
    return {
        'text_color': COLOR_THEME[configGui.theme.value]['text'],
    }


def _display_mode() -> str:
    """当前设置页显示模式：'Card'（弹窗卡片）或 'List'（内嵌列表）。"""
    try:
        mode = str(configGui.configDisplayType.value)
    except Exception:
        mode = "Card"
    return "List" if mode.lower().startswith("list") else "Card"


def _safe_int(text, default=0) -> int:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return default


def _price_of(item) -> int:
    try:
        return int(item[1])
    except (TypeError, ValueError, IndexError):
        return 0


def _raw_name(item) -> str:
    try:
        return str(item[0])
    except Exception:
        return ""


def _item_name(item) -> str:
    try:
        return bt.tr("ConfigTranslation", item[0])
    except Exception:
        return _raw_name(item)


def _price_text(item) -> str:
    return str(_price_of(item))


def _tune_scroll(scroll_area: QScrollArea) -> None:
    """Set predictable wheel increments through public scrollbar APIs."""
    bar = scroll_area.verticalScrollBar()
    bar.setSingleStep(48)
    bar.setPageStep(200)


class ShopPanel(QWidget):
    """顶栏 + 商品 4 列网格；Card 模式内部滚动，List 模式整块平铺。"""

    def __init__(
        self,
        parent=None,
        config=None,
        *,
        goods_key: str,
        refresh_key: str,
        price_list: Sequence,
        currency_unit_label: str,
        refresh_max: int,
        estimate_fn=None,
        currency_icon: str = "",
        **_ignored,
    ):
        super().__init__(parent=parent)
        self.config = config
        self.goods_key = goods_key
        self.refresh_key = refresh_key
        self.price_list = list(price_list or [])
        self.estimate_fn = estimate_fn
        self._refresh_max = int(refresh_max)
        self._currency_icon = str(currency_icon or "")
        self._mode = _display_mode()

        raw_goods = None
        try:
            raw_goods = self.config.get(key=goods_key)
        except Exception:
            try:
                raw_goods = self.config.get(goods_key)
            except Exception:
                raw_goods = None
        goods = list(raw_goods) if isinstance(raw_goods, (list, tuple)) else []
        n = max(len(goods), len(self.price_list))
        if len(goods) < n:
            goods = goods + [0] * (n - len(goods))
        if len(self.price_list) < n:
            n = min(len(goods), len(self.price_list))
            goods = goods[:n]
        self.goods = goods[:n]
        self.price_list = self.price_list[:n]
        self.goods_count = n

        self.setObjectName("shopPanel")
        self.setMinimumWidth(0)
        # 面板自身底色在 _apply_theme 里按主题设置：浅色纯白，深色透明。
        self.setAttribute(Qt.WA_StyledBackground, True)
        # Card 模式填满固定弹窗；List 模式高度随内容，跟随外层页面滚动。
        self.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Expanding if self._mode == "Card" else QSizePolicy.Minimum,
        )
        self.setProperty("shopInternalScroll", self._mode == "Card")
        self.setProperty("hoardSingleScroll", self._mode == "Card")

        root = QVBoxLayout(self)
        self._root = root
        # List 模式整块跟随外层页面滚动，底部固定留 10 像素空位：
        # 既隔开最后一行商品与下方选项，又不会撑出更大空档。
        self._pad_bottom = PAD_Y if self._mode == "Card" else 10
        root.setContentsMargins(SIDE_MARGIN, PAD_Y, SIDE_MARGIN, self._pad_bottom)
        root.setSpacing(6)
        root.setAlignment(Qt.AlignTop)

        # ===== 顶栏 =====
        head = QFrame(self)
        head.setObjectName("shopStickyHead")
        head.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        hv = QVBoxLayout(head)
        hv.setContentsMargins(0, 0, 0, 0)
        hv.setSpacing(6)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        # 字体完全跟随上游统一规则（弹窗的 * 样式表 / 设置卡的 QLabel
        # 级联），不再单独指定字号字重，避免与其他选项卡不一致。
        self.guide_label = QLabel(self.tr("请勾选购买物品"), head)
        self.guide_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.guide_label.setWordWrap(False)
        self.guide_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        title_row.addWidget(self.guide_label, 0, Qt.AlignLeft | Qt.AlignVCenter)

        self._unit_icon = None
        if self._currency_icon:
            try:
                icon = ImageLabel(self._currency_icon, head)
                icon.setFixedSize(28, 28)
                icon.setToolTip(currency_unit_label)
                self._unit_icon = icon
                title_row.addWidget(icon, 0, Qt.AlignVCenter)
            except Exception:
                self._unit_icon = None
        self.unit_label = QLabel(currency_unit_label, head)
        self.unit_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.unit_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        title_row.addWidget(self.unit_label, 0, Qt.AlignLeft | Qt.AlignVCenter)
        title_row.addStretch(1)
        hv.addLayout(title_row)

        self.estimate_label = QLabel("", head)
        self.estimate_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        refresh_box = QFrame(head)
        refresh_box.setObjectName("shopRefreshBox")
        refresh_box.setStyleSheet(
            "QFrame#shopRefreshBox {"
            "  border: 1px solid rgba(0, 0, 0, 55);"
            "  border-radius: 6px;"
            "  background: rgba(0, 0, 0, 4);"
            "}"
        )
        rr = QHBoxLayout(refresh_box)
        rr.setContentsMargins(10, 6, 10, 6)
        rr.setSpacing(8)
        self.refresh_label = QLabel(self.tr("购买刷新次数"), refresh_box)
        self.refresh_label.setWordWrap(True)
        self.refresh_input = LineEdit(refresh_box)
        self.head = head
        self.refresh_box = refresh_box
        self.refresh_input.setFixedWidth(72)
        self.refresh_input.setPlaceholderText("0")
        try:
            self.refresh_input.setText(str(self.config.get(refresh_key)))
        except Exception:
            self.refresh_input.setText("0")
        self.refresh_input.editingFinished.connect(self._commit_refresh)
        self.refresh_input.textChanged.connect(self._on_refresh_text_changed)
        rr.addWidget(self.refresh_label, 1, Qt.AlignVCenter)
        rr.addWidget(self.refresh_input, 0, Qt.AlignVCenter)
        refresh_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        self._head_details = None
        if self._mode == "Card":
            # 公式与刷新次数同行；宽度不足时换为上下排列。
            self.estimate_label.setWordWrap(False)
            self.estimate_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            self._head_details = QBoxLayout(QBoxLayout.LeftToRight)
            self._head_details.setContentsMargins(0, 0, 0, 0)
            self._head_details.setSpacing(10)
            self._head_details.addWidget(
                self.estimate_label,
                1,
                Qt.AlignLeft | Qt.AlignVCenter,
            )
            self._head_details.addWidget(
                refresh_box,
                0,
                Qt.AlignRight | Qt.AlignVCenter,
            )
            hv.addLayout(self._head_details)
        else:
            # List 模式：算式与刷新次数上下排列，放在刷新次数之前的
            # 顺序即「请勾选购买物品、货币单位、算式」在上，保证外层
            # 页面可以顺畅滚完全部内容。
            self.estimate_label.setWordWrap(True)
            self.estimate_label.setSizePolicy(
                QSizePolicy.Expanding, QSizePolicy.Minimum
            )
            hv.addWidget(self.estimate_label, 0)
            hv.addWidget(refresh_box, 0)
        root.addWidget(head, 0)

        # ===== 商品区 =====
        self.grid = ShopGoodsGrid(self if self._mode == "List" else None)
        cards: List[GoodsCard] = []
        for i in range(self.goods_count):
            item = self.price_list[i]
            raw = _raw_name(item)
            cat = classify_goods(raw)
            card = GoodsCard(
                index=i,
                name=_item_name(item),
                price_text=_price_text(item),
                checked=bool(self.goods[i] == 1),
                category=cat,
                parent=self.grid,
            )
            card.toggled.connect(self._on_card_toggled)
            cards.append(card)
        self.grid.set_cards(cards)
        self.cards = cards

        self._scroll = None
        if self._mode == "Card":
            self._scroll = QScrollArea(self)
            self._scroll.setObjectName("shopGoodsScroll")
            self._scroll.setWidgetResizable(True)
            self._scroll.setFrameShape(QFrame.NoFrame)
            self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            self._scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            self._scroll.setStyleSheet(
                "QScrollArea#shopGoodsScroll{background:transparent;border:none;}"
                "QScrollBar:vertical{width:10px;background:transparent;margin:2px;}"
                "QScrollBar::handle:vertical{"
                "background:rgba(100,140,180,130);border-radius:5px;min-height:28px;}"
                "QScrollBar::handle:vertical:hover{background:rgba(80,130,180,180);}"
                "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
                "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}"
            )
            try:
                self._scroll.viewport().setStyleSheet("background:transparent;")
            except Exception:
                pass
            _tune_scroll(self._scroll)

            body = QWidget()
            body.setObjectName("shopGoodsBody")
            body.setAttribute(Qt.WA_StyledBackground, True)
            body.setStyleSheet("QWidget#shopGoodsBody{background:transparent;}")
            body.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
            bv = QVBoxLayout(body)
            bv.setContentsMargins(0, CARD_BODY_PAD_TOP, 0, CARD_BODY_PAD_BOTTOM)
            bv.setSpacing(0)
            bv.setAlignment(Qt.AlignTop)
            self.grid.setParent(body)
            bv.addWidget(self.grid, 0, Qt.AlignTop)
            bv.addStretch(1)

            self._scroll.setWidget(body)
            root.addWidget(self._scroll, 1)
        else:
            # List 模式：不套内部滚动区，网格直接平铺跟随外层滚动。
            root.addWidget(self.grid, 0, Qt.AlignTop)

        self._refresh_estimate()
        self._apply_theme()
        configGui.themeChanged.connect(self._apply_theme)
        if self._mode == "Card":
            self._update_header_direction(self.width())
        self._allow_click_outside_to_commit()

    # ------------------------------------------------------------------
    # 主题
    # ------------------------------------------------------------------

    def _apply_theme(self, *_):
        """顶栏文字颜色跟随亮/暗主题；面板底色浅色纯白、深色透明。"""
        colors = _theme_colors()
        text_style = f"color:{colors['text_color']};background:transparent;"
        # 「请勾选购买物品」作为顶栏标题使用粗体。
        self.guide_label.setStyleSheet(text_style + "font-weight:bold;")
        for label in (
            self.unit_label,
            self.estimate_label,
            self.refresh_label,
        ):
            label.setStyleSheet(text_style)
        panel_bg = "transparent" if is_dark_theme() else "#FFFFFF"
        self.setStyleSheet(f"QWidget#shopPanel{{background:{panel_bg};}}")

    # ------------------------------------------------------------------
    # 顶栏方向（仅 Card 模式）
    # ------------------------------------------------------------------

    def _update_header_direction(self, width: int):
        if self._head_details is None:
            return
        required = (
            self.estimate_label.sizeHint().width()
            + self.refresh_box.sizeHint().width()
            + self._head_details.spacing()
            + SIDE_MARGIN * 2
        )
        direction = (
            QBoxLayout.TopToBottom
            if int(width or 0) < required
            else QBoxLayout.LeftToRight
        )
        if self._head_details.direction() != direction:
            self._head_details.setDirection(direction)
            self.updateGeometry()

    def _allow_click_outside_to_commit(self):
        """Commit the refresh input when clicking anywhere else.

        Qt-native: blank areas become click-focusable, so a click outside the
        input moves focus and triggers the native editingFinished signal.
        No application-wide event filtering is involved.
        """
        self.setFocusPolicy(Qt.ClickFocus)
        window = self.window()
        if window is not None and window is not self:
            if window.focusPolicy() == Qt.NoFocus:
                window.setFocusPolicy(Qt.ClickFocus)
        for widget in self.findChildren(QWidget):
            if widget is self.refresh_input:
                continue
            if self.refresh_input.isAncestorOf(widget):
                continue
            if widget.focusPolicy() == Qt.NoFocus:
                widget.setFocusPolicy(Qt.ClickFocus)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._mode == "Card":
            self._update_header_direction(event.size().width())
        else:
            self._sync_list_height(event.size().width())

    def _sync_list_height(self, width: int):
        """List 模式：宽度定下后让外层展开卡按真实内容高度重算。

        展开卡的固定高度只在展开/增删控件时取自 sizeHint，窗口或
        内容宽度变化后不会自己重算；面板每次定宽后主动触发一次，
        底部就不会预留出多余空白。
        """
        if int(width or 0) <= 0:
            return
        card = self._host_expand_card()
        if card is not None:
            card._adjustViewSize()

    def _host_expand_card(self):
        parent = self.parent()
        while parent is not None:
            if isinstance(parent, ExpandSettingCard):
                return parent
            parent = parent.parent()
        return None

    # ------------------------------------------------------------------
    # 尺寸：List 模式高度完全随内容，供外层页面顺畅滚动
    # ------------------------------------------------------------------

    def _min_content_width(self) -> int:
        return (
            GRID_COLUMNS * MIN_COL_W
            + GRID_H_SPACING * (GRID_COLUMNS - 1)
            + SIDE_MARGIN * 2
        )

    def _content_height_at(self, width: int) -> int:
        inner = max(1, int(width) - SIDE_MARGIN * 2)
        if self._mode == "Card":
            # 先按目标宽度决定顶栏排列方向再量高：构造期宽度为 0，
            # 公式与刷新次数被量成上下堆叠，会把弹窗量得偏高。
            self._update_header_direction(int(width))
        # 顶栏里有自动换行的标签，必须按当前宽度量高；直接取
        # sizeHint 会在标签尚未定宽时得到远超实际的高度。
        if self.head.hasHeightForWidth():
            head_h = self.head.heightForWidth(inner)
        else:
            head_h = self.head.sizeHint().height()
        # Card 模式给滚动条预留宽度后再量网格（见 CARD_SCROLLBAR_RESERVE）；
        # List 模式无内部滚动，直接按可用宽度量。
        grid_w = (
            max(1, inner - CARD_SCROLLBAR_RESERVE)
            if self._mode == "Card"
            else inner
        )
        grid_h = self.grid.heightForWidth(grid_w)
        body_pad = (
            CARD_BODY_PAD_TOP + CARD_BODY_PAD_BOTTOM
            if self._mode == "Card"
            else 0
        )
        return (
            PAD_Y
            + self._pad_bottom
            + head_h
            + self._root.spacing()
            + body_pad
            + grid_h
        )

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._content_height_at(width)

    def sizeHint(self):
        if self._mode == "List":
            # 用当前真实宽度报高，避免按固定宽度预估造成底部留白。
            width = max(
                self.width() if self.width() > 0 else DEFAULT_VIEW_WIDTH,
                self._min_content_width(),
            )
            return QSize(width, self._content_height_at(width))
        return QSize(DEFAULT_VIEW_WIDTH, 440)

    def minimumSizeHint(self):
        if self._mode == "List":
            # 最小高度必须按当前真实宽度量：布局引擎不允许控件矮于
            # minimumSizeHint 的高度，若按最窄宽度量高，宽窗口下会
            # 永久预留一条长名称换行出来的空白带。
            width = max(
                self.width() if self.width() > 0 else DEFAULT_VIEW_WIDTH,
                self._min_content_width(),
            )
            return QSize(self._min_content_width(), self._content_height_at(width))
        return QSize(480, 280)

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------

    def _on_card_toggled(self, index: int, checked: bool):
        self.goods[index] = 1 if checked else 0
        self.config.set(key=self.goods_key, value=list(self.goods))
        self._refresh_estimate()

    def _on_refresh_text_changed(self, text: str):
        cleaned = "".join(ch for ch in (text or "") if ch.isdigit())
        if cleaned != text:
            pos = self.refresh_input.cursorPosition()
            self.refresh_input.blockSignals(True)
            self.refresh_input.setText(cleaned)
            self.refresh_input.setCursorPosition(
                max(0, pos - (len(text) - len(cleaned)))
            )
            self.refresh_input.blockSignals(False)

    def _commit_refresh(self):
        raw = (self.refresh_input.text() or "").strip()
        val = 0 if raw == "" else _safe_int(raw, 0)
        val = max(0, min(val, self._refresh_max))
        self.refresh_input.blockSignals(True)
        self.refresh_input.setText(str(val))
        self.refresh_input.blockSignals(False)
        self.config.set(self.refresh_key, val)
        self._refresh_estimate()

    def _checked_mask(self) -> List[int]:
        return [1 if c.is_checked() else 0 for c in self.cards]

    def _refresh_estimate(self):
        if not self.estimate_fn:
            self.estimate_label.setText("")
            return
        refresh_n = _safe_int(self.refresh_input.text(), 0)
        title, detail = self.estimate_fn(
            self, self._checked_mask(), refresh_n, self.price_list
        )
        parts = [str(part).replace("\n", " ").strip() for part in (title, detail)]
        self.estimate_label.setText(" ".join(part for part in parts if part))
        self.estimate_label.updateGeometry()


def estimate_arena_daily(panel, checked: List[int], refresh_n: int, price_list: Sequence) -> Tuple[str, str]:
    refresh_n = max(0, min(int(refresh_n), 3))
    one_pass = 0
    for i, flag in enumerate(checked):
        if flag and i < len(price_list):
            one_pass += _price_of(price_list[i])
    total = one_pass + one_pass * refresh_n + refresh_n * 10
    title = panel.tr("每天消耗约 {0} 竞技币").replace("{0}", str(total))
    detail = panel.tr("({0}+{0}×{1}+{1}×10)").replace("{0}", str(one_pass)).replace(
        "{1}", str(refresh_n)
    )
    return title, detail


def estimate_common_shop_daily(panel, checked: List[int], refresh_n: int, price_list: Sequence) -> Tuple[str, str]:
    # 运行时只支持 3 次刷新（价格 40/60/80），估算必须与执行器一致。
    refresh_n = max(0, min(int(refresh_n), 3))
    one_pass_credit = 0
    one_pass_pyro = 0
    for i, flag in enumerate(checked):
        if not flag or i >= len(price_list):
            continue
        item = price_list[i]
        price = _price_of(item)
        currency = item[2] if len(item) > 2 else "creditpoints"
        if currency == "creditpoints":
            one_pass_credit += price
        else:
            one_pass_pyro += price
    rounds = refresh_n + 1
    credit_total = one_pass_credit * rounds
    pyro_refresh = 0
    costs = [40, 60, 80]
    for k in range(refresh_n):
        pyro_refresh += costs[k]
    pyro_total = one_pass_pyro * rounds + pyro_refresh
    if one_pass_pyro or pyro_refresh:
        title = panel.tr("每天约 {0} 信用点").replace("{0}", str(credit_total))
        detail = panel.tr("青辉石约 {0}（含刷新）").replace("{0}", str(pyro_total))
        return title, detail
    title = panel.tr("每天消耗约 {0} 信用点").replace("{0}", str(credit_total))
    return title, ""
