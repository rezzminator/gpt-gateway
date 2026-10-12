"""gpt-gateway: a local passthrough between Claude Code and the Anthropic API, with middleware hooks.

`gateway.py` beside this package is the entry point; `gpt_gateway.cli` dispatches its commands.
The middleware contract lives in `gpt_gateway.hooks` (per request) and `gpt_gateway.middleware`
(per module); `gpt_gateway.harness` runs a middleware file in process for its tests.
"""
