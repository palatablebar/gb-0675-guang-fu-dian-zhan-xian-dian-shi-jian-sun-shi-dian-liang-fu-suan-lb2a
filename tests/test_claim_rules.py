from app.engine import evaluate
from app.modules.claim.rules import adjudicate, build_events, contract_of, money_cents, yuan
from decimal import Decimal
from copy import deepcopy
import pytest

V09 = {'version': 'C-2026-09', 'title': '秋季版', 'price_per_kwh': '0.45', 'monthly_cap_cny': '8.00'}

def claim(payload=None):
    payload = payload or {'dataset': 'clean'}
    return evaluate(payload)['claim']

def test_clean_cap_caps_claim_and_keeps_uncovered_balance():
    rows = claim()['rows']
    by = {r['event_id']: r for r in rows}
    first = by['EA1+EA2']
    assert first['status'] == '部分覆盖'
    assert first['due_cny'] == '9.00' and first['claim_cny'] == '8.00'
    assert first['uncovered_cny'] == '1.00' and first['cap_remaining_after_cny'] == '0.00'
    second = by['EB1']
    assert second['status'] == '未覆盖'
    assert second['claim_cny'] == '0.00' and second['uncovered_cny'] == '3.00'

def test_month_summary_explains_every_cent():
    month = claim()['months'][0]
    assert month['cap_cny'] == '8.00' and month['claim_cny'] == '8.00'
    assert month['cap_used_cny'] == '8.00' and month['cap_remaining_cny'] == '0.00'
    assert month['due_cny'] == '12.00' and month['uncovered_cny'] == '4.00'
    assert month['balanced'] and all(month['invariants'].values())

def test_dirty_pending_event_does_not_occupy_cap():
    data = claim({'dataset': 'dirty'})
    pending = [r for r in data['rows'] if r['status'] == '待查']
    assert len(pending) == 1 and pending[0]['event_id'] == 'EA1+EA2'
    assert pending[0]['unknown_segments'] == 2 and pending[0]['claim_cny'] is None
    decided = [r for r in data['rows'] if r['status'] != '待查']
    assert {r['event_id'] for r in decided} == {'EB1'}
    assert decided[0]['claim_cny'] == '3.00'
    summary = data['months'][0]
    assert summary['pending_events'] == 1 and summary['pending_loss_kwh'] == '6.6667'
    assert summary['claim_cny'] == '3.00' and summary['cap_remaining_cny'] == '5.00'

def test_allocation_order_is_start_time_then_device_then_event_id():
    state = evaluate({'dataset': 'clean'})
    events = build_events(state)
    # 让 EB1 明确早于 EA 事件，验证先确认先占用，与设备编号无关。
    for e in events:
        if e['event_id'] == 'EB1':
            e['start'] = '2026-10-15T01:00:00+00:00'
        if e['event_id'] == 'EA1+EA2':
            e['start'] = '2026-10-15T02:00:00+00:00'
    contract = contract_of({**state['context'], 'contract': V09})
    rows = adjudicate(events, contract)
    assert rows[0]['event_id'] == 'EB1' and rows[0]['claim_cny'] == '3.00'
    assert rows[1]['event_id'] == 'EA1+EA2' and rows[1]['claim_cny'] == '5.00'

def test_money_rounded_in_cents_never_breaks_cap():
    assert money_cents(Decimal('26.6667') * Decimal('0.45')) == 1200
    assert yuan(800) == '8.00'

def test_cap_larger_than_due_covers_everything():
    generous = {'version': 'C-X', 'title': '足额', 'price_per_kwh': '0.45', 'monthly_cap_cny': '99.00'}
    data = claim({'dataset': 'clean', 'overrides': {'contract': generous}})
    assert all(r['status'] == '全额覆盖' for r in data['rows'])
    m = data['months'][0]
    assert m['claim_cny'] == '12.00' and m['uncovered_cny'] == '0.00' and m['cap_remaining_cny'] == '87.00'

def test_contract_rule_update_recalculates_affected_events():
    old = claim()
    new_contract = {'version': 'C-2026-10', 'title': '修订', 'price_per_kwh': '0.50', 'monthly_cap_cny': '10.00'}
    new = claim({'dataset': 'clean', 'overrides': {'contract': new_contract}})
    assert old['rows'][0]['contract_version'] == 'C-2026-09'
    assert new['rows'][0]['contract_version'] == 'C-2026-10'
    by = {r['event_id']: r for r in new['rows']}
    assert by['EA1+EA2']['due_cny'] == '10.00' and by['EA1+EA2']['claim_cny'] == '10.00'
    assert by['EB1']['status'] == '未覆盖' and by['EB1']['uncovered_cny'] == '3.33'

def test_invalid_contract_rejected():
    with pytest.raises(Exception):
        claim({'dataset': 'clean', 'overrides': {'contract': {'version': 'BAD', 'price_per_kwh': '0', 'monthly_cap_cny': '1'}}})

def test_event_loss_losskwh_matches_reconciled_total():
    state = evaluate({'dataset': 'clean'})
    total = sum(Decimal(r['confirmed_loss_kwh']) for r in state['claim']['rows'])
    assert total == Decimal(state['balance']['loss_kwh'])
