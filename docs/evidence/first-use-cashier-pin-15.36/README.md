# Exact production-base candidate: synthetic development screenshots

Source tested: `cf57000d9d5157143996514f505bec6b018f7a18`, production
15.36.0 `e792515a7da5a41b8e90e7f00b40ea5a78b6dffd` plus only the first-use
PIN RED/GREEN commits. This evidence-only branch is not the release source.

Real POS Awesome Vue application and Vuetify dialogs, with synthetic Frappe
session/API fixtures. All screenshots carry the exact source SHA and an
explicit synthetic/non-production banner. No live company, account or PIN.

| Scenario | Desktop 1440 x 1000 | Mobile 390 x 844 |
| --- | --- | --- |
| Assigned authenticated cashier with no PIN | [Create](desktop-create-pin.png) | [Create](mobile-create-pin.png) |
| Mismatched confirmation; zero save calls; locked | [Mismatch](desktop-mismatch.png) | [Mismatch](mobile-mismatch.png) |
| Matching entries; one native save call; attested unlock | [Unlocked](desktop-unlocked.png) | [Unlocked](mobile-unlocked.png) |

Capture assertions passed in all six states; zero uncaught page errors.
API response, session and PIN-storage behavior are simulated. This does not
prove native database/Redis authorization, production deployment, or a live
cashier outcome. Dev-server asset/websocket/service-worker noise remains in
the local capture logs. Candidate Python 45/45 and linked frontend 65/65
tests, type-check/build and Chrome 109 CSS audit passed separately.

Reproduction artifacts retained by the operator under
`/tmp/pos-first-pin-sol-20261016.L9MF7r/devtools/` (reused synthetic harness),
`/tmp/pos-first-pin-backport-evidence/` (screenshots and sanitized calls/state),
and `/tmp/pos-first-pin-backport-capture.log`. The harness ran localhost:4309
with production-base frontend explicitly selected and outbound browser
requests blocked. No harness or fixture is shipped in the release candidate.
