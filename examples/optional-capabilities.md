# Optional capability behavior

Every fixture is useful with only a local checkout and Python. No fixture
contains a credential, endpoint, scheduled job, or network command.

Run `python -m tasktra capabilities --root .` to obtain a credential-free,
offline provider snapshot. GitHub and Jira appear as optional unavailable
capabilities when their host integrations are not configured; the report keeps
`local_work_can_continue` true. Local work items, validation, compilation, and
evidence files remain available.

Research and scheduling are likewise optional host activities. If no research
connector is available, retain local evidence and mark the research dependency
unavailable. If no scheduler is available, run the same validation commands
manually or from CI. Neither absence changes the goal scope or authorizes a
remote effect.
