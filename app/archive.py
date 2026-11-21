import io, csv, json, zipfile, hashlib
from app.errors import BusinessError

def serialize(result, invoice_ledger=None):
    if not result.get('manifest') or not result.get('views'):
        raise BusinessError('INCOMPLETE_RUN','批次尚未完成')
    evidence={'counts':result['counts'],'views':result['views'],'quarantine':result['quarantine']}
    if invoice_ledger is not None:
        # 开票台账与只追加事件日志随封存包永久留存；票据事件不删除、不改写
        evidence['invoice_ledger']=invoice_ledger
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json',json.dumps({**result['manifest'],'revision':result.get('revision',1),'status':result.get('status','待复核')},ensure_ascii=False,indent=2))
        archive.writestr('evidence.json',json.dumps(evidence,ensure_ascii=False,indent=2))
    return buffer.getvalue()
