"""Writes the VS Code / Copilot custom agent files from the single source of the agent instructions.

    python scripts/sync_agent_files.py

Source: src/service_test_mcp/prompts/service-tester.md (also the standalone agent's default system prompt).
Targets: .github/agents/service-tester.agent.md and bundle/templates/service-tester.agent.md.
tests/test_unit.py fails when a target is out of date.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "service_test_mcp" / "prompts" / "service-tester.md"
TARGETS = (ROOT / ".github" / "agents" / "service-tester.agent.md",
           ROOT / "bundle" / "templates" / "service-tester.agent.md")
FRONT_MATTER = """---
description: Service-layer test expert (mobile WCF and other services) - with defined operations, tests and approaches.
tools: ['archi-service-test/*', 'edit', 'search', 'read']
---
<!-- Generated from src/service_test_mcp/prompts/service-tester.md by scripts/sync_agent_files.py - edit the source. -->
"""


def render() -> str:
    return FRONT_MATTER + SOURCE.read_text(encoding="utf-8")


def main() -> None:
    text = render()
    for target in TARGETS:
        target.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {target.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
