# Changelog

## [Unreleased]

### Changed

- `status_label` answers report zero usage throughout: the `output_tokens` figure that counted the label's words is gone, since no model produced the label and the gateway counts no tokens for it.
