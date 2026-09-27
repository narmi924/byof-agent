"""Generate the unstarted baseline only. Never use to rebuild WIP/history."""
import argparse
from common import ROOT, read, integer, unique, write_pending

FIELDS = ['batch_id','order_id','product_id','split_revision','sequence_no','routing_version','quantity','status']

def generate(seed):
    products = unique(read(seed/'products.csv'), 'product_id')
    orders = read(seed/'sales_orders.csv')
    unique(orders, 'order_id')
    routes = read(seed/'routing_steps.csv')
    result = []
    for order in sorted(orders, key=lambda r:r['order_id']):
        if order['status'] != 'CONFIRMED' or integer(order['version']) != 1:
            raise ValueError('Baseline generator requires CONFIRMED version-1 orders; reconcile revisions/WIP separately')
        p = products[order['product_id']]
        qty, size = integer(order['quantity']), integer(p['batch_size'])
        if size != 50 or qty <= 0 or qty % size:
            raise ValueError('UNSUPPORTED_BATCH_QUANTITY: '+order['order_id'])
        versions = {r['routing_version'] for r in routes if r['product_id']==p['product_id']}
        if len(versions)!=1: raise ValueError('Exactly one baseline routing version is required')
        version = versions.pop()
        for seq in range(1, qty//size+1):
            result.append(dict(zip(FIELDS, [f"{order['order_id']}-R001-B{seq:03d}",order['order_id'],p['product_id'],1,seq,version,size,'PENDING'])))
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed-dir',type=__import__('pathlib').Path,default=ROOT/'seed')
    parser.add_argument('--output',type=__import__('pathlib').Path,default=ROOT/'generated/production_batches.csv')
    args=parser.parse_args()
    rows=generate(args.seed_dir)
    write_pending(args.output,FIELDS,rows)
    print(f'Generated {len(rows)} batches: {args.output}')

if __name__=='__main__': main()
