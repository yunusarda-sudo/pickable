# DNS-AID operator setup for solveny.dev

DNS for AI Discovery (DNS-AID) records are published in DNS, not in this
repository. This file is the operator runbook; nothing here takes effect until
the records below are added at the authoritative DNS provider and DNSSEC is
enabled for the zone.

- Draft: https://datatracker.ietf.org/doc/draft-mozleywilliams-dnsop-dnsaid/
- Record types: https://www.rfc-editor.org/rfc/rfc9460 (SVCB/HTTPS)

## What to publish

Publish ServiceMode records under the `_agents` namespace. `_index` points at
the real HTTPS service. Do not publish `_a2a` until a real A2A interface
exists (`/.well-known/agent-card.json` is intentionally 404 on this service).

```dns
; BIND-style snippet. Adjust TTL to your provider defaults.
; Apex HTTPS hint for the existing web+MCP origin.
solveny.dev.                    3600 IN HTTPS 1 . alpn="h2,h3"

; DNS-AID discovery entrypoint for the MCP Streamable HTTP endpoint.
; key65280/key65281 are experimental SvcParamKeys used until DNS-AID
; parameter names are registered; keep the numeric keyNNNNN form.
_index._agents.solveny.dev.     3600 IN SVCB 1 solveny.dev. alpn="h2,h3" port=443 mandatory=alpn,port key65280="endpoint=/mcp" key65281="transport=streamable-http"
```

Cloudflare DNS dashboard equivalent (DNS > Add record):

- Type: HTTPS, Name: `@`, Target: `.`, Priority: `1`, ALPN: `h2,h3`
- Type: SVCB, Name: `_index._agents`, Target: `solveny.dev.`, Priority: `1`,
  Port: `443`, ALPN: `h2,h3`, Custom parameters:
  `key65280="endpoint=/mcp"`, `key65281="transport=streamable-http"`

## DNSSEC

1. At the DNS provider, enable DNSSEC signing for `solveny.dev`.
2. Publish the DS record at the registrar (`.dev` requires DNSSEC via the
   registrar; Cloudflare Registrar does this automatically when the domain
   uses Cloudflare DNS).
3. Verify the chain with `dig +dnssec` or a validating resolver before
   announcing the records.

## Validate (after DNS is published)

DNS-AID is checked over DNS-over-HTTPS (Cloudflare
`https://cloudflare-dns.com/dns-query`, fallback Google
`https://dns.google/resolve`):

```bash
# SVCB record check
curl -s 'https://cloudflare-dns.com/dns-query?name=_index._agents.solveny.dev&type=64' \
  -H 'accept: application/dns-json' | python3 -m json.tool
```

Then rescan:

```http
POST https://isitagentready.com/api/scan
Content-Type: application/json

{"url": "https://solveny.dev"}
```

`checks.discoverability.dnsAid.status` should be `"pass"`. Until the operator
completes the steps above, that check is expected to fail; no code change in
this repository can substitute for the DNS deployment.
