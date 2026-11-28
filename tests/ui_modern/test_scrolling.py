import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QTreeWidgetItem, QWidget
from qfluentwidgets import ScrollArea, TreeWidget

from ui_modern.app import NativeWheelScrolling


@pytest.mark.parametrize("kind", ["page", "picker"])
def test_wheel_moves_immediately_and_stops(app, pump, kind):
    scrolling = NativeWheelScrolling()
    app.installEventFilter(scrolling)
    view = ScrollArea() if kind == "page" else TreeWidget()
    try:
        if kind == "page":
            content = QWidget()
            content.resize(400, 2000)
            view.setWidget(content)
        else:
            for index in range(100):
                QTreeWidgetItem(view, [str(index)])
        view.resize(300, 180)
        view.show()
        app.processEvents()
        bar = view.verticalScrollBar()
        bar.setValue(bar.maximum() // 2)
        positions = [bar.value()]
        for delta in (-120, -120, 120):
            event = QWheelEvent(QPointF(10, 10), QPointF(10, 10), QPoint(), QPoint(0, delta),
                                Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
            app.sendEvent(view.viewport(), event)
            positions.append(bar.value())
        first, second, third, reversed_once = positions
        assert second - first == third - second > 0
        assert reversed_once == second
        pump(timeout=.5)
        assert bar.value() == reversed_once
    finally:
        app.removeEventFilter(scrolling)
        view.close()
