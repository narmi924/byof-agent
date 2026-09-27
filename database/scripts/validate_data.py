"""Validate CSVs by loading schema.sql into ephemeral SQLite and checking the reference baseline.

No persistent database or remote changes. Baseline files are pinned by SHA256 in
data/development/skf-baseline-hashes.json.
"""
import argparse
import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from common import ROOT

ORDER=['OP10','OP20','OP30','OP60','OP40','OP50','OP70','OP80']
BASELINE=ROOT.parent/'data'/'development'/'skf-baseline-hashes.json'

def digest(path):
    # Git may check text files out with CRLF; the pinned digest uses normalized line endings.
    return hashlib.sha256(path.read_text(encoding='utf-8').encode('utf-8')).hexdigest()

def validate(root=ROOT):
    errors=[]; checks=[]
    manifest=json.loads((root/'source_manifest.json').read_text(encoding='utf-8'))
    def check(name, result):
        checks.append({'check':name,'passed':bool(result)})
        if not result: errors.append(name)
    pinned={k.split('/',1)[1]:v for k,v in json.loads(BASELINE.read_text(encoding='utf-8')).items() if k.startswith('database/')}
    check('Baseline files match their pinned SHA256',all((root/k).is_file() and digest(root/k)==v for k,v in pinned.items()))
    def utc(s):
        return datetime.strptime(s,'%Y-%m-%d %H:%M').replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    con=sqlite3.connect(':memory:')
    con.execute('PRAGMA foreign_keys=ON')
    con.executescript((root/'schema.sql').read_text(encoding='utf-8'))
    check('schema.sql creates SQLite database with foreign keys enabled', con.execute('PRAGMA foreign_keys').fetchone()[0]==1)
    data={}
    try:
        for name in manifest['table_order']:
            path=root/('generated' if name in ('production_batches','operations') else 'seed')/(name+'.csv')
            columns=con.execute(f'PRAGMA table_info({name})').fetchall()
            fields=[c[1] for c in columns]
            with path.open(encoding='utf-8',newline='') as f:
                reader=csv.DictReader(f)
                if reader.fieldnames!=fields: raise ValueError('CSV header/schema mismatch: '+name)
                rows=list(reader)
            data[name]=rows
            check(name+' baseline row count',len(rows)==manifest['expected_counts'][name])
            for i,r in enumerate(rows,2):
                if None in r or any(v is None or not v.strip() for v in r.values()): raise ValueError(f'{name}:{i}: missing field/ID or extra column')
                for _,field,typ,*_ in columns:
                    if typ=='INTEGER' and not re.fullmatch(r'-?\d+',r[field],flags=re.ASCII): raise ValueError(f'{name}:{i}: invalid integer {field}')
                    if field in ('due_at','eta','snapshot_clock','horizon_start','horizon_end','start_at','end_at'):
                        dt=datetime.strptime(r[field],'%Y-%m-%dT%H:%M:%SZ')
                        if dt.strftime('%Y-%m-%dT%H:%M:%SZ')!=r[field]: raise ValueError('Noncanonical UTC time')
                con.execute(f'INSERT INTO {name} ({",".join(fields)}) VALUES ({",".join("?" for _ in fields)})', [r[f] for f in fields])
        check('CSV types, PK, FK, NOT NULL and CHECK constraints',not con.execute('PRAGMA foreign_key_check').fetchall())
        def project(name,*cols): return Counter(tuple(r[c] for c in cols) for r in data[name])
        check('Products use the fixed batch size',all(p['batch_size']=='50' for p in data['products']))
        check('Orders are confirmed initial version',all(r['status']=='CONFIRMED' and r['version']=='1' for r in data['sales_orders']))
        check('Opening inventory is unreserved; no incoming merged',all(r['reserved']=='0' for r in data['inventory']))
        check('Inbounds are confirmed records',all(r['status']=='CONFIRMED' for r in data['material_inbounds']))
        check('Resources and workers start available',all(r['status']=='AVAILABLE' for r in data['resources']+data['workers']))
        check('Priority weights',project('priority_levels','priority','tardiness_weight')==Counter([('NORMAL','1'),('HIGH','3'),('URGENT','10')]))
        products={p['product_id']:p for p in data['products']}
        materials={m['material_id']:m for m in data['materials']}
        expected_bom=[]
        cats=[('IR','Inner Ring','INNER_RING','EA','OP20'),('OR','Outer Ring','OUTER_RING','EA','OP20'),('BALLSET','Ball Set','BALL_SET','SET','OP20'),('CAGE','Cage','CAGE','SET','OP30'),('SEAL-RS1','RS1 Seal','SEAL','EA','OP50'),('GREASE','Grease Fill Unit','GREASE','GFU','OP40'),('BOX','Package','PACKAGE','EA','OP80')]
        expected_materials=set()
        for p in products.values():
            for prefix,n,typ,unit,op in cats:
                mid='GREASE-G01' if prefix=='GREASE' else prefix+'-'+p['product_name'].split('-')[0]
                expected_bom.append((p['product_id'],mid,op))
                expected_materials.add((mid,n,typ,unit))
        check('SKU-specific BOM components and consumption stages',project('bom_items','product_id','material_id','consume_op_code')==Counter(expected_bom))
        check('All 25 material categories and units are correct',set(project('materials','material_id','material_name','material_type','unit'))==expected_materials)
        check('Each material has one opening inventory record',set(materials)=={r['material_id'] for r in data['inventory']})
        skillmap=dict(zip(['OP10','OP20','OP30','OP40','OP50','OP60','OP70','OP80'],['PRE_ASSEMBLY','ASSEMBLY','ASSEMBLY','LUBE_SEAL','LUBE_SEAL','INSPECTION','INSPECTION','PACKAGING']))
        types={r['resource_id']:r['resource_type'] for r in data['resources']}
        rtmap={c['op_code']:types[c['resource_id']] for c in data['resource_operations']}
        for pid in products:
            steps=sorted([r for r in data['routing_steps'] if r['product_id']==pid],key=lambda r:int(r['sequence_no']))
            check(pid+' complete eight-step dependency order',[r['op_code'] for r in steps]==ORDER and [int(r['sequence_no']) for r in steps]==list(range(1,9)))
            check(pid+' routing skill/resource/version',all(r['required_skill']==skillmap[r['op_code']] and r['required_resource_type']==rtmap.get(r['op_code']) and r['routing_version']=='V1.6' for r in steps))
        check('All routing requirements have available qualified resources and workers',all(con.execute('SELECT COUNT(*) FROM resources r JOIN resource_operations c ON c.resource_id=r.resource_id WHERE c.op_code=? AND r.resource_type=? AND r.status=?',(s['op_code'],s['required_resource_type'],'AVAILABLE')).fetchone()[0]>0 and con.execute('SELECT COUNT(*) FROM workers w JOIN worker_skills s ON s.worker_id=w.worker_id WHERE s.skill=? AND w.status=?',(s['required_skill'],'AVAILABLE')).fetchone()[0]>0 for s in data['routing_steps']))
        batches=data['production_batches']; operations=data['operations']
        for order in data['sales_orders']:
            bs=sorted([b for b in batches if b['order_id']==order['order_id']],key=lambda b:int(b['sequence_no']))
            check(order['order_id']+' exact deterministic split and quantity conservation',len(bs)==int(order['quantity'])//50 and sum(int(b['quantity']) for b in bs)==int(order['quantity']) and all(int(b['sequence_no'])==i and b['batch_id']==f"{order['order_id']}-R001-B{i:03d}" and b['split_revision']=='1' and b['quantity']=='50' for i,b in enumerate(bs,1)))
        stepmap={r['routing_step_id']:r for r in data['routing_steps']}
        orders={o['order_id']:o for o in data['sales_orders']}
        check('Initial batches and operations all unstarted',all(r['status']=='PENDING' for r in batches+operations))
        for b in batches:
            os=sorted([o for o in operations if o['batch_id']==b['batch_id']],key=lambda o:int(o['sequence_no']))
            if [o['op_code'] for o in os]!=ORDER: errors.append('Operation completeness '+b['batch_id'])
            for o in os:
                s=stepmap[o['routing_step_id']]
                # Duration recomputed from the routing step itself, not from the generator helper.
                expected=int((Decimal(s['setup_min'])+Decimal(s['cycle_sec_per_unit'])*Decimal(b['quantity'])/Decimal(60)).to_integral_value(rounding=ROUND_CEILING))
                if int(o['duration_min'])!=expected or o['operation_id']!=b['batch_id']+'-'+o['op_code'] or s['product_id']!=orders[b['order_id']]['product_id'] or s['op_code']!=o['op_code']:
                    errors.append('Operation duration/ID '+o['operation_id'])
        check('Each batch has exactly eight routing-duration operations',not any(e.startswith(('Operation completeness','Operation duration')) for e in errors))
        check('108 batches / 864 operations / 5400 units',len(batches)==108 and len(operations)==864 and sum(int(b['quantity']) for b in batches)==5400)
        check('Each batch has 66 minutes excluding changeover',all(sum(int(o['duration_min']) for o in operations if o['batch_id']==b['batch_id'])==66 for b in batches))
        setting=data['planning_settings'][0]
        check('Planning horizon and freeze/changeover parameters',[setting[k] for k in ['domain_version','display_timezone','snapshot_clock','horizon_start','horizon_end','freeze_window_min','first_changeover_min','same_sku_changeover_min','different_sku_changeover_min','workers_per_operation','initial_wip_present','initial_published_plan_present']]==['V1.6','Asia/Singapore',utc('2026-09-14 08:30'),utc('2026-09-14 08:30'),utc('2026-09-18 17:30'),'60','0','1','5','1','0','0'])
        calendar=[]
        for day in range(14,19):
            for typ,start,end,approval in [('NORMAL','08:30','12:00','0'),('BREAK','12:00','13:00','0'),('NORMAL','13:00','17:30','0'),('OVERTIME_WINDOW','17:30','19:30','1')]:
                calendar.append((typ,utc(f'2026-09-{day} {start}'),utc(f'2026-09-{day} {end}'),approval))
        check('Five-day shift, lunch and conditional overtime windows',project('calendar_periods','period_type','start_at','end_at','requires_manager_approval')==Counter(calendar))
        check('No scheduling assignment fields in operations',not {'scheduled_start','scheduled_end','assigned_resource','assigned_worker'} & set(operations[0]))
    except (ValueError,KeyError,sqlite3.Error,IndexError,OSError) as exc:
        errors.append(str(exc))
    finally:
        con.close()
    return {'status':'FAIL' if errors else 'PASS','checks':checks,'errors':errors,'row_counts':{t:len(r) for t,r in data.items()},'source_sha256':manifest['source_sha256'],'database_engine_tested':'SQLite (in-memory)','postgresql_execution_tested':False,'solver_run':False}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database-dir',type=Path,default=ROOT)
    p.add_argument('--report',type=Path,default=ROOT/'validation_report.json')
    a=p.parse_args()
    result=validate(a.database_dir)
    a.report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':result['status'],'row_counts':result['row_counts'],'errors':result['errors']},ensure_ascii=False,indent=2))
    raise SystemExit(0 if result['status']=='PASS' else 1)

if __name__=='__main__': main()
