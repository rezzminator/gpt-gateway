"""Per-lane input estimates calibrated from the last completed response.

Image allowance distilled from claude-code-proxy, Copyright (c) 2026
Raine Virta (MIT; permission notice in THIRD_PARTY_NOTICES.md).
"""
import math


class Estimator:
    """One instance per lane. Counts are translated request characters, not bytes."""

    def __init__(self):
        self.ratio = 0.25

    # claude-code-proxy/src/providers/kimi/count_tokens.rs:7; image allowance.
    def estimate(self, chars, attachments=0):
        """Ceiling of characters times the measured ratio, plus 2000 per attachment (a base64 image or PDF)."""
        if chars < 0 or attachments < 0:
            raise ValueError("character and attachment counts must be nonnegative")
        return math.ceil(chars * self.ratio) + 2000 * attachments

    def observe(self, chars, input_tokens):
        """Use total upstream input (including cached tokens); ignore zero characters."""
        if chars < 0 or input_tokens < 0:
            raise ValueError("character and token counts must be nonnegative")
        if chars:
            self.ratio = input_tokens / chars
