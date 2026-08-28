"""F23: route handlers split out of ``dashboard.register_routes``.

Three domains, one module each:

  ``public``    — public read-only data endpoints + SSE feed + SPA hosting
  ``config``    — operator-gated config write / backup / rollback / schema
  ``operator``  — token-gated operator console actions (trackers/close/mode/terminal)

Each module exposes a ``register_*_routes(app)`` function with the route
bodies carried over verbatim from the former monolithic ``register_routes``;
the SPA history-mode catch-all stays last so API routes keep precedence.
"""
