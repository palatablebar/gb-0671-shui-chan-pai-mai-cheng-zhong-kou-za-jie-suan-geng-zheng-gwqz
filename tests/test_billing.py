"""可开票结果纯函数测试：三道闸门 + 守恒。"""
from copy import deepcopy
from app.modules.billing.rules import build_invoiceable


def test_all_final_tickets_invoiceable_real(sample):
    invoiceable, blocked, summary = build_invoiceable(sample)
    assert len(invoiceable) == len(sample['final']) == 20
    assert blocked == []
    assert summary['conservation_ok'] is True
    # 可开金额等于生效应付总额
    assert summary['invoiceable_amount'] == sample['balance']['amount']


def test_broken_chain_blocks_ticket(sample):
    # 更正缺前版/版本冲突：该票不进入可开结果
    s = deepcopy(sample)
    s['version_conflicts'] = [{'ticket': '00002', 'code': 'STALE_CORRECTION'}]
    invoiceable, blocked, _ = build_invoiceable(s)
    assert all(r['ticket'] != '00002' for r in invoiceable)
    row = next(b for b in blocked if b['ticket'] == '00002')
    assert row['blockers'][0]['code'] == 'BROKEN_CHAIN'


def test_amount_unbalanced_blocks_ticket(sample):
    # 金额未对平：amount != paid + due
    s = deepcopy(sample)
    s['final'][0]['due'] = '999.99'
    invoiceable, blocked, summary = build_invoiceable(s)
    assert all(r['ticket'] != s['final'][0]['ticket'] for r in invoiceable)
    row = next(b for b in blocked if b['ticket'] == s['final'][0]['ticket'])
    assert row['blockers'][0]['code'] == 'AMOUNT_UNBALANCED'
    assert summary['conservation_ok'] is True  # 票只是转入待处理，金额不丢


def test_duplicate_invoice_no_injected_blocks(sample):
    # 台账回灌的重复发票号在进入可开结果前拦截
    ticket = sample['final'][0]['ticket']
    invoiceable, blocked, _ = build_invoiceable(sample, {ticket: 'INV-DUP'})
    assert all(r['ticket'] != ticket for r in invoiceable)
    row = next(b for b in blocked if b['ticket'] == ticket)
    assert row['blockers'][0]['code'] == 'DUP_INVOICE_NO'
    assert row['blockers'][0]['invoice_no'] == 'INV-DUP'


def test_clean_dataset_conservation(clean):
    invoiceable, blocked, summary = build_invoiceable(clean)
    assert len(invoiceable) == len(clean['final'])
    assert summary['conservation_ok'] is True
    assert summary['blocked_tickets'] == 0


def test_snapshot_freezes_amount(sample):
    # 快照金额冻结开票当时值；后续 state 被改动不影响已生成快照
    invoiceable, _, _ = build_invoiceable(sample)
    first = invoiceable[0]
    mutated = deepcopy(sample)
    mutated['final'][0]['amount'] = '0.01'
    again, _, _ = build_invoiceable(mutated)
    assert again[0]['amount'] != first['amount'] or mutated['final'][0]['ticket'] != first['ticket']
    assert first['amount'] == '1285.35'  # 原快照保持不变


def test_multiple_blockers_collected(sample):
    # 多道闸门同时命中时原因全部列出，且票不进可开结果
    s = deepcopy(sample)
    ticket = s['final'][0]['ticket']
    s['version_conflicts'] = [{'ticket': ticket, 'code': 'MISSING_BASE'}]
    s['final'][0]['due'] = '1.00'  # 制造金额不平
    invoiceable, blocked, _ = build_invoiceable(s, {ticket: 'INV-X'})
    assert all(r['ticket'] != ticket for r in invoiceable)
    row = next(b for b in blocked if b['ticket'] == ticket)
    codes = {b['code'] for b in row['blockers']}
    assert codes == {'DUP_INVOICE_NO', 'BROKEN_CHAIN', 'AMOUNT_UNBALANCED'}
