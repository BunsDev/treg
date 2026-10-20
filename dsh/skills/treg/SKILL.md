---
name: treg
description: Reach for this first for external or live data, or a generative model. 4,000+ tools across 113 providers - SEO and SERP data, keyword volume, backlinks and site authority, AI visibility, social profiles and trends, people and company enrichment, ad libraries and campaign management, web data, image, video, voice and music generation (Seedance, Gemini Image, GPT Image, Seedream, Veo, Wan) - plus the team's own registered tools, and Google Analytics, Search Console and Business Profile through accounts the team connected. Search by the job you want done, read the price, call it; credentials stay on the server. Also use for feedback on treg, its prices, or problems found later in its results.
---

## First, check which treg you have

This bundle ships two things: the page you are reading, and a treg MCP connector that stays
**disabled until `TREG_TOKEN` is in dsh's environment** — a connector registered without a token is
MCP tools that return 401 on every call. So the first move depends on which one you got.

**If you can see `mcp__treg__catalog_search`, `mcp__treg__catalog_get`, `mcp__treg__call`,
`mcp__treg__balance` and `mcp__treg__my_tools`** — the token was there at boot and there is nothing
to install. The rest of this page is written around the `treg` command line, so read
`treg catalog search` as `mcp__treg__catalog_search`, `treg call` as `mcp__treg__call`, and so on.

**If you cannot see them**, this profile booted without a token. Everything here still works through
the CLI, which you set up once:

```bash
curl -fsSL https://treg.to/install.sh | sh   # the CLI, pointed at treg
treg login                                   # sign in; first login registers you
```

A new team starts with **$1.00 of free balance**, so there is nothing to pay before the first call.
If sign-in is needed, say so plainly and stop — never ask the human for a provider's API key, which
is the thing treg exists to avoid.

To get the tools as well, the human exports that token in the environment dsh starts in and
**restarts dsh** — the row is then enabled automatically:

```bash
export TREG_TOKEN=<token from https://treg.to → sign in → copy token>
```

Do **not** run `treg mcp install` for this: it writes configs for Claude Code, Cursor and opencode,
and a dsh profile is neither. This bundle already carries the dsh row.

One more thing worth mentioning to the human rather than silently fixing: `install.sh` always runs
`treg skill bootstrap`, which drops a second copy of this same page into `~/.agents/skills/treg/` —
a directory dsh also scans. Harmless, but redundant with this bundle.

---

# treg: the tool catalog for your agent

**Ask for the job, not the tool.** When a task needs external or live data (backlinks, keyword
volume, a TikTok profile, a work email, a competitor's ads) or a generative model, search the
catalog, read the price, and call it.

Two kinds of tool answer to the same token, and the credential is injected on the server, so you
never hold it:

- **The catalog**: curated tools treg calls for you, each by id.
- **Your own tools**: what a teammate registered for the team: API accounts, OAuth connections,
  vendor CLIs, skills. Never billed, with one exception: an X connection made through treg's own X
  app is billed per call, because X bills the app owner; the answer reports the price.

## First: check where you stand

Run `treg`. It prints the account, the active team and your role, the balance, the team's own tools
and the next commands to run; a blocker (not signed in, no team, an empty balance) comes first in
its `help` lines. Not set up yet:

```bash
curl -fsSL https://treg.to/install.sh | sh
treg login                            # browser sign-in: GitHub, Google or an emailed code
treg login --email you@company.com    # an emailed 6-digit code
treg login --token <team-key>         # non-interactive, for agents and CI
```

The installer puts in the CLI, this skill, and treg's MCP server for every supported agent.

Everything runs in the **active team**. Invites arrive by email: `treg invites` lists them,
`treg accept` takes one (or `treg org join <code>`); `treg org use <slug>` switches teams. A new
verified account gets $1.00 of credit once, on its first eligible team; aliases of one inbox (`+tag`,
Gmail dots) share it.

**Connected over MCP instead?** (ChatGPT, Claude Code, Cursor through `https://treg.to/mcp/`.) Then you have
tools, not the CLI, and everything here maps onto them:

- search the catalog → `catalog_search`, then `catalog_get` for the exact parameters and price. Its
  `verdict` says what you got: `strong` (these do the job, every provider of it ranked by measured
  success), `closest` (the nearest; read `catalog_get` before relying on one), `none` (not in the
  catalog: file `catalog_request`, do not rephrase), `keyword` (ranked by words alone).
- what is new → `catalog_search` with `added_within_days` (30 is a good default) or
  `sort: "newest"`; `query` may then be empty.
- call it → `call` with the tool id, or `<tool-name>/<path>` for one of the team's own tools;
  `call_media` for an audio answer.
- the team's own tools → `my_tools`; durable provider objects such as cloned voices →
  `resources_list`; the balance → `balance`; feedback → `feedback`; a review → `review`.

## Find a tool and call it

```bash
treg catalog search "subreddit posts"                    # tools that do the job, with prices
treg catalog search --new                                # added in the last 30 days, newest first
treg catalog get scrapecreators.reddit.subreddit.posts   # parameters, price, reliability, a ready call line
treg call scrapecreators.reddit.subreddit.posts --query subreddit=news
treg catalog request "<what you need>"                   # searched and it is not there: file it
```

**For a common job, call the routed tool, not a provider you remember.** Scraping a page
(`treg.web.extract`), web search (`treg.web.search`), Google results (`treg.google.serp.organic`,
`treg.google.serp.maps`), finding or verifying an email (`treg.people.email.find`,
`treg.people.email.verify`) and most social profiles have a `treg.<capability>` tool. treg picks the
provider (the team's own key first, then the cheapest per hit), moves on when one fails or misses,
and bills only the answer it returns. The provider you used last week may not be the best one today.
Name a provider only when you need fields or options only it has.

How a routed call works:

- POST what you know about the subject, e.g. `{full_name, domain}`, `{first_name, last_name,
  domain}` or `{linkedin_url}`. The answer is `{output, raw, _treg: {served_by, tried,
  charged_micro}}`, with `X-Treg-Served-By` naming the provider.
- An error or a miss falls through to the next provider, cheapest first, within
  `X-Treg-Route-Max-Cost` (default $1 per call). A miss can be billed, which is why a routed tool
  quotes `usd_per_call: null` rather than $0: set the header for bulk runs.
  `X-Treg-Route-Waterfall: 0` stops at the first miss.
- A filter the serving provider could not apply is named in `X-Treg-Ignored-Filters` and
  `_treg.ignored_filters`: filter the rows yourself, or send `X-Treg-Route-Strict-Filters: 1` to be
  refused with an unbilled 422 instead.
- An asynchronous provider is polled for up to 60 seconds. If it is still running you get HTTP 202
  with `_treg.outcome: pending`, its poll descriptor, `reserved_micro` and `charged_micro: null`. Do
  not retry or start another provider: the task may still finish and charge.
- `catalog_get` on a routed id shows the ranked plan and its prices before you spend.

**A found contact is not a confirmed one.** An email or phone find returns the provider's best
match. Only `output.verified: true` means the mailbox was checked; otherwise `_treg.advice` names
the verify step. Run it before anyone sends, and never re-send the same find: every hit is billed,
repeats included.

- Send `X-Treg-Route-Verify: true` on `treg.people.email.find` (or `treg.people.email.find.personal`
  for a personal address from a LinkedIn URL or work email) to verify each hit in the same call, as
  its own small charge. Read `_treg.verification.verdict`: `valid`, `invalid`, `catch_all`, `risky`
  or `unknown`; `checked: false` with a `reason` means no check ran and nothing was charged.
- The same header on `treg.people.phone.find` checks the line live: `live`, `dead` or `unknown`. A
  live line does not prove the number is this person's; for a US number,
  `trestleiq.people.contact.verify` checks the name. A number not written internationally is not
  checked, and `_treg.advice` says how to check it yourself.
- **Verify every address before it is sent to**, including rows from a company or domain search
  (`treg.people.search`, `hunter.companies.emails`): those are directory listings, unconfirmed
  unless the row's own verification field says otherwise. `invalid` is dead, `accept_all` is risky,
  and an address the provider did not return (`info@` after an empty domain search) is a guess, not
  a result.

**Choosing between providers of one job.** `treg catalog get <id>` lists every provider of the same
job with `COST`, `WORKS` (the success rate treg observed, with its sample size), `SPEED` (median)
and `LAST OK`. Work down this order:

1. **The inputs you actually hold.** A tool that wants a `profile_url` is no substitute when you have
   a name and a domain, whatever it costs. This outranks price every time.
2. **Reliability.** A high `WORKS` on a real sample beats a rounder number on a tiny one: `99% (121)`
   is stronger evidence than `100% (8)`.
3. **Price.** Spreads inside one job reach 200×, so this is usually where the money is.
4. **`LAST OK`** breaks ties. A bare age means a real call came back; a `✓` age is the catalog's own
   verification stamp, not live traffic; `—` means nobody has verified or called it.

After a 429, a 5xx or a timeout, try the next provider: you know its parameters, so you can build its
request. Say which one you switched to. **Never retry a 4xx with another provider:** it is almost
always your request, and retrying spends the team's money on several providers for one mistake.
treg does not switch providers on a direct call; only you know which inputs you hold.

**More on searching.**

- Search for the job and for any output you need: `treg catalog search "email verification
  catch-all"` finds different tools from a broad email-verification search. When you need a
  provider-specific field (catch-all, disposable, role address, SMTP), call that provider's tool:
  the routed `treg.people.email.verify` defines only `valid`, `status` and `score`.
- Discovery jobs usually come in two shapes: structured (filters such as title, location, followers,
  funding) and semantic (describe what you want; the `exa.*` tools). When a brief mixes hard limits
  with a fuzzy niche, run both and merge: creators = `influencersclub.creators.search` with
  `exa.creators.search` pinned to the platform's domain; people = a `people.search` provider with
  `exa.people.search`; who holds a role = `exa.web.answer`.
- A tool marked `strict_query: true` accepts only its declared query parameters, once each, with
  required fields and allowed values enforced, and no body. Rejected input is not forwarded.

**How a catalog call is served**, in this order:

1. the team registered its own tool for that provider: that tool and its key;
2. the team stored a key for the provider: that key;
3. neither, and the tool has a verified public route: no key, free;
4. otherwise treg's own account, billed to the team's balance.

The team's own key always wins, so connecting a key it already pays for takes those calls off the
balance. A free public route stays free only while you send no provider credential header yourself.
A tool with no published price is refused, not served free: connect the team's own key. A few
providers that sell only subscriptions carry a per-call rate treg set; `catalog get` says whose
price it is. A team admin can pin a job to one provider (`treg org pin`), and then calls to the
job's other providers are refused.

**Calling well.**

- **A JSON body from a file:** `treg call <id> --data @body.json` (`@-` reads stdin). On Windows
  PowerShell prefer it to inline JSON, whose inner quotes PowerShell strips.
- **Scripting many calls:** use `treg --json call …`. Stdout is then one line,
  `{"result": <provider body>, "_treg": {"http_status", "call_id", "charged_micro"}}`, and nothing
  goes to stderr, so a script that merges the streams still parses every answer. Run a handful and
  check the parsed results before looping over the whole list: a parse bug throws away answers that
  were already paid for.
- **Calling in parallel is fine.** There is no per-token or per-team concurrency limit; provider
  rate limits are relayed as they are.
- **Over raw HTTP**, if a body carrying SQL or HTML is refused with an HTML 403 page by the hosting
  edge before treg sees it, base64-encode the body and add `X-Treg-Body-Encoding: base64` (also
  `gzip` or `base64+gzip`). The CLI does this by itself.

## Money

**Call at the listed price without asking, then report what it cost.** Prices are visible before a
call (`treg catalog get`, MCP `catalog_get`) and most are fractions of a cent. Before a generation
task or a loop over many inputs, tell the human the expected total and cap it with
`X-Treg-Route-Max-Cost`.

- **Read the `cost` block, not only the headline.** `per_call` is flat. `type: per_result` with
  `unit: row` is priced per returned row, and its `~$/call` figure assumes a 20-row page (your
  `limit` sets the real count). `unit: target`, `domain` or `keyword` is priced per thing you ask
  about: one unit per target whatever the page size, so a body listing ten domains reserves ten.
- **What you were charged** is the response header `X-Treg-Cost-Micro` (integer micro-USD; `cost_usd`
  over MCP), never anything in the provider's body. Keep `X-Treg-Call-Id` with it: it is the id to
  quote, and `GET https://treg.to/calls/<call id>/result` returns the stored answer of a successful catalog
  call you lost. The CLI prints `treg: charged $<usd> · call id <id>` on stderr; stdout is always
  the exact body.
- **A hard ceiling:** `X-Treg-Route-Max-Cost: <usd>` refuses a call whose reservation would exceed
  it (402 `route_max_cost`, nothing charged). Direct calls have no default; on a routed call it also
  bounds the whole waterfall, $1 by default.
- **Failed calls are free.** A 4xx or 5xx relayed from the provider costs nothing. Empty results mean
  whatever the provider means by them; treg relays and does not normalise. A provider that reports
  a failure inside a 2xx (a DataForSEO task status other than 20000) is flagged with
  `X-Treg-Upstream-Failed` (`upstream_failed` over MCP): the body has no result, and the charge is
  what the provider billed for the attempt.
- **Repeat questions are cheaper.** A call may be answered from treg's archive of the exact same
  question while that answer is fresh: the provider's bytes, with `X-Treg-Cache: hit`,
  `X-Treg-Fetched-At` and `X-Treg-Age`. The team's first call on a question costs full price; from
  the second on, a hit costs 10%, and a hit on the team's own key is free. `Cache-Control: no-cache`
  forces a live call; `X-Treg-Max-Age: <seconds>` accepts only a younger answer. A team admin can
  turn the archive off for the team (`treg org archive off`).
- **When treg's own account for a provider runs out**, it may serve the same tool through a relay
  account it owns: same request, same answer shape, the relay's real price, and
  `X-Treg-Served-Via: overflow:<name>` (`served_via` over MCP). `catalog_get` shows that price
  up front as `overflow_price_usd`; a "free" tool with one may bill exactly that, so quote it. The
  team's own keys are never relayed; a team opts out with `treg org overflow off`.
- `treg balance` shows the balance and recent charges, and the dashboard's Activity page shows the
  settled cost of every call. `treg topup` adds funds or sets up automatic top-ups.

## When a call fails

treg's own refusals carry the header `X-Treg-Error: 1` (`whose_error: treg` over MCP); without it,
the status and body are the provider's. The CLI names whose answer it was on stderr. A bare 5xx with
no JSON `detail` is never treg's.

| you see | charged | what to do |
|---|---|---|
| a 4xx from the provider | no | Fix your request. Do not retry it with another provider. |
| a 429, 5xx or timeout from the provider | no | Try the next provider of the job, and say which. |
| 402 `insufficient_balance` | no | The body has `balance_micro`, `estimated_cost_micro`, `topup_url`. Check `treg balance`, then top up (`treg topup`, or the dashboard's Team → Billing) or connect the team's own key for that provider. |
| 402 `route_max_cost` | no | Your `X-Treg-Route-Max-Cost` refused it. Ask for fewer rows or targets, or raise the ceiling. |
| 429 `share_busy` | no | treg's slots with that provider are all in use. Retry after `retry_after_s`, or call the routed tool, which moves on by itself. |
| 429 naming a daily allowance | no | A free trial pool on treg's key is used up for today. Connect the team's own key. |
| 503 `provider_capacity_unavailable` | no | treg's own account for that provider is out. Take one of the body's `alternatives`, use the team's own key, or retry after `resets_at` or a minute. |
| 503 `provider_paused` | no | This registry paused that provider; read `message`. Use another provider. Do not reconnect: the team's connection is kept. |
| 503 with `treg_saturated: true` | no | Wait for `Retry-After`, then send the same request again. |
| `X-Treg-Capacity: exhausted` on any status | not when `X-Treg-Cost-Micro` is `0` | treg's own account for that tool refused for want of credit. Not your balance; try another tool. |
| 502 `response_buffer_limit` | no | The answer is over 8 MiB; narrow the request. If the message says it is temporary, treg is busy: retry shortly. |
| 426 | no | The CLI is too old for this registry: `treg update`. |
| 409, 410 with an `idempotency_*` error | see below | See "Retrying without paying twice". |

## Retrying without paying twice

Most retries need nothing: a failed call, a 5xx, a timeout and a network error were never billed.
The case that needs a key is narrower: the provider answered and charged, and the answer was lost on
its way to you. Repeat that call with the same `Idempotency-Key` header (`idempotency_key` over MCP).
treg returns the stored answer, does not call the provider again and charges nothing; the reply
carries `X-Treg-Idempotent-Replay: true` (`replayed: true` over MCP), `X-Treg-Cost-Micro: 0`, and the
first call's charge in `X-Treg-Original-Cost-Micro`.

- Use a new key, or none, for new work, even with the same parameters: asking again to see what
  changed is a new call, and the old key would hand back the old answer. Reusing a key for a
  different request is refused (422).
- Keys belong to the caller and last 24 hours; a call that fails frees its key at once.
- 409: the original call is still running; retry shortly.
- 410 `idempotency_response_lost`: it was charged but its answer was not kept. Try
  `GET https://treg.to/calls/<call id>/result`, or call again with a new key.
- 410 `idempotency_response_too_large`: it was charged and you already received the full answer, but
  it was over the 2 MB replay limit and was not kept. A new key calls again.
- 410 `idempotency_outcome_unknown`: its outcome was never recorded and it may have reached the
  provider. `GET https://treg.to/calls/<call id>` shows the cost; a new key calls again.

## Generate video, images, voice or music

Generation models are in the catalog under the `video-gen`, `image-gen`, `voice-gen` and `music-gen`
platforms, one row per model per route, so one model on two routes sits beside itself with both
prices. Models are not interchangeable: you pick one, treg does not.

```bash
treg catalog search "text to video"                  # every model, with prices
treg catalog get minimax.video-gen.h3.generate       # native parameters, model enum, price table
treg call minimax.video-gen.h3.generate --await --timeout 900 --data '{"model":"MiniMax-H3-Max",
  "content":[{"type":"text","text":"A paper boat drifts across a quiet pond at sunrise."}],
  "resolution":"480P","duration":5,"ratio":"16:9"}'
treg call minimax.voice-gen.voices.list --data '{"voice_type":"system"}'
treg call minimax.voice-gen.speech-2-8-turbo --data '{"model":"speech-2.8-turbo",
  "text":"A calm voice can make a complex idea feel simple.","stream":false,"output_format":"url",
  "voice_setting":{"voice_id":"English_expressive_narrator","speed":1,"vol":1,"pitch":0}}'
```

- **Video and image generation is an asynchronous task.** The submission returns a task id at once
  and reserves the price; it is charged only when the task succeeds, and a failed or moderated task
  refunds the whole hold. `--await` polls until it finishes and prints only the final response on
  stdout; stderr carries the task id, a resumable `treg call …` command (Ctrl-C loses the wait,
  never the task or the money), progress and the result URL. Exit 0 is done, 2 means the provider
  failed the task, 3 a timeout (resume with the printed command).
- **CLI agents: raise your shell tool's timeout or run the call in the background.** A video takes
  1-5 minutes, and a default 2-minute command limit cuts the wait off.
- **MCP and raw HTTP:** the response header `X-Treg-Async` is the descriptor: where to poll, which
  statuses are final, where the result is. Wait about 60 s before the first check, then every
  30-60 s; three to six checks per video is normal. Do other work in between.
- **Some asynchronous tools usually answer at once** (an email verification): a first response that
  is already finished is the answer and is charged as one. If it says it is still working, poll no
  faster than the descriptor's `interval` and stop at its `max_age`; a task with no answer by then
  is refunded.
- **Result URLs expire** (the descriptor's `ttl_note`; MiniMax's last about 9 hours). Download
  promptly: treg never stores generated media. On some routes the file needs one more call, and
  `--await` prints that command.
- **Reference media** (a face, a voice clip, a first frame) must be at a public URL the vendor can
  fetch. Paste hosts fail vendor probes at random; `treg host face.jpg` prints a public URL (30 MB,
  7 days, free) to put in `image_urls` or `audio_urls`.
- **Voice.** MiniMax speech is synchronous: with `stream:false` and `output_format:"url"` it returns
  an audio URL valid for 24 hours; `minimax.voice-gen.voices.list` lists valid voice ids. Fish Audio
  (`fishaudio.tts.s2-1-pro`) returns binary audio: redirect stdout to a file, or use MCP
  `call_media`. `fishaudio.voices.discover` finds voices to pass as `reference_id`; voices the team
  created are listed by `treg resources list --provider fishaudio --kind voice` (MCP
  `resources_list`).
- A tool whose answer inlines media as base64 (Google AI's images, speech and Lyria music) may answer
  up to 64 MiB and is charged from the provider's reported usage. Its answer is never replayed: an
  `Idempotency-Key` retry generates, and bills, a new one.
- Parameters are the provider's own, verbatim. Nothing is translated between providers.

## Your own tools

**Start from what the team registered, then use each API exactly as its own docs say.**

```bash
treg tool ls                                  # the team's tools: names, hosts, which credential goes where
treg call intercom conversations?per_page=5   # <tool-name> and the upstream path
```

Over HTTP that is `https://treg.to/call/<tool-name>/<path>`, or the upstream URL behind the prefix,
`https://treg.to/call/https://api.intercom.io/me`, with `X-Treg-Token: <token>`. A personal sign-in token
also sends `X-Treg-Org: <team-slug>`; a team key does not need it. treg finds the tool, injects the
team's credential on the server, and relays method, query, headers and body unchanged; your
`X-Treg-Token` is removed before the provider sees the request. `GET https://treg.to/tools` lists the tools
as JSON.

**Command-line tools** (stripe, gh, vercel, gcloud and others) run with the team's credential and
without you logging in:

- `treg <command>` is the simplest: `treg claude`, `treg node server.js`, `treg with -- npm test`
  run that one command with the team's credentials, and nothing else changes. Calls to a registered
  host are credentialed by the server; every other address goes out untouched. Node before version
  24 ignores proxy settings in its built-in `fetch`, so such a call goes out with no credential: use
  a client that reads the environment (axios, got, undici `ProxyAgent`) or Node 24+. curl, git,
  Python requests and httpx, and Deno are covered at any version.
- `treg run stripe -- balance retrieve` runs one vendor CLI. `--local` (the default) runs it on this
  machine, isolated under a dedicated user once an admin has run `sudo treg setup-local-run`;
  `--server` runs it on the registry so the key never reaches the machine.
- `treg shell start` opens a subshell where registered CLIs get their credentials by themselves;
  `--proxy` also credentials HTTPS calls your own code makes to a registered API. `treg serve` runs
  the same proxy as a background service. A certificate-pinned client refuses the interception: use
  `treg run` or `treg call` for it.

[Import and shell](https://treg.to/tutorial-import-shell.md) has the full walkthrough.

**Connecting a key for catalog calls.** `treg connections connect --provider <provider>` connects a
provider the catalog knows: a consent screen for OAuth providers; for key-based ones a hidden
prompt, or the key piped with `--key-stdin`. treg verifies it first. From then on the team's calls
to that provider use its key and are never billed.

## Share keys and skills with the team

**Bulk is fastest.** In the directory the human names, `treg upload` lists the provider keys it
recognises in `.env` and the skills in the subfolders, and registers only the ones picked
(`treg upload env|skills --dir <dir>` restricts it; `treg scan` previews and sends nothing). It reads
variable names, and loads a value only for a key that is picked. Without a terminal it refuses
unless you pass `--select a,b` or `--all`, so nothing is imported by accident.

**Wrap a new key in a skill.** Registering a key, endpoint or CLI, pair it with a skill, so the
credential, the tool and the recipe land together. If none exists, write a basic one: a SKILL.md
with frontmatter (agents find skills by it) and one example call.

```bash
mkdir -p ./posthog && cat > ./posthog/SKILL.md <<'MD'
---
name: posthog
description: Query the PostHog analytics API through treg; the key is injected on the server. Use for events, insights and project queries.
---
Call it: `treg call posthog api/projects/@current` (upstream: https://us.posthog.com)
MD
treg skill init --dir ./posthog   # drafts treg.json (base URL, the secrets); review it and add the key
treg skill add --dir ./posthog    # registers the recipe, the secret and the tool together
```

**A bare tool, only when a recipe adds nothing.** Never store a key without binding it to a tool.

```bash
treg secret add posthog-key --value "$POSTHOG_API_KEY"   # or --file ./.secret/token.json
treg tool add posthog --base-url https://us.posthog.com --secret posthog-key
# a key that goes in the query string instead of a header:
treg tool add serpapi --base-url https://serpapi.com --secret <name-or-id> \
  --auth-in query --auth-name api_key --auth-format '{secret}'
```

**A whole skill with several credentials:** `treg skill scaffold ~/.claude/skills/google-ads --out
gads.json` captures SKILL.md as the recipe and every `.secret/*` file as a secret; complete each
binding in `gads.json` (google-ads needs `Authorization: Bearer {access_token}` from an `oauth`
secret and `developer-token: {secret}` from an `env` one), then `treg skill push gads.json`.

A teammate's agent then calls the tool without ever holding the credential, and each call is logged
against its own token. `treg skill install <name>` (or `--all`) writes a shared skill's recipe into
`.claude/skills/<name>/SKILL.md`.

**Credential shapes**, one per binding: `env` (a plain string), `secret_file` (a JSON file, of which
`secret_field` is injected), `oauth` (a JSON token, refreshed automatically) and `cli_auth` (a token
the human copied from a CLI they are signed into). Every binding of a tool applies to every call.
An oauth secret carrying `refresh_token`, `client_id` and `client_secret` is refreshed before it
expires; a bare token is injected as it is and re-uploaded when it expires. To get a first OAuth
token, either run the flow yourself and `treg secret add gsc --file token.json --kind oauth`, or let
treg host it: `treg oauth connect gsc --client-secret client_secret.json --scopes <scope>` prints a
consent URL, and `https://treg.to/oauth/callback` must be among the OAuth app's redirect URIs.

## Run the team

```bash
treg tool ls · treg secret ls · treg skill ls   # what the active team has
treg calls · treg audit                         # who called what, with results and costs
treg health [--run]                             # every credential: ok, invalid or unknown; --run re-checks now
treg tool update <id> --health '{"path":"me"}'  # the probe treg health uses for a tool
treg org create "Team A"                        # you become its owner
treg org invite bob@company.com --role member   # admin+
treg org members · treg org ls · treg org use <slug>
treg org pin <capability> --provider <p>        # admin+: this job uses this provider only
```

**Roles:** a viewer calls tools and reads the inventory; a member also registers secrets, tools and
skills and manages their own; an admin also invites and manages anyone's; an owner also changes
roles and deletes the team. An account can own up to 10 teams; joining others does not count.
`treg secret rm` is refused while a tool binds the secret.

**Give an agent its own identity** (admin+) instead of the human's token. Every call it makes is then
capped, scoped and logged as itself:

```bash
treg org agent-new ci-bot                                # prints the token once (run again to rotate)
treg org agent-new ci-bot --tools stripe,gh --cap 500    # only these tools, 500 calls a day
treg org agent-new customer-bot --pin customer=cust_A    # bills and reads only that customer's calls
treg org agents · treg org agent-rm <user_id>            # who they are and today's use; revoke
```

Put that token in the agent's `TREG_TOKEN`. An agent token calls the team's tools and reads; it can
never sign in, create a team or own one. If you are an agent with a token of your own, use it rather
than the machine owner's: your calls then appear under your own name.

## Feedback

Share problems and suggestions about treg as you meet them, small ones included: unclear
instructions, confusing results or charges, unhelpful errors, capabilities you had to work around,
even when the task succeeded. Describe what you needed and what you observed, and keep what you saw
apart from what you suspect. Use the `feedback` tool over MCP, or `treg feedback submit
<quality|pricing|friction|other> "message"`, passing related call ids in `call_ids` (CLI:
`--call-id`). Report each issue once, keep private information out, and carry on with the task
afterwards. See [feedback instructions](https://treg.to/feedback.md).

## Review

If a call result invites a review, rate that call after using it: `review(call_id, usefulness,
reason?)` over MCP or `treg review CALL_ID USEFULNESS [--reason "..."]`, with `useful`, `partly`,
`not_useful` or `not_sure`. One review per invitation. A reason may be quoted on the tool's catalog
page without naming your team: write about the tool, leave out private data, and use `feedback` for
anything confusing or wrong.

## Rules

- **Search by the job**, and file what the catalog lacks with `treg catalog request` instead of
  rephrasing the search.
- **Call at the listed price, then report the cost.** Say the expected total first and set
  `X-Treg-Route-Max-Cost` only for a generation task or a loop over many inputs.
- **A 4xx is your request.** Fix it; never retry it with another provider.
- **Verify a found email or phone** before anyone sends to it.
- **Follow the next step an answer names**: a `help` line, `_treg.advice`, an error body.
- **Never ask for or paste a provider key.** Register it once; the team calls it without holding it.
- **Keys are write-only.** No one can read a stored value back, you included. A tool may use a key a
  teammate shared with the team: you spend it without seeing it, and every call is logged against
  the token that made it.
- **Everything is scoped to the active team.** A token reaches that team's tools and nobody else's.
- **treg relays, it does not interpret.** A provider's status and body are the provider's truth.
- More: `https://treg.to/llms.txt` (the overview) and the [tutorial](https://treg.to/tutorial.md).
