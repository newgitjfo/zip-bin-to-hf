"""Canonical price precision per symbol; unknown symbols require an explicit value."""
SYMBOL_DIGITS = {
    **dict.fromkeys(('EURUSD','USDCHF','USDCAD','AUDUSD','GBPUSD','NZDUSD'), 5),
    **dict.fromkeys(('USDJPY','XAUUSD','XAGUSD','BRENTCMDUSD','LIGHTCMDUSD',
                     'DOLLARIDXUSD','USA30IDXUSD','USA500IDXUSD','USSC2000IDXUSD',
                     'USATECHIDXUSD'), 3),
    **dict.fromkeys(('ETHUSD','BTCUSD'), 1),
    'GASCMDUSD': 4,
}


def resolve_digits(symbol, explicit=None):
    symbol = symbol.strip().upper()
    if explicit is not None and (not isinstance(explicit, int) or not 0 <= explicit <= 12):
        raise ValueError('digits must be an integer from 0 to 12')
    expected = SYMBOL_DIGITS.get(symbol)
    if expected is not None:
        if explicit is not None and explicit != expected:
            raise ValueError(f'{symbol} requires digits={expected}; remove --digits or use the correct value')
        return expected
    if explicit is None:
        raise ValueError(f'Unknown symbol {symbol}: provide --digits explicitly')
    return explicit
