# Universal trial support (frontend only)

The `1.1.18-trial.3` frontend adds an opt-in human support chat. It does not
change billing, generation, transcription, database schema or customer accounts.

## Scope and privacy

- Both `VITE_APP_ENV=trial` and exact hostname `trial.genly.pro` are required.
  Production, staging and Vercel preview hostnames do not load Crisp.
- Signed-in users choose **Soporte Genly → Abrir chat**. Before this action,
  the integration makes no Crisp request. The panel discloses the provider.
- No Genly authentication token, email/profile, lyrics, prompts or audio is
  explicitly supplied. Crisp receives standard connection/page metadata and
  anything the visitor chooses to submit. Do not claim zero third-party data.
- `data-browsing-ignore` masks the whole body, including React portals, from
  MagicBrowse. This is not a general security boundary against third-party JS.
  The workspace owner should also disable MagicBrowse/MagicType in Crisp.
- Conversation continuity uses a cryptographically random, tab-scoped token.
  Same-account reloads retain it. Logout/account changes clear the local binding
  and reset Crisp. Actual cross-tab identity changes hide support until reload;
  profile updates and token refreshes leave it alone. No cross-device continuity.
- SDK failure/12-second timeout leaves an email fallback to the existing
  `tomas@epical.digital` help-center contact. Retrying a failed loader requires
  page reload, avoiding duplicate/late SDK injection. Video editing is unaffected.
- After opening, Crisp's native bubble handles unread messages and reopening.
  Existing conversations remain in the operator inbox after client logout.

## Operator setup and acceptance

1. Use the user's existing Crisp workspace (public Website ID ends `2928154`).
   Do not create a second workspace or subscribe to a paid plan.
2. Set workspace/team display name to Genly. Free retains Crisp branding;
   do not rely on the temporary Essentials trial for required functionality.
3. Set availability honestly; leave the offline contact form usable. No bot or
   AI automation is required. Avoid collecting confidential song assets.
4. Install Crisp's mobile app, sign into that same workspace and enable both
   Crisp notification preferences and operating-system push permissions.
5. Send an explicitly labeled QA message. The human operator must confirm the
   notification arrives and reply from the app/inbox; verify it in the visitor
   chat, including minimized/reopened view. This cannot be certified by widget
   loading or mocked SDK tests alone.

## Release and rollback

- Base frontend release on `release/universal-trial-frontend`, forked from the
  deployed backend-compatible `5680148f` snapshot. Full clean-checkout PR CI
  remains required before merge.
- Deploy **only** Vercel project `genly-universal-trial`
  (`prj_4RS3NJEacrUqwa5SBiwibmepaCvd`), alias `trial.genly.pro`.
- Do not advance `release/universal-es-trial-20260911`: that branch triggers
  three Railway services. Backend remains `1.1.18-trial.2` / `5680148f`.
- Rollback is promotion of the previous isolated Vercel deployment; no database
  rollback, account reset, credit renewal or backend redeploy is required.
- Preserve the active Jorge/Ignacio grants (3 Scenes videos each, existing
  September 16 deadlines). Do not run old QA scripts that assume a shared grant.

Official references: [SDK](https://docs.crisp.chat/guides/chatbox-sdks/web-sdk/dollar-crisp/),
[session lifecycle](https://docs.crisp.chat/guides/chatbox-sdks/web-sdk/session-continuity/),
[MagicBrowse exclusion](https://help.crisp.chat/en/article/how-to-prevent-elements-from-being-tracked-by-magicbrowse-ju7mcg/).
