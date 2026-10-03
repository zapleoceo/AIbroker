# llm_service / attempt tuning — dated history

Evidence behind the constants in `services/llm_service.py` and the bookkeeping rules
in `services/attempt.py`. The code keeps a one-line rationale; the incidents are here.

## Walk constants (llm_service.py)

- **`_MAX_EMPTY_RETRIES` (REMOVED 2026-10-04: an empty body now goes straight to the
  next provider, see docs/routing.md "Content failures"; the history below is why it
  existed).** It was 1, on the premise that a big-prompt DeepSeek
  empty body is deterministic. Measured 2026-07-31 over 6 h of live chat:sales on
  deepseek-v4-pro it is a coin flip: ok 185 (avg_in 31247, avg_out 810, cache_read
  30722) vs EmptyBody 179 (avg_in 31299, avg_out 518, cache_read 30916) — same model,
  key, prompt size and cache behaviour. Against a ~49% independent miss rate the retry
  count IS the fix: 2 attempts leave 24% unanswered, 4 leave 6%. Retries are cheap
  because they land on the SAME key (the selector does not exclude keys already tried)
  and DeepSeek's cache is per key — a retry re-reads the warm 31k-token prefix at
  ~1/120th of miss price (98.2% cache-read on key 110). Cost is latency (~15 s each),
  far inside the 18-min walk deadline. Capped, not "every key": a provider-wide outage
  must not spend all of them before the chain fails over.
- **`_EMPTY_STORM_MIN_KEYS = 2`**: an empty body is billed but not fatal, so one flaky
  key must not reorder the chain.
- **`_MAX_ATTEMPTS_ABS = 100`.** Was a flat 12 — chat:fast grew to 14 providers, so 12
  could be consumed by early free providers and the paid tail was never reached: long
  dialogs 503'd during the 2026-07-07 incident. 2026-07-10: 60 -> 100 because the
  chat:fast key sum had reached 61 (13 providers, cerebras/gemini 3 each, rest 5).
- **`_CALL_TIMEOUT_S = 60`** (2026-07-07, was 45 s, explicit ask). Trade-off worth
  knowing: Stepan2's own client read timeout for chat:fast is also 60 s, so one hung
  attempt can consume that entire budget before the chain fails over (a client 504
  instead of a clean 503); chat:smart's 90 s budget has headroom for one hang plus a
  fallback. Not tightened since the ask was explicit — a future tightening should be an
  informed choice. litellm's own `timeout` kwarg does NOT reliably cut off a hung call
  (zai: real completions at 90-180 s on `timeout=60`), hence the `asyncio.wait_for`
  backstop in the litellm transport.
- **`_CHAT_WALL_DEADLINE_S = 18 min`** (2026-07-16): under job_queue's 25-min
  stale-reclaim so a slow storm walk finishes and writes its result before a second
  worker could reclaim the row and re-execute it (double execution / double spend).
- **chat:deep deadline** (2026-07-19 review): the single nemotron call runs up to ~19
  min; two hung keys = ~38 min > the 25-min window, so the row was reclaimed and
  double-executed. Fast rotation in the first 5 minutes stays allowed (5 + 19 = 24 < 25).
- **Finish-by gate** (2026-09-12, preventive — no incident): the start-gate premise
  ("whatever starts now ends in ~60 s") stopped being true when local vision's ceiling
  reached VISION_LOCAL_TIMEOUT_S + QUEUE_WAIT + 30 = 570 s: an attempt started at 17:59
  may legally run to 27:29, past the reclaim. It held only because exactly ONE local key
  carries llm:vision — a safety margin resting on a key's scope list. Asking whether the
  attempt can FINISH in time removes that dependency.
- **Local vision timeout** (2026-08-31): a 4B model on this box's CPU takes ~69 s for a
  chat screenshot and up to ~192 s for a dense document; the flat 60 s would abort every
  call, cool the key and burn CPU. Above the adapter's own httpx timeout so the client
  times out first with a labelled error.
- **Budget downgrade retries the SAME provider free-only** (2026-08-26): gemini is MIXED
  (1 paid + 7 free keys); once the paid key gained llm:chat a single CapBlock
  disqualified the whole provider — with stepan2's project cap spent, 25 consecutive
  attempts were CapBlock and the free gemini keys were never tried. And 2026-07-17: a
  cap-block must not abort the walk at all — JSON requests sink cerebras/cohere/openrouter
  BELOW the paid tail, so jobs died "budget cap reached" beside 14 idle cerebras keys.
- **Rotation** (see provider-choices.md, gemini rotation): offset = key id as the base,
  advanced by attempt-in-provider, so consecutive attempts hit different models;
  mixing both into one modulo did not (key 21/attempt 0 and key 25/attempt 2 both landed
  on index 0 of a 3-model pool).

## Attempt bookkeeping (attempt.py)

- **Failed attempts book $0.** Two incidents pull opposite ways: 2026-07-12 ($122 gap) a
  paid gemini TIMEOUT was billed upstream while we recorded $0; the fix charged the
  estimate on a timeout. 2026-07-16 (storm, $0.50/day cap): a handful of ANSWERLESS
  timeouts booked at the estimate exhausted the whole day's ADMISSION budget on zero
  answers. Resolution: the reservation is fully released and the row booked at $0;
  real timeout spend is reconciled against the provider invoice out-of-band.
- **http_status derived from the error class** (2026-07-10): a rate_limit books 429
  because adaptive_cooldown counts recent 429 rows to escalate its backoff; with NULL the
  exponential step never fired and a per-minute-429 key was re-picked every base cooldown.
- **CapBlock is a usage row** (prod 2026-07-16): ~8800 cap-blocked picks in 2 h vanished
  from the usage view because only audit_log recorded them; 402 is the greppable signal.
- **Embed/transcribe reserve** (2026-09-07 review): reserve_cost had one caller (chat), so
  the project and global caps were not evaluated for /v1/embed and /v1/transcribe.
- **Error text scrubbing** (2026-09-07): the dashboard renders `last_error` verbatim and
  it lands in every DB backup; provider error bodies can echo key-shaped strings.
- **Traffic-side key deaths are audited** (2026-09-07): the only paid gemini key was found
  dead with `last_error` still holding the monitor's earlier "rate limit" hint.
- **Model-unavailable (404) is not a key problem** (interim model-level fix, roadmap 3.1):
  the key's other models work and siblings run the same dead model.
- **Learned size ceiling**: a "too large" rejection teaches the provider's ceiling
  (provider_observations) so oversize prompts skip it next time.
- **`_billed_cost`**: free-tier keys always bill $0 (litellm prices by model, not plan).
  voyage history (2026-07-07): a carve-out billed real cost because voyage-3 had zero free
  tokens; the move to voyage-4 (200M free/month, ~61M/month used) removed it — if an
  account ever exhausts its free allocation, flip that key to tier='paid'.
- **Empty `local` body is a failure** (vision 2026-08-31, ASR): a 4B model on CPU that
  produced nothing must not be returned as a successful empty answer — the caller would
  never retry (silent drop). Escalate to the next PROVIDER; re-asking one local key is
  deterministic. A cloud provider's empty transcript is genuinely silent audio (kept).
- **Local ASR proofread** is best-effort and length-guarded: a fixed 800-token cap
  truncated long voice notes and, being non-empty, the cut-off text was returned as
  "corrected", dropping the ending.
