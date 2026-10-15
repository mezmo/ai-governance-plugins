# Security

These plugins decide whether an agent may run a tool, so a bug that lets a
denied call run is a vulnerability.

Report one privately through GitHub: open the repository's **Security** tab and
choose **Report a vulnerability**. Do not open a public issue or pull request.

Include the plugin, the agent and its version, and the smallest hook input or
steps that let a call run that should have been blocked.

Known limits, not vulnerabilities: Codex and GitHub Copilot run the tool when
the hook itself crashes or times out (see their READMEs), Goose's Code Mode
runs the tool calls inside its scripts without a hook, and the plugins send
full tool arguments to Mezmo.
