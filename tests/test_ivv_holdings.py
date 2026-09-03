from sp1execution.market_data.ivv_holdings import parse_ivv_holdings_csv


def test_parse_ivv_snapshot_and_top2():
    rows = [
        "iShares Core S&P 500 ETF",
        'Fund Holdings as of,"Aug 11, 2026"',
        'Inception Date,"May 15, 2000"',
        'Shares Outstanding,"1,000"',
        'Stock,"-"',
        'Bond,"-"',
        'Cash,"-"',
        'Other,"-"',
        "Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Quantity,Price,Location,Exchange,Currency,FX Rate,Market Currency,Accrual Date",
    ]
    for i in range(401):
        ticker = "NVDA" if i == 0 else ("AAPL" if i == 1 else f"X{i}")
        name = "NVIDIA CORP" if i == 0 else ("APPLE INC" if i == 1 else f"Company {i}")
        weight = 7.9 if i == 0 else (6.7 if i == 1 else 0.01)
        rows.append(
            f'"{ticker}","{name}","IT","Equity","1","{weight}","1","1","1","United States","NASDAQ","USD","1","USD","-"'
        )
    snap = parse_ivv_holdings_csv(("\n".join(rows) + "\n").encode())
    assert snap.as_of == "Aug 11, 2026"
    assert snap.top2[0].ticker == "NVDA"
    assert snap.top2[1].ticker == "AAPL"
