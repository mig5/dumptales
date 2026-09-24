#!/usr/bin/env python3
"""Remediate fixable Poetry dependency vulnerabilities found by Syft + Grype.

This is intentionally conservative:
* Syft inventories Python dependencies declared by the repository (including poetry.lock).
* Grype identifies vulnerabilities and fixed versions.
* Poetry is asked to update only packages for which Grype reports a fix.
* A PR is opened only when the resulting poetry.lock removes at least one fixable finding.
* Project version constraints are never widened automatically.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


@dataclass(frozen=True)
class Finding:
    package: str
    installed: str
    vulnerability: str
    severity: str
    fix_state: str
    fixed_versions: tuple[str, ...]

    @property
    def key(self) -> tuple[str, str]:
        # A version change that actually fixes a vulnerability causes Grype to stop
        # reporting this package/vulnerability pair.
        return (self.package.lower(), self.vulnerability)

    @property
    def fixable(self) -> bool:
        return self.fix_state == "fixed" and bool(self.fixed_versions)


def run(
    command: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    stdout=None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    print("+", shlex.join(command), flush=True)
    return subprocess.run(
        command,
        env=env,
        stdout=stdout,
        stderr=None,
        text=True,
        check=check,
    )


def require_command(name: str) -> None:
    try:
        run([name, "--version"], check=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"required command is unavailable: {name}") from exc


def scan(label: str, workdir: Path) -> Path:
    sbom = workdir / f"{label}-sbom.json"
    grype = workdir / f"{label}-grype.json"

    with sbom.open("w", encoding="utf-8") as handle:
        run(
            [
                "syft",
                "dir:.",
                "--select-catalogers",
                "python",
                "-o",
                "syft-json",
            ],
            stdout=handle,
        )

    with grype.open("w", encoding="utf-8") as handle:
        run(["grype", f"sbom:{sbom}", "-o", "json"], stdout=handle)

    return grype


def load_findings(path: Path) -> list[Finding]:
    with path.open("r", encoding="utf-8") as handle:
        document = json.load(handle)

    findings: list[Finding] = []
    for match in document.get("matches", []):
        artifact = match.get("artifact") or {}
        # The SBOM is intentionally Python-only, but retain this guard so a future
        # Syft catalogue change cannot accidentally drive Poetry from another ecosystem.
        if str(artifact.get("type", "")).lower() != "python":
            continue

        vulnerability = match.get("vulnerability") or {}
        fix = vulnerability.get("fix") or {}
        versions = tuple(str(v) for v in (fix.get("versions") or []) if v)
        package = str(artifact.get("name") or "").strip()
        vuln_id = str(vulnerability.get("id") or "").strip()
        if not package or not vuln_id:
            continue

        findings.append(
            Finding(
                package=package,
                installed=str(artifact.get("version") or "unknown"),
                vulnerability=vuln_id,
                severity=str(vulnerability.get("severity") or "Unknown"),
                fix_state=str(fix.get("state") or "unknown"),
                fixed_versions=versions,
            )
        )

    # Grype can occasionally return multiple match paths to the same package/vulnerability.
    # Keep one stable row per finding for remediation/reporting.
    unique: dict[tuple[str, str, str], Finding] = {}
    for finding in findings:
        unique[(finding.package.lower(), finding.installed, finding.vulnerability)] = finding
    return sorted(
        unique.values(),
        key=lambda f: (f.package.lower(), f.vulnerability, f.installed),
    )


def md(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def findings_table(findings: Iterable[Finding], *, limit: int = 75) -> str:
    rows = list(findings)
    if not rows:
        return "_None._"

    lines = [
        "| Package | Installed | Vulnerability | Severity | Fix |",
        "| --- | --- | --- | --- | --- |",
    ]
    for finding in rows[:limit]:
        fixed = ", ".join(finding.fixed_versions) if finding.fixed_versions else finding.fix_state
        lines.append(
            f"| `{md(finding.package)}` | `{md(finding.installed)}` | "
            f"`{md(finding.vulnerability)}` | {md(finding.severity)} | {md(fixed)} |"
        )
    if len(rows) > limit:
        lines.append(f"\n_And {len(rows) - limit} more findings._")
    return "\n".join(lines)


def report(
    before: list[Finding],
    after: list[Finding],
    targets: list[str],
    remediated: list[Finding],
) -> str:
    remaining_fixable = [f for f in after if f.fixable]
    target_text = ", ".join(f"`{md(package)}`" for package in targets)
    return f"""## Automated dependency security remediation

This pull request was generated by the scheduled Forgejo dependency-security workflow.
Syft created a Python dependency SBOM from the repository (including `poetry.lock`), Grype scanned that SBOM, and Poetry was asked to re-resolve only packages for which Grype reported an available fix.

The workflow **does not widen dependency constraints in `pyproject.toml`**. If a safe version cannot be selected inside the existing constraints, the workflow leaves that decision for a maintainer instead of changing policy automatically.

- Findings before update: **{len(before)}**
- Fixable findings before update: **{sum(f.fixable for f in before)}**
- Findings after update: **{len(after)}**
- Fixable findings after update: **{len(remaining_fixable)}**
- Targeted Poetry packages: {target_text or '_none_'}

### Remediated findings

{findings_table(remediated)}

### Remaining findings after the lockfile update

{findings_table(after)}

### Validation

The workflow re-ran Syft and Grype against the updated lockfile and ran `poetry check --lock` before pushing this branch.
"""


def api_json(
    method: str,
    api_url: str,
    token: str,
    path: str,
    payload: dict[str, object] | None = None,
):
    url = f"{api_url.rstrip('/')}/{path.lstrip('/')}"
    data = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"token {token}",
        "User-Agent": "forgejo-grype-poetry-remediator/1",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Forgejo API {method} {url} failed with HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Forgejo API {method} {url} failed: {exc.reason}") from exc


def take_publish_environment() -> dict[str, str]:
    # Remove the PAT from the process environment before invoking Syft, Grype,
    # Poetry, or Git. It remains only in this Python process and is reintroduced
    # solely as an HTTP auth header for the final push/API request.
    names = (
        "SECURITY_BOT_USERNAME",
        "SECURITY_BOT_TOKEN",
        "CI_FORGEJO_API_URL",
        "CI_FORGEJO_SERVER_URL",
        "CI_FORGEJO_REPOSITORY",
    )
    return {name: os.environ.pop(name, "").strip() for name in names}


def publish_pull_request(body: str, required: dict[str, str]) -> str:
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "cannot publish remediation: missing Forgejo Actions configuration: "
            + ", ".join(missing)
        )

    username = required["SECURITY_BOT_USERNAME"]
    token = required["SECURITY_BOT_TOKEN"]
    api_url = required["CI_FORGEJO_API_URL"]
    server_url = required["CI_FORGEJO_SERVER_URL"]
    repository = required["CI_FORGEJO_REPOSITORY"]
    base_branch = os.environ.get("BASE_BRANCH", "main").strip() or "main"
    security_branch = (
        os.environ.get("SECURITY_BRANCH", "security/grype-dependency-fixes").strip()
        or "security/grype-dependency-fixes"
    )

    # Deliberately use a dedicated PAT rather than Forgejo's automatic workflow token.
    # Forgejo suppresses workflows caused by changes made with its automatic token;
    # a PAT lets normal push/PR CI validate this generated branch.
    run(["git", "config", "user.name", "Dependency Security Bot"])
    run(["git", "config", "user.email", "dependency-security-bot@users.noreply.local"])
    run(["git", "checkout", "-B", security_branch])
    run(["git", "add", "poetry.lock"])
    run(["git", "commit", "-m", "security: remediate dependency vulnerabilities"])

    basic = base64.b64encode(f"{username}:{token}".encode("utf-8")).decode("ascii")
    git_env = os.environ.copy()
    git_env.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    remote = f"{server_url.rstrip('/')}/{repository}.git"
    run(
        ["git", "push", "--force", remote, f"HEAD:refs/heads/{security_branch}"],
        env=git_env,
    )

    encoded_repo = urllib.parse.quote(repository, safe="/")
    query = urllib.parse.urlencode({"state": "open", "limit": "50"})
    pulls = api_json("GET", api_url, token, f"repos/{encoded_repo}/pulls?{query}") or []
    existing = None
    for pull in pulls:
        if (
            (pull.get("head") or {}).get("ref") == security_branch
            and (pull.get("base") or {}).get("ref") == base_branch
        ):
            existing = pull
            break

    title = "security: remediate dependency vulnerabilities"
    if existing is not None:
        number = existing.get("number") or existing.get("index")
        updated = api_json(
            "PATCH",
            api_url,
            token,
            f"repos/{encoded_repo}/pulls/{number}",
            {"title": title, "body": body},
        )
        html_url = (updated or {}).get("html_url") or existing.get("html_url") or ""
        print(f"Updated existing security pull request #{number}: {html_url}")
        return str(html_url)

    created = api_json(
        "POST",
        api_url,
        token,
        f"repos/{encoded_repo}/pulls",
        {
            "title": title,
            "head": security_branch,
            "base": base_branch,
            "body": body,
        },
    )
    number = (created or {}).get("number") or (created or {}).get("index")
    html_url = (created or {}).get("html_url") or ""
    print(f"Created security pull request #{number}: {html_url}")
    return str(html_url)


def print_summary(label: str, findings: list[Finding]) -> None:
    fixable = sum(f.fixable for f in findings)
    print(f"{label}: {len(findings)} Python dependency finding(s), {fixable} fixable.")
    if findings:
        print(findings_table(findings))


def changed_paths() -> set[str]:
    completed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        check=True,
        text=True,
        capture_output=True,
    )
    paths: set[str] = set()
    for line in completed.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.add(path)
    return paths


def main() -> int:
    publish_environment = take_publish_environment()

    if not Path("pyproject.toml").is_file() or not Path("poetry.lock").is_file():
        raise RuntimeError("run this tool from a Poetry project root containing pyproject.toml and poetry.lock")

    for command in ("git", "poetry", "syft", "grype"):
        require_command(command)

    if changed_paths():
        raise RuntimeError("working tree is not clean before dependency remediation")

    with tempfile.TemporaryDirectory(prefix="forgejo-grype-") as temp:
        workdir = Path(temp)
        before_path = scan("before", workdir)
        before = load_findings(before_path)
        print_summary("Before remediation", before)

        if not before:
            print("No Python dependency vulnerabilities found; no pull request is needed.")
            return 0

        fixable = [finding for finding in before if finding.fixable]
        if not fixable:
            print(
                "Grype found dependency vulnerabilities, but none currently advertise a fixed version. "
                "Matching the previous Trivy --ignore-unfixed policy, no pull request or failure notification is needed."
            )
            return 0

        targets = sorted({finding.package for finding in fixable}, key=str.lower)
        print("Targeted Poetry update:", ", ".join(targets))
        run(["poetry", "update", *targets, "--lock", "--no-interaction"])
        run(["poetry", "check", "--lock"])

        changed = changed_paths()
        if "poetry.lock" not in changed:
            print(
                "Poetry could not select a different locked version within the current project constraints; "
                "no automatic pull request was created.",
                file=sys.stderr,
            )
            return 1
        unexpected = changed - {"poetry.lock"}
        if unexpected:
            raise RuntimeError(
                "Poetry changed files other than poetry.lock; refusing automatic remediation: "
                + ", ".join(sorted(unexpected))
            )

        after_path = scan("after", workdir)
        after = load_findings(after_path)
        print_summary("After remediation", after)

        after_keys = {finding.key for finding in after}
        remediated = [finding for finding in fixable if finding.key not in after_keys]
        if not remediated:
            print(
                "The updated lockfile did not remove any fixable Grype finding; refusing to publish a PR.",
                file=sys.stderr,
            )
            return 1

        body = report(before, after, targets, remediated)
        publish_pull_request(body, publish_environment)

        remaining_fixable = [finding for finding in after if finding.fixable]
        if remaining_fixable:
            print(
                f"Published a partial remediation, but {len(remaining_fixable)} fixable dependency vulnerability "
                "finding(s) remain. The job is failing so the normal failure notification path still fires.",
                file=sys.stderr,
            )
            return 1

        if after:
            print(
                f"Published remediation for {len(remediated)} vulnerability finding(s). "
                f"{len(after)} remaining finding(s) currently have no advertised fix and are ignored for failure status."
            )
        else:
            print(
                f"Published remediation for {len(remediated)} vulnerability finding(s); "
                "the post-update scan is clean."
            )
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"dependency security workflow failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
