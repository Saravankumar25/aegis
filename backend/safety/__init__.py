"""Safety substrate for autonomous action (V1.5): leases, circuit breakers, kill switch.

Every module here is load-bearing, not decorative (CLAUDE.md §2): execution paths MUST
consult all of them, and each has its own tests. None of them trusts application-level
discipline where the database can enforce the invariant instead.
"""
