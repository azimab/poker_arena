import pytest

from poker_arena import (
    Action,
    ActionType,
    IllegalAction,
    Observation,
    Street,
    TableConfig,
    play_hand,
)
from poker_arena.bots import CallBot, FoldBot, RandomBot

CFG = TableConfig(starting_stack=10_000, sb=50, bb=100)


class ScriptedBot:
    """Plays a fixed list of actions, then calls forever."""

    name = "scripted"

    def __init__(self, *actions: Action):
        self.actions = list(actions)
        self.seen: list[Observation] = []

    def act(self, obs: Observation) -> Action:
        self.seen.append(obs)
        return self.actions.pop(0) if self.actions else Action.call()




def test_button_posts_small_blind_and_acts_first_preflop():
    sb = ScriptedBot()
    play_hand([sb, FoldBot()], seed=1, button=0, config=CFG)

    first = sb.seen[0]
    assert first.street is Street.PREFLOP
    assert first.street_bets == (50, 100)
    assert first.to_call == 50
    assert not first.can_check


def test_small_blind_folding_preflop_loses_only_the_small_blind():
    result = play_hand([FoldBot(), CallBot()], seed=1, button=0, config=CFG)

    assert result.deltas == (-50, 50)
    assert result.winners == (1,)
    assert not result.showdown
    assert result.board == ()


def test_big_blind_gets_the_option_after_a_limp():
    bb = ScriptedBot(Action.check())
    play_hand([CallBot(), bb], seed=1, button=0, config=CFG)

    option = bb.seen[0]
    assert option.street is Street.PREFLOP
    assert option.to_call == 0
    assert option.can_check




def test_min_raise_is_a_full_bet_preflop_and_a_big_blind_postflop():
    button = ScriptedBot()
    play_hand([button, CallBot()], seed=1, button=0, config=CFG)

    preflop = button.seen[0]
    assert (preflop.min_raise_to, preflop.max_raise_to) == (200, 10_000)

    flop = next(o for o in button.seen if o.street is Street.FLOP)
    assert (flop.min_raise_to, flop.max_raise_to) == (100, 9_900)


def test_checking_into_a_bet_is_illegal():
    with pytest.raises(IllegalAction, match="checked facing a bet"):
        play_hand([ScriptedBot(Action.check()), CallBot()], seed=1, button=0, config=CFG)


def test_undersized_raise_is_illegal():
    with pytest.raises(IllegalAction, match="legal range is 200-10000"):
        play_hand(
            [ScriptedBot(Action.raise_to(150)), CallBot()], seed=1, button=0, config=CFG
        )


def test_illegal_action_names_the_offending_seat():
    with pytest.raises(IllegalAction) as excinfo:
        play_hand([CallBot(), ScriptedBot(Action.raise_to(150))], seed=1, button=0, config=CFG)
    assert excinfo.value.seat == 1


@pytest.mark.parametrize("amount", [250.5, "300", None])
def test_non_integer_raise_amount_is_illegal(amount):
    with pytest.raises(IllegalAction, match="non-integer amount"):
        play_hand(
            [ScriptedBot(Action(ActionType.RAISE, amount)), CallBot()], seed=1, button=0, config=CFG
        )


def test_raise_is_illegal_once_the_opponent_is_all_in():
    shove = ScriptedBot(Action.raise_to(10_000))
    hero = ScriptedBot(Action.raise_to(10_000))
    with pytest.raises(IllegalAction, match="cannot raise here"):
        play_hand([shove, hero], seed=1, button=0, config=CFG)


def test_a_reraise_reopens_the_action():
    button = ScriptedBot(Action.raise_to(300), Action.fold())
    bb = ScriptedBot(Action.raise_to(900))
    result = play_hand([button, bb], seed=1, button=0, config=CFG)

    assert len(button.seen) == 2, "button must act again after being re-raised"
    assert button.seen[1].to_call == 600
    assert result.deltas == (-300, 300)




def test_uncalled_bet_is_returned():
    result = play_hand(
        [ScriptedBot(Action.raise_to(10_000)), FoldBot()], seed=1, button=0, config=CFG
    )

    # The shove wins the blinds only; the uncalled 9,900 goes back.
    assert result.deltas == (100, -100)
    assert result.pot == 200


def test_called_all_in_runs_the_full_board_out():
    result = play_hand(
        [ScriptedBot(Action.raise_to(10_000)), CallBot()], seed=1, button=0, config=CFG
    )

    assert len(result.board) == 5
    assert result.showdown
    assert result.pot == 20_000
    assert sorted(result.deltas) == [-10_000, 10_000]


def test_showdown_winner_is_the_better_hand():
    from treys import Card, Evaluator

    result = play_hand([CallBot(), CallBot()], seed=7, button=0, config=CFG)
    assert result.showdown

    ev = Evaluator()
    board = [Card.new(c) for c in result.board]
    scores = [ev.evaluate([Card.new(c) for c in hole], board) for hole in result.holes]
    assert result.winners == ((0,) if scores[0] < scores[1] else (1,))


def test_tied_hands_split_the_pot():
    for seed in range(500):
        result = play_hand([CallBot(), CallBot()], seed=seed, button=0, config=CFG)
        if result.winners == (0, 1):
            assert result.deltas == (0, 0)
            return
    pytest.fail("no tied hand found in 500 seeds")




def test_chips_are_conserved_across_random_hands():
    for seed in range(300):
        result = play_hand(
            [RandomBot(seed), RandomBot(seed + 1)], seed=seed, button=seed % 2, config=CFG
        )
        assert sum(result.deltas) == 0
        assert all(abs(d) <= CFG.starting_stack for d in result.deltas)
        # Heads-up, both seats always end up matched: the winner takes exactly
        # what the loser put in.
        if result.winners != (0, 1):
            assert result.pot == 2 * max(result.deltas)




def test_same_seed_replays_identically():
    a = play_hand([RandomBot(3), RandomBot(4)], seed=42, button=1, config=CFG)
    b = play_hand([RandomBot(3), RandomBot(4)], seed=42, button=1, config=CFG)

    assert (a.holes, a.board, a.history, a.deltas) == (b.holes, b.board, b.history, b.deltas)


def test_cards_belong_to_the_seat_not_the_bot():
    # Duplicate scoring depends on this: swap the bots, same seed, and each bot
    # gets exactly the cards the other one held.
    a = play_hand([CallBot(), CallBot()], seed=99, button=0, config=CFG)
    b = play_hand([FoldBot(), CallBot()], seed=99, button=0, config=CFG)

    assert a.holes == b.holes


def test_the_runout_does_not_depend_on_how_the_hand_is_played():
    passive = play_hand([CallBot(), CallBot()], seed=11, button=0, config=CFG)
    aggro = play_hand(
        [ScriptedBot(Action.raise_to(400)), CallBot()], seed=11, button=0, config=CFG
    )

    assert passive.board == aggro.board


def test_a_bot_only_ever_sees_its_own_hole_cards():
    seat0, seat1 = ScriptedBot(), ScriptedBot()
    result = play_hand([seat0, seat1], seed=5, button=0, config=CFG)

    for bot, seat in ((seat0, 0), (seat1, 1)):
        for obs in bot.seen:
            assert obs.seat == seat
            assert obs.hole == result.holes[seat]
            assert set(obs.hole).isdisjoint(result.holes[1 - seat])


def test_history_records_every_decision_in_order():
    result = play_hand([CallBot(), CallBot()], seed=1, button=0, config=CFG)

    assert [r.seat for r in result.history[:2]] == [0, 1]
    assert result.history[0].type is ActionType.CALL
    assert result.history[0].amount == 50
    assert [r.street for r in result.history] == sorted(
        (r.street for r in result.history), key=lambda s: list(Street).index(s)
    )
