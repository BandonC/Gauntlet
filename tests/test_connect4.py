from arena.games.connect4 import COLS, ROWS, C4State, Connect4

G = Connect4()


def play(cols: list[int]):
    s = G.initial_state(2, seed=0)
    for c in cols:
        seat = G.current_player(s)
        assert G.validate_action(s, seat, {"column": c}) is None, c
        s = G.apply(s, seat, {"column": c})
    return s


def test_vertical_win():
    s = play([0, 1, 0, 1, 0, 1, 0])
    assert G.is_terminal(s) and s.winner == 0
    assert G.scores(s) == [1.0, 0.0] and G.placements(s) == [1, 2]


def test_horizontal_win():
    s = play([0, 0, 1, 1, 2, 2, 3])
    assert s.winner == 0


def test_diagonal_win():
    # X builds a / diagonal on columns 0-3
    s = play([0, 1, 1, 2, 2, 3, 2, 3, 3, 6, 3])
    assert s.winner == 0


def test_anti_diagonal_win_for_second_player():
    s = play([6, 3, 6, 2, 6, 2, 0, 1, 1, 1, 0, 0, 5, 0])
    assert s.winner == 1 and s.columns[3] == (1,) and s.columns[0][3] == 1


def test_full_column_rejected():
    s = play([0] * ROWS)
    err = G.validate_action(s, G.current_player(s), {"column": 0})
    assert err == "column 0 is full"


def test_bad_actions_rejected():
    s = G.initial_state(2, seed=0)
    assert G.validate_action(s, 1, {"column": 0}) == "it is not your turn"
    assert G.validate_action(s, 0, {"column": 7}) is not None
    assert G.validate_action(s, 0, {"column": True}) is not None
    assert G.validate_action(s, 0, {"col": 3}) is not None


def test_apply_does_not_mutate():
    s0 = G.initial_state(2, seed=0)
    s1 = G.apply(s0, 0, {"column": 3})
    assert s0.columns[3] == () and s1.columns[3] == (0,)


def test_draw():
    # Full board with no four-in-a-row: columns pair up as XOXOXO / OXOXOX.
    pattern = [(0, 1) * 3, (0, 1) * 3, (1, 0) * 3, (1, 0) * 3, (0, 1) * 3, (0, 1) * 3, (0, 1) * 3]
    almost = C4State(
        columns=tuple(pattern[:6]) + (pattern[6][:-1],),  # last O not yet played
        to_move=1,
        move_count=COLS * ROWS - 1,
    )
    s = G.apply(almost, 1, {"column": 6})
    assert s.winner is None and s.draw and G.scores(s) == [0.5, 0.5]
    assert G.placements(s) == [1, 1]  # the default: derived from scores, ties share a rank


def test_forfeit():
    s = G.forfeit(G.initial_state(2, seed=0), 0)
    assert G.is_terminal(s) and G.scores(s) == [0.0, 1.0] and s.forfeited_by == 0


def test_view_renders_board():
    s = play([3])
    view = G.player_view(s, 1)
    assert view["you"] == "O" and view["board"][-1] == "...X..."
    assert "Legal columns: 0, 1, 2, 3, 4, 5, 6" in G.render_view(view)
