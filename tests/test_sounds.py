import sounds


def test_sound_path_for_known_categories():
    assert sounds.sound_path("person") == sounds.ASSETS_DIR / "person.mp3"
    assert sounds.sound_path("animal") == sounds.ASSETS_DIR / "animal.mp3"
    assert sounds.sound_path("vehicle") == sounds.ASSETS_DIR / "vehicle.mp3"


def test_sound_path_for_unknown_category_is_none():
    assert sounds.sound_path("unknown") is None
    assert sounds.sound_path("spaceship") is None


def test_bundled_assets_exist():
    for category in ("person", "animal", "vehicle"):
        assert sounds.sound_path(category).is_file()
