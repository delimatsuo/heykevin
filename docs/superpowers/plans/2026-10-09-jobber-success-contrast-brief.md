# Jobber completion-page contrast repair

Base: `901cfbca96d365f0840e17c2ecd4071ac1ff1a12`, tree
`2f27379e2bfe98d382c4e4a4471bf15b8fba86e6`.

The local visual check found that the Jobber completion page inherits a
translucent card over a blue/purple gradient. Its ordinary 16px paragraph and
14px hint have only about 2.2–3.8:1 contrast across the gradient endpoints.
[W3C SC 1.4.3](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html)
requires 4.5:1 for ordinary text. This is a source-only readability repair.

Builder: headless `agy`, `gemini-3.7-flash-high`. Root owns this brief,
verification, independent review and all Git operations. The work is one
function with dependent implementation and verification; no independent
implementation jobs exist, so use a loop rather than artificial fan-out.

## Pinned implementation

Only edit `app/api/integrations.py`, within `_success_page`:

- Add a fixed conditional CSS string for the existing `is_jobber` branch.
- Override `body` background to `#0f172a` and `.card` background to `#1e293b`.
- Set `p` color to `#cbd5e1` and opacity to `1`.
- Set `.hint` color to `#94a3b8` and opacity to `1`.
- Insert the conditional CSS at the end of the existing style element.
  The non-Jobber string must be empty and preserve every byte of existing
  Google Calendar and other non-Jobber output, including whitespace.
- Preserve all headings, copy, links, route headers, OAuth/authentication,
  account/lifecycle logic, native behavior and provider requests.
- No scripts, external resources, new routes, classes, credentials, tests or
  unrelated refactoring. Do not edit this root-owned brief.
- Do not run tests, Git mutations, network/provider/cloud commands, builds,
  credential discovery or read dotenv files. Return a short change report.

## Root acceptance

- Extract the actual pure functions without importing the backend. Compare
  Google Calendar and other non-Jobber output byte-for-byte with the base.
- Inspect actual browser-computed colors at 320px, 390px and desktop widths;
  ordinary text must exceed 4.5:1 and no horizontal overflow may occur.
- Existing setup/manage guides and their exact fixed app link remain intact.
- Run relevant existing Python handoff/PKCE tests with dotenv disabled and
  outbound/default credential discovery blocked. Run fatal Ruff and diff checks.
- Independent fresh-context source review, exact-head CI and configured
  Codex review must pass before merge. Standard public Ubuntu runners only;
  no paid Actions, dispatch, rerun, cloud/provider mutation or deployment.
- Do not claim OAuth, provider, Marketplace, installed-device or release
  qualification from this local fictional visual preview.
