

def test_partial_publication_lags_keep_the_defaults():
    """backtest.yaml 只寫了幾種頻率的發佈延遲：其餘用預設值補上（以前缺的那種頻率一回測就 KeyError 中斷）。"""
    from crudelab.schema import DEFAULT_PUBLICATION_LAGS, BacktestSettings
    settings = BacktestSettings(publication_lag_days={"weekly": 7})
    assert settings.publication_lag_days == {**DEFAULT_PUBLICATION_LAGS, "weekly": 7}
