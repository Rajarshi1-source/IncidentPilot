"""Slack as a write path, not a read path.

Every message is persisted as it arrives (``ingestor``), every edit and delete
is appended as a revision (``mutations``), and the intent ladder's impure layers
live in ``classifier``. Nothing in this package ever calls
``conversations.history`` -- that is the reconciler's single, budgeted
privilege (INV-02, B-01).
"""
