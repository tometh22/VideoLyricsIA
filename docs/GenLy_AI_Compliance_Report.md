# GenLy AI — AI Tools Compliance & Transparency Report

**Version:** 1.0  
**Date:** April 2026  
**Classification:** Confidential  

---

## 1. Executive Summary

GenLy AI is a lyric video generation platform that combines AI-assisted background generation with traditional video compositing tools to produce professional lyric videos. This document details our AI tool usage, data protection practices, copyright considerations, content safety measures, and compliance framework.

Our platform is designed with industry-standard compliance in mind, ensuring that:

- All AI tools used are provided by Google through an **enterprise-grade Vertex AI agreement** with contractual data protection guarantees
- AI is used exclusively for **background visual elements**, not for main content generation
- Every AI invocation is **logged with full provenance** for copyright registration and transparency
- All generated content goes through a **mandatory human review and approval workflow** before publication
- **Content validation** automatically scans outputs for prohibited content (people, faces, trademarks)
- Users must be **pre-authorized** before accessing AI generation features

---

## 2. AI Tools & Providers

### 2.1 Tools in Use

GenLy AI exclusively uses Google Cloud services through the **Vertex AI Enterprise API**. No third-party or consumer-grade AI tools are used in production.

| Tool | Provider | Purpose | Enterprise Agreement |
|------|----------|---------|---------------------|
| **Google Veo 3.1** | Google Cloud Vertex AI | Background video generation | Yes — Vertex AI Enterprise API with contractual data protection |
| **Google Imagen 4** | Google Cloud Vertex AI | Background image generation (fallback) | Yes — same Vertex AI agreement |
| **Google Gemini 2.5 Flash** | Google Cloud Vertex AI | Lyrics-to-visual style mapping; YouTube SEO metadata | Yes — same Vertex AI agreement |
| **OpenAI Whisper** | Self-hosted (local) | Audio transcription | N/A — runs locally, no data leaves our infrastructure |

**Key distinction:** Google Veo is accessed through the **Vertex AI Enterprise API** — Google's enterprise cloud platform with full Terms of Service, Data Processing Amendment (DPA), and contractual commitments. This is not the consumer Google AI Studio product. Our Vertex AI project operates under enterprise-grade SLAs and data governance.

### 2.2 Tools NOT in Use

GenLy AI does **not** use, and has never used, any of the following tools:

- Midjourney
- OpenAI Sora or DALL-E
- Runway
- Hailuo / Minimax
- Stability AI (Stable Diffusion hosted)
- Any tool offered by companies in active litigation with major rights holders

This has been verified through a full codebase audit. Our dependency chain contains zero references to any of these services.

### 2.3 Traditional (Non-AI) Tools

The majority of GenLy's video production pipeline relies on traditional, non-AI tools:

| Tool | Purpose | AI? |
|------|---------|-----|
| **moviepy** | Video compositing, text overlay, timeline assembly | No |
| **FFmpeg** | Video encoding, looping, format conversion | No |
| **ImageMagick** | Text rendering with typography | No |
| **Pillow (PIL)** | Thumbnail generation, image processing | No |
| **Google Fonts** | Typography (SIL OFL license — full commercial use) | No |

---

## 3. How AI is Used — Scope & Limitations

### 3.1 AI Scope: Background Elements Only

AI generation is strictly limited to **background visual elements** — landscapes, abstract scenes, atmospheric effects. This represents a supporting role in the final composition, not the primary content.

The primary content of every GenLy video consists of:

- **Lyrics text** — transcribed (optionally by Whisper, running locally), then **reviewed and edited by a human operator** before use
- **Typography** — rendered with traditional tools (moviepy + ImageMagick) using licensed Google Fonts
- **Audio** — the original MP3 provided by the client, unmodified
- **Composition** — text positioning, timing, animation, and style are determined by human selection and traditional software

### 3.2 What AI Does NOT Do

GenLy AI does **not**:

- Generate people, faces, hands, or human figures (explicitly blocked in all prompts and validated post-generation)
- Create main characters or featured performers
- Perform lip-sync, face-swap, or likeness regeneration
- Generate or modify audio content
- Extend or modify existing footage
- Create full scenes or primary visual content

### 3.3 Human-Provided Background Option

Users can upload their own background assets (video or image) to replace AI generation entirely. When a human-provided background is used, the AI background generation step is skipped completely, and the provenance system records the asset as `human-provided`.

### 3.4 Prompt Safety

All prompts sent to video/image generation APIs include explicit exclusion instructions:

```
"No text, no words, no letters, no people, no faces, no hands, no CGI, no animation."
```

This is enforced at the code level and cannot be overridden by users.

---

## 4. Copyright Considerations

### 4.1 Copyright Status of Elements

GenLy produces videos containing both AI-generated and human-created elements. Per the US Copyright Office guidance (Copyright Registration Guidance, February 2023 and subsequent rulings):

| Element | Created By | Copyright Status |
|---------|-----------|-----------------|
| Background visuals | AI (Veo 3.1 / Imagen 4) | **Must be disclaimed** in US copyright registration — AI-generated from prompt |
| Lyrics text overlay | Human (reviewed/edited) | **Copyrightable** — human creative contribution |
| Typography & font selection | Human | **Copyrightable** — human creative selection |
| Composition & layout | Human (via traditional tools) | **Copyrightable** — human-directed arrangement |
| Audio-visual synchronization | Human | **Copyrightable** — human creative timing |
| Style & aesthetic choices | Human | **Copyrightable** — human creative direction |
| Audio recording | Human (pre-existing) | **Copyrightable** — pre-existing human work |

### 4.2 Provenance Export for Copyright Registration

GenLy provides a machine-readable **provenance export** for every generated video that:

- Lists every AI tool invocation with the exact model, prompt, and timestamp
- Clearly separates AI-generated elements from human-created elements
- Includes a copyright disclaimer with specific elements to disclaim
- Identifies copyrightable human contributions
- Is formatted for use in copyright registration filings

This export is available via API (`/provenance/{job_id}/export`) and through the platform's user interface.

### 4.3 AI Used for Background — Not Full Content

Because AI is used only for background elements (analogous to a landscape or atmospheric effect), while all primary content (lyrics, typography, composition, audio sync) is human-created, the overall work retains significant copyrightable elements. The AI-generated backgrounds must be disclaimed per USCO requirements, but the human-created elements remain fully protectable.

---

## 5. Content Safety & Validation

### 5.1 Three-Layer Protection

GenLy implements three layers of content safety to prevent generation of prohibited content:

**Layer 1 — Prompt-Level Blocking:**  
All generation prompts include explicit instructions to exclude people, faces, hands, text, logos, and trademarks. The system prompt for scene analysis instructs: *"NEVER include people, faces, hands, or text in the prompt."*

**Layer 2 — Automated Output Validation:**  
After generation, frames are extracted from the video at regular intervals and analyzed by Google Gemini Vision to detect:
- People, faces, hands, or body parts
- Text, words, or letters
- Logos, trademarks, or recognizable brand symbols
- Known artist likenesses

If any prohibited content is detected, the job is automatically blocked with status `validation_failed` and cannot proceed to review.

**Layer 3 — Mandatory Human Review:**  
Every job that passes automated validation enters `pending_review` status. A human reviewer must examine the video, short, and thumbnail before approving. Downloads and YouTube publication are blocked until explicit human approval. The reviewer's identity, timestamp, and any notes are recorded in the audit log.

### 5.2 No Likeness Generation

GenLy does not generate, modify, or extend images of real people. There is no face-swap, lip-sync, deepfake, or likeness regeneration capability in the platform.

---

## 6. Data Protection & Training Policy

### 6.1 No Training on Client Data

GenLy uses Google Cloud Vertex AI under enterprise terms. Per Google Cloud's Terms of Service and Data Processing Amendment:

- **Customer data is not used to train Google's foundation models**
- Customer data is processed in real-time for the API request and is not retained beyond the request lifecycle
- There is no opt-in to any Google training programs
- GenLy does **not** perform fine-tuning on any models

### 6.2 Data Minimization

We minimize the data sent to AI APIs:

| API Call | Data Sent | Data NOT Sent |
|----------|-----------|---------------|
| Lyrics analysis (Gemini) | First 600 chars of lyrics, artist name (configurable) | Full audio, full lyrics, user PII, billing data |
| Video generation (Veo) | AI-generated scene prompt only | Audio, lyrics, artist name, user data |
| Image generation (Imagen) | AI-generated scene prompt only | Audio, lyrics, artist name, user data |
| YouTube metadata (Gemini) | Artist name, song name, 300 chars of lyrics | Full audio, full lyrics, user PII |
| Content validation (Gemini Vision) | Extracted video frames (images) | Audio, lyrics, artist name, user data |

**Note:** The artist name can be anonymized in lyrics analysis calls via platform configuration (`SEND_ARTIST_TO_AI=false`), further reducing data exposure.

### 6.3 Data Flow

```
User MP3 → [Local Whisper - no data leaves] → Transcribed lyrics
Lyrics → [Gemini - 600 chars max] → Scene description prompt
Scene prompt → [Veo 3.1 - no lyrics/artist data] → Background video
Background + Lyrics → [moviepy/ffmpeg - local only] → Final video
Final video frames → [Gemini Vision - images only] → Validation result
```

Audio files are **never** sent to any external AI service. Whisper runs locally within our infrastructure.

---

## 7. Authorization & Access Control

### 7.1 User Authorization

All users must be explicitly authorized by a platform administrator before they can access AI generation features. Unauthorized users receive a `403 Forbidden` response when attempting to generate content.

Authorization is tracked with:
- Who authorized the user
- When authorization was granted
- Full audit trail in the system log

### 7.2 Approval Workflow

Every generated video follows this workflow:

```
Upload MP3 → AI Generation → Content Validation → Pending Review → Human Approval → Available
```

At no point is AI-generated content made available for download or publication without human review and explicit approval.

---

## 8. Provenance & Audit Trail

### 8.1 What is Recorded

Every AI tool invocation is recorded in a dedicated `ai_provenance` database table with:

- **Job ID** — links to the specific video project
- **Step** — which part of the pipeline (lyrics_analysis, video_bg, image_bg, yt_metadata, output_validation)
- **Tool name & version** — exact model identifier (e.g., `veo-3.1-generate-001`)
- **Provider** — the AI service provider (e.g., `google_vertex`)
- **Full prompt** — the complete text sent to the AI
- **Prompt hash** — SHA-256 for deduplication and searchability
- **Response summary** — truncated output for audit purposes
- **Input data types** — what categories of data were included (e.g., `lyrics_text_600chars`, `artist_name`)
- **Output artifact** — path to the generated file
- **Duration** — how long the API call took
- **Timestamp** — exact date and time

### 8.2 Transparency in UI

The platform provides a **Provenance tab** in every job detail view, showing a visual timeline of all AI calls with collapsible prompt details. This is accessible to the job owner and platform administrators.

### 8.3 Export for Compliance

The `/provenance/{job_id}/export` endpoint generates a comprehensive JSON document suitable for:
- Copyright registration filings (with clear AI vs. human element attribution)
- Regulatory compliance documentation
- AI transparency and labelling requirements
- Internal audit and review processes

---

## 9. Platform Compliance Dashboard

GenLy includes a built-in **Compliance tab** in the admin panel that provides real-time status of all compliance measures:

- Tool approval status
- Prohibited tools check
- User authorization system status
- AI usage scope verification
- Data protection policy status
- Content safety system status
- Clearance workflow status
- Provenance tracking status

Each check displays a green (OK), amber (action needed), or red (issue) indicator with detailed descriptions.

---

## 10. Summary

| Area | GenLy AI Compliance |
|------|-------------------|
| **AI Tools** | Exclusively Google Vertex AI Enterprise (Veo, Imagen, Gemini). No prohibited tools. |
| **AI Scope** | Background elements only. Primary content is human-created. |
| **Copyright** | Full provenance export with AI/human element separation and USCO-compliant disclaimers. |
| **Content Safety** | Triple protection: prompt blocking + automated validation + human review. |
| **Data Protection** | Vertex AI Enterprise (no training). Data minimization. No audio sent to AI. |
| **Authorization** | Pre-authorization required. Admin-controlled access. |
| **Clearance** | Mandatory human review before any content is downloadable or publishable. |
| **Audit Trail** | Every AI call logged with tool, prompt, data types, and artifacts. |

---

*This document reflects the current state of the GenLy AI platform as of April 2026. For technical verification, the compliance status endpoint (`/compliance/status`) and data policy endpoint (`/compliance/data-policy`) provide real-time, machine-readable information.*
