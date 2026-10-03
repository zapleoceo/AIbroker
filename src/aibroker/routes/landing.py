"""Public landing page — bilingual EN/RU, default EN.

Single self-contained HTML with all sections. Language toggle is client-side:
on first paint shows EN; persisted choice in localStorage; ?lang=ru forces RU
on first paint (useful for sharing); ?lang=en forces EN.
"""
from __future__ import annotations

from html import escape

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.routing import APIRoute

from aibroker import __version__
from aibroker.providers.registry import paid_providers, provider_names
from aibroker.routes import proxy
from aibroker.routes.dashboard_assets import ASSETS_VERSION
from aibroker.routing.chains import (
    CAPABILITY_CHAINS,
    CAPABILITY_SCOPE,
    usable_scopes_for_provider,
)

# SVG favicon — hub-and-spokes: central node = broker, 4 satellites = providers
# being routed. 32×32 viewBox; scales cleanly to 16×16 in tab strips.
# Embedded as a module-level string so tests + multiple routes can reuse it.
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="6" fill="#0b0d11"/>'
    '<line x1="16" y1="10" x2="16" y2="6"  stroke="#4dabf7" stroke-width="1.5" opacity=".6"/>'
    '<line x1="22" y1="16" x2="26" y2="16" stroke="#4dabf7" stroke-width="1.5" opacity=".6"/>'
    '<line x1="16" y1="22" x2="16" y2="26" stroke="#4dabf7" stroke-width="1.5" opacity=".6"/>'
    '<line x1="10" y1="16" x2="6"  y2="16" stroke="#4dabf7" stroke-width="1.5" opacity=".6"/>'
    '<circle cx="16" cy="5"  r="2.2" fill="#4dabf7" opacity=".75"/>'
    '<circle cx="27" cy="16" r="2.2" fill="#4dabf7" opacity=".75"/>'
    '<circle cx="16" cy="27" r="2.2" fill="#4dabf7" opacity=".75"/>'
    '<circle cx="5"  cy="16" r="2.2" fill="#4dabf7" opacity=".75"/>'
    '<circle cx="16" cy="16" r="6"   fill="#4dabf7"/>'
    '</svg>'
)

# Common <head> include — every page imports this so the favicon binding
# stays in lock-step with the SVG above (single source of truth).
FAVICON_LINKS = (
    '<link rel="icon" type="image/svg+xml" href="/favicon.svg">'
    '<link rel="alternate icon" href="/favicon.ico">'
    '<link rel="apple-touch-icon" href="/favicon.svg">'
)


# Shared design tokens + the public-pages stylesheet (same tokens as the admin UI).
CSS_LINKS = (
    f'<link rel="stylesheet" href="/dashboard/static/css/tokens.css?v={ASSETS_VERSION}">'
    f'<link rel="stylesheet" href="/dashboard/static/css/public.css?v={ASSETS_VERSION}">'
)

# Display names for providers whose key differs from how they are written.
_PROVIDER_LABEL = {"nvidia": "nvidia nim"}


def wired_providers() -> list[str]:
    """Providers the broker can actually route to: a model table AND a seat in
    at least one capability chain. Derived from the routing tables (the single
    source of truth) so the landing page cannot drift from them — mistral, which
    has a model table but sits in no chain since 2026-09-12, is correctly absent.
    Deliberately NOT derived from which providers hold active keys: the public
    page must not reveal per-provider key state (see the /v1/health aggregate)."""
    wired = [p for p in provider_names() if usable_scopes_for_provider(p)]
    paid_set = paid_providers()
    free = [p for p in wired if p not in paid_set and p != "local"]
    paid = [p for p in wired if p in paid_set]
    return sorted(free) + sorted(paid) + (["local"] if "local" in wired else [])


def _providers_html() -> str:
    rows = []
    for p in wired_providers():
        if p == "local":
            badge = '<span class="badge" data-i18n data-en="self-hosted" data-ru="свой сервер">self-hosted</span>'
        elif p in paid_providers():
            badge = '<span class="badge paid">paid</span>'
        else:
            badge = '<span class="badge">free</span>'
        rows.append(f'      <div class="prov">{escape(_PROVIDER_LABEL.get(p, p))} {badge}</div>')
    return "\n".join(rows)


# One-line notes for the client endpoints, keyed by path; a route with no note
# still lists (the page is generated from the router, so it cannot omit one).
_ENDPOINT_NOTES = {
    "/jobs": "chat · async", "/jobs/{job_id}": "poll", "/embed": "sync",
    "/transcribe": "sync", "/transcribe/jobs": "async", "/decisions": "sync",
    "/deep": "chat:deep · async", "/deep/{job_id}": "poll", "/chat": "410 Gone",
}
_VERB_ORDER = {"POST": 0, "GET": 1}


def client_endpoints() -> list[tuple[str, str, str]]:
    """(METHOD, '/v1/...', note) for every route of the client API, in the
    order they are declared — read from the real router."""
    out = []
    for r in proxy.router.routes:
        if not isinstance(r, APIRoute):
            continue
        for m in sorted(r.methods or (), key=lambda x: _VERB_ORDER.get(x, 9)):
            if m == "HEAD":
                continue
            out.append((m, "/v1" + r.path, _ENDPOINT_NOTES.get(r.path, "")))
    return out


def _client_endpoints_html() -> str:
    return "\n".join(
        f'          <div class="ep"><span class="verb {m.lower()}">{m}</span>'
        f'<span class="path">{escape(path)}</span><span class="note">{escape(note)}</span></div>'
        for m, path, note in client_endpoints()
    )


def _capability_list() -> str:
    return ", ".join(f"`{c}`" for c in CAPABILITY_CHAINS)


def _scope_list() -> str:
    return ", ".join(f"`{s}`" for s in dict.fromkeys(CAPABILITY_SCOPE.values()))


router = APIRouter(tags=["landing"])


_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{favicon}
<title>AIbroker — one API key, every LLM provider · free-first routing, cost guard, self-hosted</title>
<meta name="description" content="Self-hosted LLM key broker. One API across {n_providers} providers (Cerebras, Groq, Gemini, DeepSeek, Anthropic, OpenAI, Voyage and more, plus a self-hosted local one). Free-tier first with paid fallback, per-key cost caps, automatic health probing, encrypted token storage. Self-host on any VPS.">
<meta name="keywords" content="LLM router, LLM proxy, AI gateway, OpenAI alternative, key rotation, free LLM tier, multi-provider LLM, LiteLLM, AI cost management, self-hosted LLM broker, Cerebras Groq Gemini Cohere OpenRouter DeepSeek Anthropic Voyage">
<meta name="author" content="zapleoceo">
<meta name="robots" content="index, follow">
<link rel="canonical" href="https://aib.zapleo.com/">
<link rel="alternate" hreflang="en" href="https://aib.zapleo.com/?lang=en">
<link rel="alternate" hreflang="ru" href="https://aib.zapleo.com/?lang=ru">
<link rel="alternate" hreflang="x-default" href="https://aib.zapleo.com/">

<!-- Open Graph (Facebook, LinkedIn, iMessage, Telegram link previews) -->
<meta property="og:type" content="website">
<meta property="og:url" content="https://aib.zapleo.com/">
<meta property="og:title" content="AIbroker — one API key, every LLM provider">
<meta property="og:description" content="Self-hosted LLM key broker. Free-first routing across {n_providers} providers, cost caps, health monitoring. Self-host on any VPS.">
<meta property="og:site_name" content="AIbroker">
<meta property="og:locale" content="en_US">
<meta property="og:locale:alternate" content="ru_RU">

<!-- Twitter / X card -->
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="AIbroker — one API key, every LLM provider">
<meta name="twitter:description" content="Self-hosted LLM key broker · free-first across {n_providers} providers · cost guard · encrypted keys.">

<!-- Schema.org structured data — picked up by Google rich-results AND by
     LLM crawlers (Perplexity, ChatGPT browse, Claude search). Two graphs:
     SoftwareApplication for the broker itself, FAQPage for the FAQ section. -->
<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@graph": [
    {{
      "@type": "SoftwareApplication",
      "name": "AIbroker",
      "url": "https://aib.zapleo.com/",
      "applicationCategory": "DeveloperApplication",
      "operatingSystem": "Linux / Docker",
      "description": "Self-hosted LLM key broker. Routes calls across Cerebras, Groq, Gemini, Cohere, OpenRouter, DeepSeek, Anthropic, OpenAI, Voyage, SambaNova, NVIDIA NIM, Cloudflare Workers AI and Z.ai, plus a self-hosted local provider (vision, transcription fallback) — free tiers first, paid fallback. Per-key, per-project and global cost caps. Automatic health probing and adaptive cooldowns. Encrypted token storage. Self-hosted on any VPS.",
      "softwareVersion": "{version}",
      "license": "Proprietary — all rights reserved. Source visible on GitHub, not licensed for reuse.",
      "offers": {{"@type": "Offer", "price": "0", "priceCurrency": "USD"}},
      "codeRepository": "https://github.com/zapleoceo/AIbroker",
      "programmingLanguage": "Python",
      "featureList": [
        "Free-tier-first routing across {n_providers} LLM providers",
        "Adaptive per-provider cooldowns with exponential backoff and jitter",
        "Per-key, per-project and global daily cost caps",
        "Atomic SELECT FOR UPDATE SKIP LOCKED key selection",
        "Background health monitor with Telegram alerts",
        "Fernet-encrypted provider tokens at rest",
        "JSON-reliable routing and native json_schema structured output",
        "Prompt caching with per-call cache-token metering",
        "Per-project and per-workflow cost attribution",
        "Telegram-login dashboard with per-project usage drill-down"
      ]
    }},
    {{
      "@type": "FAQPage",
      "mainEntity": [
        {{
          "@type": "Question",
          "name": "Does AIbroker store my prompts?",
          "acceptedAnswer": {{
            "@type": "Answer",
            "text": "Request bodies are not logged: only metadata is (provider, model, capability, tokens, cost, latency, status). Async jobs (POST /v1/jobs, /v1/deep, /v1/transcribe/jobs) do keep their request and result in the job queue so you can poll them; those payloads are purged after 7 days."
          }}
        }},
        {{
          "@type": "Question",
          "name": "What happens if all keys for a provider fail?",
          "acceptedAnswer": {{
            "@type": "Answer",
            "text": "The capability chain falls back to the next provider, rotating keys until one answers. Chat runs as an async job (POST /v1/jobs, poll GET /v1/jobs/{{id}}), so a slow provider never times the caller out."
          }}
        }},
        {{
          "@type": "Question",
          "name": "How is the dashboard secured?",
          "acceptedAnswer": {{
            "@type": "Answer",
            "text": "The Telegram Login Widget signs in only the owner's Telegram account. The session cookie is HMAC-signed. An X-Admin-Key header is the fallback for ops scripts."
          }}
        }},
        {{
          "@type": "Question",
          "name": "Can I rotate provider tokens?",
          "acceptedAnswer": {{
            "@type": "Answer",
            "text": "Yes. Add a new key via dashboard or POST /admin/keys, then disable the old one (POST /admin/keys/{{id}}/disable) or delete it (DELETE /admin/keys/{{id}}). New requests pick up the new key at once."
          }}
        }}
      ]
    }}
  ]
}}
</script>

{css}
</head>
<body>
<noscript>
  <div style="background:var(--panel);border-bottom:1px solid var(--line);padding:16px 24px;color:var(--text);font-size:14px;line-height:1.6">
    <strong>AIbroker</strong> is a self-hosted LLM key broker: one project key, free-tier-first routing
    across {n_providers} providers, cost caps, async chat jobs (POST /v1/jobs, poll GET /v1/jobs/{{id}}), and sync
    embeddings / transcription / decisions. This page's text is filled in by JavaScript, so enable it
    for the full page. Meanwhile:
    <a href="/docs">/docs</a> &middot; <a href="/openapi.json">/openapi.json</a> &middot;
    <a href="/llms.txt">/llms.txt</a> &middot; <a href="/v1/health">/v1/health</a> &middot;
    <a href="https://github.com/zapleoceo/AIbroker">GitHub</a>
  </div>
</noscript>

<header>
  <div class="container nav">
    <div class="brand"><span class="dot"></span> AIbroker</div>
    <nav class="nav-links">
      <a href="#how" data-i18n="nav.how" data-en="How it works" data-ru="Как работает"></a>
      <a href="#features" data-i18n="nav.features" data-en="Features" data-ru="Возможности"></a>
      <a href="#providers" data-i18n="nav.providers" data-en="Providers" data-ru="Провайдеры"></a>
      <a href="#api" data-i18n="nav.api" data-en="API" data-ru="API"></a>
      <a href="#pricing" data-i18n="nav.pricing" data-en="Pricing" data-ru="Цены"></a>
    </nav>
    <div class="nav-right">
      <a href="https://github.com/zapleoceo/AIbroker" target="_blank" rel="noopener"
         class="gh-link" title="Source on GitHub" aria-label="GitHub repository">
        <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-hidden="true">
          <path d="M12 .5C5.65.5.5 5.65.5 12c0 5.08 3.29 9.39 7.86 10.91.58.11.79-.25.79-.55v-1.93c-3.2.7-3.87-1.54-3.87-1.54-.52-1.33-1.28-1.69-1.28-1.69-1.05-.72.08-.7.08-.7 1.16.08 1.77 1.19 1.77 1.19 1.03 1.77 2.7 1.26 3.36.96.1-.75.4-1.26.73-1.55-2.55-.29-5.24-1.28-5.24-5.69 0-1.26.45-2.29 1.18-3.1-.12-.29-.51-1.46.11-3.04 0 0 .96-.31 3.15 1.18a10.95 10.95 0 0 1 5.74 0c2.19-1.49 3.15-1.18 3.15-1.18.63 1.58.23 2.75.11 3.04.73.81 1.18 1.84 1.18 3.1 0 4.42-2.7 5.39-5.27 5.68.41.36.78 1.06.78 2.14v3.17c0 .31.21.66.79.55C20.21 21.39 23.5 17.08 23.5 12 23.5 5.65 18.35.5 12 .5z"/>
        </svg>
      </a>
      <div class="lang-toggle">
        <button data-lang="en" class="active">EN</button>
        <button data-lang="ru">RU</button>
      </div>
      <a href="/dashboard" class="btn" data-i18n="nav.dashboard" data-en="Dashboard" data-ru="Панель"></a>
    </div>
  </div>
</header>

<!-- HERO -->
<section class="hero">
  <div class="container">
    <div class="eyebrow" data-i18n="hero.eyebrow" data-en="LLM key broker · self-hosted" data-ru="Брокер ключей для LLM · self-hosted"></div>
    <h1 data-i18n="hero.title"
        data-en="One key. Every LLM provider. Free first."
        data-ru="Один ключ. Все LLM-провайдеры. Бесплатные — первыми."></h1>
    <p class="lead" data-i18n="hero.lead"
       data-en="Centralized broker that routes your LLM calls through free tiers before falling back to paid ones, monitors key health, enforces cost caps, and never lets your projects see provider credentials."
       data-ru="Централизованный брокер: маршрутизирует LLM-вызовы через бесплатные тарифы, контролирует здоровье ключей, ставит лимиты на расходы и не показывает вашим проектам секреты провайдеров."></p>
    <div class="hero-cta">
      <a href="#api" class="btn btn-primary" data-i18n="hero.cta1" data-en="Get started" data-ru="Начать"></a>
      <a href="/docs" class="btn" data-i18n="hero.cta2" data-en="API reference" data-ru="Документация API"></a>
      <a href="https://github.com/zapleoceo/AIbroker" target="_blank" rel="noopener"
         class="btn" data-i18n="hero.cta3"
         data-en="★ Star on GitHub" data-ru="★ Star на GitHub"></a>
    </div>
    <div class="hero-stats">
      <div class="stat"><div class="num">{n_providers}</div><div class="lbl" data-i18n="hero.s1" data-en="Providers" data-ru="Провайдеров"></div></div>
      <div class="stat"><div class="num">7</div><div class="lbl" data-i18n="hero.s2" data-en="Scopes" data-ru="Scope-ов"></div></div>
      <div class="stat"><div class="num">~$0</div><div class="lbl" data-i18n="hero.s3" data-en="Avg cost / call" data-ru="Средн. стоим. вызова"></div></div>
      <div class="stat"><div class="num" style="font-size:22px;line-height:42px">Visible</div><div class="lbl" data-i18n="hero.s4" data-en="Source-available" data-ru="Source-available (код виден)"></div></div>
    </div>
  </div>
</section>

<!-- PROBLEM -->
<section id="problem">
  <div class="container">
    <div class="eyebrow" data-i18n="prob.eyebrow" data-en="Why" data-ru="Зачем"></div>
    <h2 data-i18n="prob.title"
        data-en="Multi-provider LLM apps leak secrets and burn money"
        data-ru="Мульти-провайдерные LLM-приложения утекают ключами и сжигают бюджет"></h2>
    <p class="section-intro" data-i18n="prob.intro"
       data-en="Every new project copies your Anthropic/OpenAI/Gemini keys into its own .env. Free-tier rate limits hit silently. Paid keys burn while a free one sits idle. There is no single place to revoke, no single place to see what is spending."
       data-ru="Каждый новый проект копирует ваши ключи Anthropic/OpenAI/Gemini в свой .env. Бесплатные тарифы упираются в лимиты молча. Платный ключ жжёт деньги, пока бесплатный простаивает. Нет единого места, чтобы отозвать ключ или посмотреть, кто тратит."></p>
    <div class="problem-grid">
      <div class="problem-card before">
        <div class="label" data-i18n="prob.before" data-en="Without broker" data-ru="Без брокера"></div>
        <ul>
          <li data-i18n="prob.b1" data-en="Each project holds its own provider keys" data-ru="Каждый проект хранит свои ключи провайдеров"></li>
          <li data-i18n="prob.b2" data-en="Rate limit on free tier → silent failure" data-ru="Лимит free-тарифа → молчаливый сбой"></li>
          <li data-i18n="prob.b3" data-en="Paid keys spend before free ones are tried" data-ru="Платные ключи тратятся раньше бесплатных"></li>
          <li data-i18n="prob.b4" data-en="No central audit, no cost cap, no rotation" data-ru="Нет центрального аудита, лимитов и ротации"></li>
        </ul>
      </div>
      <div class="problem-card after">
        <div class="label" data-i18n="prob.after" data-en="With AIbroker" data-ru="С AIbroker"></div>
        <ul>
          <li data-i18n="prob.a1" data-en="Projects use a single X-Project-Key" data-ru="Проекты используют один X-Project-Key"></li>
          <li data-i18n="prob.a2" data-en="Free-first chain; auto-fallback on 429/401" data-ru="Free-first цепочка; авто-fallback на 429/401"></li>
          <li data-i18n="prob.a3" data-en="Cost guard halts before the cap" data-ru="Cost-guard останавливает до лимита"></li>
          <li data-i18n="prob.a4" data-en="One dashboard for keys, usage, health" data-ru="Одна панель для ключей, расходов, здоровья"></li>
        </ul>
      </div>
    </div>
  </div>
</section>

<!-- HOW IT WORKS -->
<section id="how">
  <div class="container">
    <div class="eyebrow" data-i18n="how.eyebrow" data-en="How it works" data-ru="Как это работает"></div>
    <h2 data-i18n="how.title"
        data-en="Async jobs and sync endpoints. One project key."
        data-ru="Асинхронные задачи и синхронные эндпоинты. Один проектный ключ."></h2>
    <p class="section-intro" data-i18n="how.intro"
       data-en="The broker keeps provider tokens inside — your project never sees them. Chat and other slow work run as async jobs you poll; fast calls answer inline."
       data-ru="Брокер держит токены провайдеров внутри — ваш проект их не видит. Чат и другая медленная работа идут асинхронными задачами, которые вы опрашиваете; быстрые вызовы отвечают сразу."></p>
    <div class="modes">
      <div class="mode">
        <div class="mode-head">
          <h3 data-i18n="how.async.h" data-en="Async jobs" data-ru="Асинхронные задачи"></h3>
          <span class="mode-tag" data-i18n="how.proxy.tag" data-en="Recommended" data-ru="Рекомендуется"></span>
        </div>
        <p data-i18n="how.proxy.desc"
           data-en="Chat is async: POST /v1/jobs to submit, poll GET /v1/jobs/{{id}} for the result. Broker rotates keys until it has an answer — no held connection, no timeout. Embeddings stay synchronous on /v1/embed. Your code never touches provider credentials."
           data-ru="Чат асинхронный: POST /v1/jobs — отправить, GET /v1/jobs/{{id}} — забрать результат. Брокер ротирует ключи до ответа, без удержания соединения и таймаутов. Эмбеддинги синхронно на /v1/embed. Ваш код не касается креденшелов провайдера."></p>
        <pre><code>curl -X POST "https://aib.zapleo.com/v1/jobs?capability=chat:fast" \\
  -H "X-Project-Key: aib_prj_..." \\
  -H "Content-Type: application/json" \\
  -d '{{ "messages": [{{"role":"user","content":"hi"}}] }}'
# → {{"job_id": 123, "poll_url": "/v1/jobs/123"}}
curl https://aib.zapleo.com/v1/jobs/123 -H "X-Project-Key: aib_prj_..."</code></pre>
      </div>
      <div class="mode">
        <div class="mode-head">
          <h3 data-i18n="how.embed.h" data-en="Sync endpoints" data-ru="Синхронные эндпоинты"></h3>
          <span class="mode-tag" data-i18n="how.embed.tag" data-en="Fast" data-ru="Быстрые"></span>
        </div>
        <p data-i18n="how.embed.desc"
           data-en="Embeddings (and typed decisions) answer inline in seconds. Transcription is synchronous too, but the self-hosted whisper fallback can take minutes, so send longer audio to POST /v1/transcribe/jobs and poll GET /v1/jobs/{{id}}. Same key rotation and cost metering as chat."
           data-ru="Эмбеддинги (и типизированные решения) отвечают сразу, за секунды. Транскрипция тоже синхронная, но self-hosted whisper-фолбэк может работать минуты, поэтому длинное аудио отправляйте в POST /v1/transcribe/jobs и опрашивайте GET /v1/jobs/{{id}}. Та же ротация ключей и учёт стоимости, что и в чате."></p>
        <pre><code>curl -X POST https://aib.zapleo.com/v1/embed \\
  -H "X-Project-Key: aib_prj_..." \\
  -H "Content-Type: application/json" \\
  -d '{{ "texts": ["hello world"] }}'</code></pre>
      </div>
      <div class="mode" id="signup">
        <div class="mode-head">
          <h3 data-i18n="how.signup.h" data-en="Get a key in one call" data-ru="Ключ одним запросом"></h3>
          <span class="mode-tag" data-i18n="how.signup.tag" data-en="No approval" data-ru="Без одобрения"></span>
        </div>
        <p data-i18n="how.signup.desc"
           data-en="Any service can mint its own project key: free providers only ($0/day) and 100 requests in total. Enough to connect and test. Need more? Ask the owner to raise the limits. The key is shown once."
           data-ru="Любой сервис может сам получить проектный ключ: только бесплатные провайдеры ($0/день) и 100 запросов всего. Достаточно, чтобы подключиться и протестировать. Нужно больше — попросите владельца поднять лимиты. Ключ показывается один раз."></p>
        <pre><code>curl -X POST https://aib.zapleo.com/v1/signup \
  -H "Content-Type: application/json" \
  -d '{{ "name": "my-service", "contact": "me@example.com" }}'
# → {{"project_key": "aib_prj_...", "limits": {{...}}}}</code></pre>
      </div>
    </div>
  </div>
</section>

<!-- FEATURES -->
<section id="features">
  <div class="container">
    <div class="eyebrow" data-i18n="feat.eyebrow" data-en="Features" data-ru="Возможности"></div>
    <h2 data-i18n="feat.title"
        data-en="Built for projects that ship"
        data-ru="Сделано для проектов, которые работают"></h2>
    <p class="section-intro" data-i18n="feat.intro"
       data-en="Everything you need to run multi-provider LLM workloads in production. No SaaS dependency."
       data-ru="Всё необходимое для продакшен-нагрузки на несколько LLM-провайдеров. Без зависимости от SaaS."></p>
    <div class="features">
      <div class="feat">
        <div class="feat-icon">01</div>
        <h3 data-i18n="f1.t" data-en="Free-first routing" data-ru="Free-first маршрутизация"></h3>
        <p data-i18n="f1.d"
           data-en="Per-capability chains try free tiers first (chat:fast is free-only). The money lanes are the deliberate exception: chat:smart tries free Gemini first and keeps DeepSeek as the paid fallback; chat:sales leads with Claude Sonnet. Each money lane has its own daily cap."
           data-ru="Цепочки по способностям сначала пробуют free-тарифы (chat:fast — только бесплатные). Денежные линии — осознанное исключение: chat:smart сначала пробует бесплатный Gemini, а DeepSeek остаётся платным запасным; chat:sales ведёт Claude Sonnet. У каждой денежной линии свой дневной кап."></p>
      </div>
      <div class="feat">
        <div class="feat-icon">02</div>
        <h3 data-i18n="f2.t" data-en="Cost guard" data-ru="Cost guard"></h3>
        <p data-i18n="f2.d"
           data-en="Per-key, per-project, global daily caps. Reservation pattern: estimate before, settle after. Blocks before you go broke."
           data-ru="Лимиты на ключ, на проект, глобальные суточные. Reservation-паттерн: оценка до, расчёт после. Останавливает до того, как разоритесь."></p>
      </div>
      <div class="feat">
        <div class="feat-icon">03</div>
        <h3 data-i18n="f3.t" data-en="Health monitor" data-ru="Мониторинг ключей"></h3>
        <p data-i18n="f3.d"
           data-en="Background container probes every active key. 401 → mark dead + alert. 429 → cooldown. Recovery alerts when keys come back alive."
           data-ru="Фоновый контейнер пингует каждый активный ключ. 401 → пометка dead + алерт. 429 → cooldown. Recovery-алерт при возвращении."></p>
      </div>
      <div class="feat">
        <div class="feat-icon">04</div>
        <h3 data-i18n="f4.t" data-en="At-rest encryption" data-ru="Шифрование на диске"></h3>
        <p data-i18n="f4.d"
           data-en="Provider tokens stored Fernet-encrypted in Postgres. Dashboard never displays full tokens."
           data-ru="Токены провайдеров хранятся в Postgres под Fernet. Панель не показывает полные токены."></p>
      </div>
      <div class="feat">
        <div class="feat-icon">05</div>
        <h3 data-i18n="f5.t" data-en="Atomic key selection" data-ru="Атомарный выбор ключа"></h3>
        <p data-i18n="f5.d"
           data-en="SELECT FOR UPDATE SKIP LOCKED — no two requests get the same key. Round-robin among healthy keys."
           data-ru="SELECT FOR UPDATE SKIP LOCKED — два запроса не получат один и тот же ключ. Round-robin среди здоровых ключей."></p>
      </div>
      <div class="feat">
        <div class="feat-icon">06</div>
        <h3 data-i18n="f6.t" data-en="Scope-gated projects" data-ru="Scope-проекты"></h3>
        <p data-i18n="f6.d"
           data-en="Each project key carries allowed_scopes (llm:chat, llm:edit, llm:deep, llm:vision, llm:audio, llm:embed, llm:decision). Wrong scope → 403, no provider call."
           data-ru="Каждый ключ проекта имеет allowed_scopes (llm:chat, llm:edit, llm:deep, llm:vision, llm:audio, llm:embed, llm:decision). Не тот scope → 403, провайдер не дёргается."></p>
      </div>
      <div class="feat">
        <div class="feat-icon">07</div>
        <h3 data-i18n="f7.t" data-en="Telegram alerts" data-ru="Алерты в Telegram"></h3>
        <p data-i18n="f7.d"
           data-en="Owner gets messages on key death, cap breach, deploy fail. Throttled — no spam."
           data-ru="Владелец получает сообщения на смерть ключа, превышение лимита, упавший деплой. С троттлингом — без спама."></p>
      </div>
      <div class="feat">
        <div class="feat-icon">08</div>
        <h3 data-i18n="f8.t" data-en="Restricted SSH deploy" data-ru="Restricted SSH деплой"></h3>
        <p data-i18n="f8.d"
           data-en="GitHub Actions key in authorized_keys is wired to a single wrapper script. Leaked key = can only re-trigger our own deploy."
           data-ru="Ключ GitHub Actions в authorized_keys привязан к одному wrapper-скрипту. Утечка ключа = можно только запустить наш же деплой."></p>
      </div>
      <div class="feat">
        <div class="feat-icon">09</div>
        <h3 data-i18n="f9.t" data-en="Hard CI gates" data-ru="Жёсткие CI-гейты"></h3>
        <p data-i18n="f9.d"
           data-en="Deploy requires tests + docs to pass. Coverage gate stair-steps up; never drops. 900+ tests, unit + Postgres integration."
           data-ru="Деплой требует прохождение тестов + документации. Coverage-гейт растёт ступенями; не падает. 900+ тестов, unit + Postgres-интеграция."></p>
      </div>
      <div class="feat">
        <div class="feat-icon">10</div>
        <h3 data-i18n="f10.t" data-en="Reliable JSON" data-ru="Надёжный JSON"></h3>
        <p data-i18n="f10.d"
           data-en="Structured requests route to JSON-reliable providers first; send a json_schema and supported providers grammar-constrain output. A malformed body skips straight to the next provider — no wasted retries."
           data-ru="Структурные запросы идут к JSON-надёжным провайдерам первыми; пришлите json_schema — поддерживающие провайдеры грамматически ограничат вывод. Битый ответ — сразу к следующему провайдеру, без лишних ретраев."></p>
      </div>
      <div class="feat">
        <div class="feat-icon">11</div>
        <h3 data-i18n="f11.t" data-en="Prompt caching" data-ru="Кэш промптов"></h3>
        <p data-i18n="f11.d"
           data-en="Stable system prefixes are cached (Anthropic explicit, DeepSeek/Gemini automatic); cache reads cost ~0.1x on Anthropic and ~0.02x on DeepSeek; cache tokens are metered per call. Repeated translate phrases skip the model entirely."
           data-ru="Стабильные system-префиксы кэшируются (Anthropic явно, DeepSeek/Gemini автоматически); чтение из кэша стоит ~0.1x у Anthropic и ~0.02x у DeepSeek; кэш-токены учитываются на вызов. Повторяющиеся translate-фразы вообще минуют модель."></p>
      </div>
      <div class="feat">
        <div class="feat-icon">12</div>
        <h3 data-i18n="f12.t" data-en="Cost & health visibility" data-ru="Видимость costs и здоровья"></h3>
        <p data-i18n="f12.d"
           data-en="Spend attributed per project and per workflow; last-hour error rate shown per provider so a rate-limit storm is visible at a glance. Adaptive cooldowns with jitter and a per-request attempt cap tame retry storms."
           data-ru="Расходы разложены по проектам и workflow; error-rate за час на провайдера — шторм лимитов виден сразу. Адаптивные cooldown с jitter и лимит попыток на запрос гасят retry-штормы."></p>
      </div>
    </div>
  </div>
</section>

<!-- PROVIDERS -->
<section id="providers">
  <div class="container">
    <div class="eyebrow" data-i18n="prov.eyebrow" data-en="Providers" data-ru="Провайдеры"></div>
    <h2 data-i18n="prov.title"
        data-en="{n_providers} providers wired in. Add more in one row of code."
        data-ru="Подключено провайдеров: {n_providers}. Добавить ещё — одна строка кода."></h2>
    <p class="section-intro" data-i18n="prov.intro"
       data-en="Built on LiteLLM, so any of its 100+ providers can be plugged in. The free-first chain is configurable per capability."
       data-ru="Построено на LiteLLM — можно подключить любой из его 100+ провайдеров. Free-first цепочка настраивается на каждую способность."></p>
    <div class="providers">
{providers_html}
    </div>
  </div>
</section>

<!-- API -->
<section id="api">
  <div class="container">
    <div class="eyebrow" data-i18n="api.eyebrow" data-en="API" data-ru="API"></div>
    <h2 data-i18n="api.title"
        data-en="Four endpoint groups. Per-group auth."
        data-ru="Четыре группы эндпоинтов. Своя авторизация на каждую."></h2>

    <div class="api-grid">
      <div class="api-block">
        <h3 data-i18n="api.client" data-en="For clients · X-Project-Key" data-ru="Для клиентов · X-Project-Key"></h3>
        <div class="endpoints">
{client_endpoints_html}
        </div>
      </div>
      <div class="api-block">
        <h3 data-i18n="api.admin" data-en="For admins · X-Admin-Key" data-ru="Для админов · X-Admin-Key"></h3>
        <div class="endpoints">
          <div class="ep"><span class="verb">GET</span><span class="path">/admin/projects</span><span class="note">list</span></div>
          <div class="ep"><span class="verb post">POST</span><span class="path">/admin/projects</span><span class="note">create</span></div>
          <div class="ep"><span class="verb">GET</span><span class="path">/admin/keys</span><span class="note">list</span></div>
          <div class="ep"><span class="verb post">POST</span><span class="path">/admin/keys</span><span class="note">create</span></div>
          <div class="ep"><span class="verb post">POST</span><span class="path">/admin/keys/{{id}}/disable</span><span class="note">disable</span></div>
          <div class="ep"><span class="verb">DEL</span><span class="path">/admin/keys/{{id}}</span><span class="note">delete</span></div>
        </div>
      </div>
      <div class="api-block">
        <h3 data-i18n="api.public" data-en="Public · no auth" data-ru="Публичные · без авторизации"></h3>
        <div class="endpoints">
          <div class="ep"><span class="verb post">POST</span><span class="path">/v1/signup</span><span class="note">get a free key</span></div>
          <div class="ep"><span class="verb">GET</span><span class="path">/healthz</span><span class="note">liveness</span></div>
          <div class="ep"><span class="verb">GET</span><span class="path">/v1/health</span><span class="note">provider health</span></div>
          <div class="ep"><span class="verb">GET</span><a class="path" href="/docs">/docs</a><span class="note">Swagger</span></div>
          <div class="ep"><span class="verb">GET</span><a class="path" href="/openapi.json">/openapi.json</a><span class="note">spec</span></div>
        </div>
      </div>
      <div class="api-block">
        <h3 data-i18n="api.dash" data-en="Browser dashboard" data-ru="Браузерная панель"></h3>
        <div class="endpoints">
          <div class="ep"><span class="verb">GET</span><a class="path" href="/login">/login</a><span class="note">TG widget</span></div>
          <div class="ep"><span class="verb">GET</span><a class="path" href="/dashboard">/dashboard</a><span class="note">owner only</span></div>
        </div>
      </div>
    </div>
  </div>
</section>

<!-- PRICING -->
<section id="pricing">
  <div class="container">
    <div class="eyebrow" data-i18n="price.eyebrow" data-en="Cost" data-ru="Стоимость"></div>
    <h2 data-i18n="price.title"
        data-en="Free to run. Pay only your providers."
        data-ru="Запуск бесплатный. Платите только провайдерам."></h2>
    <p class="section-intro" data-i18n="price.intro"
       data-en="AIbroker is self-hosted. There is no SaaS fee. The broker itself runs on a single small VPS."
       data-ru="AIbroker — self-hosted. SaaS-сбора нет. Сам брокер работает на одном маленьком VPS."></p>
    <div class="pricing">
      <div class="tier">
        <div class="tier-name" data-i18n="price.t1" data-en="Self-host" data-ru="Self-host"></div>
        <div class="tier-price">$0<span class="currency">/mo</span></div>
        <div class="tier-desc" data-i18n="price.d1" data-en="Run the broker on your own VPS." data-ru="Запустите брокер на своём VPS."></div>
        <ul>
          <li data-i18n="price.t1.1" data-en="All features" data-ru="Все возможности"></li>
          <li data-i18n="price.t1.2" data-en="Unlimited projects + keys" data-ru="Безлимитные проекты и ключи"></li>
          <li data-i18n="price.t1.3" data-en="Source visible on GitHub" data-ru="Исходники видны на GitHub"></li>
          <li data-i18n="price.t1.4" data-en="GitHub issues for support" data-ru="Поддержка через GitHub issues"></li>
        </ul>
        <a href="https://github.com/zapleoceo/AIbroker" class="btn" data-i18n="price.t1.cta" data-en="View on GitHub" data-ru="Открыть на GitHub"></a>
      </div>
      <div class="tier featured">
        <div class="tier-name" data-i18n="price.t2" data-en="Hosted (this instance)" data-ru="Хостинг (этот инстанс)"></div>
        <div class="tier-price" data-i18n="price.t2.p" data-en="invite-only" data-ru="по приглашению"></div>
        <div class="tier-desc" data-i18n="price.d2" data-en="Use aib.zapleo.com for personal projects on request." data-ru="Используйте aib.zapleo.com для личных проектов по запросу."></div>
        <ul>
          <li data-i18n="price.t2.1" data-en="No setup" data-ru="Без настройки"></li>
          <li data-i18n="price.t2.2" data-en="Shared free-tier pool" data-ru="Общий пул free-тарифов"></li>
          <li data-i18n="price.t2.3" data-en="Owner approves each project key" data-ru="Владелец одобряет каждый ключ проекта"></li>
          <li data-i18n="price.t2.4" data-en="Best-effort uptime" data-ru="Uptime — best-effort"></li>
        </ul>
        <a href="https://t.me/zapleoceo" class="btn btn-primary" data-i18n="price.t2.cta" data-en="Request access" data-ru="Запросить доступ"></a>
      </div>
      <div class="tier">
        <div class="tier-name" data-i18n="price.t3" data-en="Custom deploy" data-ru="Custom-деплой"></div>
        <div class="tier-price" data-i18n="price.t3.p" data-en="contact" data-ru="по запросу"></div>
        <div class="tier-desc" data-i18n="price.d3" data-en="Help setting up AIbroker on your infra." data-ru="Помощь с разворачиванием AIbroker на вашей инфраструктуре."></div>
        <ul>
          <li data-i18n="price.t3.1" data-en="Hetzner / DO / AWS / on-prem" data-ru="Hetzner / DO / AWS / on-prem"></li>
          <li data-i18n="price.t3.2" data-en="Custom provider chains" data-ru="Кастомные цепочки провайдеров"></li>
          <li data-i18n="price.t3.3" data-en="Migration of existing keys" data-ru="Миграция существующих ключей"></li>
          <li data-i18n="price.t3.4" data-en="Onboarding support" data-ru="Поддержка при онбординге"></li>
        </ul>
        <a href="https://t.me/zapleoceo" class="btn" data-i18n="price.t3.cta" data-en="Get in touch" data-ru="Связаться"></a>
      </div>
    </div>
  </div>
</section>

<!-- FAQ -->
<section id="faq">
  <div class="container">
    <div class="eyebrow" data-i18n="faq.eyebrow" data-en="FAQ" data-ru="FAQ"></div>
    <h2 data-i18n="faq.title" data-en="Common questions" data-ru="Частые вопросы"></h2>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:32px;margin-top:28px">
      <div>
        <h3 data-i18n="faq.q1" data-en="Does AIbroker store my prompts?" data-ru="Хранит ли AIbroker мои промпты?"></h3>
        <p style="color:var(--muted);font-size:14px" data-i18n="faq.a1"
           data-en="Request bodies are not logged: only metadata is (provider, model, capability, tokens, cost, latency, status). Async jobs keep their request and result in the job queue so you can poll them; those payloads are purged after 7 days."
           data-ru="Тела запросов не логируются: пишутся только метаданные (провайдер, модель, способность, токены, стоимость, латентность, статус). Асинхронные задачи хранят запрос и результат в очереди, чтобы вы могли их опросить; эти данные удаляются через 7 дней."></p>

        <h3 data-i18n="faq.q2" data-en="What happens if all keys for a provider fail?" data-ru="Что если все ключи провайдера упали?"></h3>
        <p style="color:var(--muted);font-size:14px" data-i18n="faq.a2"
           data-en="Chain falls back to the next provider, rotating keys until one answers. Chat is an async job (POST /v1/jobs → poll GET /v1/jobs/{{id}}), so a slow or saturated provider never times the caller out."
           data-ru="Цепочка переключается на следующего провайдера, ротируя ключи до ответа. Чат — асинхронная задача (POST /v1/jobs → опрос GET /v1/jobs/{{id}}), поэтому медленный или перегруженный провайдер не отваливает клиента по таймауту."></p>
      </div>
      <div>
        <h3 data-i18n="faq.q3" data-en="How is the dashboard secured?" data-ru="Как защищена панель?"></h3>
        <p style="color:var(--muted);font-size:14px" data-i18n="faq.a3"
           data-en="The Telegram Login Widget signs in only the owner's Telegram account. The session cookie is HMAC-signed. An X-Admin-Key header is the fallback for ops scripts."
           data-ru="Telegram Login Widget пускает только Telegram-аккаунт владельца. Кука сессии подписана HMAC. Заголовок X-Admin-Key — fallback для ops-скриптов."></p>

        <h3 data-i18n="faq.q4" data-en="Can I rotate provider tokens?" data-ru="Можно ли ротировать токены провайдеров?"></h3>
        <p style="color:var(--muted);font-size:14px" data-i18n="faq.a4"
           data-en="Yes. Add a new key via dashboard or POST /admin/keys, then disable or delete the old one. New requests pick up the new key at once."
           data-ru="Да. Добавьте новый ключ через панель или POST /admin/keys, потом отключите или удалите старый. Новые запросы сразу пойдут на новый ключ."></p>
      </div>
    </div>
  </div>
</section>

<!-- FOOTER -->
<footer>
  <div class="container">
    <div class="footer-grid">
      <div>
        <div class="brand" style="margin-bottom:12px"><span class="dot"></span> AIbroker</div>
        <p style="color:var(--dim);font-size:13px;max-width:280px" data-i18n="foot.desc"
           data-en="One key, every LLM provider. Free-first, cost-guarded, self-hosted."
           data-ru="Один ключ, все LLM-провайдеры. Free-first, cost-guard, self-hosted."></p>
      </div>
      <div>
        <h4 data-i18n="foot.product" data-en="Product" data-ru="Продукт"></h4>
        <ul>
          <li><a href="#how" data-i18n="nav.how" data-en="How it works" data-ru="Как работает"></a></li>
          <li><a href="#features" data-i18n="nav.features" data-en="Features" data-ru="Возможности"></a></li>
          <li><a href="#pricing" data-i18n="nav.pricing" data-en="Pricing" data-ru="Цены"></a></li>
        </ul>
      </div>
      <div>
        <h4 data-i18n="foot.dev" data-en="Developers" data-ru="Разработчикам"></h4>
        <ul>
          <li><a href="/docs">/docs</a></li>
          <li><a href="/openapi.json">/openapi.json</a></li>
          <li><a href="/healthz">/healthz</a></li>
          <li><a href="/v1/health">/v1/health</a></li>
        </ul>
      </div>
      <div>
        <h4 data-i18n="foot.links" data-en="Links" data-ru="Ссылки"></h4>
        <ul>
          <li><a href="https://github.com/zapleoceo/AIbroker">GitHub</a></li>
          <li><a href="https://t.me/zapleoceo">Telegram</a></li>
          <li><a href="/dashboard" data-i18n="nav.dashboard" data-en="Dashboard" data-ru="Панель"></a></li>
        </ul>
      </div>
    </div>
    <div class="footer-bottom">
      <span data-i18n="foot.copy" data-en="© 2026 AIbroker" data-ru="© 2026 AIbroker"></span>
      <span style="font-family:var(--mono);font-size:12px;color:var(--dim)">v{version}</span>
    </div>
  </div>
</footer>

<script>
(function() {{
  const KEY = "aib_lang";
  const params = new URLSearchParams(location.search);
  const fromQuery = params.get("lang");
  // Storage can throw (Safari private mode, blocked site data); an uncaught
  // throw here aborts the script and leaves the JS-filled page blank (2026-10-02).
  let fromStore = null;
  try {{ fromStore = localStorage.getItem(KEY); }} catch (e) {{}}
  let lang = (fromQuery === "ru" || fromQuery === "en") ? fromQuery
            : (fromStore === "ru" || fromStore === "en") ? fromStore
            : "en";

  function apply(l) {{
    document.documentElement.lang = l;
    document.querySelectorAll("[data-i18n]").forEach(el => {{
      const txt = el.getAttribute("data-" + l);
      if (txt !== null) el.textContent = txt;
    }});
    document.querySelectorAll(".lang-toggle button").forEach(b => {{
      b.classList.toggle("active", b.dataset.lang === l);
    }});
    try {{ localStorage.setItem(KEY, l); }} catch (e) {{}}
  }}

  document.querySelectorAll(".lang-toggle button").forEach(b => {{
    b.addEventListener("click", () => apply(b.dataset.lang));
  }});

  apply(lang);
}})();
</script>

</body></html>"""


# HEAD / answered 405 (2026-10-02) — uptime checkers and link unfurlers probe
# with HEAD; FastAPI does not derive it from a GET route.
@router.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def landing() -> HTMLResponse:
    """Public landing — bilingual EN/RU, default EN."""
    return HTMLResponse(_HTML.format(
        version=__version__, favicon=FAVICON_LINKS, css=CSS_LINKS,
        n_providers=len(wired_providers()), providers_html=_providers_html(),
        client_endpoints_html=_client_endpoints_html(),
    ))


@router.get("/favicon.svg")
async def favicon_svg() -> Response:
    """SVG favicon — modern browsers prefer this for crisp tab-strip rendering."""
    return Response(content=FAVICON_SVG, media_type="image/svg+xml",
                     headers={"Cache-Control": "public, max-age=86400"})


@router.get("/favicon.ico")
async def favicon_ico() -> Response:
    """Browsers that request /favicon.ico by default — serve the same SVG.
    Modern browsers (2020+) accept image/svg+xml at any path. Avoids the
    404 in dev consoles and keeps one source of truth for the icon."""
    return Response(content=FAVICON_SVG, media_type="image/svg+xml",
                     headers={"Cache-Control": "public, max-age=86400"})


# ─── Discovery endpoints (SEO + LLM crawlers) ──────────────────────────────


_ROBOTS_TXT = """User-agent: *
Allow: /
Disallow: /admin/
Disallow: /dashboard
Disallow: /api/

Sitemap: https://aib.zapleo.com/sitemap.xml
"""


_SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:xhtml="http://www.w3.org/1999/xhtml">
  <url>
    <loc>https://aib.zapleo.com/</loc>
    <xhtml:link rel="alternate" hreflang="en" href="https://aib.zapleo.com/?lang=en"/>
    <xhtml:link rel="alternate" hreflang="ru" href="https://aib.zapleo.com/?lang=ru"/>
    <xhtml:link rel="alternate" hreflang="x-default" href="https://aib.zapleo.com/"/>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>https://aib.zapleo.com/docs</loc>
    <changefreq>weekly</changefreq>
    <priority>0.7</priority>
  </url>
  <url>
    <loc>https://aib.zapleo.com/openapi.json</loc>
    <changefreq>weekly</changefreq>
    <priority>0.5</priority>
  </url>
</urlset>
"""


# llms.txt — proposed standard (Jeremy Howard, 2024) for LLM-friendly site
# discovery. Plain markdown; crawlers like Perplexity / ChatGPT browse use it
# as a hint of what content is worth ingesting + how to structure it.
_LLMS_TXT = """# AIbroker

> Self-hosted centralized LLM key broker. One API endpoint routes calls across
> @@N@@ LLM providers (@@PROVIDERS@@; `local` is a self-hosted provider
> for vision and a transcription fallback) with free-tier-first ordering, paid fallback,
> per-key and per-project cost caps, automatic health probing, and
> Fernet-encrypted token storage at rest. Self-host on any VPS.

## Key concepts

- **Proxy mode**: the broker calls the provider with its own stored key and
  returns the response — clients never see provider credentials.
- **Privacy**: request bodies are not logged (metadata only). Async job
  payloads (`/v1/jobs`, `/v1/deep`, `/v1/transcribe/jobs`) are stored in the job
  queue so they can be polled and are purged after 7 days.
- **Capabilities**: requests are tagged with one of @@CAPABILITIES@@. Each maps
  to an ordered provider chain and a required scope. Most chains are
  free-first; `chat:smart` (free Gemini first, DeepSeek as the paid fallback)
  and `chat:sales` (Claude Sonnet-led) are the money lanes where answer quality
  outranks price, each bounded by its own daily cap. `chat:sales`
  additionally keeps Sonnet's reasoning on — it does not force JSON through
  tool-use, because on that model the two are mutually exclusive. `chat:deep`
  is a dedicated long-context/reasoning lane (1M-token context, no latency
  guarantee) gated behind its own scope so it never competes with live chat
  traffic.
- **Scopes**: every project key carries a list of allowed scopes
  (@@SCOPES@@). Mismatch → HTTP 403.
- **Adaptive cooldown**: per-provider base wait (Gemini 60s,
  OpenRouter 5min, etc.) with exponential backoff per consecutive 429.
- **Reserved lane**: a key marked `is_reserve=true` is picked last in its
  group — the safety net behind the shared pool.

## Endpoints

- [/](https://aib.zapleo.com/) — bilingual EN/RU landing page (this page)
- [/docs](https://aib.zapleo.com/docs) — Swagger UI, full API reference
- [/openapi.json](https://aib.zapleo.com/openapi.json) — OpenAPI 3 schema
- [/healthz](https://aib.zapleo.com/healthz) — liveness probe
- [/v1/health](https://aib.zapleo.com/v1/health) — per-provider key health
- `POST /v1/signup` — no auth: mint your own project key (free providers only, $0/day,
  100 requests in total, `llm:chat` + `llm:embed`); body `{"name": "...", "contact": "...", "purpose": "..."}`;
  the key is returned once. Over the limit: `429 {"error": "request_cap_exhausted"}` — ask the owner to raise it
- `POST /v1/jobs?capability=...` — submit a chat job, returns a `job_id` (needs `X-Project-Key`)
- `GET /v1/jobs/{id}` — poll for the job result
- `POST /v1/embed?provider=voyage` — proxy mode embed
- `POST /v1/decisions` — typed decisions (choice / score / yes-no), synchronous
- `POST /v1/deep` + `GET /v1/deep/{job_id}` — async job API for
  `capability=chat:deep` (long-context/reasoning; latency observed up to
  ~8 min, past Cloudflare/nginx proxy timeouts — submit, then poll)
- `POST /v1/transcribe` — proxy mode audio → text (synchronous; the self-hosted
  whisper fallback can take minutes — use `POST /v1/transcribe/jobs`, then poll
  `GET /v1/jobs/{id}`)
- `POST /v1/chat` — removed, returns 410 Gone (use `/v1/jobs`)
- `DELETE /admin/keys/{id}` — admin API (`X-Admin-Key`), alongside `POST /admin/keys`

## Code

- GitHub: https://github.com/zapleoceo/AIbroker (source visible, proprietary — not licensed for reuse)
- License: Proprietary, all rights reserved (owner: zapleoceo)
- Stack: FastAPI · SQLAlchemy 2 async · asyncpg · LiteLLM · Postgres · Docker
"""


def llms_text() -> str:
    """_LLMS_TXT with the provider / capability / scope lists filled in from the
    routing tables (they used to be hand-copied and drifted)."""
    names = ", ".join(_PROVIDER_LABEL.get(p, p).title() if p != "local" else "local"
                      for p in wired_providers() if p != "local")
    return (_LLMS_TXT.replace("@@N@@", str(len(wired_providers())))
            .replace("@@PROVIDERS@@", names)
            .replace("@@CAPABILITIES@@", _capability_list())
            .replace("@@SCOPES@@", _scope_list()))


@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt() -> PlainTextResponse:
    """Allow indexing of public pages; block admin / dashboard / api callbacks."""
    return PlainTextResponse(_ROBOTS_TXT)


@router.get("/sitemap.xml")
async def sitemap_xml() -> Response:
    """Standard XML sitemap with hreflang alternates for EN/RU."""
    return Response(content=_SITEMAP_XML, media_type="application/xml")


@router.get("/llms.txt", response_class=PlainTextResponse)
async def llms_txt() -> PlainTextResponse:
    """LLM-friendly site descriptor (Jeremy Howard's proposed /llms.txt).
    Plain markdown — crawlers like Perplexity, ChatGPT browse, Claude search
    use it as a hint of what content matters and how it's structured."""
    return PlainTextResponse(llms_text())
