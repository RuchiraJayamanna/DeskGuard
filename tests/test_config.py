import json

from deskguard.config import Config, ConfigStore, from_dict


def test_missing_file_gives_defaults(tmp_path):
    cfg = ConfigStore(tmp_path / "config.json").load()
    assert cfg == Config()
    assert (cfg.window_w, cfg.window_h) == (256, 144)
    assert (cfg.width, cfg.height) == (640, 360) and cfg.flip_h is True and cfg.ignore_zones == []
    assert cfg.motion_enabled is False and cfg.always_on_top is True and cfg.fps == 10


def test_round_trip(tmp_path):
    store = ConfigStore(tmp_path / "sub" / "config.json")
    cfg = Config(camera_index=1, camera_name="USB Cam", width=1280, height=720, fps=15,
                 window_x=-500, window_y=20, window_w=320, window_h=200, opacity=85,
                 motion_enabled=True, motion_sensitivity="high", motion_cooldown=10)
    assert store.save(cfg)
    assert store.load() == cfg
    assert not (tmp_path / "sub" / "config.tmp").exists()  # atomic save cleaned up


def test_invalid_values_fall_back_to_defaults():
    cfg = from_dict({
        "fps": 60, "opacity": 3, "motion_sensitivity": "extreme", "window_w": 5,
        "always_on_top": "yes", "camera_index": -1, "width": 999, "height": 1,
        "unknown_key": 1, "motion_cooldown": 5, "window_x": None,
    })
    d = Config()
    assert (cfg.fps, cfg.opacity, cfg.motion_sensitivity, cfg.window_w) == (d.fps, d.opacity, d.motion_sensitivity, d.window_w)
    assert cfg.always_on_top is True and cfg.camera_index == 0
    assert (cfg.width, cfg.height) == (640, 360)
    assert cfg.motion_cooldown == 5


def test_bool_is_not_accepted_as_int():
    assert from_dict({"camera_index": True}).camera_index == 0


def test_corrupt_file_is_backed_up(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    cfg = ConfigStore(path).load()
    assert cfg == Config()
    assert (tmp_path / "config.bad.json").exists()
    assert not path.exists()


def test_non_object_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps([1, 2]), encoding="utf-8")
    assert ConfigStore(path).load() == Config()


def test_new_fields_round_trip(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    cfg = Config(flip_h=False, flip_v=True, zoom=2.5, pan_x=0.3, pan_y=0.7,
                 ignore_zones=[[0.5, 0.4, 0.5, 0.6]], camera_props={"HP Cam": {"brightness": 10.0}})
    assert store.save(cfg)
    assert store.load() == cfg


def test_invalid_new_fields_fall_back():
    cfg = from_dict({
        "zoom": 9, "pan_x": -1, "flip_h": 1,
        "ignore_zones": [[0, 0, 0, 0.5]],                   # zero width
        "camera_props": {"Cam": {"evil_prop": 1}},
        "config_version": 2,
    })
    d = Config()
    assert (cfg.zoom, cfg.pan_x, cfg.flip_h, cfg.ignore_zones, cfg.camera_props) == (
        d.zoom, d.pan_x, d.flip_h, [], {})
    assert from_dict({"ignore_zones": [[0, 0, 0.1, 0.1]] * 9, "config_version": 2}).ignore_zones == []


def test_v1_defaults_migrate_to_wide_view():
    old = {"width": 640, "height": 480, "window_w": 240, "window_h": 150, "fps": 15}
    cfg = from_dict(old)
    assert (cfg.width, cfg.height, cfg.window_w, cfg.window_h, cfg.fps) == (640, 360, 256, 144, 15)
    assert cfg.flip_h is True and cfg.config_version == 2
    assert old["width"] == 640 and old["height"] == 480  # input not mutated


def test_v1_user_choices_are_kept():
    cfg = from_dict({"width": 1920, "height": 1080, "window_w": 160, "window_h": 100})
    assert (cfg.width, cfg.height, cfg.window_w, cfg.window_h) == (1920, 1080, 160, 100)


def test_v2_640x480_is_not_migrated_again():
    cfg = from_dict({"width": 640, "height": 480, "config_version": 2})
    assert (cfg.width, cfg.height) == (640, 480)
