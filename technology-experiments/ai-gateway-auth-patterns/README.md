# AI Gateway Auth Patterns

Two authentication patterns for accessing Databricks AI Gateway from external applications.

---

## Route 1: Pure Service Principal (M2M)

The application authenticates as the SP itself. All requests appear as a single identity.

```
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│  ┌──────────┐       ┌──────────────────┐       ┌────────────────┐  │
│  │          │       │                  │       │                │  │
│  │  Secret  │──2───▶│   Application    │──3───▶│   Databricks   │  │
│  │  Store   │       │   (Cloud Run,    │       │   /oidc/v1/    │  │
│  │          │◀──────│    Lambda, etc.) │◀──4───│   token        │  │
│  └──────────┘       │                  │       │                │  │
│                     │                  │       └────────────────┘  │
│                     │                  │                           │
│                     │                  │──5───▶┌────────────────┐  │
│                     │                  │       │  AI Gateway    │  │
│                     │                  │◀──6───│  (as SP)       │  │
│                     └──────────────────┘       └────────────────┘  │
│                              ▲                                     │
│                              │1                                    │
│                     ┌────────┴─────────┐                           │
│                     │   User / Agent   │                           │
│                     └──────────────────┘                           │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

| Step | Action |
|------|--------|
| 1 | User/Agent sends request to the application |
| 2 | App retrieves SP credentials (`client_id` + `client_secret`) from secret store |
| 3 | App requests token: `grant_type=client_credentials` |
| 4 | Databricks returns token with **SP identity** |
| 5 | App queries AI Gateway using SP token |
| 6 | Response returned — audit logs attribute request to **the SP** |

**Characteristics:**
- All users share one identity (the SP)
- Rate limits applied to the SP as a whole
- No per-user audit trail
- Simplest to implement

---

## Route 2: On-Behalf-Of (OBO) via Service Principal

The application authenticates users via an external IdP, then exchanges their token for a Databricks token carrying the **user's** identity.

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│                                                                                  │
│                            ┌──────────────┐                                      │
│                            │ Secret Store │                                      │
│                            └──────┬───────┘                                      │
│                                   │4                                             │
│                                   ▼                                              │
│  ┌──────────────┐       ┌──────────────────┐       ┌────────────────────────┐    │
│  │              │       │                  │       │                        │    │
│  │  IdP         │       │   Application    │──5───▶│   Databricks           │    │
│  │  (e.g., GCP, │       │   (Cloud Run,    │       │   /oidc/v1/token       │    │
│  │   Okta, etc.)│       │    Lambda, etc.) │◀──6───│                        │    │
│  │              │       │                  │       │   Validates:           │    │
│  └──────────────┘       │                  │       │   - SP credentials ✓   │    │
│        ▲   │            │                  │       │   - User JWT email     │    │
│       1│   │2           │                  │       │     → Databricks user ✓│    │
│        │   ▼            │                  │       └────────────────────────┘    │
│  ┌──────────────┐       │                  │                                     │
│  │              │──3───▶│                  │──7───▶┌────────────────────────┐    │
│  │  User        │       │                  │       │  AI Gateway            │    │
│  │              │◀──9───│                  │◀──8───│  (as USER)             │    │
│  └──────────────┘       └──────────────────┘       └────────────────────────┘    │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘
```

| Step | Action |
|------|--------|
| 1 | User authenticates with external IdP (e.g., Google Sign-In, Okta, IAP) |
| 2 | IdP returns **user's ID token** (JWT with `email` claim) to the user |
| 3 | User sends request to App, ID token included (via header, cookie, or redirect callback) |
| 4 | App retrieves SP credentials (`client_id` + `client_secret`) from secret store |
| 5 | App calls Databricks token endpoint with: | 
|   | - `grant_type=urn:ietf:params:oauth:grant-type:token-exchange` |
|   | - `subject_token=<user's JWT>` |
|   | - `subject_token_type=urn:ietf:params:oauth:token-type:id_token` |
|   | - HTTP Basic Auth: `client_id` / `client_secret` (SP credentials) |
| 6 | Databricks validates both SP + user JWT, returns token with **user identity** |
| 7 | App queries AI Gateway using OBO token |
| 8 | AI Gateway returns response to App |
| 9 | App returns response to User — audit logs attribute request to **the user** |

**Characteristics:**
- Each user has their own identity at the Databricks layer
- Per-user rate limiting in AI Gateway
- Per-user audit trail and cost attribution
- User must have permissions on the endpoint (SP permissions irrelevant for resource access)
- Requires IdP registered in Databricks Account Console

---

## Prerequisites Comparison

| Requirement | Pure SP | OBO via SP |
|-------------|---------|------------|
| Service Principal (client_id/secret) | ✓ | ✓ |
| Secret store for SP credentials | ✓ | ✓ |
| External IdP registered in Account Console | ✗ | ✓ |
| User exists in Databricks (email match) | ✗ | ✓ |
| User authenticates to your app | ✗ | ✓ |
| SP needs endpoint permissions | ✓ | ✗ |
| User needs endpoint permissions | ✗ | ✓ |

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
