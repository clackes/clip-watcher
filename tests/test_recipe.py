"""What a Short was rendered with is kept beside the staged clip and comes back as it went in."""

from clipwatcher.models import Word
from clipwatcher.render import recipe


def test_a_recipe_comes_back_as_the_arguments_of_the_render(tmp_path):
    words = [Word(0.5, 0.9, "NO"), Word(0.9, 1.4, "WAY")]
    layout = {
        "has_webcam": True,
        "cam": (10, 20, 300, 200),
        "game": "1920:1080:0:0",
        "face": None,
        "reframe": False,
    }
    recipe.keep(
        tmp_path, words=words, hook="Watch this", layout=layout, beats=[(3.0, "hype", "*W*")],
        actions=[(5.0, 7.5, "*WATCHING*")], cold_open=(12.0, 2.0),
    )  # fmt: skip
    got = recipe.read(tmp_path)
    assert got["words"] == words and got["hook"] == "Watch this"
    assert got["cam"] == (10, 20, 300, 200) and got["game"] == "1920:1080:0:0" and got["has_webcam"] is True
    assert got["beats"] == [(3.0, "hype", "*W*")] and got["actions"] == [(5.0, 7.5, "*WATCHING*")]
    assert got["cold_open"] == (12.0, 2.0)
    assert recipe.read(tmp_path / "nothing-here") is None
    recipe.keep(
        tmp_path / "no" / "such" / "folder", words=words, hook=None, layout=layout
    )  # logged, not raised
    recipe.keep(None, words=words, hook=None, layout=layout)
