"""Thin HTTP clients for the stack's services.

No retry/backoff: transient failures surface as `None`; callers decide
whether to retry or alert.
"""
