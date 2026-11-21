"""开票台账：只追加（append-only）事件日志 + 发票现状投影。

设计要点
--------
1. invoice_events 只 INSERT，从不 UPDATE/DELETE 业务事件：原始凭证与每次票据动作
   永久留存（_resume 仅清理从未发布成功的投影行，并追加 voided 事件留痕）。
2. invoices 是事件投影，状态变更与事件写入在同一显式事务；发布中断（进程退出/异常）
   时事务整体回滚，旧版发票记录原样保留；残留 publishing 由 resume 收敛。
3. 同一票重跑幂等：事件键 UNIQUE + 投影现状检查，重复开票请求直接返回既有记录。
4. 发票号由数据库部分唯一索引强制唯一（红冲作废号除外），重复号进不了可开结果。
5. 后到更正不改写已开发票：重算后按"最终应付 - 已开净额"开调整票（正补开/负红冲）。
"""
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from app.coerce import money
from app.errors import BusinessError

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoice_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  event_key TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL,
  ticket TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  ticket TEXT NOT NULL,
  kind TEXT NOT NULL,
  version INTEGER NOT NULL,
  invoice_no TEXT,
  amount TEXT NOT NULL,
  status TEXT NOT NULL,
  updated_seq INTEGER NOT NULL,
  UNIQUE(run_id, ticket, kind, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_invoice_no ON invoices(invoice_no)
  WHERE invoice_no IS NOT NULL AND status != 'void';
CREATE INDEX IF NOT EXISTS idx_invoices_run ON invoices(run_id, ticket);
"""


def connect(path):
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA foreign_keys=ON')
    con.executescript(SCHEMA)
    return con


def _now():
    return datetime.now(timezone.utc).isoformat()


def _event_key(run_id, ticket, kind, version, event_type):
    # 事件键含事件类型：同一动作的 issue/adjust 与 published 确认各自独立、均可落库
    return f'{run_id}|{ticket}|{kind}|v{version}|{event_type}'


def _append(con, run_id, event_type, ticket, payload, kind, version):
    """写事件；事件键冲突说明该事件已存在（重入），返回 None。"""
    try:
        cur = con.execute(
            'INSERT INTO invoice_events(run_id,event_key,event_type,ticket,payload,created_at)'
            ' VALUES(?,?,?,?,?,?)',
            (run_id, _event_key(run_id, ticket, kind, version, event_type), event_type, ticket,
             json.dumps(payload, ensure_ascii=False), _now()))
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def _snapshot_map(state):
    return {r['ticket']: r for r in state['invoiceable']}


def _guard_ticket(state, ticket):
    snap = _snapshot_map(state).get(ticket)
    if snap is not None:
        return snap
    blocked = next((b for b in state['invoice_blocked'] if b['ticket'] == ticket), None)
    if blocked:
        raise BusinessError('TICKET_BLOCKED', '该票未通过闸门，暂不可开票：'
                            + '；'.join(x['code'] for x in blocked['blockers']))
    raise BusinessError('TICKET_NOT_FOUND', '票号不在本批次生效结果中', 404)


def _check_invoice_no(con, invoice_no, ticket):
    dup = con.execute(
        "SELECT ticket FROM invoices WHERE invoice_no=? AND status!='void' AND ticket!=?",
        (invoice_no, ticket)).fetchone()
    if dup:
        raise BusinessError('DUP_INVOICE_NO', f'发票号 {invoice_no} 已被票 {dup["ticket"]} 占用')


def _publish(con, run_id, ticket, kind, version, invoice_no, amount):
    """同一事务内：插投影(publishing) → 发布确认事件 → 投影置 issued。

    任一步失败由 with connect 的事务整体回滚，旧版票据记录不受影响。
    """
    seq = _append(con, run_id, 'issue' if kind == 'original' else 'adjust', ticket,
                  {'invoice_no': invoice_no, 'version': version, 'amount': amount},
                  kind, version)
    if seq is None:
        return False
    cur = con.execute(
        "INSERT INTO invoices(run_id,ticket,kind,version,invoice_no,amount,status,updated_seq)"
        " VALUES(?,?,?,?,?,?,'publishing',?)",
        (run_id, ticket, kind, version, invoice_no, amount, seq))
    invoice_row_id = cur.lastrowid
    done = _append(con, run_id, 'published', ticket,
                   {'invoice_no': invoice_no, 'version': version}, kind, version)
    con.execute(
        'UPDATE invoices SET status=?, updated_seq=COALESCE(?,updated_seq) WHERE id=?',
        ('issued', done, invoice_row_id))
    return True


def issue(path, state, ticket, invoice_no):
    """对一票开具当前应付发票。幂等：同票同版本已开则返回原票，不重复开票。"""
    ticket = str(ticket).strip()
    invoice_no = str(invoice_no).strip()
    if not invoice_no:
        raise BusinessError('BAD_INVOICE_NO', '发票号不能为空')
    snap = _guard_ticket(state, ticket)
    run_id = state['manifest']['run_id']
    version = int(snap['version'])

    # 先在独立事务完成中断恢复，避免与本次开票的回滚相互影响
    with connect(path) as con:
        _resume(con, run_id)

    con = connect(path)
    try:
        con.execute('BEGIN IMMEDIATE')
        existing = con.execute(
            "SELECT * FROM invoices WHERE run_id=? AND ticket=? AND kind='original' AND version=?",
            (run_id, ticket, version)).fetchone()
        if existing and existing['status'] == 'issued':
            con.commit()
            return _ticket_view(con, run_id, ticket)
        _check_invoice_no(con, invoice_no, ticket)
        try:
            created = _publish(con, run_id, ticket, 'original', version, invoice_no, snap['amount'])
        except sqlite3.IntegrityError:
            con.rollback()  # 发票号唯一索引兜底：整体回滚，不残留半张票
            raise BusinessError('DUP_INVOICE_NO', f'发票号 {invoice_no} 重复，已拒绝开票')
        if not created:
            con.rollback()
        else:
            con.commit()
        return _ticket_view(con, run_id, ticket)
    finally:
        con.close()


def _resume(con, run_id):
    """发布中断恢复（在调用方事务内执行，可安全重入）。

    有 issue/adjust 事件但无 published 事件 => 发布未完成：删除该投影行并追加
    voided(publish_interrupted) 事件留痕；旧版已 issued 发票保持不动。
    """
    recovered = []
    stuck = con.execute("SELECT * FROM invoices WHERE run_id=? AND status='publishing'",
                        (run_id,)).fetchall()
    for row in stuck:
        prefix = f'{run_id}|{row["ticket"]}|{row["kind"]}|v{row["version"]}|'
        kinds = {e['event_type'] for e in con.execute(
            'SELECT event_type FROM invoice_events WHERE event_key LIKE ?', (prefix + '%',))}
        if 'published' not in kinds:
            con.execute('DELETE FROM invoices WHERE id=?', (row['id'],))
            _append(con, run_id, 'voided', row['ticket'],
                    {'reason': 'publish_interrupted', 'invoice_no': row['invoice_no'],
                     'version': row['version']}, row['kind'], row['version'])
            recovered.append({'ticket': row['ticket'], 'invoice_no': row['invoice_no']})
        else:
            con.execute('UPDATE invoices SET status=? WHERE id=?', ('issued', row['id']))
    return {'recovered': recovered}


def resume_path(path, state):
    run_id = state['manifest']['run_id']
    with connect(path) as con:
        return _resume(con, run_id)


def adjust(path, state, ticket, invoice_no):
    """后到更正闭合后开调整票：delta = 最终应付 - 已开净额。正补开、负红冲。"""
    ticket = str(ticket).strip()
    invoice_no = str(invoice_no).strip()
    if not invoice_no:
        raise BusinessError('BAD_INVOICE_NO', '发票号不能为空')
    snap = _guard_ticket(state, ticket)
    run_id = state['manifest']['run_id']
    version = int(snap['version'])

    with connect(path) as con:
        _resume(con, run_id)

    con = connect(path)
    try:
        con.execute('BEGIN IMMEDIATE')
        opened_rows = con.execute(
            "SELECT amount FROM invoices WHERE run_id=? AND ticket=? AND status='issued'",
            (run_id, ticket)).fetchall()
        opened = money(sum((Decimal(r['amount']) for r in opened_rows), Decimal(0)))
        delta = money(Decimal(snap['amount']) - Decimal(opened))
        if Decimal(delta) == 0:
            con.commit()
            return _ticket_view(con, run_id, ticket)
        if con.execute(
                "SELECT id FROM invoices WHERE run_id=? AND ticket=? AND kind='adjustment' AND version=?",
                (run_id, ticket, version)).fetchone():
            con.commit()
            return _ticket_view(con, run_id, ticket)
        _check_invoice_no(con, invoice_no, ticket)
        try:
            _publish(con, run_id, ticket, 'adjustment', version, invoice_no, delta)
        except sqlite3.IntegrityError:
            con.rollback()
            raise BusinessError('DUP_INVOICE_NO', f'发票号 {invoice_no} 重复，已拒绝开票')
        con.commit()
        return _ticket_view(con, run_id, ticket)
    finally:
        con.close()


def _ticket_view(con, run_id, ticket):
    rows = [dict(r) for r in con.execute(
        'SELECT ticket,kind,version,invoice_no,amount,status FROM invoices '
        "WHERE run_id=? AND ticket=? ORDER BY id", (run_id, ticket)).fetchall()]
    issued = [r for r in rows if r['status'] == 'issued']
    opened = money(sum((Decimal(r['amount']) for r in issued), Decimal(0)))
    return {'ticket': ticket, 'invoices': rows, 'opened_amount': opened}


def ledger(path, state):
    """页面台账：每票核对 已开金额 / 之后调整 / 最终应付 / 待处理票。"""
    run_id = state['manifest']['run_id']
    final_map = {r['ticket']: r for r in state['final']}
    with connect(path) as con:
        _resume(con, run_id)
        rows = con.execute(
            "SELECT ticket, kind, version, invoice_no, amount, status FROM invoices "
            "WHERE run_id=? ORDER BY ticket, id", (run_id,)).fetchall()
        events = [dict(r) for r in con.execute(
            'SELECT seq,event_type,ticket,payload,created_at FROM invoice_events '
            'WHERE run_id=? ORDER BY seq', (run_id,)).fetchall()]

    per_ticket = {}

    def blank(t):
        return {'ticket': t, 'original_amount': '0.00', 'adjustment_amount': '0.00',
                'opened_amount': '0.00', 'final_amount': final_map.get(t, {}).get('amount', '0.00'),
                'remaining': final_map.get(t, {}).get('amount', '0.00'),
                'invoice_nos': [], 'publishing': False}

    for r in rows:
        item = per_ticket.setdefault(r['ticket'], blank(r['ticket']))
        item['publishing'] = item['publishing'] or r['status'] == 'publishing'
        if r['kind'] == 'original':
            item['original_amount'] = r['amount']
        elif r['kind'] == 'adjustment' and r['status'] == 'issued':
            item['adjustment_amount'] = money(Decimal(item['adjustment_amount']) + Decimal(r['amount']))
        if r['invoice_no']:
            item['invoice_nos'].append(r['invoice_no'])
    for t, item in per_ticket.items():
        item['opened_amount'] = money(Decimal(item['original_amount']) + Decimal(item['adjustment_amount']))
        item['remaining'] = money(Decimal(item['final_amount']) - Decimal(item['opened_amount']))
    for snap in state['invoiceable']:
        per_ticket.setdefault(snap['ticket'], blank(snap['ticket']))
    return {'tickets': sorted(per_ticket.values(), key=lambda x: x['ticket']),
            'blocked': state['invoice_blocked'], 'events': events}
