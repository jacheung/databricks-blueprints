# AI Gateway Auth Patterns

Two authentication patterns for accessing Databricks AI Gateway from external applications.

---

## The two routes

![Route 1 (M2M): the app reads SP credentials from a secret store, gets a client_credentials token, and calls AI Gateway as the SP. Route 2 (OBO): the user signs in to an IdP, the app exchanges the user's ID token plus SP credentials for a token carrying the user's identity, and calls AI Gateway as the user](docs/auth_routes.svg)

Each number in the diagram is a row in the table below.

| Phase | Route 1: Pure SP (M2M) | Route 2: OBO via SP |
|-------|------------------------|---------------------|
| ① User sign-in | — | User signs in to an external IdP (Google Sign-In, Okta, IAP, …), which returns an **ID token** (JWT with an `email` claim) |
| ② Request to app | User/Agent calls the app | User calls the app with the ID token (header, cookie, or redirect callback) |
| ③ SP credentials | App reads `client_id` + `client_secret` from a secret store | Same |
| ④ Get a Databricks token | `POST /oidc/v1/token`, `grant_type=client_credentials` → token as **the SP** | `POST /oidc/v1/token`, token exchange: SP credentials authenticate the call, the user's JWT is the `subject_token` → token as **the user** |
| ⑤ Call AI Gateway | Runs as the SP | Runs as the user |
| Rate limits, audit, cost | Shared: one identity for every caller | Per user |
| Endpoint permission needed by | The SP | The user (the SP's permissions don't matter) |
| Extra setup | — | IdP registered in the Account Console; SP allowed to perform token exchange; each user exists in Databricks with the same email (see below) |
| Use when | Simplest to implement; per-user attribution isn't needed | You need per-user limits, audit, or cost attribution |

The token exchange in ④ (full example in `02_sp_obo_auth_patterns`):

```python
requests.post(
    f"{WORKSPACE_URL}/oidc/v1/token",
    data={
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": user_id_token,  # the user's JWT from the IdP
        "subject_token_type": "urn:ietf:params:oauth:token-type:id_token",
        "scope": "all-apis",
    },
    auth=(SP_CLIENT_ID, SP_CLIENT_SECRET),  # HTTP Basic: the SP authenticates the exchange
)
```

---

## Identity Resolution in OBO

Databricks resolves the user via **exact email match**:

1. Extracts `email` claim from the user's JWT
2. Looks up a Databricks user with that exact email
3. Match → issues token as that user | No match → exchange fails

Example: If the JWT contains `email: jon.cheung@databricks.com`, there must be a Databricks user with exactly that email.

---

## Notebooks in This Folder

| Notebook | Description |
|----------|-------------|
| `01_sp_secrets` | SP credential management and token retrieval patterns |
| `02_sp_obo_auth_patterns` | OBO token exchange implementation |
| `02b_google_adk_via_sp` | Google ADK agent accessing AI Gateway via SP |
