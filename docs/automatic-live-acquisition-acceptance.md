# Automatic live acquisition acceptance

Version: 1.0.0

- [x] Validated search-result URLs with AI action `download` are attempted first; when none are
  selected, at most two `inspect` sources are automatically promoted with direct files first.
- [x] Exact host allowlisting, DNS pinning, public-address validation, timeouts, rate limiting,
  byte budgets, no redirects, and no credentials protect live requests.
- [x] Successful bytes are classified from content and stored immutably by SHA-256.
- [x] Failed sources retain structured reason, retryability, locator hash, and source identity.
- [x] A failed source does not stop acquisition of other sources.
- [x] The workbench exposes live artifacts and acquisition status without placeholder values.
- [x] Mock transport tests cover the successful DNS-pinned acquisition path.
