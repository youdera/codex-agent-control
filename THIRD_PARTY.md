# Dependencies and references

- Runtime adapter: [PsChina/deepseek-as-subagent](https://github.com/PsChina/deepseek-as-subagent), MIT, tested commit `7e54026392f85ccfc796f947faf9ffac3968a49e`. Installed separately by requirements; its source is not vendored here. Preserve its own license when redistributing that dependency.
- MCP Python SDK and optional pywin32 are installed dependencies and retain their own licenses.
- Design references: AgentMeter, Vibe Kanban, Agent Orchestrator and GasTown. This project does not embed these frameworks or claim their runtime validation.
- Optional character artwork is described separately in [ASSETS.md](ASSETS.md); it is not covered by the software MIT license.
- The bundled `grill-me` skill is a workflow adaptation of [Matt Pocock's grill-me / grilling](https://github.com/mattpocock/skills). The MIT copyright and permission notice is preserved in [third_party/mattpocock-skills-LICENSE](third_party/mattpocock-skills-LICENSE). The adaptation reuses confirmed task contracts instead of forcing a fresh full interview for each worker.

Codex and DeepSeek names identify integration targets. This is an independent community tool.
