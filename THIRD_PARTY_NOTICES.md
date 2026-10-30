# Third-party notices and release inventory

This file records the reviewed dependency families for the current server
runtime baseline. It is engineering evidence, not legal advice and not a
substitute for reviewing the complete upstream license text. The machine-
generated SPDX document at [`sbom/server-runtime.spdx.json`](sbom/server-runtime.spdx.json)
is the authoritative record of the exact installed versions and dependency
relationships accepted by the current release gate.

| Component | Exact accepted version | Runtime role | Upstream license metadata |
|---|---:|---|---|
| Flask | 3.1.3 | HTTP application framework | BSD-3-Clause |
| Waitress | 3.0.2 | Production WSGI server | ZPL-2.1 |
| cryptography | 46.0.7 | Ed25519 signing and verification | Apache-2.0 OR BSD-3-Clause |
| blinker | 1.9.0 | Flask signal support | MIT |
| click | 8.5.0 | Flask command support | BSD-3-Clause |
| itsdangerous | 2.2.0 | Flask signing support | BSD-3-Clause |
| Jinja2 | 3.1.6 | Flask template dependency | BSD-3-Clause |
| MarkupSafe | 3.0.4 | Jinja2/Werkzeug escaping | BSD-3-Clause |
| Werkzeug | 3.1.9 | Flask HTTP utilities | BSD-3-Clause |
| cffi | 2.1.1 | cryptography native interface | MIT-0 |
| pycparser | 3.11 | cffi parser dependency | BSD-3-Clause |

The table is intentionally version-specific. A dependency upgrade is a release
change: regenerate the SPDX document from the candidate virtual environment,
review its license metadata and notices, then update this table in the same
change. A version range in `pyproject.toml` is never sufficient release
evidence.

## Scope and exclusions

- The SPDX document follows the three declared application dependencies and
  every installed runtime dependency reachable from their active PEP 508
  requirements. Optional extras and test dependencies are excluded unless they
  become reachable runtime requirements.
- Python, Enterprise Linux, Nginx, OpenSSL, SQLite, Certbot and other operating-
  system components are deployment prerequisites, not Python packages bundled
  by this repository. Production image/package compliance must inventory those
  separately.
- Build and test tools are not part of the runtime SBOM. If a future artifact
  bundles any build tool or system library, its license and exact version must
  be added to the appropriate artifact SBOM before release.
- `NOASSERTION` in SPDX means the installed package did not publish an
  unambiguous SPDX license expression. The generator preserves the raw
  `License`, `License-Expression`, license classifier and `License-File`
  metadata so a reviewer can make, and record, the legal determination.

Formal commercial distribution remains blocked until the release-specific
legal checklist in [`docs/LEGAL_AND_SBOM_RELEASE_GATE.md`](docs/LEGAL_AND_SBOM_RELEASE_GATE.md)
has a named human approver. Passing automated checks does not constitute legal
approval.
