from contextlib import asynccontextmanager
from importlib import import_module
from fastapi import FastAPI, Request, Body
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from app.config import ROOT, META, MODULES, DB_PATH
from app.errors import BusinessError
from app.engine import evaluate
from app import store
from app.archive import serialize

@asynccontextmanager
async def lifespan(app):
    store.save(DB_PATH,evaluate())
    yield
app=FastAPI(title=META['title'],lifespan=lifespan)
app.mount('/static',StaticFiles(directory=str(ROOT/'app/static')),name='static')
@app.exception_handler(BusinessError)
async def business_error(request:Request,error:BusinessError):
    return JSONResponse({'code':error.code,'detail':error.message},status_code=error.status)
@app.get('/')
def home():
    return RedirectResponse('/'+MODULES[0],status_code=303)
@app.get('/healthz')
def health():
    return {'status':'ok','project':META['short']}
@app.post('/api/runs')
def create(payload:dict=Body(default={})):
    return store.save(DB_PATH,evaluate(payload))
@app.post('/api/runs/{run_id}/rerun')
def rerun(run_id:str):
    previous=store.get(DB_PATH,run_id)
    if 'input_snapshot' not in previous:
        raise BusinessError('INPUT_SNAPSHOT_MISSING','旧批缺少源快照，请重新导入')
    return store.save(DB_PATH,evaluate({'dataset':previous['source']},snapshot=previous['input_snapshot']))
@app.get('/api/runs')
def runs():
    return store.history(DB_PATH)
@app.get('/api/runs/{run_id}')
def run(run_id:str):
    return store.get(DB_PATH,run_id)
@app.post('/api/runs/{run_id}/review')
def review(run_id:str,payload:dict=Body(...)):
    revision=payload.get('revision')
    if not isinstance(revision,int) or revision<1: raise BusinessError('BAD_REVISION','版本号须为正整数')
    return store.review(DB_PATH,run_id,revision)
@app.get('/api/export/{run_id}')
def export(run_id:str):
    result=store.get(DB_PATH,run_id)
    return Response(serialize(result),media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="{run_id}.zip"'})
@app.get('/api/contracts')
def contracts():
    return {'active':store.active_contract(DB_PATH),'registry':store.contracts(DB_PATH)}
@app.post('/api/contracts/activate')
def activate_contract(payload:dict=Body(...)):
    version=payload.get('version')
    if not isinstance(version,str) or not version.strip(): raise BusinessError('BAD_CONTRACT','合同版本号无效')
    return store.switch_contract(DB_PATH,version.strip())
@app.post('/api/runs/{run_id}/claim/finalize')
def finalize_claim(run_id:str):
    return {'run_id':run_id,'ledger':store.finalize_claim(DB_PATH,run_id)}
@app.post('/api/runs/{run_id}/claim/confirm')
def confirm_claim(run_id:str):
    return store.confirm_claim(DB_PATH,run_id)
@app.get('/api/claim-ledger')
def claim_ledger(month:str|None=None):
    return {'rows':store.claim_ledger(DB_PATH,month=month),'occupation':store.month_occupation(DB_PATH)}
for key in MODULES:
    app.include_router(import_module(f'app.modules.{key}.handler').router)
