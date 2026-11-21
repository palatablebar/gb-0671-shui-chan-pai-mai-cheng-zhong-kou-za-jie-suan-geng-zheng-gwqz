"""可开票结果纯函数规则。

输入：correction 之后的 state（final/version_conflicts/balance）。
输出：每票可开票快照 invoiceable 与待处理票 blocked。
本模块不落库、不产生副作用；发票号查重、开票幂等与发布事务在 app.invoice_store 中保证。

三道闸门（任一命中即不进入可开结果，落入待处理票）：
  BROKEN_CHAIN      更正缺前版/版本冲突，唯一生效版无法确定（correction 已裁决）
  AMOUNT_UNBALANCED 生效应付 != 已付 + 待付
  DUP_INVOICE_NO    发票号重复（由台账在开票请求时回灌阻断，见 invoice_store）
"""
from decimal import Decimal
from app.coerce import money

GATE_REASONS = {
    'DUP_INVOICE_NO': '发票号重复',
    'BROKEN_CHAIN': '更正链缺前版或版本冲突，唯一生效版无法确定',
    'AMOUNT_UNBALANCED': '金额未对平：生效应付不等于已付加待付',
}


def _d(value):
    return Decimal(str(value))


def build_invoiceable(state, rejected_invoice_nos=None):
    """按票生成可开票结果。返回 (invoiceable, blocked, summary)。

    rejected_invoice_nos: 台账已占用/重复的发票号映射 {ticket: invoice_no}，
    由调用方在开票前注入，命中 DUP_INVOICE_NO 闸门。

    可开票快照冻结开票当时的生效版本与金额；后到的更正不会改写历史快照，
    只会在重算时形成新的差额，由台账以调整票补开/红冲。
    """
    rejected_invoice_nos = rejected_invoice_nos or {}
    broken_tickets = {c['ticket'] for c in state.get('version_conflicts', [])}

    invoiceable, blocked = [], []
    for row in state['final']:
        ticket = row['ticket']
        blockers = []
        if ticket in rejected_invoice_nos:
            blockers.append({'code': 'DUP_INVOICE_NO', 'detail': GATE_REASONS['DUP_INVOICE_NO'],
                             'invoice_no': rejected_invoice_nos[ticket]})
        if ticket in broken_tickets:
            blockers.append({'code': 'BROKEN_CHAIN', 'detail': GATE_REASONS['BROKEN_CHAIN']})
        amount, paid, due = _d(row['amount']), _d(row['paid']), _d(row['due'])
        if amount != paid + due:
            blockers.append({'code': 'AMOUNT_UNBALANCED', 'detail': GATE_REASONS['AMOUNT_UNBALANCED'],
                             'gap': money(amount - paid - due)})
        if blockers:
            blocked.append({'ticket': ticket, 'version': row['version'], 'amount': row['amount'],
                            'blockers': blockers})
            continue
        invoiceable.append({
            'ticket': ticket,
            'version': row['version'],
            'fish': row.get('fish', ''),
            'amount': money(amount),
            'paid': money(paid),
            'due': money(due),
            'lines': row.get('lines', []),
            'code': row.get('code', ''),
        })

    summary = _summarize(invoiceable, blocked, state)
    return invoiceable, blocked, summary


def _summarize(invoiceable, blocked, state):
    inv_amount = sum((_d(r['amount']) for r in invoiceable), Decimal(0))
    blk_amount = sum((_d(r['amount']) for r in blocked), Decimal(0))
    return {
        'invoiceable_tickets': len(invoiceable),
        'blocked_tickets': len(blocked),
        'invoiceable_amount': money(inv_amount),
        'invoiceable_due': money(sum((_d(r['due']) for r in invoiceable), Decimal(0))),
        'blocked_amount': money(blk_amount),
        'final_amount': state['balance']['amount'],
        # 整批守恒：可开票金额 + 待处理票金额 == 生效应付总额，不丢票、不重计
        'conservation_ok': money(inv_amount + blk_amount) == state['balance']['amount'],
    }


def apply(state):
    invoiceable, blocked, summary = build_invoiceable(state)
    state['invoiceable'] = invoiceable
    state['invoice_blocked'] = blocked
    state['invoice_summary'] = summary
    state['views']['billing'] = {
        'title': '可开票结果',
        'rows': invoiceable,
        'blocked': blocked,
        'metrics': {
            '可开票': summary['invoiceable_tickets'],
            '待处理票': summary['blocked_tickets'],
            '可开金额_CNY': summary['invoiceable_amount'],
            '待付_CNY': summary['invoiceable_due'],
        },
    }
