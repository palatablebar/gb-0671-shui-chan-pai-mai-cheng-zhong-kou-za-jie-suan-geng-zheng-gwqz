"""开票 API 与页面端到端测试。"""
import io
import json
import zipfile


def _client(tmp_path, monkeypatch):
    from app import main
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'api.sqlite'))
    from fastapi.testclient import TestClient
    return TestClient(main.app)


def test_billing_page_and_full_run(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={}).json()
    rid = run['manifest']['run_id']
    page = c.get(f'/billing?run_id={rid}')
    assert page.status_code == 200
    text = page.text
    # 断言表格主体确实渲染（曾因 base.html 的 ledger 宏名遮蔽 context 变量而静默为空）
    assert '20 票' in text
    assert 'data-ticket="00001"' in text
    assert '可开票汇总' in text and '待处理票' in text
    # 8 个模块导航都在
    assert c.get('/archive?run_id=' + rid).status_code == 200


def test_issue_api_idempotent_and_ledger(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={}).json()['manifest']['run_id']
    ticket = '00002'
    r1 = c.post(f'/api/runs/{rid}/invoices', json={'ticket': ticket, 'invoice_no': 'INV-A1'})
    assert r1.status_code == 200
    r2 = c.post(f'/api/runs/{rid}/invoices', json={'ticket': ticket, 'invoice_no': 'INV-A1'})
    assert r2.status_code == 200 and r2.json() == r1.json()
    ledger = c.get(f'/api/runs/{rid}/invoices').json()
    row = next(t for t in ledger['tickets'] if t['ticket'] == ticket)
    assert row['invoice_nos'] == ['INV-A1']
    assert row['remaining'] == '0.00'
    # 开票后页面台账行能看到发票号（端到端渲染）
    page = c.get(f'/billing?run_id={rid}').text
    assert 'INV-A1' in page and 'data-ticket="00002"' in page


def test_duplicate_invoice_no_http_422(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={}).json()['manifest']['run_id']
    c.post(f'/api/runs/{rid}/invoices', json={'ticket': '00002', 'invoice_no': 'DUP'})
    r = c.post(f'/api/runs/{rid}/invoices', json={'ticket': '00003', 'invoice_no': 'DUP'})
    assert r.status_code == 422
    assert r.json()['code'] == 'DUP_INVOICE_NO'


def test_adjust_and_resume_api(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={}).json()['manifest']['run_id']
    c.post(f'/api/runs/{rid}/invoices', json={'ticket': '00002', 'invoice_no': 'INV-B1'})
    r = c.post(f'/api/runs/{rid}/invoices/adjust', json={'ticket': '00002', 'invoice_no': 'INV-B2'})
    # 无差额时返回当前视图（200），不产生新票
    assert r.status_code == 200
    resume = c.post(f'/api/runs/{rid}/invoices/resume', json={})
    assert resume.status_code == 200
    assert resume.json() == {'recovered': []}


def test_export_contains_invoice_ledger(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={}).json()['manifest']['run_id']
    c.post(f'/api/runs/{rid}/invoices', json={'ticket': '00002', 'invoice_no': 'INV-E1'})
    z = zipfile.ZipFile(io.BytesIO(c.get(f'/api/export/{rid}').content))
    evidence = json.loads(z.read('evidence.json'))
    assert 'invoice_ledger' in evidence
    tickets = {t['ticket']: t for t in evidence['invoice_ledger']['tickets']}
    assert tickets['00002']['invoice_nos'] == ['INV-E1']
    # 原始凭证仍在
    assert len(evidence['quarantine']) == 8
