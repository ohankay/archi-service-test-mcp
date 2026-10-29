# Phase 2: generic, organization-independent platform

Status: **not started** - deferred by the project owner. Start only when the owner says "faz 2'ye geçelim" (let's
move to phase 2); then bring up the items below and agree on the order before changing code.

Scope: only making the project generic and independent of any organization, so that other APIs/services can be
added with definitions alone and other people or companies can adopt it. Nothing else belongs to this phase.

## 1. Definition layer for any API (today it fits JSON-over-POST services with a shared envelope)

| Gap | Today | Goal |
|---|---|---|
| Path parameters (`/accounts/{id}`) | the operation path is fixed; inputs never go into the path | input location `in: path` |
| Query parameters (`?from=...`) | inputs only go into the JSON body; GET requests are sent with a body | `in: query`, no body on GET |
| Per-operation headers | only system-wide `request.headers` | `in: header` and operation `headers` |
| Body formats | JSON only | form / raw text as well |
| SOAP services | only in the expert layer (`services.yaml`, `soap_call`) | `protocol: soap` in system definitions (the SOAP engine exists, connect it) |
| Authentication | only "login steps + token in a header"; windows / ntlm / basic / api_key only in the expert layer | `auth:` in the system definition: bearer, basic, api_key, oauth2 client credentials, ntlm, windows |
| APIs without login | works, but every operation needs `session: false` (default is "session required") | no login defined -> no session and no customer needed |
| Importing operations | traffic logs (app TransactionLog, HAR) | `import_openapi` from an OpenAPI / Swagger document |
| Log formats | the mobile app TransactionLog and HAR | a documented reader interface for new formats |
| Masking | Turkish rules built in (TCKN, TR IBAN, TR mobile numbers) | locale rules configurable, Turkish set as one option |
| Business vocabulary | "customer" (test identity) and its lock are banking-flavoured | neutral "test identity", banking as one example |

Plan from the assessment: these are additions to the existing structure, not a rewrite. Verify each step against a
real API the owner provides (a Swagger file or a HAR log).

## 2. Organization-independent public release

- Remove everything specific to the organization from the public version:
  - the prep address (`mobileprep.kuveytturk.com.tr`), device and user ids (sample system definition,
    `tests/legacy_definitions`),
  - the internal API surface: `BOA.Integration...` service paths, method names, envelope fields - in the sample
    definitions, `examples/mobile`, the mobile demo service (`demo_mobile.py`), tests and docs,
  - the organization's name in README and docs.
- Replace them with a fictional "demo bank": one login-based JSON-RPC style system and one standard REST API
  (path/query parameters, OAuth), so the samples show both styles.
- Publish from a **new repository without history** - the current history contains the internal details.
- The organization's own definitions stay private (this repo or a `company-definitions` folder installed by
  `install.cmd`).
- Whether the internal API names may be published at all is the organization's security / legal decision; the
  default is to leave them out completely.
- Public docs in English (the user-facing Turkish docs stay in the private repo).
