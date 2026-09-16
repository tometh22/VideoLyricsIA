# Adobe Firefly Services integration

## Objective

Use Adobe Firefly as the only generative image/video provider for customers
whose policy requires Adobe's enterprise environment. GenLy owns the Adobe
organization, credentials, usage, billing, and operational support. A tenant
configured for Firefly must fail closed and must never fall back to Veo,
Imagen, or another generative provider.

## What can be automated

GenLy can automate the complete technical flow:

1. obtain and cache OAuth server-to-server access tokens;
2. submit five-second Firefly video generations;
3. poll asynchronous jobs and maintain the GenLy job heartbeat;
4. download the result without exposing pre-signed URLs;
5. hash and retain the original Firefly asset;
6. validate the clip and compose the lyric video;
7. record provider job ID, model version, inputs, outputs, lineage, and cost;
8. enforce tenant-specific provider policy and cost ceilings;
9. reconcile Adobe Operations against invoices;
10. produce a customer-facing provenance export.

Adobe credentials, purchasing, acceptance of Adobe terms, and the customer's
Legal approval remain human-controlled.

## Adobe prerequisites

Ask Adobe to provision Firefly Services with Generate Video API access and
provide:

- the contracted price per 1,000 Operations;
- the Operations consumed by one five-second video at 540p, 720p, and 1080p;
- minimum commitment, overage pricing, and invoice/reporting format;
- a production rate limit above the documented default of 4 requests/minute;
- the applicable IP indemnity and C2PA/Content Credentials terms;
- data processing, retention, and residency terms for European customers.

An Adobe organization administrator must create an OAuth Server-to-Server
project and assign the Firefly Services product profile. Store only these
runtime secrets in the deployment secret manager:

```text
FIREFLY_SERVICES_CLIENT_ID
FIREFLY_SERVICES_CLIENT_SECRET
```

Do not put secrets or access tokens in Git, provenance, logs, support tickets,
or customer documentation.

## Implementation phases

### Phase 1: provider client

`firefly_client.py` implements Adobe IMS authentication, video submission,
polling, cancellation, atomic download, SHA-256 hashing, and safe result
metadata. Its tests use simulated HTTP responses and make no paid calls.

### Phase 2: GenLy provider boundary

Extract the common video-provider boundary from the current Veo-specific
pipeline. Preserve the existing prompt policy, cache rules, content validator,
heartbeat, retry behavior, and delivery guardrails.

Tenant routing should use an explicit allowlist, for example:

```text
VIDEO_PROVIDER_DEFAULT=veo
VIDEO_PROVIDER_FIREFLY_TENANTS=universal_spain
```

The persisted provider selection for a job is authoritative. Environment
changes after job creation must not silently change the provider during an
edit, retry, or scene regeneration.

### Phase 3: fail-closed policy

For a Firefly-only tenant:

- missing Adobe credentials blocks generation before any provider call;
- entitlement, quota, moderation, or generation failures remain actionable;
- automatic fallback to another AI provider is forbidden;
- retrying an ambiguous submission is forbidden because it may double-charge;
- a non-generative local fallback may only be offered if the customer approves
  that product behavior explicitly.

### Phase 4: provenance and costs

Persist structured data for each Adobe operation:

- Adobe provider job ID and terminal status;
- API endpoint and `video1_standard` model version;
- prompt hash, dimensions, seed, and camera settings;
- input asset hashes for image-to-video;
- original output SHA-256, byte size, and private storage key;
- C2PA manifest/verification result where available;
- parent-child lineage from Firefly clip to final lyric video;
- retries, failures, and explicit fallback decision;
- Operations reserved/consumed, rate-card version, modeled cost, and invoiced
  cost.

Never persist client secrets, bearer tokens, or complete pre-signed output
URLs.

### Phase 5: validation and customer handoff

1. Run one non-production 16:9 generation per supported resolution.
2. Compare visual quality and prompt adherence with the existing baseline.
3. Generate a complete three-scene lyric video under a test tenant.
4. Verify no Google visual-generation call appears in provenance.
5. Verify retry, timeout, deletion, and quota-exhaustion paths.
6. Reconcile recorded Operations with Adobe Admin Console.
7. Provide the customer with the provider/model statement and one sanitized
   provenance example before reopening the pilot.

## Delivery estimate

Once working Adobe credentials are available:

- provider smoke test: 1-2 working days;
- pipeline routing and fail-closed tenant policy: 3-5 working days;
- provenance, cost reconciliation, and compliance export: 2-4 working days;
- internal end-to-end QA: 1-2 working days.

The expected engineering window is one to two weeks. Adobe contracting and
entitlement provisioning are outside this estimate.
