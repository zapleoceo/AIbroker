"""Public "ask many models" page: GET /ask2many + JSON under /ask2many/api/*. No login.

Everything dynamic is rendered client-side with textContent (never innerHTML), so model
answers and prompts typed by strangers cannot inject markup; the server sends only a static
shell (inline CSS/JS, no external assets). Logic lives in services/compare.py.
"""
from __future__ import annotations

import json
from html import escape

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from aibroker.auth import client_ip
from aibroker.providers import model_scores
from aibroker.routes.landing import FAVICON_LINKS
from aibroker.services import compare as svc

router = APIRouter(tags=["ask2many"])


class SubmitIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=svc.MAX_PROMPT_CHARS)
    models: list[str] = Field(min_length=1, max_length=svc.MAX_MODELS)


def _secure(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


@router.api_route("/ask2many", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def page(request: Request) -> HTMLResponse:
    html = (_HTML.replace("__FAVICON__", FAVICON_LINKS)
            .replace("__MAX_CHARS__", str(svc.MAX_PROMPT_CHARS))
            .replace("__MAX_MODELS__", str(svc.MAX_MODELS))
            .replace("__BUSY__", json.dumps(svc.BUSY_MESSAGE, ensure_ascii=False))
            .replace("__ATTRIBUTION__", escape(model_scores.ATTRIBUTION))
            .replace("__CAPTURED__", escape(model_scores.CAPTURED_ON)))
    resp = HTMLResponse(html)
    svc.set_browser_cookie(resp, svc.browser_id_from(request.cookies.get(svc.COOKIE_NAME)),
                           secure=_secure(request))
    return resp


@router.get("/ask2many/api/models")
async def models() -> dict[str, object]:
    keys = ("id", "name", "provider", "intelligence_index", "subscription")
    return {"data": [{k: m[k] for k in keys} for m in svc.chat_models()],
            "attribution": model_scores.ATTRIBUTION, "source": model_scores.SOURCE,
            "captured_on": model_scores.CAPTURED_ON,
            "limits": {"max_prompt_chars": svc.MAX_PROMPT_CHARS, "max_models": svc.MAX_MODELS}}


@router.post("/ask2many/api/requests", status_code=202)
async def submit(body: SubmitIn, request: Request, response: Response) -> dict[str, object]:
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(422, "пустой вопрос")
    catalog = {m["id"]: m for m in svc.chat_models()}
    wanted = list(dict.fromkeys(body.models))
    if any(m.startswith(tuple(f"{p}/" for p in svc.UNSAFE_SUBSCRIPTION_PROVIDERS)) for m in wanted):
        raise HTTPException(422, "эта модель недоступна на публичной странице")
    unknown = [m for m in wanted if m not in catalog]
    if unknown:
        raise HTTPException(422, f"неизвестные модели: {', '.join(unknown)[:200]}")
    project = await svc.get_project()
    if project is None:
        raise HTTPException(503, "проект сравнения не создан на сервере")
    bid = svc.browser_id_from(request.cookies.get(svc.COOKIE_NAME))
    svc.set_browser_cookie(response, bid, secure=_secure(request))
    request_id = await svc.create_request(prompt, bid, client_ip(request),
                                          [catalog[m] for m in wanted])
    svc.spawn_tracker(request_id, project, prompt)
    return {"id": request_id}


@router.get("/ask2many/api/requests/{request_id}")
async def poll(request_id: int) -> dict[str, object]:
    await svc.sync_request(request_id)
    out = await svc.get_request(request_id)
    if out is None:
        raise HTTPException(404, "запрос не найден")
    return out


@router.get("/ask2many/api/history")
async def history(q: str = Query("", max_length=200), page: int = Query(1, ge=1, le=10_000)
                  ) -> dict[str, object]:
    return await svc.search_history(q.strip(), page)


# Static shell. Placeholders (__X__) instead of str.format so CSS/JS braces stay readable.
_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ask2many — один вопрос, много моделей</title>
__FAVICON__
<style>
:root{color-scheme:light dark;--bg:#f7f7f8;--panel:#fff;--text:#1a1c20;--muted:#6b7280;--line:#e3e5e9;
 --accent:#4f46e5;--accent-fg:#fff;--accent-soft:rgba(79,70,229,.1);--good:#1a9b5b;--warn:#c58a14;--bad:#b4474b;--bad-soft:rgba(180,71,75,.08)}
@media (prefers-color-scheme:dark){:root{--bg:#121316;--panel:#1a1c21;--text:#e8e9ec;--muted:#8d93a0;--line:#2a2d34;
 --accent:#8b85ff;--accent-fg:#12131a;--accent-soft:rgba(139,133,255,.14);--good:#3ec47f;--warn:#e0aa3a;--bad:#e07a7e;--bad-soft:rgba(224,122,126,.1)}}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);
 font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:24px;line-height:1.25;margin:8px 0 4px;font-weight:650}
h2{font-size:17px;margin:40px 0 10px;font-weight:600}
.muted{color:var(--muted)} .small{font-size:13px}
textarea,input[type=search]{width:100%;border:1px solid var(--line);border-radius:12px;background:var(--panel);
 color:var(--text);font:inherit;padding:12px 14px;outline:none}
textarea{min-height:150px;resize:vertical;margin-top:18px}
textarea:focus,input[type=search]:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.bar{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin:8px 0}
.links button{background:none;border:0;color:var(--accent);font:inherit;font-size:13px;cursor:pointer;padding:2px 6px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 18px}
.chip{position:relative;cursor:pointer}
.chip input{position:absolute;opacity:0;inset:0;margin:0;cursor:pointer}
.chip-body{display:inline-flex;align-items:center;gap:6px;padding:6px 12px;border:1px solid var(--line);
 border-radius:999px;background:var(--panel);font-size:14px;transition:border-color .15s,background .15s}
.chip:hover .chip-body{border-color:var(--muted)}
.chip input:checked+.chip-body{border-color:var(--accent);background:var(--accent-soft)}
.chip input:focus-visible+.chip-body{outline:2px solid var(--accent);outline-offset:2px}
.chip small{color:var(--muted);font-size:12px}
.chip em{font-style:normal;font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:6px;padding:0 5px}
.go{width:100%;padding:13px 18px;border:0;border-radius:12px;background:var(--accent);color:var(--accent-fg);
 font:inherit;font-weight:600;cursor:pointer}
.go:disabled{opacity:.45;cursor:not-allowed}
.msg{display:none;margin:12px 0;padding:11px 14px;border-radius:10px;background:var(--bad-soft);color:var(--bad);font-size:14px}
.grid{display:grid;grid-template-columns:1fr;gap:12px;margin-top:16px}
@media (min-width:720px){.grid{grid-template-columns:1fr 1fr}}
.card{border:1px solid var(--line);border-radius:12px;background:var(--panel);padding:14px;min-width:0}
.card-h{display:flex;align-items:center;gap:8px;justify-content:space-between;margin-bottom:8px}
.card-h b{font-weight:600;overflow-wrap:anywhere}
.st{display:inline-flex;align-items:center;gap:6px;color:var(--muted);font-size:13px;white-space:nowrap}
.dot{width:8px;height:8px;border-radius:50%;background:var(--warn)}
.done .dot{background:var(--good)} .error .dot,.refused .dot{background:var(--bad)}
.pending .dot{animation:p 1.2s ease-in-out infinite}
@keyframes p{50%{opacity:.35}}
.ans{white-space:pre-wrap;overflow-wrap:anywhere;font-size:15px}
.error .ans,.refused .ans{color:var(--bad)}
.card-meta{margin-top:8px;font-size:12px;color:var(--muted);overflow-wrap:anywhere}
details.h{border:1px solid var(--line);border-radius:12px;background:var(--panel);margin:8px 0;padding:10px 14px}
details.h summary{cursor:pointer;overflow-wrap:anywhere;list-style-position:inside}
details.h .date{color:var(--muted);font-size:13px;margin-right:8px}
.more{margin:8px 0;background:none;border:1px solid var(--line);border-radius:10px;color:var(--accent);
 font:inherit;padding:8px 14px;cursor:pointer}
footer{margin-top:40px;font-size:13px;color:var(--muted)}
</style></head><body><div class="wrap">
<h1>Ask2many — один вопрос, много моделей</h1>
<p class="muted" id="public-note" style="margin:0">Вопросы и ответы видны всем в общей истории.</p>

<textarea id="prompt" maxlength="__MAX_CHARS__" placeholder="Задайте вопрос" aria-label="Ваш вопрос"></textarea>
<div class="bar small muted"><span><span id="cnt">0</span> / __MAX_CHARS__</span>
 <span>Моделей: <span id="sel">0</span> из __MAX_MODELS__ возможных</span></div>

<div class="bar"><span class="muted small">Модели, по убыванию балла</span>
 <span class="links"><button id="all" type="button">выбрать все</button><button id="clear" type="button">снять все</button></span></div>
<div class="chips" id="models"></div>

<button class="go" id="go" type="button" disabled>Спросить</button>
<div class="msg" id="msg" role="alert"></div>
<div class="grid" id="out"></div>

<h2>История</h2>
<input type="search" id="q" placeholder="Поиск по тексту вопроса" autocomplete="off" aria-label="Поиск по истории">
<div id="hist"></div>
<button class="more" id="more" type="button" hidden>Показать ещё</button>
<footer>__ATTRIBUTION__. Снимок оценок: __CAPTURED__ (Intelligence Index). «—» значит, что для точно такой
версии модели данных нет.</footer>
</div>
<script>
(function(){
var MAX=__MAX_MODELS__;
var BUSY=__BUSY__;
var $=function(i){return document.getElementById(i);};
var names={}, active=false, page=1, hq="", debounce=null;
function el(tag,cls,text){var e=document.createElement(tag); if(cls)e.className=cls; if(text!=null)e.textContent=text; return e;}
function score(v){return v==null?"\\u2014":String(v);}
function showMsg(t){var m=$("msg"); m.textContent=t||""; m.style.display=t?"block":"none";}
function boxes(){return Array.prototype.slice.call(document.querySelectorAll("#models input"));}
function checked(){return boxes().filter(function(i){return i.checked;}).map(function(i){return i.value;});}
function refresh(){var n=checked().length; $("sel").textContent=n; $("cnt").textContent=$("prompt").value.length;
 $("go").disabled=active||!n||n>MAX||!$("prompt").value.trim(); $("go").textContent=active?"Ждём ответы\\u2026":"Спросить";}
var LABEL={pending:"ждём",done:"готово",error:"ошибка",refused:"отказ"};
function card(r){var c=el("div","card "+r.status); var h=el("div","card-h");
 h.appendChild(el("b",null,names[r.model_id]||r.model_id));
 var st=el("span","st"); st.appendChild(el("span","dot"));
 st.appendChild(el("span",null,r.latency_ms!=null?(r.latency_ms/1000).toFixed(1)+" с":(LABEL[r.status]||r.status)));
 h.appendChild(st); c.appendChild(h);
 var body=r.status==="done"?r.answer:(r.status==="pending"?"":(r.error||"")); if(body)c.appendChild(el("div","ans",body));
 c.appendChild(el("div","card-meta","Балл: "+score(r.intelligence_index)+" \\u00b7 отвечала: "+r.model_served)); return c;}
function render(target,data){target.textContent=""; data.results.forEach(function(r){target.appendChild(card(r));});}
function poll(id){fetch("/ask2many/api/requests/"+id).then(function(r){return r.json();}).then(function(d){
 render($("out"),d); if(d.done){active=false; refresh(); loadHistory(true);}
 else setTimeout(function(){poll(id);},2000);}).catch(function(){setTimeout(function(){poll(id);},4000);});}
$("go").onclick=function(){showMsg(""); active=true; refresh();
 fetch("/ask2many/api/requests",{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify({prompt:$("prompt").value,models:checked()})}).then(function(r){return r.json().then(function(j){return [r.status,j];});})
 .then(function(x){if(x[0]===202){$("out").textContent=""; poll(x[1].id);}
  else{active=false; var d=x[1].detail; showMsg(x[0]===429?BUSY:(typeof d==="string"?d:"Не удалось отправить вопрос")); refresh();}})
 .catch(function(){active=false; showMsg("Нет связи с сервером"); refresh();});};
$("prompt").oninput=refresh;
$("all").onclick=function(){boxes().forEach(function(b,i){b.checked=i<MAX;}); refresh();};
$("clear").onclick=function(){boxes().forEach(function(b){b.checked=false;}); refresh();};
function loadHistory(reset){if(reset)page=1; fetch("/ask2many/api/history?page="+page+"&q="+encodeURIComponent(hq)).then(function(r){return r.json();}).then(function(d){
 var box=$("hist"); if(reset)box.textContent="";
 if(reset&&!d.items.length)box.appendChild(el("p","muted small","Ничего не найдено"));
 d.items.forEach(function(it){var det=el("details","h"); var s=el("summary");
  s.appendChild(el("span","date",it.created_at.replace("T"," ").slice(0,16))); s.appendChild(el("span",null,it.prompt.slice(0,140)));
  det.appendChild(s); det.appendChild(el("p","muted small",it.prompt)); var g=el("div","grid"); render(g,it); det.appendChild(g); box.appendChild(det);});
 $("more").hidden=!d.has_more;});}
$("more").onclick=function(){page++; loadHistory(false);};
$("q").oninput=function(){clearTimeout(debounce); debounce=setTimeout(function(){hq=$("q").value.trim(); loadHistory(true);},300);};
fetch("/ask2many/api/models").then(function(r){return r.json();}).then(function(d){
 var dup={}; d.data.forEach(function(m){dup[m.name]=(dup[m.name]||0)+1;});
 var box=$("models"); d.data.forEach(function(m){var label=m.name+(dup[m.name]>1?" ("+m.provider+")":""); names[m.id]=label;
  var l=el("label","chip"); var c=el("input"); c.type="checkbox"; c.value=m.id;
  c.onchange=function(){if(checked().length>MAX){c.checked=false;} refresh();};
  var b=el("span","chip-body"); b.appendChild(el("span",null,label)); b.appendChild(el("small",null,score(m.intelligence_index)));
  if(m.subscription)b.appendChild(el("em",null,"подписка"));
  l.title=m.id; l.appendChild(c); l.appendChild(b); box.appendChild(l);}); refresh(); loadHistory(true);});
refresh();
})();
</script></body></html>"""
