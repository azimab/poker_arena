from poker_arena import Street, TableConfig, play_match
from poker_arena.bots import CallBot, FoldBot, RandomBot

CFG = TableConfig(starting_stack=1_000, sb=50, bb=100)


def test_plays_the_requested_number_of_hands():
    result = play_match([CallBot(), CallBot()], seed=1, hands=10, config=CFG)
    assert len(result.hands) == 10
    assert result.busted is None


def test_button_alternates_each_hand():
    result = play_match([CallBot(), CallBot()], seed=1, hands=4, config=CFG)
    buttons = [h.button for h in result.hands]
    assert buttons == [0, 1, 0, 1]


def test_stacks_carry_forward_between_hands():
    result = play_match([CallBot(), CallBot()], seed=1, hands=5, config=CFG)

    running = [CFG.starting_stack, CFG.starting_stack]
    for hand in result.hands:
        running[0] += hand.deltas[0]
        running[1] += hand.deltas[1]
    assert tuple(running) == result.final_stacks


def test_final_stacks_sum_to_double_the_starting_stack():
    result = play_match([RandomBot(3), RandomBot(4)], seed=7, hands=20, config=CFG)
    assert sum(result.final_stacks) == 2 * CFG.starting_stack


def test_match_stops_early_once_a_seat_busts():
    tiny = TableConfig(starting_stack=100, sb=50, bb=100)
    shove_bot = RandomBot(5)
    result = play_match([FoldBot(), shove_bot], seed=1, hands=1_000, config=tiny)

    assert result.busted is not None
    assert min(result.final_stacks) <= 0
    assert len(result.hands) <= 1_000


def test_same_seed_replays_identically():
    a = play_match([RandomBot(1), RandomBot(2)], seed=42, hands=8, config=CFG)
    b = play_match([RandomBot(1), RandomBot(2)], seed=42, hands=8, config=CFG)

    assert a.final_stacks == b.final_stacks
    assert [h.history for h in a.hands] == [h.history for h in b.hands]


def test_hand_seeds_differ_across_the_match():
    result = play_match([RandomBot(1), RandomBot(2)], seed=1, hands=10, config=CFG)
    seeds = {h.seed for h in result.hands}
    assert len(seeds) == len(result.hands)
