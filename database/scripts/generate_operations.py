"""Expand batches using the explicitly ordered product routing template."""
import argparse
from pathlib import Path
from common import ROOT, read, integer, unique, duration, write_pending

FIELDS=['operation_id','batch_id','product_id','routing_version','routing_step_id','op_code','sequence_no','duration_min','status']
ORDER=['OP10','OP20','OP30','OP60','OP40','OP50','OP70','OP80']

def generate(batches, routes):
    unique(batches,'batch_id'); unique(routes,'routing_step_id')
    result=[]
    for b in sorted(batches,key=lambda r:r['batch_id']):
        if b['status']!='PENDING': raise ValueError('Cannot regenerate started or blocked batches')
        quantity=integer(b['quantity'])
        if quantity!=50: raise ValueError('UNSUPPORTED_BATCH_QUANTITY')
        steps=sorted([r for r in routes if (r['product_id'],r['routing_version'])==(b['product_id'],b['routing_version'])], key=lambda r:integer(r['sequence_no']))
        if [r['op_code'] for r in steps]!=ORDER or [integer(r['sequence_no']) for r in steps]!=list(range(1,9)):
            raise ValueError('Incomplete/incorrect routing for '+b['batch_id'])
        for r in steps:
            if DecimalSafe(r['setup_min'])<0 or DecimalSafe(r['cycle_sec_per_unit'])<0: raise ValueError('Negative processing time')
            result.append(dict(zip(FIELDS,[b['batch_id']+'-'+r['op_code'],b['batch_id'],b['product_id'],b['routing_version'],r['routing_step_id'],r['op_code'],r['sequence_no'],duration(r,quantity),'PENDING'])))
    return result

def DecimalSafe(s):
    from decimal import Decimal
    x=Decimal(s)
    if not x.is_finite(): raise ValueError('Nonfinite processing time')
    return x

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--seed-dir',type=Path,default=ROOT/'seed')
    p.add_argument('--batches',type=Path,default=ROOT/'generated/production_batches.csv')
    p.add_argument('--output',type=Path,default=ROOT/'generated/operations.csv')
    a=p.parse_args()
    rows=generate(read(a.batches),read(a.seed_dir/'routing_steps.csv'))
    write_pending(a.output,FIELDS,rows)
    print(f'Generated {len(rows)} operations: {a.output}')

if __name__=='__main__': main()
