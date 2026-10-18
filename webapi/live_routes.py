"""Authenticated read-only routes mounted by the existing API monitor mode."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder

from webapi.live_monitor import reader
from webapi.schemas import LoginRequest, LoginResponse
from webapi.security import authenticate, create_access_token, get_current_user

router = APIRouter()


@router.post('/auth/login', response_model=LoginResponse)
def login(payload: LoginRequest):
    user = authenticate(payload.username, payload.password)
    if user is None:
        raise HTTPException(401, 'Credenciais inválidas')
    return LoginResponse(access_token=create_access_token(user['username'], user['role']), role=user['role'])


@router.get('/dashboard')
@router.get('/live/overview')
def dashboard(_user=Depends(get_current_user)):
    return jsonable_encoder(reader.snapshot())


@router.get('/live/history')
def history(strategy: str | None = Query(default=None, max_length=100),
            symbol: str | None = Query(default=None, max_length=20),
            timeframe: str | None = Query(default=None, max_length=10),
            since: datetime | None = None, until: datetime | None = None,
            _user=Depends(get_current_user)):
    def utc_naive(value):
        if value is None: return None
        if value.tzinfo is None: raise HTTPException(422, 'Informe período com fuso horário explícito')
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    start, end = utc_naive(since), utc_naive(until)
    if start and end and start > end:
        raise HTTPException(422, 'Período inválido')
    try:
        data = reader._read_db(strategy or None, symbol or None, timeframe or None, start, end)
        return jsonable_encoder({'trades': data['history'], 'orders': data['orders'], 'at': data['at'], 'limit': 200})
    except Exception:
        raise HTTPException(503, 'Histórico MySQL indisponível; detalhes omitidos') from None


@router.get('/live/brl-quote')
def brl_quote(_user=Depends(get_current_user)):
    from webapi.brl_quote import get_brl_quote
    return get_brl_quote()


from pydantic import BaseModel, StrictBool
from typing import Literal
from fastapi import Request
from webapi.platform_control import controller, PlatformStartError
from webapi.security import require_roles


class PlatformStartRequest(BaseModel):
    component: Literal['worker', 'daemon']
    confirm_real_orders: StrictBool = False


@router.get('/platform/status')
def platform_status(_user=Depends(get_current_user)):
    return controller.status()


@router.post('/platform/start')
def platform_start(payload: PlatformStartRequest, request: Request,
                   _user=Depends(require_roles('operator','administrator'))):
    origin=request.headers.get('origin')
    if origin and origin not in {'http://127.0.0.1:5173','http://localhost:5173'}:
        raise HTTPException(403,'Origem não permitida.')
    try:
        return controller.start(payload.component,payload.confirm_real_orders)
    except PlatformStartError as exc:
        raise HTTPException(409,str(exc)) from None
    except Exception:
        raise HTTPException(503,'Não foi possível iniciar o serviço. Nenhum detalhe sensível foi exibido.') from None
