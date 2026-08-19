"""Orchestration: the chore itself, its trail, and where its input comes from.

`close` is the ten-step chore. `journal` is the record it writes as it goes,
which is what makes an unattended run something you can check rather than
something you have to believe. `mailbox` is where a month's documents come from.

This layer knows the order of the work. It does not know how to reach a model,
a database or an inbox, which is what `adapters` is for.
"""
