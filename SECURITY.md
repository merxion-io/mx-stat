# Security policy

If you find a security vulnerability in mx-stat, please do not open a public issue.
Report it privately to **support@merxion.xyz** with a description, steps to reproduce and
the affected version (see `VERSION`). We aim to reply within 7 days.

mx-stat runs locally, reads process and system information and, for agent monitoring,
reads session files under the current user's home directory (for example `~/.claude`).
It opens no network listeners.
