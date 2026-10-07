"""The panel's screens, one module and one router per area.

`login` is the way in, `today` the live screen and its buttons, `requests` what
the children asked for, `children` everything set per child, `voices` the
parent's own recordings, `settings` the family-wide ones, `audit` the log and
`misc` what is not a screen at all: the health check and the PWA files. The
wizard is separate (`biokids_panel.wizard`), because it runs before anyone can
sign in.
"""
