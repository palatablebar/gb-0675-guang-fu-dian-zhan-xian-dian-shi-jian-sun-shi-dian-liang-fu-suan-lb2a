from fastapi.testclient import TestClient
from app import main, store
from app.engine import evaluate
from app.errors import BusinessError
import pytest


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'claim.sqlite'))
    return TestClient(main.app)


def make_run(p, overrides=None):
    payload = {'dataset': 'clean'}
    if overrides:
        payload['overrides'] = overrides
    state = evaluate(payload)
    return store.save(p, state)


def test_finalize_idempotent_rerun_keeps_single_occupation(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    p = main.DB_PATH
    run = make_run(p)
    rid = run['manifest']['run_id']
    first = c.post(f'/api/runs/{rid}/claim/finalize').json()['ledger']
    second = c.post(f'/api/runs/{rid}/claim/finalize').json()['ledger']
    assert len(first) == len(second) == 2
    assert sum(r['claim_cents'] for r in second) == 800
    ledger = c.get('/api/claim-ledger').json()
    assert len(ledger['rows']) == 2
    occ = ledger['occupation'][0]
    assert occ['claim_cny'] == '8.00' and occ['within_cap'] is True


def test_confirm_then_rerun_preserves_confirmed_records(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    p = main.DB_PATH
    rid = make_run(p)['manifest']['run_id']
    c.post(f'/api/runs/{rid}/claim/finalize')
    confirmed = c.post(f'/api/runs/{rid}/claim/confirm').json()
    assert confirmed['confirmed_rows'] == 2
    again = c.post(f'/api/runs/{rid}/claim/confirm').json()
    assert again['confirmed_rows'] == 0
    c.post(f'/api/runs/{rid}/claim/finalize')
    rows = store.claim_ledger(p, run_id=rid)
    assert all(r['confirmed'] for r in rows)
    assert sum(r['claim_cents'] for r in rows) == 800


def test_contract_switch_and_versioned_recalc_occupation(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    p = main.DB_PATH
    rid_old = make_run(p)['manifest']['run_id']
    c.post(f'/api/runs/{rid_old}/claim/finalize')
    c.post(f'/api/runs/{rid_old}/claim/confirm')
    contracts = c.get('/api/contracts').json()
    assert contracts['active']['version'] == 'C-2026-09'
    # 旧版本合同下的新批次不能直接定稿
    new_contract = {'version': 'C-2026-10', 'title': '修订', 'price_per_kwh': '0.50', 'monthly_cap_cny': '10.00'}
    rid_new = make_run(p, {'contract': new_contract})['manifest']['run_id']
    assert rid_old != rid_new
    resp = c.post(f'/api/runs/{rid_new}/claim/finalize')
    assert resp.status_code == 422 and resp.json()['code'] == 'CONTRACT_MISMATCH'
    # 切换后按新版本定稿，两个版本各自占用各自额度，互不重复
    switched = c.post('/api/contracts/activate', json={'version': 'C-2026-10'})
    assert switched.status_code == 200 and switched.json()['version'] == 'C-2026-10'
    ok = c.post(f'/api/runs/{rid_new}/claim/finalize')
    assert ok.status_code == 200
    occupation = {o['contract_version']: o for o in c.get('/api/claim-ledger').json()['occupation']}
    assert occupation['C-2026-09']['claim_cny'] == '8.00'
    assert occupation['C-2026-10']['claim_cny'] == '10.00'
    # 旧批次的已确认记录仍然保留
    old_rows = store.claim_ledger(p, run_id=rid_old)
    assert all(r['confirmed'] and r['contract_version'] == 'C-2026-09' for r in old_rows)


def test_switch_unknown_version_rejected(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    resp = c.post('/api/contracts/activate', json={'version': 'NOPE'})
    assert resp.status_code == 422 and resp.json()['code'] == 'CONTRACT_UNKNOWN'
    assert c.get('/api/contracts').json()['active']['version'] == 'C-2026-09'


def test_switch_failure_rolls_back_to_old_version(tmp_path, monkeypatch):
    p = str(tmp_path / 'rollback.sqlite')
    monkeypatch.setattr(main, 'DB_PATH', p)
    store.switch_contract(p, 'C-2026-10')
    assert store.active_contract(p)['version'] == 'C-2026-10'
    with pytest.raises(BusinessError) as exc:
        store.switch_contract(p, 'C-2026-09', verify=lambda row: False)
    assert exc.value.code == 'CONTRACT_SWITCH_FAILED'
    assert store.active_contract(p)['version'] == 'C-2026-10'


def test_finalize_missing_claim_422(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    state = evaluate({'dataset': 'clean'}, until=7)
    state.pop('claim', None)
    rid = store.save(main.DB_PATH, state)['manifest']['run_id']
    resp = c.post(f'/api/runs/{rid}/claim/finalize')
    assert resp.status_code == 422 and resp.json()['code'] == 'INCOMPLETE_RUN'


def test_claim_page_and_api_available(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={}).json()
    rid = run['manifest']['run_id']
    assert c.post('/api/claim', json={}).status_code == 200
    page = c.get(f'/claim?run_id={rid}')
    assert page.status_code == 200
    assert '月度可申报补偿构成' in page.text and '待查事件' in page.text


def test_ledger_empty_confirm_rejected(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = make_run(main.DB_PATH)['manifest']['run_id']
    resp = c.post(f'/api/runs/{rid}/claim/confirm')
    assert resp.status_code == 422 and resp.json()['code'] == 'LEDGER_EMPTY'


def test_archive_zip_contains_claim_evidence(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = make_run(main.DB_PATH)['manifest']['run_id']
    zipped = c.get(f'/api/export/{rid}').content
    import io, zipfile
    with zipfile.ZipFile(io.BytesIO(zipped)) as zf:
        assert 'claim.json' in zf.namelist()
