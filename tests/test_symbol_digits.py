import unittest
from symbol_digits import SYMBOL_DIGITS, resolve_digits

class DigitsTests(unittest.TestCase):
    def test_all_mappings_and_case(self):
        groups = {5:'EURUSD USDCHF USDCAD AUDUSD GBPUSD NZDUSD',
                  3:'USDJPY XAUUSD XAGUSD BRENTCMDUSD LIGHTCMDUSD DOLLARIDXUSD USA30IDXUSD USA500IDXUSD USSC2000IDXUSD USATECHIDXUSD',
                  1:'ETHUSD BTCUSD',4:'GASCMDUSD'}
        names = []
        for digit, symbols in groups.items():
            for symbol in symbols.split():
                names.append(symbol)
                self.assertEqual(resolve_digits(symbol.lower()), digit)
                self.assertEqual(resolve_digits(symbol, digit), digit)
        self.assertEqual(set(names), set(SYMBOL_DIGITS))
        self.assertEqual(len(names),19)

    def test_conflict_and_unknown(self):
        with self.assertRaises(ValueError): resolve_digits('EURUSD',3)
        with self.assertRaises(ValueError): resolve_digits('OTHER')
        with self.assertRaises(ValueError): resolve_digits('OTHER',13)
        self.assertEqual(resolve_digits('OTHER',2),2)

class CliTests(unittest.TestCase):
    def test_cli_uses_symbol_mapping_before_download(self):
        import sys
        from unittest.mock import patch
        import import_zip
        for symbol, expected in [('EURUSD',5),('BTCUSD',1),('GASCMDUSD',4)]:
            with self.subTest(symbol=symbol), patch.object(sys, 'argv', ['import_zip.py','--symbol',symbol,'--url','https://example.com/data.zip']), patch.object(import_zip,'download',side_effect=RuntimeError('called')):
                with self.assertRaisesRegex(RuntimeError,'called'), patch('builtins.print') as output:
                    import_zip.main()
                output.assert_any_call('DIGITS', symbol, expected, flush=True)

    def test_cli_rejects_known_conflict_before_download(self):
        import sys
        from unittest.mock import patch
        import import_zip
        with patch.object(sys, 'argv', ['import_zip.py','--symbol','EURUSD','--digits','3','--url','https://example.com/data.zip']), patch.object(import_zip,'download') as download:
            with self.assertRaises(SystemExit): import_zip.main()
            download.assert_not_called()
