import pytest

from poker_arena import BotLoadError, load_bot

VALID_BOT = """
from poker_arena import Action

class CountingBot:
    name = "counting"
    instances = 0

    def __init__(self):
        CountingBot.instances += 1
        self.id = CountingBot.instances

    def act(self, obs):
        return Action.call()

def create_bot():
    return CountingBot()
"""


def write(tmp_path, source, filename="bot.py"):
    path = tmp_path / filename
    path.write_text(source)
    return path


def test_loads_a_valid_submission(tmp_path):
    bot = load_bot(write(tmp_path, VALID_BOT))
    assert bot.name == "counting"
    assert callable(bot.act)


def test_each_load_returns_a_fresh_instance(tmp_path):
    path = write(tmp_path, VALID_BOT)
    a = load_bot(path)
    b = load_bot(path)
    assert a is not b

    a.id = "mutated"
    assert b.id != "mutated"


def test_missing_create_bot_is_rejected(tmp_path):
    source = "class NoFactory:\n    name = 'x'\n    def act(self, obs): pass\n"
    with pytest.raises(BotLoadError, match="create_bot"):
        load_bot(write(tmp_path, source))


def test_create_bot_must_return_something_named(tmp_path):
    source = """
class Nameless:
    def act(self, obs): pass

def create_bot():
    return Nameless()
"""
    with pytest.raises(BotLoadError, match="name"):
        load_bot(write(tmp_path, source))


def test_create_bot_must_return_something_with_act(tmp_path):
    source = """
class ActLess:
    name = "actless"

def create_bot():
    return ActLess()
"""
    with pytest.raises(BotLoadError, match="act"):
        load_bot(write(tmp_path, source))


def test_import_errors_are_wrapped(tmp_path):
    source = "raise RuntimeError('boom during import')\n"
    with pytest.raises(BotLoadError, match="raised on import"):
        load_bot(write(tmp_path, source))


def test_create_bot_raising_is_wrapped(tmp_path):
    source = """
def create_bot():
    raise ValueError("nope")
"""
    with pytest.raises(BotLoadError, match="create_bot\\(\\) raised"):
        load_bot(write(tmp_path, source))


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(BotLoadError):
        load_bot(tmp_path / "does_not_exist.py")
