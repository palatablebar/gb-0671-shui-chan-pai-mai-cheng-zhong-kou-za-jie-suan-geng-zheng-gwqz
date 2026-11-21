"""开票台账测试：幂等、发票号查重、调整票、发布中断恢复、只追加凭证。"""
import sqlite3
import pytest
from app import invoice_store
from app.errors import BusinessError


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / 'invoices.sqlite')


def _ticket(state, idx=0):
    return state['final'][idx]['ticket']


def test_issue_once_and_idempotent_rerun(db, sample):
    ticket = _ticket(sample)
    first = invoice_store.issue(db, sample, ticket, 'INV-0001')
    assert first['opened_amount'] == sample['invoiceable'][0]['amount']
    # 同一票重跑：不产生第二张票，返回既有记录
    again = invoice_store.issue(db, sample, ticket, 'INV-0001')
    assert again == first
    ledger = invoice_store.ledger(db, sample)
    row = next(t for t in ledger['tickets'] if t['ticket'] == ticket)
    assert row['original_amount'] == first['opened_amount']
    assert row['remaining'] == '0.00'


def test_duplicate_invoice_no_rejected(db, sample):
    t1, t2 = _ticket(sample, 0), _ticket(sample, 1)
    invoice_store.issue(db, sample, t1, 'INV-DUP')
    with pytest.raises(BusinessError) as exc:
        invoice_store.issue(db, sample, t2, 'INV-DUP')
    assert exc.value.code == 'DUP_INVOICE_NO'
    # 被拒票没有任何发票/事件残留
    ledger = invoice_store.ledger(db, sample)
    assert all(t['ticket'] != t2 or t['opened_amount'] == '0.00' for t in ledger['tickets'])


def test_blocked_ticket_cannot_be_issued(db, sample):
    s = dict(sample)
    s['invoice_blocked'] = [{'ticket': '99999', 'version': 1, 'amount': '10.00',
                             'blockers': [{'code': 'BROKEN_CHAIN', 'detail': 'x'}]}]
    s['invoiceable'] = [r for r in sample['invoiceable'] if r['ticket'] != '99999']
    with pytest.raises(BusinessError) as exc:
        invoice_store.issue(db, s, '99999', 'INV-X')
    assert exc.value.code == 'TICKET_BLOCKED'


def test_adjustment_after_correction_delta(db, sample):
    # 先按当前应付开票；模拟后到更正使最终应付变大，重算后只补差额
    ticket = _ticket(sample)
    invoice_store.issue(db, sample, ticket, 'INV-0001')
    s2 = dict(sample)
    snap = dict(sample['invoiceable'][0])
    snap['amount'] = '1400.00'
    snap['due'] = '231.50'
    s2['invoiceable'] = [snap] + sample['invoiceable'][1:]
    final0 = dict(sample['final'][0]); final0['amount'] = '1400.00'; final0['due'] = '231.50'
    s2['final'] = [final0] + sample['final'][1:]
    view = invoice_store.adjust(db, s2, ticket, 'INV-0002')
    assert view['opened_amount'] == '1400.00'
    ledger = invoice_store.ledger(db, s2)
    row = next(t for t in ledger['tickets'] if t['ticket'] == ticket)
    assert row['original_amount'] == '1285.35'
    assert row['adjustment_amount'] == '114.65'
    assert row['final_amount'] == '1400.00'
    assert row['remaining'] == '0.00'
    # 调整票重跑幂等：同版本不重复开
    again = invoice_store.adjust(db, s2, ticket, 'INV-0002')
    assert again == view


def test_negative_adjustment_credit_note(db, sample):
    ticket = _ticket(sample)
    invoice_store.issue(db, sample, ticket, 'INV-0001')
    s2 = dict(sample)
    snap = dict(sample['invoiceable'][0])
    snap['amount'] = '1000.00'; snap['due'] = '-168.50'
    s2['invoiceable'] = [snap] + sample['invoiceable'][1:]
    final0 = dict(sample['final'][0]); final0['amount'] = '1000.00'; final0['due'] = '-168.50'
    s2['final'] = [final0] + sample['final'][1:]
    view = invoice_store.adjust(db, s2, ticket, 'INV-CN1')
    # 红冲：已开净额回到最终应付
    assert view['opened_amount'] == '1000.00'


def test_publish_interruption_restores_old_records(db, sample):
    ticket = _ticket(sample)
    run_id = sample['manifest']['run_id']
    invoice_store.issue(db, sample, ticket, 'INV-0001')
    # 模拟发布中断：手工制造一条 publishing 投影（有 issue 事件、无 published 事件）
    con = invoice_store.connect(db)
    with con:
        seq = con.execute('SELECT MAX(seq) FROM invoice_events').fetchone()[0]
        con.execute("INSERT INTO invoices(run_id,ticket,kind,version,invoice_no,amount,status,updated_seq)"
                    " VALUES(?,?, 'original', 9, 'INV-STUCK', '9.99', 'publishing', ?)",
                    (run_id, ticket, seq))
    # 旧版发票记录仍在
    old = con.execute("SELECT status FROM invoices WHERE ticket=? AND version=2", (ticket,)).fetchone()
    assert old['status'] == 'issued'
    con.close()

    result = invoice_store.resume_path(db, sample)
    assert result['recovered'] and result['recovered'][0]['ticket'] == ticket
    con = invoice_store.connect(db)
    stuck = con.execute("SELECT * FROM invoices WHERE status='publishing'").fetchall()
    assert stuck == []
    # 旧版记录原样保留
    old = con.execute("SELECT invoice_no,status FROM invoices WHERE ticket=? AND version=2", (ticket,)).fetchone()
    assert dict(old) == {'invoice_no': 'INV-0001', 'status': 'issued'}
    # 中断动作有 voided 留痕，事件日志不丢
    events = [r['event_type'] for r in con.execute('SELECT event_type FROM invoice_events')]
    assert 'voided' in events
    con.close()
    # resume 可安全重入
    assert invoice_store.resume_path(db, sample)['recovered'] == []


def test_events_append_only_no_loss(db, sample):
    t1, t2 = _ticket(sample, 0), _ticket(sample, 1)
    invoice_store.issue(db, sample, t1, 'INV-0001')
    with pytest.raises(BusinessError):
        invoice_store.issue(db, sample, t2, 'INV-0001')  # 重复号被拒
    invoice_store.issue(db, sample, t2, 'INV-0002')
    con = invoice_store.connect(db)
    # 事件表只追加：不存在任何更新/删除接口；成功动作的事件齐全
    types = [r[0] for r in con.execute('SELECT event_type FROM invoice_events ORDER BY seq')]
    assert types.count('issue') == 2
    assert types.count('published') == 2
    con.close()


def test_ledger_shows_unissued_invoiceable_tickets(db, sample):
    ledger = invoice_store.ledger(db, sample)
    # 未开票的可开票也出现在台账，待开差额=最终应付
    assert len(ledger['tickets']) == len(sample['invoiceable'])
    row = ledger['tickets'][0]
    assert row['remaining'] == row['final_amount']
    assert row['invoice_nos'] == []


def test_empty_invoice_no_and_unknown_ticket(db, sample):
    with pytest.raises(BusinessError) as e:
        invoice_store.issue(db, sample, _ticket(sample), '  ')
    assert e.value.code == 'BAD_INVOICE_NO'
    with pytest.raises(BusinessError) as e2:
        invoice_store.issue(db, sample, 'NO-SUCH-TICKET', 'INV-Z')
    assert e2.value.code == 'TICKET_NOT_FOUND'
    assert e2.value.status == 404


def test_zero_delta_adjustment_creates_nothing(db, sample):
    ticket = _ticket(sample)
    invoice_store.issue(db, sample, ticket, 'INV-0001')
    before = invoice_store.ledger(db, sample)
    # 最终应付未变：调整差额为 0，不产生新票
    view = invoice_store.adjust(db, sample, ticket, 'INV-ZERO')
    assert len(view['invoices']) == 1
    after = invoice_store.ledger(db, sample)
    assert len(after['events']) == len(before['events'])


def test_publish_crash_leaves_no_half_invoice(db, sample, monkeypatch):
    # 模拟发布确认事件写入时崩溃：事务整体回滚，无残留半张票、无事件
    ticket = _ticket(sample, 3)
    original_append = invoice_store._append
    calls = {'n': 0}

    def flaky(con, run_id, event_type, *args):
        if event_type == 'published':
            calls['n'] += 1
            raise sqlite3.OperationalError('simulated crash during publish')
        return original_append(con, run_id, event_type, *args)

    monkeypatch.setattr(invoice_store, '_append', flaky)
    with pytest.raises(sqlite3.OperationalError):
        invoice_store.issue(db, sample, ticket, 'INV-CRASH')

    monkeypatch.undo()
    con = invoice_store.connect(db)
    assert con.execute("SELECT COUNT(*) FROM invoices WHERE ticket=?", (ticket,)).fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM invoice_events WHERE ticket=?", (ticket,)).fetchone()[0] == 0
    con.close()
    # 恢复后可重新开票（旧版记录无损，因为根本没写成功）
    view = invoice_store.issue(db, sample, ticket, 'INV-CRASH')
    assert view['invoices'][0]['status'] == 'issued'


def test_original_invoice_untouched_after_correction(db, sample):
    # 后到更正后原发票金额与发票号保持不变，只新增调整票
    ticket = _ticket(sample)
    invoice_store.issue(db, sample, ticket, 'INV-KEEP')
    s2 = dict(sample)
    snap = dict(sample['invoiceable'][0])
    snap['amount'] = '1300.00'
    snap['due'] = str(__import__('decimal').Decimal('1300.00') - __import__('decimal').Decimal(snap['paid']))
    s2['invoiceable'] = [snap] + sample['invoiceable'][1:]
    f0 = dict(sample['final'][0]); f0['amount'] = '1300.00'
    f0['due'] = snap['due']
    s2['final'] = [f0] + sample['final'][1:]
    invoice_store.adjust(db, s2, ticket, 'INV-KEEP-ADJ')
    con = invoice_store.connect(db)
    original = con.execute(
        "SELECT invoice_no,amount,status FROM invoices WHERE ticket=? AND kind='original'",
        (ticket,)).fetchone()
    assert dict(original) == {'invoice_no': 'INV-KEEP', 'amount': sample['invoiceable'][0]['amount'],
                              'status': 'issued'}
    con.close()
