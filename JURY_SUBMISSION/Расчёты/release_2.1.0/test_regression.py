import copy
import json
import random
import unittest
from datetime import timedelta
from pathlib import Path
from generate import best_load, trial_trip, dt, simulate as legacy
from optimizer import load, simulate, _objective

ROOT = Path(__file__).resolve().parent

class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.c = json.loads((ROOT/'scenario_config.json').read_text(encoding='utf-8'))
        self.s = load(ROOT/'data/scenarios/r1_baseline_central_central', self.c)
        self.u = copy.deepcopy(self.s['units'][0]); self.u['tug_draft_m'] = .1
        self.x = copy.deepcopy(self.s['cargo'][0]); self.x.update(cargo_type='coal', ready_date=self.c['start'], due_date=self.c['end'])
        self.conditions = {(v['date'],v['segment_id']):dict(v, usable_depth_m=.552, max_allowed_draft_m=.402, is_closed=0) for v in self.s['conditions']}
        self.start = dt(self.c['start']); self.close = dt(self.c['end']) + timedelta(days=1)

    def test_small_load_and_legacy(self):
        self.assertIsNone(trial_trip(self.c,self.u,self.x,.9,self.start,self.conditions,self.close))
        self.assertIsNotNone(trial_trip(self.c,self.u,self.x,.4,self.start,self.conditions,self.close))
        t = best_load(self.c,self.u,self.x,.9,self.start,self.conditions,self.close)
        self.assertIsNotNone(t); self.assertGreaterEqual(t['volume_t'], .4)
        x = dict(self.x,volume_t=.9)
        trips,_ = legacy(self.c,[x],[self.u],list(self.conditions.values()),'baseline')
        self.assertTrue(trips); self.assertLess(trips[0]['volume_t'], .9)

    def test_grid_search_matches_brute_force(self):
        rng=random.Random(7)
        for case in range(12):
            cm=copy.deepcopy(self.conditions)
            for row in cm.values():
                allowed=rng.choice([.401,.402,.403,.404,.8])
                row.update(max_allowed_draft_m=allowed,usable_depth_m=allowed+self.c['underkeel_m'])
            end_tick=rng.randint(300,900)
            actual=best_load(self.c,self.u,self.x,end_tick/1000,self.start,cm,self.close)
            expected=None
            for k in range(end_tick,0,-1):
                if trial_trip(self.c,self.u,self.x,k/1000,self.start,cm,self.close): expected=k/1000; break
            self.assertEqual(None if actual is None else actual['volume_t'],expected,case)

    def test_nonmonotone_opening_window_matches_grid(self):
        c=copy.deepcopy(self.c); c.update(end='2026-06-12',handling_fixed_hours=0)
        c['segments']=[dict(segment_id='only',distance_km=1)]
        u=copy.deepcopy(self.u); u.update(handling_tph=.01,speed_upstream_kmh=10,speed_downstream_kmh=10,available_to='2026-06-13T00:00:00')
        start=dt('2026-06-10T13:00:00'); close=dt('2026-06-13T00:00:00')
        cm={}
        for n in range(4):
            day=(dt(c['start'])+timedelta(days=n)).date().isoformat()
            cm[day,'only']=dict(date=day,segment_id='only',usable_depth_m=5,max_allowed_draft_m=4.85,is_closed=int(n==0))
        self.assertIsNone(trial_trip(c,u,self.x,.1,start,cm,close))
        self.assertIsNotNone(trial_trip(c,u,self.x,.2,start,cm,close))
        self.assertIsNone(trial_trip(c,u,self.x,.9,start,cm,close))
        expected=max(k for k in range(1,901) if trial_trip(c,u,self.x,k/1000,start,cm,close))/1000
        self.assertEqual(best_load(c,u,self.x,.9,start,cm,close)['volume_t'],expected)

    def test_known_future_and_no_early_loading(self):
        s=copy.deepcopy(self.s); s['units']=s['units'][:1]
        x=dict(self.x,cargo_id='A',volume_t=10,ready_date='2026-06-10T00:00:00',due_date='2026-06-30T00:00:00')
        y=dict(x,cargo_id='B',volume_t=180,ready_date='2026-06-10T12:00:00')
        s['cargo']=[x,y]
        for row in s['conditions']: row.update(usable_depth_m=5,max_allowed_draft_m=4.85,is_closed=0)
        fcfs=simulate(s,'FCFS'); opt=simulate(s,'OPTIMIZED')
        self.assertEqual(fcfs['trips'][0]['cargo_id'],'A')
        self.assertEqual(opt['trips'][0]['cargo_id'],'B')
        self.assertGreaterEqual(dt(opt['trips'][0]['loading_start']),dt(y['ready_date']))

    def test_lexicographic_preference_for_late_delivery(self):
        s=copy.deepcopy(self.s); s['units']=s['units'][:1]
        s['cargo']=[dict(self.x,cargo_id='on_time',volume_t=10,due_date='2026-06-20T00:00:00'),
                    dict(self.x,cargo_id='late',volume_t=10,due_date='2026-06-10T01:00:00')]
        for row in s['conditions']: row.update(usable_depth_m=5,max_allowed_draft_m=4.85,is_closed=0)
        result=simulate(s,'OPTIMIZED')
        self.assertEqual(result['metrics']['delivered_on_time_t'],10)
        self.assertEqual(result['metrics']['delivered_total_t'],20)
        ontime=[t for t in result['trips'] if t['cargo_id']=='on_time']
        late=[t for t in result['trips'] if t['cargo_id']=='late']
        self.assertTrue(ontime and late)
        from validate import audit
        strategy_on_time_only = ontime
        strategy_with_late = ontime + late
        first = audit(s['config'],s['cargo'],s['units'],s['conditions'],strategy_on_time_only)
        second = audit(s['config'],s['cargo'],s['units'],s['conditions'],strategy_with_late)
        self.assertEqual(first[1],second[1])
        self.assertGreater(second[0],first[0])
        self.assertGreater(_objective(strategy_with_late,s['cargo']),_objective(strategy_on_time_only,s['cargo']))
        # Equal on-time and total tonnage: fewer trips wins.
        t=late[0]; a=dict(t,volume_t=5); b=dict(t,volume_t=5)
        self.assertGreater(_objective(ontime+[t],s['cargo']),_objective(ontime+[a,b],s['cargo']))
        # On-time tonnage is the first criterion.
        self.assertGreater(_objective(ontime,s['cargo']),_objective(late,s['cargo']))

    def test_shared_tug_in_both_engines(self):
        from validate import audit
        units=[copy.deepcopy(self.u),copy.deepcopy(self.u)]
        units[1].update(unit_id='second',barge_id='other_barge')
        cargo=[dict(self.x,cargo_id='A',volume_t=10),dict(self.x,cargo_id='B',volume_t=10)]
        conditions=list(copy.deepcopy(self.conditions).values())
        for row in conditions: row.update(usable_depth_m=5,max_allowed_draft_m=4.85,is_closed=0)
        trips,_=legacy(self.c,cargo,units,conditions,'baseline')
        audit(self.c,cargo,units,conditions,trips)
        self.assertEqual(len(trips),2)
        s=dict(id='shared',config=self.c,cargo=cargo,units=units,conditions=conditions)
        audit(self.c,cargo,units,conditions,simulate(s,'FCFS')['trips'])

    def test_individual_availability_legacy(self):
        u1=copy.deepcopy(self.u); u2=copy.deepcopy(self.u)
        u1.update(unit_id='early',barge_id='b1',tug_id='t1',available_to='2026-06-11T00:00:00')
        u2.update(unit_id='late',barge_id='b2',tug_id='t2',available_to='2026-06-30T00:00:00')
        x=dict(self.x,volume_t=10,ready_date='2026-06-12T00:00:00')
        cm=copy.deepcopy(self.conditions)
        for row in cm.values(): row.update(usable_depth_m=5,max_allowed_draft_m=4.85,is_closed=0)
        trips,_=legacy(self.c,[x],[u1,u2],list(cm.values()),'baseline')
        self.assertEqual(len(trips),1); self.assertEqual(trips[0]['unit_id'],'late')

if __name__ == '__main__': unittest.main()
