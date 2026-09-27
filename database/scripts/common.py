"""Standard-library CSV helpers. No database, network or application service starts."""
import csv
import os
import tempfile
from pathlib import Path
from decimal import Decimal, ROUND_CEILING

ROOT = Path(__file__).resolve().parents[1]

def read(path):
    with Path(path).open(encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f))

def integer(value):
    # Reject decimal quantities rather than truncate them.
    if not value or not value.isascii() or not value.isdigit():
        raise ValueError('Expected unsigned integer: '+repr(value))
    return int(value)

def unique(rows, key):
    result = {}
    for r in rows:
        k = r[key]
        if not k or k in result:
            raise ValueError('Missing or duplicate '+key+': '+repr(k))
        result[k] = r
    return result

def duration(step, quantity):
    return int((Decimal(step['setup_min']) + Decimal(step['cycle_sec_per_unit']) * quantity / 60).to_integral_value(rounding=ROUND_CEILING))

def write_pending(path, fields, rows):
    path = Path(path)
    if path.exists() and any(r.get('status') != 'PENDING' for r in read(path)):
        raise ValueError('Refusing to overwrite non-PENDING history: '+str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replacement after complete validation; failed generation preserves old files.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='', dir=path.parent, delete=False) as f:
        temp = f.name
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    try:
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)
