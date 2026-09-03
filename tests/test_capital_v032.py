from sp1execution.engine.capital import initial_capital_eur


def test_initial_capital_10k(monkeypatch):
    monkeypatch.setenv("SP1_STRATEGY_INITIAL_CAPITAL_EUR", "10000")
    assert initial_capital_eur() == 10000.0


def test_capital_is_independent_of_50k_account(monkeypatch):
    monkeypatch.setenv("SP1_STRATEGY_INITIAL_CAPITAL_EUR", "10000")
    broker_account_total = 50000.0
    assert initial_capital_eur() == 10000.0
    assert initial_capital_eur() != broker_account_total


def test_invalid_capital_rejected(monkeypatch):
    monkeypatch.setenv("SP1_STRATEGY_INITIAL_CAPITAL_EUR", "0")
    try:
        initial_capital_eur()
    except ValueError:
        pass
    else:
        raise AssertionError("Expected ValueError")
