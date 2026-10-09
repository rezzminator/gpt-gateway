"""model-gateway: a local passthrough between Claude Code and the Anthropic API, with middleware hooks.

`gateway.py` beside this package is the entry point; `model_gateway.cli` dispatches its commands.
The middleware contract lives in `model_gateway.hooks` (per request) and `model_gateway.middleware`
(per module); `model_gateway.harness` runs a middleware file in process for its tests.
"""
