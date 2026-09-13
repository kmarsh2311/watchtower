from librarymanager import scene_hook_has_naming_changes


def test_empty_scene_update_does_not_trigger_automatic_rename():
    assert scene_hook_has_naming_changes({}) is False
    assert scene_hook_has_naming_changes(None) is False


def test_path_only_scene_update_does_not_trigger_automatic_rename():
    assert scene_hook_has_naming_changes({"path": "/tmp/renamed.m4v"}) is False


def test_actual_naming_metadata_edit_still_triggers_automatic_rename():
    assert scene_hook_has_naming_changes({"title": "New title"}) is True
    assert scene_hook_has_naming_changes({"studio_id": "12"}) is True
    assert scene_hook_has_naming_changes({"performer_ids": ["1", "2"]}) is True
