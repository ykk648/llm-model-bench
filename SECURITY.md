# Security Policy

Do not file API keys, bearer tokens, non-public endpoint URLs, benchmark prompts containing customer data, or SQLite result databases in public issues or pull requests.

Report a suspected credential disclosure or a vulnerability that could expose server-side environment variables privately to the maintainer. Public dashboard deployments should keep `[web].allow_benchmark = false` unless access is protected by deployment-level authentication and an administrator token.
