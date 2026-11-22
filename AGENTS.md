# Agents

This file tells coding agents how changes to this repository are landed.

## Merge rule

Merge rule: every change lands as a pull request from a feature branch; a maintainer agent may squash-merge it once CI is green and the change is verified, with John Amodeo as the sole commit author and no trailers; releases, tags, PyPI publishes, force-pushes and deletions need John's explicit approval.

## Agents using the toolkit

When a person asks an agent for something benchling-tools cannot do, the agent ALWAYS explains the limitation and asks whether to log it as a bug or a feature request on jmamodeo/benchling-tools. Only after an explicit yes does it run `benchling-tools issue draft` or the MCP tool `issue_draft` with a detailed description and a proposed solution that cites official Benchling docs (docs.benchling.com, help.benchling.com, or the API reference) with the quoted line proving the solution could work. It shows the draft and files only after the person confirms. Issues never contain tenant names or URLs, private record IDs, secrets, or personal data. See "When the toolkit cannot do it" in [SKILL.md](SKILL.md).
