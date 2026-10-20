"""Import all selected daily and/or multi-day BarReplay BIN members from one ZIP."""
import datetime as dt
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import zipfile
import import_zip as legacy

_SPLIT_ONE = legacy.split_zip


def split_zip(zip_path, symbol, output, member='', max_bin_bytes=6*1024**3):
    if not re.fullmatch('[A-Z0-9]{3,32}', symbol):
        raise ValueError('Unsafe symbol')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    # Validate every selected member and the aggregate uncompressed budget up front.
    with zipfile.ZipFile(zip_path) as archive:
        candidates = [i for i in archive.infolist() if not i.is_dir() and i.filename.lower().endswith('.bin')]
        if member:
            candidates = [i for i in candidates if i.filename == member]
            if len(candidates) != 1:
                raise ValueError('Requested ZIP member not found or ambiguous')
        if not candidates or len(candidates) > 10000:
            raise ValueError('ZIP must contain 1..10000 selected BIN members')
        names = [i.filename for i in candidates]
        if len(set(names)) != len(names):
            raise ValueError('Duplicate BIN member names in ZIP')
        total_bytes = 0
        for item in candidates:
            legacy.choose_member(archive, item.filename, max_bin_bytes)
            total_bytes += item.file_size
        if total_bytes > max_bin_bytes:
            raise ValueError('Combined selected BINs exceed the uncompressed size limit')
        # ZIP order need not be chronological; each member must itself be sorted.
        selected = [(i.filename, i.file_size) for i in sorted(candidates, key=lambda i: i.filename)]
    stage = Path(tempfile.mkdtemp(prefix='collection-', dir=output))
    report = dict(symbol=symbol, format='BarReplay bin_v1', timezone='UTC',
                  source_format='ZIP daily and/or multi-day BIN',
                  source_member=selected[0][0] if len(selected) == 1 else f'{len(selected)} BIN members',
                  source_members=[name for name, size in selected],
                  source_uncompressed_bytes=total_bytes, source_blocks=0,
                  source_ticks=0, ticks=0, days=[], members=[], identical_duplicates=[])
    days = {}
    try:
        for name, size in selected:
            piece = None
            try:
                piece, part = _SPLIT_ONE(zip_path, symbol, output, name, max_bin_bytes)
                basename = PurePosixPath(name.replace('\\', '/')).name
                daily_name = re.fullmatch(r'([A-Za-z0-9]{3,32})_(\d{4}-\d{2}-\d{2})\.bin', basename, re.I)
                if daily_name:
                    named_symbol, named_day = daily_name.groups()
                    dt.date.fromisoformat(named_day)
                    if named_symbol.upper() != symbol:
                        raise ValueError('Daily BIN filename belongs to a different symbol: ' + name)
                    if len(part['days']) != 1 or part['days'][0]['day'] != named_day:
                        raise ValueError('Daily filename date does not match actual UTC tick dates: ' + name)
                report['source_blocks'] += part['source_blocks']
                report['source_ticks'] += part['ticks']
                report['members'].append(dict(member=name, bytes=size, ticks=part['ticks'],
                                               days=len(part['days']), kind='daily' if len(part['days']) == 1 else 'multi-day'))
                for record in part['days']:
                    day = record['day']
                    if day in days:
                        old = days[day]
                        if (old['sha256'], old['bytes'], old['ticks']) != (record['sha256'], record['bytes'], record['ticks']):
                            raise ValueError('Conflicting/overlapping source BINs for UTC day ' + day + '; no upload attempted')
                        old['source_members'].append(name)
                        report['identical_duplicates'].append(dict(day=day, member=name))
                        continue
                    entry = dict(record, source_member=name, source_members=[name])
                    destination = stage / entry['path']
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    (piece / entry['path']).replace(destination)
                    days[day] = entry
            finally:
                if piece is not None:
                    shutil.rmtree(piece, ignore_errors=True)
        report['days'] = [days[d] for d in sorted(days)]
        report['ticks'] = sum(d['ticks'] for d in report['days'])
        if not report['ticks']:
            raise ValueError('No ticks in selected source BINs')
        (stage / 'split_report.json').write_text(json.dumps(report, indent=2))
        return stage, report
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def main():
    # Retain the existing URL downloader, CLI, completeness gate and guarded HF publisher.
    original = legacy.split_zip
    legacy.split_zip = split_zip
    try:
        return legacy.main()
    finally:
        legacy.split_zip = original


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        # No URL/token values in this diagnostic. Full member provenance is in reports.
        print('ERROR:', type(error).__name__, '- inspect input format, member selection and overlapping days')
        raise SystemExit(1)
