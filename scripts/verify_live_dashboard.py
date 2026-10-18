"""Local-only visual smoke check using an isolated headless Chrome, no personal profile."""
import base64
import json
import subprocess
import tempfile
import time
from pathlib import Path

import requests
from dotenv import dotenv_values
from websockets.sync.client import connect

ROOT=Path(__file__).resolve().parents[1]
CHROME=Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
PORT=9234
out=ROOT/'optimization/results'
out.mkdir(parents=True,exist_ok=True)
config=dotenv_values(ROOT/'.env',interpolate=False)
with tempfile.TemporaryDirectory(prefix='crypto-dashboard-browser-') as profile:
    process=subprocess.Popen([str(CHROME),'--headless=new','--disable-gpu','--no-first-run',
                              '--no-default-browser-check','--disable-extensions',
                              '--remote-debugging-address=127.0.0.1',f'--remote-debugging-port={PORT}',
                              f'--user-data-dir={profile}','about:blank'],
                             stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    browser_ws=None
    try:
        for _ in range(80):
            try:
                meta=requests.get(f'http://127.0.0.1:{PORT}/json/version',timeout=1).json()
                browser_ws=meta['webSocketDebuggerUrl']
                break
            except Exception:time.sleep(.25)
        if browser_ws is None:raise RuntimeError('Isolated browser did not start')
        target=requests.put(f'http://127.0.0.1:{PORT}/json/new?http://127.0.0.1:5173/',timeout=5).json()
        with connect(target['webSocketDebuggerUrl'],max_size=20_000_000) as ws:
            seq=0
            errors=[]
            def call(method,params=None):
                global seq
                seq+=1
                ws.send(json.dumps({'id':seq,'method':method,'params':params or {}}))
                while True:
                    response=json.loads(ws.recv(timeout=35))
                    if response.get('method')=='Runtime.exceptionThrown':errors.append('Browser runtime exception')
                    if response.get('id')==seq:
                        if 'error' in response:raise RuntimeError('Browser command failed')
                        return response.get('result',{})
            def js(expression):
                result=call('Runtime.evaluate',{'expression':expression,'returnByValue':True,'awaitPromise':True})
                if result.get('exceptionDetails'):raise RuntimeError('Browser evaluation failed')
                return result.get('result',{}).get('value')
            def wait_text(value):
                for _ in range(90):
                    if js('document.body.innerText.includes('+json.dumps(value)+')'):return
                    time.sleep(.3)
                raise RuntimeError('Expected interface text did not appear: '+value)
            call('Page.enable');call('Runtime.enable')
            call('Emulation.setDeviceMetricsOverride',{'width':1440,'height':1000,'deviceScaleFactor':1,'mobile':False})
            wait_text('Acesso ao Dashboard')
            time.sleep(.6)
            # Submit the existing UI; never log the expression, password, token, storage or headers.
            username='admin'
            password=config.get('API_PASSWORD') or 'admin'
            js("(()=>{const inputs=[...document.querySelectorAll('input')];const set=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;set.call(inputs[0],"+json.dumps(username)+");inputs[0].dispatchEvent(new Event('input',{bubbles:true}));set.call(inputs[1],"+json.dumps(password)+");inputs[1].dispatchEvent(new Event('input',{bubbles:true}));})()")
            time.sleep(.4)
            js("[...document.querySelectorAll('button')].find(b=>b.textContent.includes('Entrar')).click()")
            wait_text('Resíduos preservados')
            wait_text('3081')
            wait_text('Retomar a plataforma')
            wait_text('LIVE ativo')
            wait_text('Candles ativos')
            assert js("document.querySelectorAll('.MuiChip-colorSuccess').length >= 2")
            assert js("![...document.querySelectorAll('button')].some(b=>b.textContent.includes('Iniciar worker LIVE')||b.textContent.includes('Iniciar download de candles'))")
            assert js("!document.body.innerText.includes('Seu perfil')")
            print('UI_ADMIN_OK: sole admin account authenticated; active services shown without start buttons')
            wait_text('Saldo da carteira USDT em R$')
            wait_text('Preju\u00edzo realizado em R$')
            for _ in range(40):
                if js("/R\\$\\s*\\d/.test(document.body.innerText)"): break
                time.sleep(.3)
            else: raise RuntimeError('BRL quote not displayed')
            body=js('document.body.innerText')
            assert 'PREJUÍZO' in body and '28,25577283' in body
            assert '\u00c3\u0161' not in body, 'Text encoding error'
            assert 'Ordens BNB/USDT abertas na Binance: 0' in body
            assert 'encontradas 2' not in body and 'desatualizada' not in body
            time.sleep(.6)
            image=call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True})
            (out/'live_dashboard_desktop.png').write_bytes(base64.b64decode(image['data']))
            first=js('document.body.innerText')
            time.sleep(18)
            second=js('document.body.innerText')
            assert first!=second,'Automatic refresh did not change timestamp'
            print('UI_DESKTOP_OK: authenticated LIVE values, DUST 3081 and loss visible; automatic refresh observed')
            call('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
            time.sleep(.7)
            assert js('document.documentElement.scrollWidth <= window.innerWidth'),'Mobile horizontal overflow'
            time.sleep(.6)
            image=call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True})
            (out/'live_dashboard_mobile.png').write_bytes(base64.b64decode(image['data']))
            print('UI_MOBILE_OK: 390px, no page horizontal overflow')
            call('Page.navigate',{'url':'http://127.0.0.1:5173/trades'})
            wait_text('Histórico de operações')
            wait_text('12763180609')
            wait_text('Comiss\u00e3o Binance')
            wait_text('0,00437953 USDT')
            wait_text('N\u00e3o registrada')
            print('UI_TRANSACTION_COSTS_OK: executed value, recorded fee, conversion rate and unavailable conversion charge visible')
            assert js("document.body.innerText.includes('PREJUÍZO')")
            call('Emulation.setDeviceMetricsOverride',{'width':1440,'height':1000,'deviceScaleFactor':1,'mobile':False})
            time.sleep(.5)
            time.sleep(.6)
            image=call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':True})
            (out/'live_dashboard_history.png').write_bytes(base64.b64decode(image['data']))
            print('UI_HISTORY_OK: real fills and trade 3081 visible')
            print('BROWSER_RUNTIME_ERRORS',len(errors))
    finally:
        if browser_ws:
            try:
                with connect(browser_ws) as ws:ws.send(json.dumps({'id':999,'method':'Browser.close'}))
            except Exception:pass
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=10)
