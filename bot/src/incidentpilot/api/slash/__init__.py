"""The seven slash commands (W5-15).

One endpoint per command rather than a single dispatcher, because Slack requires
a Request URL per command anyway -- and a per-command route means an unhandled
command is a 404 in the Slack app config rather than a silent no-op inside a
router nobody reads.
"""
