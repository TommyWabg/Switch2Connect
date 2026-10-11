from dataclasses import replace
import pytest
from app_core.models import ControllerInfo, SlotInfo
from ui_modern.widgets.player_card import PlayerCard, CARD_HEIGHT


@pytest.mark.parametrize("kind,hold,merged,image", [
    ("joycon_left", "Vertical", False, "joycon2left"),
    ("joycon_right", "Vertical", False, "joycon2right"),
    ("joycon_left", "Horizontal", False, "joycon2left_sideway"),
    ("joycon_right", "Horizontal", False, "joycon2right_sideway"),
    ("joycon_pair", None, True, "joycon2leftandright"),
    ("gamecube", None, False, "nsogamecubecontroller"),
    ("pro2", None, False, "procontroller2"),
])
def test_classic_illustration_and_hold_refresh(app, kind, hold, merged, image):
    card = PlayerCard(lambda *a: None)
    slot = SlotInfo(0, 1, controllers=(ControllerInfo(kind, kind, "a", "ble"),),
                    is_empty=False, is_merged_pair=merged, hold_mode=hold)
    card.update_snapshot(slot)
    assert card.illustration.path.stem == image and not card.illustration.pixmap.isNull()
    if kind == "joycon_left" and hold == "Vertical":
        card.update_snapshot(replace(slot, hold_mode="Horizontal"))
        assert card.illustration.path.stem == "joycon2left_sideway"
    card.deleteLater()


def test_visible_merge_selection_target_split_and_hold(app, pump):
    calls = []; card = PlayerCard(lambda *args: calls.append(args)); card.resize(230, CARD_HEIGHT)
    slot = SlotInfo(0, 1, controllers=(ControllerInfo("joycon_left", "Joy-Con L", "a", "ble"),),
                    is_empty=False, slot_token=(0, 12), merge_available=True,
                    pending_merge_index=0, hold_mode_available=True, hold_mode="Vertical", ping_available=True)
    card.update_snapshot(slot); card.show(); pump()
    assert card.merge.text() == "Selecting..." and not card.merge_hint.isHidden()
    assert not card.hold.isHidden() and "2px solid" in card.styleSheet()
    assert card.ping.geometry().right() < card.merge.geometry().left()
    assert card.merge.geometry().right() < card.more.geometry().left()
    card.merge.click(); assert calls[-1] == ("merge_select", (0, 12))
    card.hold.setCurrentItem("Horizontal"); assert calls[-1] == ("set_hold_mode", (0, 12), "Horizontal")
    card.update_snapshot(replace(slot, pending_merge_index=1, merge_target=True))
    assert card.merge.text() == "Merge" and "#e88724" in card.styleSheet()
    card.update_snapshot(replace(slot, pending_merge_index=None, is_merged_pair=True, split_available=True, hold_mode_available=False))
    assert card.merge.text() == "Split" and card.hold.isHidden()
    card.merge.click(); assert calls[-1] == ("split", (0, 12))
    card.hide(); card.deleteLater()
