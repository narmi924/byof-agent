"""Mutation tests demonstrate that validation detects bad data, not just happy paths."""
import csv
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from common import read, write_pending
from generate_batches import generate as batches
from generate_operations import generate as operations
from validate_data import validate

class DataLayerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'database'
        shutil.copytree(ROOT,self.root,ignore=shutil.ignore_patterns('__pycache__'))

    def change(self,table,fn):
        p=self.root/('generated' if table in ('operations','production_batches') else 'seed')/(table+'.csv')
        rows=read(p); fields=list(rows[0]); fn(rows)
        with p.open('w',encoding='utf-8',newline='') as f:
            w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)

    def bad(self):
        result=validate(self.root)
        self.assertEqual(result['status'],'FAIL',result)
        self.assertTrue(result['errors'])

    def test_baseline(self): self.assertEqual(validate(self.root)['status'],'PASS')
    def test_duplicate_primary_key(self):
        self.change('products',lambda r:r.append(r[0].copy())); self.bad()
    def test_missing_id(self):
        self.change('workers',lambda r:r[0].update(worker_id='')); self.bad()
    def test_foreign_key(self):
        self.change('worker_skills',lambda r:r[0].update(worker_id='W99')); self.bad()
    def test_negative_inventory(self):
        self.change('inventory',lambda r:r[0].update(on_hand='-1')); self.bad()
    def test_reserved_exceeds_inventory(self):
        self.change('inventory',lambda r:r[0].update(reserved='99999')); self.bad()
    def test_incoming_cannot_be_merged_with_stock(self):
        self.change('inventory',lambda r:r[0].update(on_hand='1800')); self.bad()
    def test_missing_operation(self):
        self.change('operations',lambda r:r.pop()); self.bad()
    def test_wrong_duration(self):
        self.change('operations',lambda r:r[0].update(duration_min='11')); self.bad()
    def test_bom_seal_quantity(self):
        def mutate(rows):
            next(r for r in rows if r['material_id']=='SEAL-RS1-6202')['qty_per_unit']='1'
        self.change('bom_items',mutate); self.bad()
    def test_wrong_product_routing(self):
        self.change('operations',lambda r:r[0].update(routing_step_id='BRG-6203-2RS1-V1.6-OP10')); self.bad()
    def test_missing_capability(self):
        self.change('resource_operations',lambda r:r.pop()); self.bad()
    def test_wrong_skill(self):
        self.change('routing_steps',lambda r:r[0].update(required_skill='PACKAGING')); self.bad()
    def test_route_reordering(self):
        def mutate(rows): rows[3]['sequence_no'],rows[4]['sequence_no']=rows[4]['sequence_no'],rows[3]['sequence_no']
        self.change('routing_steps',mutate); self.bad()
    def test_invalid_date(self):
        self.change('material_inbounds',lambda r:r[0].update(eta='2026-02-30T00:30:00Z')); self.bad()
    def test_fractional_order_quantity_rejected(self):
        self.change('sales_orders',lambda r:r[0].update(quantity='1000.5'))
        with self.assertRaises(ValueError): batches(self.root/'seed')
        self.bad()
    def test_nonmultiple_rejected_without_rounding(self):
        self.change('sales_orders',lambda r:r[0].update(quantity='1001'))
        with self.assertRaisesRegex(ValueError,'UNSUPPORTED_BATCH_QUANTITY'): batches(self.root/'seed')
    def test_revision_requires_reconciliation(self):
        self.change('sales_orders',lambda r:r[0].update(version='2'))
        with self.assertRaises(ValueError): batches(self.root/'seed')
    def test_started_batch_not_regenerated(self):
        bs=read(self.root/'generated/production_batches.csv'); bs[0]['status']='IN_PROGRESS'
        with self.assertRaises(ValueError): operations(bs,read(self.root/'seed/routing_steps.csv'))
    def test_history_file_not_overwritten(self):
        self.change('operations',lambda r:r[0].update(status='COMPLETED'))
        p=self.root/'generated/operations.csv'; before=p.read_bytes()
        with self.assertRaises(ValueError): write_pending(p,[],[])
        self.assertEqual(before,p.read_bytes())
    def test_generation_is_deterministic(self):
        a=batches(self.root/'seed'); b=batches(self.root/'seed'); self.assertEqual(a,b)
        old=read(self.root/'generated/production_batches.csv')
        self.assertEqual([{k:str(v) for k,v in r.items()} for r in a],old)
        ops=operations(old,read(self.root/'seed/routing_steps.csv'))
        self.assertEqual([{k:str(v) for k,v in r.items()} for r in ops],read(self.root/'generated/operations.csv'))

if __name__=='__main__': unittest.main()
