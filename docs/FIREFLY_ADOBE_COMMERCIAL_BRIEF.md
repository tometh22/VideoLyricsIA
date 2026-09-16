# Firefly Services commercial brief for Adobe

Use this brief when the Adobe representative answers the consultation request.
Do not send credentials or access tokens by email.

## Account

- Company: GenLy
- Adobe ID: `tomas@genly.pro`
- Adobe organization: `tomas-genly.pro`
- Organization ID: `4353251`
- Current role: System Administrator
- Current console state: Firefly API cards are visible, but project creation is
  disabled with `License required`.

## Intended use

GenLy generates lyric videos for enterprise music customers. The application
will call Adobe from GenLy's server-side workers, retain the original generated
asset and its Content Credentials, validate it, and compose it into a final
video. Customer users will not receive Adobe credentials.

Initial scope:

- Generate Video API: text-to-video and image-to-video, 16:9 and 9:16;
- Generate Image API: text-to-image and image editing where the product flow
  requires a still before video animation;
- OAuth Server-to-Server credentials;
- production use for customers in Spain and the European Union;
- low pilot volume followed by concurrent scene generation.

## Required answers before activation

1. Can Adobe provision Firefly Services without a fixed annual minimum and
   invoice actual Operations consumed?
2. What is GenLy's price per 1,000 Operations?
3. How many Operations does one five-second Generate Video call consume at
   each supported resolution and for text-to-video versus image-to-video?
4. How many Operations do the required Generate Image and image-edit actions
   consume?
5. Are failed, moderated, cancelled, or timed-out calls billed?
6. Can Adobe provide pilot credits or a non-production entitlement?
7. Can the default 4 requests/minute limit be raised for concurrent scene
   generation? Does the 9,000 requests/day limit also change?
8. Which exact Firefly API features in the proposed order receive IP
   indemnification? The order must link to the applicable Firefly Product
   Description.
9. Are Content Credentials/C2PA attached to API-generated image and video
   files, and are they preserved in the downloadable original?
10. What input/output retention, model-training, subprocessors, DPA, and EU
    data-transfer terms apply?
11. Which Admin Console product profiles must be assigned for Generate Video,
    Generate Image, Storage Upload, and any required image-edit endpoint?
12. How can Operations usage be exported or reconciled automatically against
    invoices?

## Requested provisioning outcome

- Assign the production `Firefly - Firefly Services` entitlement to
  `tomas-genly.pro`.
- Enable the Firefly API and the image/video capabilities listed above.
- Allow the System Administrator to create an OAuth Server-to-Server project.
- Supply the signed Operations rate card and its effective version/date.
- Identify the Adobe account manager and technical onboarding contact.
