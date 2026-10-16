"""Per-lane input estimates calibrated from the last completed response.

Image allowance distilled from claude-code-proxy, Copyright (c) 2026
Raine Virta (MIT; permission notice in THIRD_PARTY_NOTICES.md).
"""
import math

_ATTACHMENT_TOKENS = 2000


class Estimator:
    """One instance per lane. Counts are translated request characters, not bytes."""

    def __init__(self):
        self.ratio = 0.25
        self.observed = False

    # claude-code-proxy/src/providers/kimi/count_tokens.rs:7; image allowance.
    def estimate(self, chars, attachments=0):
        """Ceiling of characters times the measured ratio, plus the attachment allowance per attachment (a base64 image or PDF)."""
        if chars < 0 or attachments < 0:
            raise ValueError("character and attachment counts must be nonnegative")
        return math.ceil(chars * self.ratio) + _ATTACHMENT_TOKENS * attachments

    def observe(self, chars, input_tokens, attachments=0):
        """Smooth the measured text tokens per character, with the attachment allowance taken out of the total
        upstream input (cached tokens included); ignore zero characters, and a turn whose allowance meets or exceeds the
        total, which measures the allowance's error, not the text."""
        if chars < 0 or input_tokens < 0 or attachments < 0:
            raise ValueError("character, token and attachment counts must be nonnegative")
        text_tokens = input_tokens - _ATTACHMENT_TOKENS * attachments
        if chars and (text_tokens > 0 or not attachments):
            measured = max(0, text_tokens) / chars
            self.ratio = (self.ratio + measured) / 2 if self.observed else measured
            self.observed = True
