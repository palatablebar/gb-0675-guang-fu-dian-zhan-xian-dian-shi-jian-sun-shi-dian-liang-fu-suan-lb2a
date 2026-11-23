from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta, timezone
from app.coerce import number
from app.errors import BusinessError

SHANGHAI = timezone(timedelta(hours=8))


def money_cents(value):
    """金额统一按分（整数）记账，元只用于展示，杜绝逐事件四舍五入撑破封顶。"""
    return int((number(value) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def yuan(cents):
    return format(Decimal(cents) / 100, '.2f')


def month_of(instant_text):
    return datetime.fromisoformat(instant_text).astimezone(SHANGHAI).strftime('%Y-%m')


def contract_of(context):
    contract = context.get('contract')
    if not isinstance(contract, dict):
        raise BusinessError('CONTRACT_MISSING', '缺少合同规则，无法定稿申报')
    try:
        price = number(contract['price_per_kwh'])
        cap = number(contract['monthly_cap_cny'])
    except (KeyError, BusinessError):
        raise BusinessError('CONTRACT_INVALID', '合同电价或封顶额度无效')
    if price <= 0 or cap < 0 or not contract.get('version'):
        raise BusinessError('CONTRACT_INVALID', '合同电价、封顶额度或版本号无效')
    return {'version': str(contract['version']), 'title': str(contract.get('title', contract['version'])),
            'price': price, 'cap': cap, 'cap_cents': money_cents(cap), 'currency': str(contract.get('currency', 'CNY'))}


def build_events(state):
    """把片段损失归集到并集事件；含未定片段的事件整体列为待查，不进入申报。"""
    rows = state['energy_rows']
    events = []
    for union in state['event_unions']:
        event_ids = union['event_ids']
        members = [r for r in rows if r['event_ids'] == event_ids and r['device'] == union['device']]
        unknown = [r for r in members if r['loss_kwh'] is None]
        confirmed = [r for r in members if r['loss_kwh'] is not None]
        loss = sum((number(r['loss_kwh']) for r in confirmed), Decimal(0)).quantize(Decimal('.0001'))
        events.append({
            'event_id': '+'.join(event_ids),
            'event_ids': event_ids,
            'device': union['device'],
            'start': union['start'],
            'end': union['end'],
            'month': month_of(union['start']),
            'seconds': union['seconds'],
            'confirmed_loss_kwh': str(loss),
            'unknown_segments': len(unknown),
            'unknown_seconds': sum(r['seconds'] for r in unknown),
            'segments': len(members),
        })
    return events


def adjudicate(events, contract):
    """裁定规则：待查事件不申报；可申报事件按开始时刻（再设备、事件号）排序，
    先确认先占用月度封顶额度，额度只减不增，最后一个事件可能被部分覆盖。"""
    price = contract['price']
    cap_cents = contract['cap_cents']
    pending = [e for e in events if e['unknown_segments']]
    claimable = sorted((e for e in events if not e['unknown_segments']),
                       key=lambda e: (e['start'], e['device'], e['event_id']))
    result = []
    remaining = cap_cents
    for e in claimable:
        due_cents = money_cents(number(e['confirmed_loss_kwh']) * price)
        covered_cents = min(due_cents, max(remaining, 0))
        remaining -= covered_cents
        if covered_cents == 0:
            status, reason = '未覆盖', '封顶额度已用尽'
        elif covered_cents < due_cents:
            status, reason = '部分覆盖', '月度封顶额度不足'
        else:
            status, reason = '全额覆盖', '额度内确认'
        result.append({
            **e,
            'contract_version': contract['version'],
            'due_cny': yuan(due_cents),
            'claim_cny': yuan(covered_cents),
            'uncovered_cny': yuan(due_cents - covered_cents),
            'due_cents': due_cents,
            'claim_cents': covered_cents,
            'uncovered_cents': due_cents - covered_cents,
            'cap_remaining_after_cny': yuan(max(remaining, 0)),
            'status': status,
            'reason': reason,
        })
    for e in pending:
        due_cents = money_cents(number(e['confirmed_loss_kwh']) * price)
        result.append({
            **e,
            'contract_version': contract['version'],
            'due_cny': yuan(due_cents),
            'claim_cny': None,
            'uncovered_cny': None,
            'due_cents': due_cents,
            'claim_cents': None,
            'uncovered_cents': None,
            'cap_remaining_after_cny': None,
            'status': '待查',
            'reason': '存在未定片段，损失未全部确认，暂不占用额度',
        })
    result.sort(key=lambda r: (r['start'], r['device'], r['event_id']))
    return result


def summarize(rows, contract, month):
    decided = [r for r in rows if r['claim_cents'] is not None]
    due = sum(r['due_cents'] for r in decided)
    claim = sum(r['claim_cents'] for r in decided)
    uncovered = sum(r['uncovered_cents'] for r in decided)
    pending = [r for r in rows if r['status'] == '待查']
    pending_loss = sum((number(r['confirmed_loss_kwh']) for r in pending), Decimal(0))
    loss = sum((number(r['confirmed_loss_kwh']) for r in decided), Decimal(0))
    invariants = {
        '分账守恒': claim + uncovered == due,
        '额度不超限': claim <= contract['cap_cents'],
        '占用不超应付': claim <= due,
        '无重复占用': len({r['event_id'] for r in decided}) == len(decided),
    }
    return {
        'month': month,
        'contract_version': contract['version'],
        'contract_title': contract['title'],
        'price_per_kwh': str(contract['price']),
        'cap_cny': yuan(contract['cap_cents']),
        'confirmed_loss_kwh': str(loss.quantize(Decimal('.0001'))),
        'due_cny': yuan(due),
        'claim_cny': yuan(claim),
        'cap_used_cny': yuan(claim),
        'cap_remaining_cny': yuan(max(contract['cap_cents'] - claim, 0)),
        'uncovered_cny': yuan(uncovered),
        'pending_events': len(pending),
        'pending_loss_kwh': str(pending_loss.quantize(Decimal('.0001'))),
        'invariants': invariants,
        'balanced': all(invariants.values()),
    }


def apply(state):
    contract = contract_of(state['context'])
    events = build_events(state)
    months = sorted({e['month'] for e in events})
    rows = adjudicate(events, contract)
    summaries = [summarize([r for r in rows if r['month'] == m], contract, m) for m in months]
    # 持久化形态：Decimal 全部转字符串，金额整数分保留，保证批次可 JSON 存档。
    stored_contract = {**contract, 'price': str(contract['price']), 'cap': str(contract['cap'])}
    state['claim'] = {'contract': stored_contract, 'rows': rows, 'months': summaries}
    display_rows = [{k: v for k, v in r.items() if k != 'event_ids'} for r in rows]
    state['views']['claim'] = {
        'title': '月度限电补偿申报',
        'rows': display_rows,
        'months': summaries,
        'metrics': {
            '合同版本': contract['version'],
            '封顶额度_元': yuan(contract['cap_cents']),
            '待查事件': sum(1 for r in rows if r['status'] == '待查'),
            '未覆盖事件': sum(1 for r in rows if r['status'] in {'部分覆盖', '未覆盖'}),
        },
    }
