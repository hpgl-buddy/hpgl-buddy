# Security Policy

hpgl-buddy is a local command-line tool that talks to a plotter over a serial
port. Its attack surface is small, but a few things are worth reporting privately:
parsing untrusted HP-GL files, the serial/transport layer, and anything in the
release/publish pipeline.

## Supported versions

Fixes are released against the latest published version on PyPI. Please reproduce
on the current release (`pip install --upgrade hpgl-buddy`; check with
`hpgl-buddy -V`) before reporting.

## Reporting a vulnerability

Please report security issues **privately**, not in a public issue:

- Use GitHub's **"Report a vulnerability"** (Security tab → Advisories) on
  https://github.com/hpgl-buddy/hpgl-buddy, or
- email **hello@pavelkim.com** with details and, ideally, a minimal reproduction.

You can expect an acknowledgement within a few days. Once a fix is available it
will be released and the report credited unless you prefer to stay anonymous.
Please give us reasonable time to address the issue before any public disclosure.
