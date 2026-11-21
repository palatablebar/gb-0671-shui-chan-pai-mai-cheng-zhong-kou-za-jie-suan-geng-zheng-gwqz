from fastapi import Request
from fastapi.templating import Jinja2Templates
from app.config import ROOT, META
from app.engine import evaluate
from app import store, invoice_store

templates = Jinja2Templates(directory=str(ROOT / 'app/templates'))


def render(request: Request, key):
    from app.main import DB_PATH  # 延迟引用，允许测试 monkeypatch
    run_id = request.query_params.get('run_id')
    if run_id:
        state = store.get(DB_PATH, run_id)
    else:
        state = store.save(DB_PATH, evaluate({'dataset': request.query_params.get('dataset', 'dirty')}))
    context = {'request': request, 'meta': META, 'key': key, 'state': state,
               'view': state['views'][key]}
    if key == 'billing':
        # 变量名避开 base.html 中的 ledger 宏，否则会被宏名遮蔽
        context['invoice_ledger'] = invoice_store.ledger(DB_PATH, state)
        context['version_map'] = {r['ticket']: r['version'] for r in state['final']}
    return templates.TemplateResponse(f'{key}.html', context)
