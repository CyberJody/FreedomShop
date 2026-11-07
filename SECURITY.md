# Security policy

FreedomShop is a pre-release. Security fixes currently target the latest development version; there is no supported stable-release series yet.

## Deployment scope

Run FreedomShop on a trusted local network, with remote access through a private VPN such as Tailscale restricted to authorized users and devices. **Direct public-internet exposure is not recommended.** See [INSTALL.md](INSTALL.md#network-access) for deployment guidance.

Passing application, installation and security regression tests is not a comprehensive assessment for public-internet hosting. A public hosting offering would need a separate security and operations assessment, including isolation between shops, access controls, monitoring, patching and recovery. Hosting a server privately does not remove those operating responsibilities. Continue to use application logins, permissions and HTTPS within the private network.

## Report a vulnerability privately

Use [GitHub private vulnerability reporting](https://github.com/CyberJody/FreedomShop/security/advisories/new): open the repository's **Security** tab, then **Report a vulnerability**. Include the affected version or commit, steps to reproduce with generated data, expected and actual behavior, and the likely impact. Keep credentials, customer records and live database dumps out of the report.

If the form is unavailable, request a private contact through a project issue without disclosing vulnerability details. Do not put the vulnerability itself in a public issue or the in-shop Notes & Requests notebook.

Maintainers will review reports privately and coordinate a fix and disclosure. There is no guaranteed response window during pre-release development. See [GitHub's private reporting instructions](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately) for how the reporting channel works.
