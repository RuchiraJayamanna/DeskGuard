import json

from deskguard.config import Config, ConfigStore, from_dict


def test_missing_file_gives_defaults(tmp_path):
    cfg = ConfigStore(tmp_path / "config.json").load()
    assert cfg == Config()
    assert (cfg.window_w, cfg.window_h) == (240, 150)
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
    assert (cfg.width, cfg.height) == (640, 480)
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
