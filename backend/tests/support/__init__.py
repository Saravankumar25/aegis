"""Test doubles. NOT product code — nothing here is importable from the app.

These exist so tests are deterministic and CI never spends tokens. They live in the
test tree precisely so they can never be mistaken for a runtime code path: the
application has exactly one LLM provider (OpenRouter) and exactly one evidence
gateway (real MCP over stdio).
"""
