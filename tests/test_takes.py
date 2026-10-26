"""Which record sides a take covers, and the take options offered for single and double LPs."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox

from vinyl.ui.main_window import ALL_SIDES, fill_side_combo, side_name, take_sides

app = QApplication.instance() or QApplication([])


def options(sides):
    combo = QComboBox()
    fill_side_combo(combo, sides, whole=True)
    return [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]


def test_single_lp_options():
    assert [d for _, d in options(["A", "B"])] == [ALL_SIDES, "A", "B"]


def test_double_lp_offers_each_disc():
    opts = options(["A", "B", "C", "D"])
    assert [d for _, d in opts] == [ALL_SIDES, "A+B", "C+D", "A", "B", "C", "D"]
    assert opts[2][0] == "Disc 2: sides C+D in one take"


def test_take_sides_and_names():
    assert take_sides(ALL_SIDES, ["A", "B", "C", "D"]) == ["A", "B", "C", "D"]
    assert take_sides("C+D", ["A", "B", "C", "D"]) == ["C", "D"]
    assert take_sides("AA", ["A", "AA"]) == ["AA"]
    assert side_name("C+D") == "Sides C+D" and side_name("A") == "Side A" and side_name(ALL_SIDES) == "Whole LP"
