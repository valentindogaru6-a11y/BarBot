# Dead Ends and Rejected Approaches

Metadata: scope `KOS integration`; trust `verified`; lifecycle `active`; last
verified `2026-09-29`.

- Do not treat sample KOS template content as BarBot project truth.
- Do not require Graphify for repository comprehension; use `rg`, import maps,
  targeted source reads, and tests when Graphify is unavailable.
- Do not require PowerShell on this Linux host; reproduce validator checks with
  available local tools instead of installing software.
- Do not enable the optional Telegram utility sender. The BarBot pilot writes
  local files only and must not read production secrets.
- Do not resolve static seed count `414` versus owner-confirmed production count
  `416` by mutating either source. They describe different scopes.
