import sqlite3, json
from pathlib import Path
from app.errors import BusinessError
from app.config import load_contracts

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    con.execute("""CREATE TABLE IF NOT EXISTS claim_ledger (
        run_id TEXT NOT NULL, month TEXT NOT NULL, event_id TEXT NOT NULL,
        device TEXT NOT NULL, contract_version TEXT NOT NULL,
        loss_kwh TEXT NOT NULL, due_cents INTEGER NOT NULL, claim_cents INTEGER NOT NULL,
        uncovered_cents INTEGER NOT NULL, status TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (run_id, month, event_id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS contracts (
        version TEXT PRIMARY KEY, title TEXT NOT NULL, price_per_kwh TEXT NOT NULL,
        monthly_cap_cny TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 0)""")
    return con

def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
    return get(path,run_id)

def get(path, run_id):
    with connect(path) as con: row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
    result=json.loads(row['result']);result.update(revision=row['revision'],status=row['status'])
    return result

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)

# ---------- 月度补偿台账 ----------

def _seed_contracts(con):
    registry=load_contracts()
    for version, spec in registry['contracts'].items():
        con.execute("""INSERT INTO contracts(version,title,price_per_kwh,monthly_cap_cny,active)
            VALUES(?,?,?,?,0) ON CONFLICT(version) DO UPDATE SET
            title=excluded.title, price_per_kwh=excluded.price_per_kwh,
            monthly_cap_cny=excluded.monthly_cap_cny""",
            (version,spec['title'],spec['price_per_kwh'],spec['monthly_cap_cny']))
    if not con.execute('SELECT 1 FROM contracts WHERE active=1').fetchone():
        con.execute('UPDATE contracts SET active=1 WHERE version=?',(registry['active'],))

def active_contract(path):
    with connect(path) as con:
        _seed_contracts(con)
        row=con.execute('SELECT * FROM contracts WHERE active=1').fetchone()
        return dict(row)

def contracts(path):
    with connect(path) as con:
        _seed_contracts(con)
        return [dict(r) for r in con.execute('SELECT version,title,price_per_kwh,monthly_cap_cny,active FROM contracts ORDER BY version')]

def switch_contract(path, version, verify=None):
    """事务化切换合同激活版本：校验失败或切换后校验不过则整体回滚，旧版保持激活。"""
    with connect(path) as con:
        _seed_contracts(con)
        target=con.execute('SELECT * FROM contracts WHERE version=?',(version,)).fetchone()
        if not target: raise BusinessError('CONTRACT_UNKNOWN','合同版本不存在')
        old=dict(con.execute('SELECT * FROM contracts WHERE active=1').fetchone())
        try:
            con.execute('UPDATE contracts SET active=0')
            con.execute('UPDATE contracts SET active=1 WHERE version=?',(version,))
            now=dict(con.execute('SELECT * FROM contracts WHERE active=1').fetchone())
            if now['version']!=version:
                raise BusinessError('CONTRACT_SWITCH_FAILED','额度规则切换失败，已恢复旧版')
            if verify is not None and not verify(now):
                raise BusinessError('CONTRACT_SWITCH_FAILED','额度规则切换校验失败，已恢复旧版')
        except Exception:
            con.rollback()
            raise
    return active_contract(path)

def _month_cap_cents(con, month):
    row=con.execute('SELECT monthly_cap_cny FROM contracts WHERE active=1').fetchone()
    return int(round(float(row['monthly_cap_cny'])*100)) if row else None

def finalize_claim(path, run_id):
    """将批次的事件级裁定写入月度台账（幂等）：
    已确认行永久保留；同批重跑只刷新未确认行，不新增占用；同月下其他批次占用计入封顶校验。"""
    result=get(path, run_id)
    claim=result.get('claim')
    if not claim: raise BusinessError('INCOMPLETE_RUN','批次尚未完成补偿核算')
    rows=claim['rows']
    with connect(path) as con:
        _seed_contracts(con)
        active=dict(con.execute('SELECT * FROM contracts WHERE active=1').fetchone())
        if any(r['contract_version']!=active['version'] for r in rows):
            raise BusinessError('CONTRACT_MISMATCH','批次合同版本与当前激活版本不一致，请按新规则重算后再定稿')
        for month in sorted({r['month'] for r in rows}):
            cap_cents=int(round(float(active['monthly_cap_cny'])*100))
            decided=[r for r in rows if r['month']==month and r['claim_cents'] is not None]
            other=con.execute("""SELECT COALESCE(SUM(claim_cents),0) FROM claim_ledger
                WHERE month=? AND run_id<>? AND contract_version=?""",
                (month,run_id,active['version'])).fetchone()[0]
            if other + sum(r['claim_cents'] for r in decided) > cap_cents:
                raise BusinessError('CAP_EXCEEDED', f'{month} 月度封顶额度不足，无法定稿，请重算裁定')
            for r in rows:
                if r['month']!=month: continue
                existing=con.execute('SELECT confirmed FROM claim_ledger WHERE run_id=? AND month=? AND event_id=?',
                                     (run_id,month,r['event_id'])).fetchone()
                if existing and existing['confirmed']:
                    continue  # 已确认记录保留，重跑不覆盖、不重复占用
                if r['claim_cents'] is None:
                    claim_cents, uncovered_cents, due_cents = 0, 0, r['due_cents']
                else:
                    claim_cents, uncovered_cents, due_cents = r['claim_cents'], r['uncovered_cents'], r['due_cents']
                con.execute("""INSERT INTO claim_ledger(run_id,month,event_id,device,contract_version,
                    loss_kwh,due_cents,claim_cents,uncovered_cents,status,confirmed)
                    VALUES(?,?,?,?,?,?,?,?,?,?,0)
                    ON CONFLICT(run_id,month,event_id) DO UPDATE SET
                    device=excluded.device, contract_version=excluded.contract_version,
                    loss_kwh=excluded.loss_kwh, due_cents=excluded.due_cents,
                    claim_cents=excluded.claim_cents, uncovered_cents=excluded.uncovered_cents,
                    status=excluded.status, confirmed=claim_ledger.confirmed""",
                    (run_id,month,r['event_id'],r['device'],r['contract_version'],
                     str(r['confirmed_loss_kwh']),due_cents,claim_cents,uncovered_cents,r['status']))
    return claim_ledger(path, run_id=run_id)

def confirm_claim(path, run_id):
    with connect(path) as con:
        _seed_contracts(con)
        cur=con.execute("UPDATE claim_ledger SET confirmed=1 WHERE run_id=? AND confirmed=0 AND status<>'待查'",(run_id,))
        if not con.execute('SELECT 1 FROM claim_ledger WHERE run_id=?',(run_id,)).fetchone():
            raise BusinessError('LEDGER_EMPTY','台账为空，请先定稿申报')
        changed=cur.rowcount
    return {'run_id':run_id,'confirmed_rows':changed,'ledger':claim_ledger(path, run_id=run_id)}

def claim_ledger(path, run_id=None, month=None):
    with connect(path) as con:
        sql='SELECT * FROM claim_ledger WHERE 1=1'; params=[]
        if run_id: sql+=' AND run_id=?'; params.append(run_id)
        if month: sql+=' AND month=?'; params.append(month)
        sql+=' ORDER BY month, run_id, event_id'
        rows=[dict(r) for r in con.execute(sql,params)]
    for r in rows:
        r['due_cny']=f"{r['due_cents']/100:.2f}"; r['claim_cny']=f"{r['claim_cents']/100:.2f}"
        r['uncovered_cny']=f"{r['uncovered_cents']/100:.2f}"; r['confirmed']=bool(r['confirmed'])
    return rows

def month_occupation(path):
    """按月汇总实际额度占用（仅已确认行），验证重跑不重复占用。"""
    with connect(path) as con:
        _seed_contracts(con)
        out=[]
        groups=con.execute("""SELECT month, contract_version, COUNT(*) AS events,
            SUM(due_cents) AS due_cents, SUM(claim_cents) AS claim_cents,
            SUM(uncovered_cents) AS uncovered_cents, SUM(confirmed) AS confirmed
            FROM claim_ledger GROUP BY month, contract_version ORDER BY month, contract_version""")
        for g in groups:
            cap=con.execute('SELECT monthly_cap_cny FROM contracts WHERE version=?',(g['contract_version'],)).fetchone()
            cap_cents=int(round(float(cap['monthly_cap_cny'])*100)) if cap else None
            claim=g['claim_cents'] or 0
            out.append({'month':g['month'],'contract_version':g['contract_version'],'events':g['events'],
                'due_cny':f"{(g['due_cents'] or 0)/100:.2f}",'claim_cny':f"{claim/100:.2f}",
                'uncovered_cny':f"{(g['uncovered_cents'] or 0)/100:.2f}",
                'cap_cny':f"{cap_cents/100:.2f}" if cap_cents is not None else None,
                'cap_remaining_cny':f"{(cap_cents-claim)/100:.2f}" if cap_cents is not None else None,
                'within_cap': cap_cents is None or claim<=cap_cents,
                'confirmed_rows':g['confirmed']})
        return out
