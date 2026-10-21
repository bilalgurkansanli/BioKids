"""The panel's screens, one module and one router per area.

`login` is the way in, `today` the live screen and its buttons, `requests` what
the children asked for, `children` everything set per child, `special_days` a
day or a holiday with rules of its own (D-215), `voices` the
parent's own recordings, `settings` the family-wide ones, `history` one day and
the fortnight before it, `insights` a week or a month in plain words, `audit`
the log and `misc` what is not a screen at all: the health check and the PWA
files. The wizard is separate (`biokids_panel.wizard`), because it runs before
anyone can sign in.
"""
