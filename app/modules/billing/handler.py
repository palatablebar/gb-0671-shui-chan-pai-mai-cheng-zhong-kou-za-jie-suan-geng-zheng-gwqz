from fastapi import APIRouter, Request, Body
from app.engine import evaluate
from app.web import render
from app import store, invoice_store
from app.errors import BusinessError

router = APIRouter()


def _db():
    from app.main import DB_PATH  # 延迟引用，允许测试 monkeypatch
    return DB_PATH


@router.get('/billing')
def page(request: Request):
    return render(request, 'billing')


@router.post('/api/billing')
def calculate(payload: dict = Body(default={})):
    # billing 是第 7 个模块（archive 之前）
    return evaluate(payload, until=7)


def _load(run_id: str):
    state = store.get(_db(), run_id)
    if 'invoiceable' not in state:
        raise BusinessError('INCOMPLETE_RUN', '批次未运行到可开票阶段')
    return state


@router.get('/api/runs/{run_id}/invoices')
def invoice_ledger(run_id: str):
    state = _load(run_id)
    ledger = invoice_store.ledger(_db(), state)
    return {'summary': state['invoice_summary'], **ledger}


@router.post('/api/runs/{run_id}/invoices')
def issue_invoice(run_id: str, payload: dict = Body(...)):
    state = _load(run_id)
    ticket, invoice_no = payload.get('ticket'), payload.get('invoice_no')
    if not isinstance(ticket, str) or not isinstance(invoice_no, str):
        raise BusinessError('BAD_PAYLOAD', 'ticket 与 invoice_no 须为字符串')
    return invoice_store.issue(_db(), state, ticket, invoice_no)


@router.post('/api/runs/{run_id}/invoices/adjust')
def adjust_invoice(run_id: str, payload: dict = Body(...)):
    state = _load(run_id)
    ticket, invoice_no = payload.get('ticket'), payload.get('invoice_no')
    if not isinstance(ticket, str) or not isinstance(invoice_no, str):
        raise BusinessError('BAD_PAYLOAD', 'ticket 与 invoice_no 须为字符串')
    return invoice_store.adjust(_db(), state, ticket, invoice_no)


@router.post('/api/runs/{run_id}/invoices/resume')
def resume_invoices(run_id: str):
    state = _load(run_id)
    return invoice_store.resume_path(_db(), state)
